"""What a mediated operation may and may not do to the provider (#5223).

Exercised through a mock transport rather than a stubbed client, so the assertions
are about the actual HTTP the platform would send: which endpoints, which method,
and — for the ref update — that `force` is false and the parent is the head we
observed. A stub that recorded method names would pass while sending a force push.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.agentauth.github_operations import OperationAssignment, OperationRefusedError
from src.agentauth.github_provider import (
    ARCHIVE_SLICE_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_BLOB_BYTES,
    MAX_COMMIT_CONTENT_BYTES,
    MAX_FILES_PER_COMMIT,
    FileChange,
    GitHubProvider,
    ProviderConflictError,
    ProviderUnavailableError,
    reconcile_commit,
    reconcile_pull_request,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
REPOSITORY_ID = 987654
BRANCH = "agent/issue-5223"
HEAD = "a" * 40
DEFAULT_HEAD = "b" * 40
NEW_COMMIT = "c" * 40


def _assignment(**overrides) -> OperationAssignment:
    fields = {
        "tenant_id": "org-a",
        "invocation_id": "run-1",
        "attempt": 1,
        "workload_binding": "pod-uid-1",
        "claim_generation": 1,
        "installation_id": 4242,
        "repository_id": REPOSITORY_ID,
        "repository": "acme/widgets",
        "branch": BRANCH,
        "default_branch": "main",
        "node_id": "node-1",
        "accepted_plan_version": 2,
        "not_after": NOW + timedelta(minutes=10),
    }
    fields.update(overrides)
    return OperationAssignment(**fields)


class Recorder:
    """A mock provider that records every request and serves canned state."""

    def __init__(self, *, branch_head=HEAD, overrides=None, commit_message="fix"):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.branch_head = branch_head
        self.overrides = overrides or {}
        self.commit_message = commit_message

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        key = f"{request.method} {request.url.path}"
        self.requests.append((request.method, str(request.url.path) + (f"?{request.url.query.decode()}" if request.url.query else ""), body))
        for candidate, response in self.overrides.items():
            if key.startswith(candidate):
                return response() if callable(response) else response
        return self._default(request, key)

    def _default(self, request, key):
        path = request.url.path
        if key == "GET /repos/acme/widgets":
            return httpx.Response(200, json={"id": REPOSITORY_ID, "default_branch": "main"})
        if path == f"/repos/acme/widgets/git/ref/heads/{BRANCH}":
            if self.branch_head is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branch_head}})
        if path == "/repos/acme/widgets/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": DEFAULT_HEAD}})
        if path.startswith("/repos/acme/widgets/compare/"):
            return httpx.Response(200, json={"status": "identical"})
        if path.startswith("/repos/acme/widgets/git/commits/") and request.method == "GET":
            return httpx.Response(200, json={"tree": {"sha": "t" * 40}, "message": self.commit_message, "parents": [{"sha": DEFAULT_HEAD}]})
        if key == "POST /repos/acme/widgets/git/blobs":
            return httpx.Response(201, json={"sha": "blob" + "0" * 36})
        if key == "POST /repos/acme/widgets/git/trees":
            return httpx.Response(201, json={"sha": "tree" + "0" * 36})
        if key == "POST /repos/acme/widgets/git/commits":
            return httpx.Response(201, json={"sha": NEW_COMMIT})
        if path == f"/repos/acme/widgets/git/refs/heads/{BRANCH}":
            return httpx.Response(200, json={"object": {"sha": NEW_COMMIT}})
        if key == "POST /repos/acme/widgets/git/refs":
            return httpx.Response(201, json={"object": {"sha": NEW_COMMIT}})
        if path == "/repos/acme/widgets/pulls" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/repos/acme/widgets/pulls" and request.method == "POST":
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.test/pr/7"})
        if path.startswith("/repos/acme/widgets/pulls/") and request.method == "GET":
            return httpx.Response(200, json={"number": 7, "head": {"ref": "someone-else"}, "user": {"id": 99}})
        if path.endswith("/reviews"):
            return httpx.Response(200, json={"id": 11, "state": "COMMENTED"})
        if path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": NEW_COMMIT})
        if path.startswith("/repos/acme/widgets/pulls/") and request.method == "PATCH":
            return httpx.Response(200, json={"number": 7, "html_url": "https://github.test/pr/7"})
        return httpx.Response(404, json={"message": "unexpected"})


def provider(recorder, assignment=None):
    client = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(recorder.handler),
        headers={"Authorization": "Bearer test-token"},
    )
    return GitHubProvider(token="test-token", assignment=assignment or _assignment(), client=client)


class Counter:
    """A `reauthorize` that counts calls, and can start refusing part-way."""

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    async def __call__(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OperationRefusedError("authority withdrawn")


# --- Ref updates are never forced -------------------------------------------


async def test_commit_updates_the_ref_without_force_and_on_the_observed_parent():
    """The two properties that keep a mediated commit from destroying work:
    the parent is the head we actually read, and `force` is false."""
    recorder = Recorder()
    async with provider(recorder) as gh:
        published = await gh.publish_commit(
            changes=[FileChange(path="src/app.py", content=b"print(1)")],
            message="fix",
            expected_head=HEAD,
            reauthorize=Counter(),
        )
    assert published.sha == NEW_COMMIT
    assert published.parent_sha == HEAD

    commit = next(body for method, path, body in recorder.requests if path.endswith("/git/commits") and method == "POST")
    assert commit["parents"] == [HEAD]
    patch = next(body for method, path, body in recorder.requests if method == "PATCH" and "/git/refs/heads/" in path)
    assert patch == {"sha": NEW_COMMIT, "force": False}


async def test_a_moved_branch_is_a_visible_conflict_not_an_overwrite():
    recorder = Recorder(branch_head="d" * 40)
    async with provider(recorder) as gh:
        with pytest.raises(ProviderConflictError):
            await gh.publish_commit(
                changes=[FileChange(path="src/app.py", content=b"x")],
                message="fix",
                expected_head=HEAD,
                reauthorize=Counter(),
            )
    assert not any(method in {"PATCH", "POST"} and "git/ref" in path for method, path, _ in recorder.requests)


async def test_provider_rejection_of_a_non_fast_forward_surfaces_as_a_conflict():
    """422 on the ref update is GitHub's "not a fast-forward"; the worker must see
    a conflict it can reconcile, not an authorization failure it would give up on."""
    recorder = Recorder(overrides={"PATCH /repos/acme/widgets/git/refs": httpx.Response(422, json={"message": "not fast forward"})})
    async with provider(recorder) as gh:
        with pytest.raises(ProviderConflictError):
            await gh.publish_commit(changes=[FileChange(path="a.py", content=b"x")], message="fix", expected_head=HEAD, reauthorize=Counter())


async def test_a_new_branch_is_created_rather_than_patched():
    recorder = Recorder(branch_head=None)
    async with provider(recorder) as gh:
        published = await gh.publish_commit(changes=[FileChange(path="a.py", content=b"x")], message="fix", expected_head=None, reauthorize=Counter())
    assert published.parent_sha == DEFAULT_HEAD
    create = next(body for method, path, body in recorder.requests if method == "POST" and path.endswith("/git/refs"))
    assert create == {"ref": f"refs/heads/{BRANCH}", "sha": NEW_COMMIT}


async def test_base_outside_the_repository_history_is_refused():
    recorder = Recorder(branch_head=None, overrides={"GET /repos/acme/widgets/compare/": httpx.Response(200, json={"status": "diverged"})})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_commit(changes=[FileChange(path="a.py", content=b"x")], message="fix", expected_head=None, reauthorize=Counter())


async def test_default_branch_is_never_written():
    """Unreachable through a valid assignment; asserted at the function that would
    actually do it, so a future caller cannot reintroduce it."""
    recorder = Recorder()
    gh = provider(recorder, assignment=_assignment())
    async with gh:
        object.__setattr__(gh.assignment, "branch", "main")
        with pytest.raises(OperationRefusedError):
            await gh._update_ref(NEW_COMMIT, expected_old=HEAD, create=False)


# --- Authorization before every effect --------------------------------------


async def test_every_mutating_step_reauthorizes():
    """One check per publish would let a mid-sequence revocation still land a
    commit. Blobs, tree, commit and ref each get their own."""
    recorder = Recorder()
    counter = Counter()
    async with provider(recorder) as gh:
        await gh.publish_commit(
            changes=[FileChange(path="a.py", content=b"x"), FileChange(path="b.py", content=b"y")],
            message="fix",
            expected_head=HEAD,
            reauthorize=counter,
        )
    # start + 2 blobs + tree + commit + ref
    assert counter.calls == 6


async def test_authority_withdrawn_after_upload_stops_the_commit():
    """The upload is the long part. Losing authority during it must prevent the
    tree/commit/ref steps that would make the change visible."""
    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_commit(
                changes=[FileChange(path="a.py", content=b"x")],
                message="fix",
                expected_head=HEAD,
                reauthorize=Counter(fail_after=2),
            )
    assert not any(path.endswith("/git/trees") for _, path, _ in recorder.requests)
    assert not any(method == "PATCH" for method, _, _ in recorder.requests)


# --- Content that git records ----------------------------------------------


async def test_binary_content_survives_as_bytes():
    """Base64 happens at the API boundary, so a PNG is not corrupted by decoding."""
    payload = bytes(range(256))
    recorder = Recorder()
    async with provider(recorder) as gh:
        await gh.publish_commit(changes=[FileChange(path="logo.png", content=payload)], message="fix", expected_head=HEAD, reauthorize=Counter())
    import base64

    blob = next(body for method, path, body in recorder.requests if path.endswith("/git/blobs"))
    assert base64.b64decode(blob["content"]) == payload
    assert blob["encoding"] == "base64"


async def test_deletions_and_modes_are_published_as_git_records_them():
    recorder = Recorder()
    async with provider(recorder) as gh:
        await gh.publish_commit(
            changes=[
                FileChange(path="gone.py", deleted=True),
                FileChange(path="run.sh", content=b"#!/bin/sh\n", mode="100755"),
                FileChange(path="link", content=b"target", mode="120000"),
            ],
            message="fix",
            expected_head=HEAD,
            reauthorize=Counter(),
        )
    tree = next(body for method, path, body in recorder.requests if path.endswith("/git/trees"))
    entries = {entry["path"]: entry for entry in tree["tree"]}
    assert entries["gone.py"]["sha"] is None
    assert entries["run.sh"]["mode"] == "100755"
    assert entries["link"]["mode"] == "120000"


@pytest.mark.parametrize(
    "change",
    [
        {"path": "", "content": b"x"},
        {"path": "/etc/passwd", "content": b"x"},
        {"path": "a/../b", "content": b"x"},
        {"path": "sub", "content": b"x", "mode": "160000"},
        {"path": "a.py"},
        {"path": "a.py", "content": b"x" * (MAX_BLOB_BYTES + 1)},
        {"path": "a.py", "content": b"x", "deleted": True},
    ],
)
def test_unusable_changes_are_refused_before_any_call(change):
    """A gitlink mode (160000) is refused too: a submodule pointer imports code
    nothing that reviewed this change has seen."""
    with pytest.raises(OperationRefusedError):
        FileChange(**change)


# --- A publish REQUEST must also fit the deployed edge (#5223) --------------


# The same `aws_api_gateway_rest_api` 10 MB payload quota that sizes the archive
# slice applies to a publish request travelling the other way: the worker signs for
# `execute-api` and POSTs to `/internal/v1/agent/self/github-operation`. The
# response side was bounded for this; the request side was not, so these pin it.
_EDGE_REQUEST_LIMIT_BYTES = 10 * 1000 * 1000


def test_one_permitted_file_encodes_inside_the_edge_request_limit():
    """The defect this pins: `MAX_BLOB_BYTES` was 8 MiB, which is ~11.18 MB once
    base64'd into the request body — refused by the edge before the gateway could
    apply its own bound, so an oversized file surfaced as a transport failure
    instead of a refusal naming the limit."""
    import base64 as _base64

    encoded = len(_base64.b64encode(bytes(MAX_BLOB_BYTES)))
    assert encoded < _EDGE_REQUEST_LIMIT_BYTES, f"a permitted file ({encoded} B encoded) must fit the edge's 10 MB request limit"
    # Headroom for the rest of the envelope (message, title, body, paths).
    assert _EDGE_REQUEST_LIMIT_BYTES - encoded > 1_000_000


def test_the_route_content_bound_cannot_drift_above_the_provider_cap():
    """The schema bound is derived from `MAX_BLOB_BYTES`, not restated, so lowering
    the provider cap cannot leave a larger parse-time bound admitting bodies the
    provider then refuses."""
    from src.agentauth.github_operation_routes import _MAX_CONTENT_CHARS

    assert _MAX_CONTENT_CHARS == (MAX_BLOB_BYTES + 2) // 3 * 4
    assert _MAX_CONTENT_CHARS < _EDGE_REQUEST_LIMIT_BYTES


async def test_many_individually_permitted_files_are_refused_in_aggregate():
    """The bound the per-file cap cannot express. Each file here is well inside
    `MAX_BLOB_BYTES`, and the count is inside `MAX_FILES_PER_COMMIT`, but together
    they exceed what the edge will carry — previously unbounded entirely, so 500
    permitted files could sum to gigabytes."""
    import base64 as _base64

    per_file = 512 * 1024
    count = MAX_COMMIT_CONTENT_BYTES // per_file + 2
    changes = [FileChange(path=f"f{i}.bin", content=bytes(per_file)) for i in range(count)]
    assert all(len(c.content) < MAX_BLOB_BYTES for c in changes), "each file is individually permitted"
    assert len(changes) < MAX_FILES_PER_COMMIT, "the file count is individually permitted"

    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_commit(changes=changes, message="fix", expected_head=HEAD, reauthorize=Counter())
    assert recorder.requests == [], "refused before any provider call, not part-way through an upload"

    encoded = len(_base64.b64encode(bytes(MAX_COMMIT_CONTENT_BYTES)))
    assert encoded < _EDGE_REQUEST_LIMIT_BYTES, f"the permitted aggregate ({encoded} B encoded) must fit the edge"


async def test_a_commit_at_the_aggregate_bound_is_still_published():
    """The bound refuses what the edge cannot carry without refusing ordinary work:
    content summing to exactly the limit still publishes. Spread over two files
    because the aggregate bound is deliberately above the per-file cap, so no single
    permitted file can reach it."""
    half = MAX_COMMIT_CONTENT_BYTES // 2
    changes = [
        FileChange(path="a.bin", content=bytes(half)),
        FileChange(path="b.bin", content=bytes(MAX_COMMIT_CONTENT_BYTES - half)),
    ]
    recorder = Recorder()
    async with provider(recorder) as gh:
        published = await gh.publish_commit(changes=changes, message="fix", expected_head=HEAD, reauthorize=Counter())
    assert published.sha


async def test_an_empty_or_unmessaged_commit_is_refused():
    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_commit(changes=[], message="fix", expected_head=HEAD, reauthorize=Counter())
        with pytest.raises(OperationRefusedError):
            await gh.publish_commit(changes=[FileChange(path="a.py", content=b"x")], message="   ", expected_head=HEAD, reauthorize=Counter())
    assert recorder.requests == []


# --- Repository identity ---------------------------------------------------


async def test_a_renamed_repository_does_not_match_the_assignment():
    """The numeric ID is the authorization target. A squatter on the freed name
    resolves to a different ID and is refused."""
    recorder = Recorder(overrides={"GET /repos/acme/widgets": httpx.Response(200, json={"id": 111, "default_branch": "main"})})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.read_repository()


# --- Pull requests and review ---------------------------------------------


async def test_pull_request_is_opened_only_between_assigned_refs():
    recorder = Recorder()
    async with provider(recorder) as gh:
        result = await gh.upsert_pull_request(title="Fix", body="body", reauthorize=Counter())
    assert result == {"number": 7, "html_url": "https://github.test/pr/7", "created": True}
    created = next(body for method, path, body in recorder.requests if method == "POST" and path == "/repos/acme/widgets/pulls")
    assert created["head"] == BRANCH
    assert created["base"] == "main"


def _assigned_open_pull(number: int = 7) -> dict:
    """A PR listing entry that genuinely belongs to this assignment."""
    return {
        "number": number,
        "html_url": f"https://github.test/pr/{number}",
        "head": {"ref": BRANCH, "repo": {"id": REPOSITORY_ID}},
        "base": {"ref": "main"},
    }


async def test_an_existing_pull_request_is_updated_not_duplicated():
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": httpx.Response(200, json=[_assigned_open_pull()])})
    async with provider(recorder) as gh:
        result = await gh.upsert_pull_request(title="Fix", body="body", reauthorize=Counter())
    assert result["created"] is False
    assert not any(method == "POST" and path == "/repos/acme/widgets/pulls" for method, path, _ in recorder.requests)


# --- An update re-establishes identity before it mutates (#5223) -------------


async def test_updating_a_pull_request_reverifies_the_repository_identity():
    """The PR listing is keyed on the repository NAME. A rename plus a squatter on
    the freed name must not resolve here as the assignment's repository, so the
    immutable numeric id is re-read before the PATCH — as `publish_commit` does."""
    recorder = Recorder(
        overrides={
            "GET /repos/acme/widgets": httpx.Response(200, json={"id": 111, "default_branch": "main"}),
            "GET /repos/acme/widgets/pulls": httpx.Response(200, json=[_assigned_open_pull()]),
        }
    )
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.upsert_pull_request(title="Fix", body="body", reauthorize=Counter())
    assert not any(method == "PATCH" for method, _, _ in recorder.requests), "identity was refused, so nothing may have been mutated"


async def test_updating_refuses_a_same_named_branch_on_a_fork():
    """A fork can carry a branch of the same name, and the listing filters on the
    name. Without the head-repository check this would PATCH a stranger's PR."""
    fork = {**_assigned_open_pull(), "head": {"ref": BRANCH, "repo": {"id": REPOSITORY_ID + 1}}}
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": httpx.Response(200, json=[fork])})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.upsert_pull_request(title="Fix", body="body", reauthorize=Counter())
    assert not any(method == "PATCH" for method, _, _ in recorder.requests)


async def test_updating_refuses_a_pull_request_aimed_at_another_base():
    """A PR retargeted at another base is not the one this assignment covers;
    updating it would move work onto a branch nothing authorized."""
    retargeted = {**_assigned_open_pull(), "base": {"ref": "release/1.x"}}
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": httpx.Response(200, json=[retargeted])})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.upsert_pull_request(title="Fix", body="body", reauthorize=Counter())
    assert not any(method == "PATCH" for method, _, _ in recorder.requests)


async def test_the_author_of_a_pull_request_cannot_approve_it():
    """A PR on the assigned branch is one this platform authored. Approving it
    would forge the review the merge gate depends on."""
    recorder = Recorder(
        overrides={"GET /repos/acme/widgets/pulls/7": httpx.Response(200, json={"number": 7, "head": {"ref": BRANCH}, "user": {"id": 1}})}
    )
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="lgtm", event="APPROVE", reauthorize=Counter())
    assert not any(path.endswith("/reviews") for _, path, _ in recorder.requests)


async def test_an_author_may_still_comment_on_its_own_pull_request():
    """Commenting is not an authorization event, so it stays available."""
    recorder = Recorder(
        overrides={
            "GET /repos/acme/widgets/pulls/7": httpx.Response(
                200, json={"number": 7, "head": {"ref": BRANCH, "repo": {"id": REPOSITORY_ID}}, "base": {"ref": "main"}, "user": {"id": 1}}
            )
        }
    )
    async with provider(recorder) as gh:
        result = await gh.publish_review(pull_number=7, body="notes", event="COMMENT", reauthorize=Counter())
    assert result["id"] == 11


async def test_an_unknown_head_ref_is_treated_as_self_authored():
    """When we cannot establish the work is someone else's, we do not approve it."""
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls/7": httpx.Response(200, json={"number": 7, "user": {"id": 1}})})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="lgtm", event="APPROVE", reauthorize=Counter())


async def test_a_review_cannot_be_published_on_an_unrelated_pull_request():
    """`pull_number` is the one provider identifier a caller supplies, so it is an
    assertion to check, not a selector to obey. The Recorder's default pull request
    is on a foreign branch, which is precisely the unrelated-PR case."""
    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="lgtm", event="APPROVE", reauthorize=Counter())
    # Refused before the review was posted: no POST to /reviews happened.
    assert not [path for _, path, _ in recorder.requests if path.endswith("/reviews")]


async def test_a_review_is_refused_when_the_head_repository_is_not_the_assigned_one():
    """A branch NAME is not ours to control. Anyone who can fork this repository can
    push `agent/issue-<n>` to their own fork and open a PR into `main`, producing a
    PR whose head.ref and base.ref match ours exactly. If ownership were decided on
    those two strings, an outsider's branch would collect this assignment's review —
    and, under an accepted `Action.MERGE`, its merge. The head repository's immutable
    numeric id is the field they cannot forge."""
    forked = Recorder(
        overrides={
            "GET /repos/acme/widgets/pulls/7": httpx.Response(
                200,
                json={
                    "number": 7,
                    "head": {"ref": BRANCH, "repo": {"id": REPOSITORY_ID + 1}},
                    "base": {"ref": "main"},
                    "user": {"id": 99},
                },
            )
        }
    )
    async with provider(forked) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="notes", event="COMMENT", reauthorize=Counter())
    assert not [path for _, path, _ in forked.requests if path.endswith("/reviews")]

    # Same refusal on the merge path, where the consequence is a merged commit.
    async with provider(forked) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.merge_pull_request(pull_number=7, expected_head=HEAD, reauthorize=Counter())
    assert not [path for _, path, _ in forked.requests if path.endswith("/merge")]


async def test_a_review_is_refused_when_the_head_repository_cannot_be_established():
    """Unestablished ownership is not ownership: a head we cannot attribute is
    refused rather than accepted on its ref name alone."""
    for head in ({"ref": BRANCH}, {"ref": BRANCH, "repo": {}}, {"ref": BRANCH, "repo": {"id": True}}):
        recorder = Recorder(
            overrides={
                "GET /repos/acme/widgets/pulls/7": httpx.Response(200, json={"number": 7, "head": head, "base": {"ref": "main"}, "user": {"id": 1}})
            }
        )
        async with provider(recorder) as gh:
            with pytest.raises(OperationRefusedError):
                await gh.publish_review(pull_number=7, body="notes", event="COMMENT", reauthorize=Counter())
        assert not [path for _, path, _ in recorder.requests if path.endswith("/reviews")]


async def test_a_review_is_refused_when_the_base_is_not_the_assigned_base():
    """Head alone is insufficient: a review argues for a merge, so a PR targeting a
    branch this assignment never covered is refused even on the right head."""
    recorder = Recorder(
        overrides={
            "GET /repos/acme/widgets/pulls/7": httpx.Response(
                200, json={"number": 7, "head": {"ref": BRANCH}, "base": {"ref": "release/1.0"}, "user": {"id": 1}}
            )
        }
    )
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="notes", event="COMMENT", reauthorize=Counter())


async def test_merge_is_refused_on_an_unrelated_pull_request():
    """A merge authorization for THIS assignment must not merge another PR."""
    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.merge_pull_request(pull_number=7, expected_head=HEAD, reauthorize=Counter())
    assert not [path for _, path, _ in recorder.requests if path.endswith("/merge")]


@pytest.mark.parametrize("event", ["MERGE", "DISMISS", "approve", ""])
async def test_unsupported_review_events_are_refused(event):
    recorder = Recorder()
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.publish_review(pull_number=7, body="x", event=event, reauthorize=Counter())
    assert recorder.requests == []


async def test_merge_passes_the_expected_head_so_a_moved_branch_conflicts():
    recorder = Recorder(
        overrides={
            "GET /repos/acme/widgets/pulls/7": httpx.Response(
                200, json={"number": 7, "head": {"ref": BRANCH, "repo": {"id": REPOSITORY_ID}}, "base": {"ref": "main"}, "user": {"id": 1}}
            )
        }
    )
    async with provider(recorder) as gh:
        await gh.merge_pull_request(pull_number=7, expected_head=HEAD, reauthorize=Counter())
    body = next(body for _, path, body in recorder.requests if path.endswith("/merge"))
    assert body["sha"] == HEAD


# --- Timeouts reconcile rather than blindly retry --------------------------


async def test_a_timeout_is_reported_as_unavailable_not_as_success():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(timeout))
    async with GitHubProvider(token="t", assignment=_assignment(), client=client) as gh:
        with pytest.raises(ProviderUnavailableError):
            await gh.read_repository()


async def test_reconcile_finds_a_commit_that_landed_despite_the_timeout():
    """A timeout is not evidence that nothing happened. Reconciling first is what
    stops a retry from publishing the same change twice.

    Identity is the tree and parent we built, not the message — see below."""
    recorder = Recorder(branch_head=NEW_COMMIT, commit_message="fix")
    async with provider(recorder) as gh:
        found = await reconcile_commit(gh, message="fix", expected_parent=DEFAULT_HEAD, expected_tree="t" * 40)
    assert found is not None and found.sha == NEW_COMMIT
    assert found.parent_sha == DEFAULT_HEAD


async def test_reconcile_reports_nothing_when_the_commit_did_not_land():
    recorder = Recorder(branch_head=HEAD, commit_message="something else")
    async with provider(recorder) as gh:
        assert await reconcile_commit(gh, message="fix", expected_parent=DEFAULT_HEAD, expected_tree="t" * 40) is None


async def test_reconcile_refuses_to_adopt_a_commit_that_merely_shares_a_message():
    """A message is not an identifier. A commit on the branch with the same message
    but a different tree is somebody else's work (or a different change of ours);
    adopting it would report an effect we did not produce AND drop the change we
    were asked to publish."""
    recorder = Recorder(branch_head=NEW_COMMIT, commit_message="fix")
    async with provider(recorder) as gh:
        assert await reconcile_commit(gh, message="fix", expected_parent=DEFAULT_HEAD, expected_tree="different" + "0" * 31) is None


async def test_reconcile_without_prepared_objects_reports_unknown():
    """When the caller cannot say what it built, the outcome is genuinely unknown.
    Returning None keeps the provider error visible instead of resolving it by guess."""
    recorder = Recorder(branch_head=NEW_COMMIT, commit_message="fix")
    async with provider(recorder) as gh:
        assert await reconcile_commit(gh, message="fix") is None


def _open_pull(*, title="Fix it", body="details", repository_id=REPOSITORY_ID):
    return httpx.Response(
        200,
        json=[
            {
                "number": 7,
                "html_url": "https://github.test/pr/7",
                "title": title,
                "body": body,
                "head": {"ref": BRANCH, "repo": {"id": repository_id}},
            }
        ],
    )


async def test_reconcile_finds_a_pull_request_created_before_the_timeout():
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": _open_pull()})
    async with provider(recorder) as gh:
        found = await reconcile_pull_request(gh, title="Fix it", body="details")
    assert found is not None and found["number"] == 7


async def test_reconcile_refuses_a_pull_request_that_does_not_carry_our_update():
    """The defect this guards: `upsert_pull_request` PATCHes an ALREADY-OPEN PR, so
    on that path a PR exists before the call and still exists after it times out.
    Treating mere existence as success would report the requested title/body as
    published when the PATCH may never have been applied, and the retry that would
    have applied it never happens. An unobservable update stays unknown."""
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": _open_pull(title="Previous title")})
    async with provider(recorder) as gh:
        assert await reconcile_pull_request(gh, title="Fix it", body="details") is None

    stale_body = Recorder(overrides={"GET /repos/acme/widgets/pulls": _open_pull(body="previous body")})
    async with provider(stale_body) as gh:
        assert await reconcile_pull_request(gh, title="Fix it", body="details") is None


async def test_reconcile_refuses_a_pull_request_from_another_repository():
    """The branch-name query cannot establish ownership; the head repo id can."""
    recorder = Recorder(overrides={"GET /repos/acme/widgets/pulls": _open_pull(repository_id=REPOSITORY_ID + 1)})
    async with provider(recorder) as gh:
        assert await reconcile_pull_request(gh, title="Fix it", body="details") is None


async def test_server_errors_are_retryable_and_auth_failures_are_not():
    recorder = Recorder(overrides={"GET /repos/acme/widgets": httpx.Response(503)})
    async with provider(recorder) as gh:
        with pytest.raises(ProviderUnavailableError):
            await gh.read_repository()

    denied = Recorder(overrides={"GET /repos/acme/widgets": httpx.Response(403)})
    async with provider(denied) as gh:
        with pytest.raises(OperationRefusedError) as exc:
            await gh.read_repository()
        assert not isinstance(exc.value, ProviderUnavailableError)


async def test_no_provider_body_or_token_appears_in_an_error():
    """Provider bodies echo request content and URLs, and this path holds a token."""
    recorder = Recorder(overrides={"GET /repos/acme/widgets": httpx.Response(404, json={"message": "secret-detail", "token": "leaked"})})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError) as exc:
            await gh.read_repository()
    assert "secret-detail" not in str(exc.value)
    assert "leaked" not in str(exc.value)
    assert "test-token" not in str(exc.value)


def test_assignment_is_frozen_so_a_handler_cannot_retarget_it():
    """Rebinding the repository mid-request would move the effect off the
    authorized target after the checks ran."""
    assignment = _assignment()
    with pytest.raises(Exception):
        assignment.repository = "attacker/repo"  # type: ignore[misc]
    assert replace(assignment, repository="acme/widgets").repository == "acme/widgets"


@pytest.mark.parametrize("status", [401, 403, 409, 422, 429, 500, 503])
async def test_branch_read_errors_are_not_treated_as_an_absent_branch(status):
    recorder = Recorder(overrides={f"GET /repos/acme/widgets/git/ref/heads/{BRANCH}": httpx.Response(status)})
    async with provider(recorder) as gh:
        with pytest.raises(OperationRefusedError):
            await gh.read_repository()


async def test_branch_read_timeout_is_not_treated_as_an_absent_branch():
    def timeout():
        raise httpx.ReadTimeout("synthetic branch timeout")

    recorder = Recorder(overrides={f"GET /repos/acme/widgets/git/ref/heads/{BRANCH}": timeout})
    async with provider(recorder) as gh:
        with pytest.raises(ProviderUnavailableError):
            await gh.read_repository()


async def test_only_a_branch_404_is_treated_as_absent():
    recorder = Recorder(branch_head=None)
    async with provider(recorder) as gh:
        state = await gh.read_repository()
    assert state["branch_head"] is None


# --- The archive is delivered in slices the deployed edge can return (#5223) --


# The REST API Gateway in front of this service has a hard 10 MB response payload
# quota that cannot be raised (`infra/modules/api-gateway/main.tf` uses
# `aws_api_gateway_rest_api`). Base64 in JSON costs 4/3. These two facts, not taste,
# are what set `ARCHIVE_SLICE_BYTES`.
_EDGE_RESPONSE_LIMIT_BYTES = 10 * 1000 * 1000


def _archive_recorder(archive: bytes, **kwargs) -> Recorder:
    """A recorder whose tarball endpoint 302s to a codeload host serving `archive`."""
    return Recorder(
        overrides={
            "GET /repos/acme/widgets/tarball/": httpx.Response(302, headers={"location": "https://codeload.github.com/acme/widgets/tar.gz/x"}),
            **kwargs.pop("overrides", {}),
        },
        **kwargs,
    )


async def _fetch_slice(archive: bytes, *, offset: int = 0, length=None, recorder=None, monkeypatch=None):
    """Fetch one slice, serving `archive` from the redirect target.

    The codeload fetch deliberately uses a SEPARATE client with no Authorization
    header (so the installation token is never replayed to that host), so the
    provider's own MockTransport does not see it. `httpx.AsyncClient` is therefore
    patched to serve the archive for the codeload URL while still refusing any
    other host, which keeps the off-provider-redirect assertion meaningful.
    """
    recorder = recorder or _archive_recorder(archive)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)

        def serve(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers, "the codeload URL is pre-signed; the token must not be sent to it"
            return httpx.Response(200, content=archive)

        return real_client(*args, transport=httpx.MockTransport(serve), **kwargs)

    client = real_client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(recorder.handler),
        headers={"Authorization": "Bearer test-token"},
    )
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(httpx, "AsyncClient", fake_client)
        async with GitHubProvider(token="test-token", assignment=_assignment(), client=client) as gh:
            return await gh.fetch_repository_archive(offset=offset, length=length)


async def test_one_archive_slice_encodes_inside_the_edge_response_limit():
    """The defect this pins: the previous implementation returned the WHOLE archive
    base64'd in one JSON response, with a 128 MiB cap. This repository's own tarball
    is ~12.1 MiB (~16.9 MB encoded), so the first step of a mediated run could not
    be delivered by the deployed edge at all. A slice must fit."""
    import base64 as _base64

    archive = bytes(ARCHIVE_SLICE_BYTES * 3)
    sliced = await _fetch_slice(archive)

    assert len(sliced.content) == ARCHIVE_SLICE_BYTES, "a slice is bounded by the transport constant"
    encoded = len(_base64.b64encode(sliced.content))
    assert encoded < _EDGE_RESPONSE_LIMIT_BYTES, f"an encoded slice ({encoded} B) must fit the edge's 10 MB response limit"
    # Headroom for the envelope (digest, sha, repository, idempotency key).
    assert _EDGE_RESPONSE_LIMIT_BYTES - encoded > 1_000_000


async def test_a_realistic_repository_archive_is_deliverable_in_bounded_slices():
    """Sized at this repository's own measured tarball (12,665,576 B), which the
    single-response design could not deliver."""
    archive = bytes(12_665_576)
    first = await _fetch_slice(archive)
    assert first.total_bytes == len(archive)
    assert not first.complete
    # Every window, including the last, stays inside the transport bound.
    assembled, offset = bytearray(), 0
    while offset < len(archive):
        sliced = await _fetch_slice(archive, offset=offset)
        assert len(sliced.content) <= ARCHIVE_SLICE_BYTES
        assembled += sliced.content
        offset += len(sliced.content)
    assert bytes(assembled) == archive
    assert sliced.complete


async def test_every_slice_carries_the_whole_archive_digest_and_size():
    """Reassembly needs whole-archive identity on each slice: each slice request
    re-fetches, so this is what proves the pieces are of one snapshot."""
    import hashlib as _hashlib

    archive = bytes(range(256)) * 40_000
    digest = _hashlib.sha256(archive).hexdigest()
    first = await _fetch_slice(archive, offset=0)
    second = await _fetch_slice(archive, offset=ARCHIVE_SLICE_BYTES)
    for sliced in (first, second):
        assert sliced.digest == digest
        assert sliced.total_bytes == len(archive)
    assert first.content + second.content == archive[: len(first.content) + len(second.content)]


async def test_a_slice_never_exceeds_the_transport_bound_even_when_more_is_asked_for():
    """`length` is a request, not a grant: the edge limit is not negotiable."""
    archive = bytes(ARCHIVE_SLICE_BYTES * 4)
    sliced = await _fetch_slice(archive, length=ARCHIVE_SLICE_BYTES * 3)
    assert len(sliced.content) == ARCHIVE_SLICE_BYTES


async def test_an_offset_past_the_archive_is_refused_not_answered_empty():
    """An empty tail is indistinguishable from "done" to a caller looping until it
    has `total_bytes`, so a nonsense offset must refuse rather than let it spin."""
    archive = bytes(1024)
    with pytest.raises(OperationRefusedError):
        await _fetch_slice(archive, offset=4096)


async def test_an_archive_over_the_bound_is_refused_midstream():
    archive = bytes(MAX_ARCHIVE_BYTES + 1)
    with pytest.raises(OperationRefusedError):
        await _fetch_slice(archive)


async def test_an_off_provider_redirect_target_is_refused():
    """The gateway must not become a fetcher for an arbitrary URL."""
    recorder = Recorder(
        overrides={"GET /repos/acme/widgets/tarball/": httpx.Response(302, headers={"location": "https://attacker.test/archive.tar.gz"})}
    )
    with pytest.raises(OperationRefusedError):
        await _fetch_slice(bytes(16), recorder=recorder)
