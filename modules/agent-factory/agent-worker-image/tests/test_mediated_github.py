"""Worker-side mediated GitHub publication (#5223).

`collect_changes` runs against **real temporary git repositories**, not fixtures of
git output. That is the point: the reason this reads `git diff --raw` instead of a
textual patch is that a patch cannot represent a binary file and silently drops
mode transitions, and only real git can prove the raw parsing handles what git
actually emits. The gateway call itself is intercepted.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import mediated_github as mg


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real repository with one commit, isolated from developer git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Mediation test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    work = tmp_path / "work"
    git(tmp_path, "init", "--initial-branch=main", str(work))
    (work / "README.md").write_text("base\n")
    (work / "keep.py").write_text("print(0)\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    return work


@pytest.fixture
def transport(monkeypatch):
    """Intercept the gateway call, recording the payload the worker would send."""
    state = {"payloads": [], "response": {"commit_sha": "c" * 40, "branch": "agent/issue-5223", "parent_sha": "a" * 40}, "raise": None}

    def fake(payload, *, timeout=None):
        state["payloads"].append(payload)
        if state["raise"] is not None:
            raise state["raise"]
        return state["response"]

    monkeypatch.setattr(mg, "_request", fake)
    return state


# --- Real git: what gets collected -----------------------------------------


def test_a_modified_file_is_collected_with_its_content(repo, transport):
    (repo / "keep.py").write_text("print(1)\n")

    changes = mg.collect_changes(repo)

    assert [(c.path, c.content, c.deleted) for c in changes] == [("keep.py", b"print(1)\n", False)]


def test_a_binary_file_survives_collection(repo):
    """A textual patch cannot represent this, which is why raw mode is used."""
    payload = bytes(range(256))
    (repo / "logo.png").write_bytes(payload)

    changes = mg.collect_changes(repo)

    assert [c.content for c in changes if c.path == "logo.png"] == [payload]


def test_a_deletion_is_collected_as_a_deletion_not_as_empty_content(repo):
    """Empty content would publish an empty file where the run removed one."""
    (repo / "keep.py").unlink()

    changes = mg.collect_changes(repo)

    deleted = [c for c in changes if c.path == "keep.py"]
    assert len(deleted) == 1
    assert deleted[0].deleted is True
    assert deleted[0].content is None


def test_an_executable_bit_is_collected_as_a_mode_change(repo):
    """A script that is not executable on the remote does not run there."""
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)

    changes = mg.collect_changes(repo)

    assert [c.mode for c in changes if c.path == "run.sh"] == ["100755"]


def test_a_symlink_is_collected_with_its_own_mode(repo):
    (repo / "link").symlink_to("keep.py")

    changes = mg.collect_changes(repo)

    link = [c for c in changes if c.path == "link"][0]
    assert link.mode == "120000"
    assert link.content == b"keep.py"


def test_a_file_becoming_a_symlink_is_not_published_as_a_regular_file(repo):
    """The mode transition is the change; losing it publishes the link target as
    ordinary file content."""
    target = repo / "keep.py"
    target.unlink()
    target.symlink_to("README.md")

    changes = mg.collect_changes(repo)

    assert [(c.path, c.mode) for c in changes] == [("keep.py", "120000")]


def test_a_submodule_change_is_refused_before_anything_is_uploaded(repo, tmp_path):
    """A submodule pointer imports code nothing reviewing this change has seen."""
    other = tmp_path / "dep"
    git(tmp_path, "init", "--initial-branch=main", str(other))
    (other / "f.txt").write_text("x\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "dep")
    result = git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(other), "vendor/dep", check=False)
    if result.returncode != 0:
        pytest.skip("git refused a local submodule in this environment")

    with pytest.raises(mg.MediatedError, match="submodule"):
        mg.collect_changes(repo)


def test_multiple_changes_are_all_collected(repo):
    (repo / "keep.py").write_text("print(2)\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "README.md").unlink()

    changes = {c.path: c for c in mg.collect_changes(repo)}

    assert set(changes) == {"keep.py", "new.txt", "README.md"}
    assert changes["README.md"].deleted is True


def test_an_oversize_file_is_refused_locally(repo, monkeypatch):
    monkeypatch.setattr(mg, "MAX_BLOB_BYTES", 16)
    (repo / "big.bin").write_bytes(b"x" * 64)

    with pytest.raises(mg.MediatedError, match="blob limit"):
        mg.collect_changes(repo)


def test_aggregate_content_over_the_limit_is_refused_locally(repo, monkeypatch):
    """The bound the per-file cap cannot express: files each individually permitted
    that together exceed what the deployed edge will carry in one request. Refused
    here so the worker reports the size rather than seeing the edge drop the POST."""
    monkeypatch.setattr(mg, "MAX_COMMIT_CONTENT_BYTES", 100)
    for index in range(4):
        (repo / f"f{index}.bin").write_bytes(b"x" * 40)

    with pytest.raises(mg.MediatedError, match="per-commit total limit"):
        mg.collect_changes(repo)


def test_the_worker_bounds_match_the_gateways(repo):
    """Both are set by the same edge quota. A worker bound above the gateway's would
    send a request the gateway refuses; below it would refuse work the gateway
    permits."""
    assert mg.MAX_BLOB_BYTES == 5 * 1024 * 1024
    assert mg.MAX_COMMIT_CONTENT_BYTES == 6 * 1024 * 1024
    encoded = (mg.MAX_COMMIT_CONTENT_BYTES + 2) // 3 * 4
    assert encoded < mg.MAX_REQUEST_BYTES, "content alone must leave room for the request envelope"


def test_the_complete_serialized_request_is_bounded_before_transport(monkeypatch):
    """Field caps do not bound JSON expansion of permitted paths."""
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.invalid")
    monkeypatch.setattr(mg, "_identity_headers", lambda: pytest.fail("identity must not be read"))
    payload = {
        "operation": "publish_commit",
        "message": "m",
        "expected_head": "a" * 40,
        "changes": [
            {
                "path": chr(0x1F600) * 1024,
                "content_base64": base64.b64encode(b"x" * 12_582).decode(),
                "mode": "100644",
                "deleted": False,
            }
            for _ in range(500)
        ],
    }
    assert 12_582 * 500 < mg.MAX_COMMIT_CONTENT_BYTES
    wire_bytes = len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    assert wire_bytes > 10_000_000

    with pytest.raises(mg.MediatedError, match=rf"{wire_bytes} bytes.*{mg.MAX_REQUEST_BYTES} bytes"):
        mg._request(payload)


def test_too_many_files_is_refused_locally(repo, monkeypatch):
    monkeypatch.setattr(mg, "MAX_FILES_PER_COMMIT", 3)
    for index in range(5):
        (repo / f"f{index}.txt").write_text("x\n")

    with pytest.raises(mg.MediatedError, match="per-commit limit"):
        mg.collect_changes(repo)


def test_current_head_is_the_real_commit(repo):
    head = mg.current_head(repo)

    assert head == git(repo, "rev-parse", "HEAD").stdout.strip()


# --- What gets sent --------------------------------------------------------


def test_publish_sends_base64_content_and_the_expected_head(repo, transport):
    (repo / "keep.py").write_text("print(3)\n")
    head = mg.current_head(repo)

    published = mg.publish_commit(repo=repo, message="fix the thing")

    payload = transport["payloads"][0]
    assert payload["operation"] == "publish_commit"
    assert payload["expected_head"] == head
    assert base64.b64decode(payload["changes"][0]["content_base64"]) == b"print(3)\n"
    assert published.sha == "c" * 40


def test_no_method_url_installation_or_token_field_is_ever_sent(repo, transport):
    """The worker cannot describe an arbitrary provider call; it names an operation."""
    (repo / "keep.py").write_text("print(4)\n")

    mg.publish_commit(repo=repo, message="fix")

    payload = transport["payloads"][0]
    for forbidden in ("method", "url", "installation_id", "tenant_id", "token", "claim_generation", "not_after"):
        assert forbidden not in payload
    assert json.dumps(payload).count("http") == 0


def test_repository_and_branch_are_sent_only_as_assertions(repo, transport):
    (repo / "keep.py").write_text("print(5)\n")

    mg.publish_commit(repo=repo, message="fix", repository="acme/widgets", branch="agent/issue-5223")

    payload = transport["payloads"][0]
    assert payload["repository"] == "acme/widgets"
    assert payload["branch"] == "agent/issue-5223"


def test_publishing_nothing_is_refused_rather_than_sent_as_an_empty_commit(repo, transport):
    with pytest.raises(mg.MediatedError, match="no local changes"):
        mg.publish_commit(repo=repo, message="fix")
    assert transport["payloads"] == []


def test_a_commit_without_a_message_is_refused(repo, transport):
    (repo / "keep.py").write_text("print(6)\n")

    with pytest.raises(mg.MediatedError, match="message"):
        mg.publish_commit(repo=repo, message="   ")
    assert transport["payloads"] == []


def test_committed_work_and_successive_checkpoints_use_the_confirmed_base(repo, monkeypatch):
    # Match bootstrap: the local branch tracks the branch already pushed to GitHub.
    git(repo, "remote", "add", "origin", "https://example.invalid/fixture")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "branch", "--set-upstream-to", "origin/main")
    initial_head = mg.current_head(repo)
    remote = {"head": initial_head, "payloads": []}

    def publish(payload):
        if payload["expected_head"] != remote["head"]:
            raise mg.MediatedConflict("the remote branch moved")
        remote["payloads"].append(payload)
        remote["head"] = str(len(remote["payloads"])) * 40
        return {
            "commit_sha": remote["head"],
            "parent_sha": payload["expected_head"],
            "branch": "main",
        }

    monkeypatch.setattr(mg, "_request", publish)
    (repo / "keep.py").write_text("print(1)\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first local checkpoint")
    first = mg.publish_commit(repo=repo, message="first")
    assert remote["payloads"][0]["expected_head"] == initial_head
    assert [change["path"] for change in remote["payloads"][0]["changes"]] == ["keep.py"]

    # Load a new helper instance, as the documented separate python invocations do.
    spec = importlib.util.spec_from_file_location("mediated_fresh_checkpoint", mg.__file__)
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, fresh)
    spec.loader.exec_module(fresh)
    monkeypatch.setattr(fresh, "_request", publish)
    (repo / "README.md").write_text("second checkpoint\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "second local checkpoint")
    fresh.publish_commit(repo=repo, message="second")
    assert remote["payloads"][1]["expected_head"] == first.sha
    assert [change["path"] for change in remote["payloads"][1]["changes"]] == ["README.md"]


def test_successive_worktree_publications_preserve_external_conflicts(repo, monkeypatch):
    initial_head = mg.current_head(repo)
    remote = {"head": initial_head}

    def publish(payload):
        if payload["expected_head"] != remote["head"]:
            raise mg.MediatedConflict("the remote branch moved")
        remote["head"] = "c" * 40
        return {
            "commit_sha": remote["head"],
            "parent_sha": payload["expected_head"],
            "branch": "main",
        }

    monkeypatch.setattr(mg, "_request", publish)
    (repo / "keep.py").write_text("print(1)\n")
    mg.publish_commit(repo=repo, message="first")
    (repo / "keep.py").write_text("print(2)\n")
    remote["head"] = "d" * 40  # A different writer advanced GitHub.
    with pytest.raises(mg.MediatedConflict):
        mg.publish_commit(repo=repo, message="second")
    # A refusal did not advance the receipt: the same intended checkpoint can
    # still publish when the expected base is restored in this provider fixture.
    remote["head"] = "c" * 40
    mg.publish_commit(repo=repo, message="second")


def test_there_is_no_merge_function_on_the_worker_side(repo):
    """Merge exists only on the gateway, behind separately accepted authority. A
    develop or repair run cannot reach it even by constructing its own request."""
    assert not hasattr(mg, "merge_pull_request")
    assert "merge_pull_request" not in mg.__all__
    assert "merge" not in json.dumps({"exported": mg.__all__})


def test_a_pull_request_cannot_name_its_own_head_or_base(repo, transport):
    """Head and base are the assigned branch and default branch, gateway-side."""
    transport["response"] = {"pull_request": {"number": 7}}

    mg.upsert_pull_request(title="Fix", body="body")

    payload = transport["payloads"][0]
    assert "head" not in payload and "base" not in payload


def test_a_review_names_only_the_pull_request_and_the_event(repo, transport):
    transport["response"] = {"review": {"id": 11}}

    mg.publish_review(pull_number=7, body="notes", event="COMMENT")

    assert transport["payloads"][0] == {"operation": "publish_review", "pull_number": 7, "body": "notes", "review_event": "COMMENT"}


# --- Conflicts, timeouts and the absence of a fallback ---------------------


def test_a_conflict_is_visible_to_the_caller(repo, transport):
    (repo / "keep.py").write_text("print(7)\n")
    transport["raise"] = mg.MediatedConflict("moved")

    with pytest.raises(mg.MediatedConflict):
        mg.publish_commit(repo=repo, message="fix")


def test_an_unavailable_service_is_distinguishable_from_a_refusal(repo, transport):
    (repo / "keep.py").write_text("print(8)\n")
    transport["raise"] = mg.MediatedUnavailable("down")

    with pytest.raises(mg.MediatedUnavailable):
        mg.publish_commit(repo=repo, message="fix")


def test_there_is_no_installation_token_fallback_anywhere_in_this_module():
    """A fallback would restore the broad, hour-long, merge-capable credential this
    path exists to remove — and would do it exactly when authorization was in doubt."""
    source = Path(mg.__file__).read_text()
    for forbidden in ("github_installation_token", "GatewayCredentialClient", "GH_TOKEN", "ghs_", "x-access-token"):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("code", "expected"),
    [(409, mg.MediatedConflict), (503, mg.MediatedUnavailable), (404, mg.MediatedRefused), (403, mg.MediatedRefused)],
)
def test_gateway_status_codes_map_to_the_right_outcome(monkeypatch, code, expected):
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.invalid")
    monkeypatch.setattr(mg, "_identity_headers", lambda: {"X-Adp-Run-Credential": "c", "X-Adp-Workload-Token": "w"})
    monkeypatch.setattr(mg, "_sign", lambda method, url, headers, data: headers)

    class Opener:
        def open(self, request, timeout=None):
            raise HTTPError(request.full_url, code, "err", {}, None)

    monkeypatch.setattr(mg, "build_opener", lambda *_: Opener())

    with pytest.raises(expected):
        mg._request({"operation": "read_repository"})


def test_a_network_failure_is_retryable_rather_than_a_refusal(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.invalid")
    monkeypatch.setattr(mg, "_identity_headers", lambda: {})
    monkeypatch.setattr(mg, "_sign", lambda method, url, headers, data: headers)

    class Opener:
        def open(self, request, timeout=None):
            raise URLError("unreachable")

    monkeypatch.setattr(mg, "build_opener", lambda *_: Opener())

    with pytest.raises(mg.MediatedUnavailable):
        mg._request({"operation": "read_repository"})


def test_a_provider_error_body_is_not_surfaced_to_the_caller(monkeypatch):
    """The body may echo request content, and the decision does not need it."""
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.invalid")
    monkeypatch.setattr(mg, "_identity_headers", lambda: {})
    monkeypatch.setattr(mg, "_sign", lambda method, url, headers, data: headers)

    class Opener:
        def open(self, request, timeout=None):
            raise HTTPError(request.full_url, 404, "secret-detail", {}, None)

    monkeypatch.setattr(mg, "build_opener", lambda *_: Opener())

    with pytest.raises(mg.MediatedRefused) as exc:
        mg._request({"operation": "read_repository"})
    assert "secret-detail" not in str(exc.value)


# --- Transport requirements ------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    ["http://gw.example.invalid", "https://user:pw@gw.example.invalid", "https://gw.example.invalid?x=1", ""],
)
def test_a_non_clean_https_endpoint_is_refused(monkeypatch, endpoint):
    """The identity headers are bearer-shaped; plaintext or a redirect would hand
    them to whatever answered."""
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", endpoint)
    monkeypatch.setattr(mg, "_identity_headers", lambda: {})

    with pytest.raises(mg.MediatedError):
        mg._request({"operation": "read_repository"})


def test_both_worker_proofs_are_sent(monkeypatch, tmp_path):
    credential = tmp_path / "cred"
    workload = tmp_path / "wl"
    credential.write_text("adpr1.payload.mac\n")
    workload.write_text("a-projected-token\n")
    monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("ADP_WORKLOAD_TOKEN_FILE", str(workload))

    headers = mg._identity_headers()

    assert headers == {"X-Adp-Run-Credential": "adpr1.payload.mac", "X-Adp-Workload-Token": "a-projected-token"}


def test_a_missing_workload_token_is_an_error_not_an_unauthenticated_request(monkeypatch, tmp_path):
    credential = tmp_path / "cred"
    credential.write_text("adpr1.payload.mac\n")
    monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", str(credential))
    monkeypatch.delenv("ADP_WORKLOAD_TOKEN_FILE", raising=False)

    with pytest.raises(mg.MediatedError):
        mg._identity_headers()


def test_redirects_are_not_followed(monkeypatch):
    assert mg._NoRedirect().redirect_request(None, None, 302, "found", {}, "https://elsewhere.invalid") is None


def test_mediation_is_dormant_unless_explicitly_enabled(monkeypatch):
    """Kept off until the required gateway cohort is deployed, so a worker never
    depends on an endpoint that is not there yet."""
    monkeypatch.delenv("ADP_MEDIATED_GITHUB_ENABLED", raising=False)
    assert mg.enabled() is False

    monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", "true")
    assert mg.enabled() is True

    monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", "TRUE")
    assert mg.enabled() is True

    # "1" is truthy here because entrypoint._mediated_github_enabled accepts it.
    # One variable must not have two parsers: a deployment that set the flag to "1"
    # would otherwise be mediated while this helper reported mediation off.
    monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", "1")
    assert mg.enabled() is True

    monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", "yes")
    assert mg.enabled() is True

    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", value)
        assert mg.enabled() is False, value


@pytest.mark.parametrize("name", ["0:keep.py", "1:keep.py", ":keep.py"])
def test_colon_prefixed_filename_publishes_its_own_bytes(repo, transport, name):
    payload = b"this is the colon-prefixed file, not keep.py\n"
    (repo / name).write_bytes(payload)
    mg.publish_commit(repo=repo, message="colon filename")
    changes = transport["payloads"][0]["changes"]
    assert len(changes) == 1
    assert changes[0]["path"] == name
    assert base64.b64decode(changes[0]["content_base64"]) == payload


# --- The archive arrives in slices the deployed edge can return (#5223) -------


def _slice_server(archive: bytes, *, digest=None, total=None, slice_bytes=None):
    """Serve `archive` the way the gateway does: one bounded window per call.

    `digest`/`total` may be overridden per call to simulate the repository moving
    between slice requests.
    """
    import hashlib

    window = slice_bytes or mg._MAX_ARCHIVE_BYTES
    calls: list[dict] = []

    def serve(payload, *, timeout=None, max_response_bytes=None):
        calls.append(payload)
        offset = payload.get("archive_offset", 0)
        chunk = archive[offset : offset + window]
        real_digest = hashlib.sha256(archive).hexdigest()
        resolved_digest = digest(len(calls)) if callable(digest) else (digest or real_digest)
        resolved_total = total(len(calls)) if callable(total) else (total if total is not None else len(archive))
        return {
            "commit_sha": "c" * 40,
            "branch": "agent/issue-5223",
            "repository": "acme/widgets",
            "archive_format": "tar.gz",
            "archive_total_bytes": resolved_total,
            "archive_digest": resolved_digest,
            "archive_digest_algorithm": "sha256",
            "archive_offset": offset,
            "archive_slice_bytes": len(chunk),
            "archive_complete": offset + len(chunk) >= len(archive),
            "archive_base64": base64.b64encode(chunk).decode("ascii"),
            "idempotency_key": "k",
        }

    serve.calls = calls
    return serve


def _noise(length: int) -> bytes:
    """Deterministic incompressible bytes, so a gzipped tarball actually reaches a
    given size (repeated text would compress below any slice bound)."""
    import hashlib

    out = bytearray()
    seed = b"adp-5223"
    while len(out) < length:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:length])


def _tarball(files: dict[str, bytes]) -> bytes:
    """A GitHub-shaped tarball: everything under one `<owner>-<repo>-<sha>/` root."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, content in files.items():
            info = tarfile.TarInfo(f"acme-widgets-cccccccc/{name}")
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_an_archive_larger_than_one_slice_is_reassembled_and_verified(monkeypatch):
    """The defect this pins: the gateway cannot return a real repository's archive
    in one response (the deployed edge caps a response at 10 MB and base64 costs
    4/3), so the worker must request successive windows and rejoin them."""
    archive = _tarball({"README.md": _noise(40_000), "app.py": b"print(1)\n"})
    server = _slice_server(archive, slice_bytes=4096)
    monkeypatch.setattr(mg, "_request", server)

    rejoined, envelope = mg._fetch_archive(ref=None, repository=None)

    assert rejoined == archive
    assert len(server.calls) > 1, "a multi-slice archive must take more than one call"
    assert [call["archive_offset"] for call in server.calls] == [i * 4096 for i in range(len(server.calls))]
    # Transfer bookkeeping must not leak into the caller's envelope as state.
    for leaked in ("archive_base64", "archive_offset", "archive_slice_bytes", "archive_complete"):
        assert leaked not in envelope
    assert envelope["commit_sha"] == "c" * 40


def test_a_repository_that_moves_between_slices_is_refused_not_spliced(monkeypatch):
    """Each slice request re-fetches gateway-side, so a push mid-transfer would
    otherwise splice two different snapshots into one work tree."""
    archive = _tarball({"README.md": _noise(20_000)})
    server = _slice_server(archive, slice_bytes=4096, digest=lambda n: "0" * 64 if n > 1 else "1" * 64)
    monkeypatch.setattr(mg, "_request", server)

    with pytest.raises(mg.MediatedConflict):
        mg._fetch_archive(ref=None, repository=None)


def test_a_corrupted_archive_is_refused_before_it_becomes_a_work_tree(monkeypatch):
    """The digest is verified over the reassembled bytes. A wrong one must fail
    rather than produce a tree the agent then builds and publishes from."""
    archive = _tarball({"README.md": b"z" * 5_000})
    server = _slice_server(archive, digest="f" * 64)
    monkeypatch.setattr(mg, "_request", server)

    with pytest.raises(mg.MediatedError) as failure:
        mg._fetch_archive(ref=None, repository=None)
    assert "integrity" in str(failure.value)


def test_a_truncated_transfer_is_refused(monkeypatch):
    """A declared total the slices never reach must not pass as complete."""
    archive = _tarball({"README.md": b"q" * 5_000})
    server = _slice_server(archive, total=len(archive) + 4096)
    monkeypatch.setattr(mg, "_request", server)

    with pytest.raises(mg.MediatedError):
        mg._fetch_archive(ref=None, repository=None)


def test_a_gateway_that_stops_advancing_fails_instead_of_spinning(monkeypatch):
    """An empty slice before completion means no progress; looping forever on a
    stuck offset is worse than failing."""
    archive = _tarball({"README.md": b"w" * 9_000})

    def stuck(payload, *, timeout=None, max_response_bytes=None):
        return {
            "commit_sha": "c" * 40,
            "archive_total_bytes": len(archive),
            "archive_digest": "a" * 64,
            "archive_base64": "",
        }

    monkeypatch.setattr(mg, "_request", stuck)
    with pytest.raises(mg.MediatedError) as failure:
        mg._fetch_archive(ref=None, repository=None)
    assert "did not progress" in str(failure.value)


@pytest.mark.parametrize(
    "broken",
    [
        {"archive_digest": "not-a-digest"},
        {"archive_total_bytes": -1},
        {"archive_total_bytes": "many"},
        {"archive_digest_algorithm": "md5"},
        {"commit_sha": "short"},
    ],
)
def test_an_unusable_archive_envelope_is_refused(monkeypatch, broken):
    archive = _tarball({"README.md": b"r" * 1_000})
    base = _slice_server(archive)

    def broken_server(payload, *, timeout=None, max_response_bytes=None):
        return {**base(payload), **broken}

    monkeypatch.setattr(mg, "_request", broken_server)
    with pytest.raises(mg.MediatedError):
        mg._fetch_archive(ref=None, repository=None)


def test_a_materialized_tree_is_a_real_repository_recording_the_remote_head(tmp_path, monkeypatch):
    """End to end over the slice path: the archive becomes a checked-out git tree
    whose recorded remote head is the PROVIDER's commit, not the local one."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    archive = _tarball({"README.md": b"base\n", "app.py": b"print(1)\n"})
    monkeypatch.setattr(mg, "_request", _slice_server(archive, slice_bytes=2048))

    destination = tmp_path / "tree"
    result = mg.materialize_repository(str(destination), identity=("Test", "test@example.invalid"))

    assert (destination / "README.md").read_bytes() == b"base\n"
    assert (destination / "app.py").read_bytes() == b"print(1)\n"
    assert (destination / ".git").is_dir()
    assert result["remote_head"] == "c" * 40
    assert result["local_head"] != result["remote_head"], "a locally built commit cannot reproduce the provider's SHA"
    assert "archive_base64" not in result
