"""The mediated path end to end, against the real policy boundary (#5223).

Every other suite in this story proves one layer. This one proves the layers
compose, which is where the story actually failed:

* `test_github_operation_service.py` proves the gateway authorizes a commit and
  refuses a merge — against real policy rows, but never through a worker.
* `test_github_operation_routes.py` proves the route's wiring — but stubs
  `authorize_operation`, so the merge gate there is a fixture's opinion.
* `test_mediated_github_wiring.py` proves startup withholds the token from the
  agent — but at the helper boundary, and an earlier revision of it returned a
  *mocked successful* raw token, which is exactly why it stayed green while a
  mediated run died at bootstrap and never started the model.

So each layer was green and the capability could not start. The gap was between
them, and a mocked successful token is not acceptance evidence. What runs here is,
in one path: real accepted-policy/grant/approval/work-claim rows with `merge`
human-gated, the real `authorize_worker_credential`, the real route, the real
`authorize_operation` and `build_assignment`, and the real worker helper
(`lib/mediated_github.py`) loaded from the worker image and driven over a test
transport into that route.

Only the provider's own network I/O is deterministic. That boundary is deliberate
and is the one thing here that is not production code: GitHub's HTTP behaviour is
asserted in `test_github_provider.py`, and the live scratch-repository run remains
a #5174 release gate rather than something this suite can claim.

The properties asserted:

1. A human-merge-gated develop assignment cannot obtain the raw installation token
   and can perform mediated operations — including under a grant shorter than the
   token's one-hour floor, which no token can serve at all.
2. Startup acquires its work tree through mediation: a real git tree materialized
   from an authorized archive, with no clone and no credential.
3. A local edit reaches the provider as a commit and then a pull request, on the
   branch the gateway derived, carrying the *provider's* head as `expected_head`.
4. Merge stays refused for the very same assignment that just published.

The worker's own startup sequence — that `main()` reaches none of the token paths
and still launches the model — is asserted in the worker image's suite
(`tests/test_mediated_github_wiring.py`), which is where `entrypoint` is
importable. Neither file can hold both halves: the policy rows live here, the
entrypoint lives there.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.agentauth import github_operation_routes as gor
from src.agentauth import routes
from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.github_operation_service import authorize_operation
from src.agentauth.github_operations import (
    MEDIATED_GITHUB_OPERATION_PATH,
    GitHubOperation,
    OperationRefusedError,
)
from src.agentauth.github_provider import ArchiveSlice, PublishedCommit
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from src.orchestration.runtime_policy import authorize_worker_credential
from src.shared.models.audit import AuditLog  # noqa: F401 — register before the fixture creates the schema

# The real accepted-policy fixture this story is about: a developer assignment with
# `merge` gated to a human. Imported rather than rebuilt so this file cannot drift
# into asserting against a policy shape the service suite no longer uses. Aliased on
# import — the same pattern that file uses for the fixtures it inherits — because a
# fixture imported under its own name and then taken as a parameter reads to ruff as
# a redefinition.
from tests.agentauth.test_github_operation_service import (
    GENERATION,
    ISSUE,
    POD,
)
from tests.agentauth.test_github_operation_service import (
    assignment as assignment_fixture,
)
from tests.agentauth.test_github_operation_service import (
    engine as engine_fixture,
)
from tests.agentauth.test_github_operation_service import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.agentauth.test_github_operation_service import (
    policy_budget_initializers as initializers_fixture,
)
from tests.agentauth.test_github_operation_service import (
    session as session_fixture,
)
from tests.agentauth.test_github_operation_service import (
    work_claims_enabled as work_claims_enabled_fixture,
)
from tests.orchestration.test_policy_admission import REPO

assignment = assignment_fixture
engine = engine_fixture
healthy_policy_reservations = reservations_fixture
policy_budget_initializers = initializers_fixture
session = session_fixture
work_claims_enabled = work_claims_enabled_fixture

BOOTSTRAP_TOKEN_PATH = "/internal/v1/github-installation-token"
BRANCH = f"agent/issue-{ISSUE}"
REMOTE_HEAD = "a" * 40
NEW_COMMIT = "c" * 40
CREDENTIAL = "adpr1.eyJhIjoxfQ.deadbeef"
# The gateway's own credential, named so assertions can prove it never crosses out
# of the gateway process.
GATEWAY_TOKEN = "ghs_held_inside_the_gateway_only"
VERIFIED_POD = VerifiedPod(uid=POD, name="agent-a", namespace="adp-agents", service_account="agent-scaledjob-sa", ip="10.0.1.5")
PROOFS = {"X-Adp-Run-Credential": CREDENTIAL, WORKLOAD_HEADER: "projected-workload-token"}


# ---------------------------------------------------------------------------
# The policy boundary itself: the refusal that blocked this, and the permit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant_minutes,label",
    [(120, "an ordinary grant"), (10, "a grant shorter than the token's one-hour floor")],
)
async def test_the_policy_refuses_the_token_and_permits_mediation(session, assignment, grant_minutes, label):
    """Both questions, to the real function, for the same assignment.

    Asserting them together is the point. The token being refused is only a
    *blocker* if mediation is permitted for the very same assignment, and this
    story's fix is that asymmetry rather than any loosening of the refusal. Split
    across two tests, one could pass while the capability stayed dead.

    The 10-minute case is the one no token can serve: GitHub's installation tokens
    have a one-hour minimum lifetime, so a token issued under a 10-minute grant
    necessarily outlives the authority it was issued for. Mediation issues nothing,
    so there is nothing to outlive.
    """
    grant = replace(assignment.grant, expires_at=datetime.now(UTC) + timedelta(minutes=grant_minutes))

    bootstrap = await authorize_worker_credential(session, execution=assignment.execution, grant=grant, broker_path=BOOTSTRAP_TOKEN_PATH)
    mediated = await authorize_worker_credential(session, execution=assignment.execution, grant=grant, broker_path=MEDIATED_GITHUB_OPERATION_PATH)

    assert not bootstrap.permitted, (
        f"the raw installation token was permitted under {label}; `contents: write` "
        "also authorizes PUT /repos/{o}/{r}/pulls/{n}/merge, so issuing one would "
        "dissolve the human merge gate no matter what the worker was told to do"
    )
    assert mediated.permitted, (
        f"mediation was refused under {label}, which leaves this assignment no way to publish at all — the blocker this story exists to remove"
    )


async def test_merge_stays_refused_for_the_assignment_that_may_publish(session, assignment):
    """One assignment, two answers, from the real authorization function."""
    kwargs = {
        "execution": assignment.execution,
        "grant": assignment.grant,
        "workload_binding": POD,
        "claim_generation": GENERATION,
    }
    authorized = await authorize_operation(session, operation=GitHubOperation.PUBLISH_COMMIT, **kwargs)
    assert authorized.assignment.branch == BRANCH, "the branch must be derived from the protected issue number"

    with pytest.raises(OperationRefusedError):
        await authorize_operation(session, operation=GitHubOperation.MERGE_PULL_REQUEST, **kwargs)


# ---------------------------------------------------------------------------
# A deterministic provider, with production code above it
# ---------------------------------------------------------------------------


def _archive_bytes() -> bytes:
    """A tar.gz shaped the way GitHub's is: one top-level prefix directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in (("README.md", b"# flagship\n"), ("src/app.py", b"value = 1\n")):
            info = tarfile.TarInfo(f"acme-flagship-{REMOTE_HEAD[:7]}/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class RecordingProvider:
    """Stands in for GitHub's HTTP surface only, recording what reached it.

    The signatures mirror `GitHubProvider`'s exactly, including the `reauthorize`
    callback every mutation must await — the route passes it, and a stand-in that
    ignored it would hide the re-authorization this whole design rests on.
    """

    instances: list[RecordingProvider] = []
    # Set by a test to change authority in the window between "authorized" and
    # "effect" — the window `reauthorize` exists to close. Counting reauthorize
    # calls cannot prove that closure, because a no-op callback is also counted;
    # only observing that a withdrawal mid-operation stops the effect can.
    withdraw_authority = None

    def __init__(self, *, token, assignment, client=None) -> None:
        self.token = token
        self.assignment = assignment
        self.calls: list[str] = []
        RecordingProvider.instances.append(self)

    async def _reauthorized(self, reauthorize) -> None:
        """Await the route's callback, after any mid-flight authority change."""
        if RecordingProvider.withdraw_authority is not None:
            RecordingProvider.withdraw_authority()
        await reauthorize()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    @classmethod
    def all_calls(cls) -> list[str]:
        return [call for instance in cls.instances for call in instance.calls]

    @classmethod
    def commits(cls) -> list[str]:
        return [call for call in cls.all_calls() if call.startswith("commit:")]

    async def read_repository(self) -> dict:
        self.calls.append("read")
        return {
            "repository_id": self.assignment.repository_id,
            "repository": self.assignment.repository,
            "default_branch": self.assignment.default_branch,
            "default_branch_head": REMOTE_HEAD,
            "branch": self.assignment.branch,
            "branch_head": None,
        }

    async def fetch_repository_archive(self, *, ref=None, offset: int = 0, length: int | None = None) -> ArchiveSlice:
        """One WINDOW of the archive, exactly as the real provider returns it.

        This stub used to return `(commit_sha, whole_bytes)`. When the transport
        moved to slices — because the deployed REST API Gateway caps a response at
        10 MB and a whole archive cannot be delivered — the real provider's
        signature grew `offset`/`length` and its return type became `ArchiveSlice`,
        but this stub did not follow. The route passes `offset=`, so every test that
        materializes a work tree died on `TypeError` before reaching its assertions:
        the one suite that proves the layers compose was failing at the layer it
        exists to check.

        It slices for real rather than returning the whole archive at any offset,
        so the worker's reassembly loop is genuinely exercised here — a stub that
        ignored the window would let a reassembly bug pass this suite.
        """
        self.calls.append(f"archive:{ref or self.assignment.branch}@{offset}")
        whole = _archive_bytes()
        window = whole[offset:] if length is None else whole[offset : offset + length]
        return ArchiveSlice(
            commit_sha=REMOTE_HEAD,
            total_bytes=len(whole),
            digest=hashlib.sha256(whole).hexdigest(),
            content=window,
            offset=offset,
        )

    async def publish_commit(self, *, changes, message, expected_head, reauthorize) -> PublishedCommit:
        await self._reauthorized(reauthorize)
        paths = ",".join(sorted(change.path for change in changes))
        self.calls.append(f"commit:{paths}:{message}:{expected_head}")
        return PublishedCommit(sha=NEW_COMMIT, branch=self.assignment.branch, parent_sha=expected_head or REMOTE_HEAD)

    async def upsert_pull_request(self, *, title, body, reauthorize) -> dict:
        await self._reauthorized(reauthorize)
        self.calls.append(f"pr:{title}")
        return {"number": 7, "html_url": "https://github.test/pr/7", "state": "open"}

    async def publish_review(self, *, pull_number, body, event, reauthorize) -> dict:
        await self._reauthorized(reauthorize)
        self.calls.append(f"review:{pull_number}:{event}")
        return {"id": 11, "state": "COMMENTED"}

    async def merge_pull_request(self, *, pull_number, expected_head, reauthorize) -> dict:
        # Reaching this means authorization admitted a merge for a human-gated
        # assignment. The tests assert it is never recorded.
        await self._reauthorized(reauthorize)
        self.calls.append(f"merge:{pull_number}")
        return {"merged": True, "sha": NEW_COMMIT}


class RealPolicyRuntime:
    """Authenticates the way production does, reporting THIS assignment's rows.

    Both proofs are demanded, `authenticate` returns the real `ExecutionRecord`
    dataclass, and `store._read` returns the raw protected item — the same
    two-value split the route depends on, and conflating them has silently broken
    this route before.

    `grant` is reassignable so a test can revoke or shorten authority *between*
    operations, which is the only way to observe that authority is read at the
    moment of each effect.
    """

    def __init__(self, execution: dict, grant) -> None:
        self.execution = execution
        self.grant = grant
        self.store = SimpleNamespace(_read=lambda partition_key, sort_key: execution)
        self.authentications = 0

    def authenticate(self, credential_token, workload_token):
        self.authentications += 1
        if not credential_token or not workload_token:
            raise OperationRefusedError("both proofs are required on every request")
        caller = SimpleNamespace(
            tenant_id=self.execution["tenant_id"]["S"],
            invocation_id=self.execution["invocation_id"]["S"],
            attempt=1,
            principal="worker#1",
        )
        record = ExecutionRecord(
            invocation_id=caller.invocation_id,
            tenant_id=caller.tenant_id,
            current_attempt=1,
            status=ExecutionStatus.ACTIVE,
            current_credential_epoch=1,
            min_acceptable_credential_epoch=1,
            workload_binding=VERIFIED_POD.uid,
            flow_id=self.execution["flow_id"]["S"],
            repo=self.execution["repo"]["S"],
        )
        return VERIFIED_POD, caller, record, self.grant

    async def validate_flow(self, record, grant):
        assert isinstance(record, ExecutionRecord), "the route must pass the record, not the raw item"
        return None


@pytest.fixture
def mediated_gateway(assignment, session, monkeypatch):
    """The real route, over real authorization, against this test's real rows.

    Replaced: the provider's network I/O, the token mint (a real one needs tenant
    App credentials), and the session factory — redirected to the fixture's own
    session so authorization reads the accepted plan, grant and work claim this test
    created. NOT replaced: `authorize_operation`, `current_claim_generation`,
    `build_assignment`, or the route body. The merge refusal below is therefore a
    real policy decision rather than a stub's return value.
    """
    RecordingProvider.instances = []
    RecordingProvider.withdraw_authority = None
    monkeypatch.setattr(gor, "GitHubProvider", RecordingProvider)

    class SessionFactory:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionFactory)

    minted: list[dict] = []

    async def installation_token(*, org_id, installation_id, repository, permissions):
        minted.append({"repository": repository, "permissions": permissions})
        return GATEWAY_TOKEN

    monkeypatch.setattr(gor, "installation_token", installation_token)

    app = FastAPI()
    app.include_router(gor.router)
    runtime = RealPolicyRuntime(assignment.execution, assignment.grant)
    # The two-proof dependency itself is asserted in `test_github_operation_routes.py`.
    # Here `authenticate` still demands both, so an unproven request is refused.
    app.dependency_overrides[routes.require_agent_transport] = lambda: None
    app.dependency_overrides[gor.get_agent_runtime] = lambda: runtime
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test")
    return SimpleNamespace(client=client, runtime=runtime, minted=minted)


@pytest.fixture
def worker_helper(mediated_gateway, monkeypatch):
    """The real worker helper, with its transport pointed at the real route.

    Loaded from the worker image's own file — the same `importlib` approach
    `test_github_operation_routes.py` uses — so what runs is the helper the image
    ships: its request shaping, its git handling, its error mapping.

    The helper is synchronous and the route is async, so `_request` hands the call
    back to the event loop with `anyio.from_thread.run`. Helper calls therefore have
    to be made from a worker thread (see `_offload`), which is also how the real
    helper runs: blocking, one operation at a time.
    """
    helper_path = Path(__file__).resolve().parents[3] / "agent-factory/agent-worker-image/lib/mediated_github.py"
    spec = importlib.util.spec_from_file_location("worker_mediated_acceptance", helper_path)
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, helper)
    spec.loader.exec_module(helper)

    async def post(payload):
        return await mediated_gateway.client.post(MEDIATED_GITHUB_OPERATION_PATH, json=payload, headers=PROOFS)

    def transport(payload, *, timeout=None, max_response_bytes=None):
        response = anyio.from_thread.run(post, payload)
        # The same status mapping `_request` applies to a real HTTPError, repeated
        # because ASGITransport returns a response rather than raising.
        if response.status_code == 409:
            raise helper.MediatedConflict("the assigned branch or pull request moved")
        if response.status_code == 503:
            raise helper.MediatedUnavailable("the mediated operation service is unavailable")
        if response.status_code != 200:
            raise helper.MediatedRefused(f"mediated operation refused (HTTP {response.status_code})")
        return response.json()

    monkeypatch.setattr(helper, "_request", transport)
    return helper


async def _offload(function, *args, **kwargs):
    """Run a synchronous helper call in a worker thread, as the worker does."""

    def call():
        return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(call)


async def _materialize(helper, work_dir: Path) -> dict:
    return await _offload(
        helper.materialize_repository,
        str(work_dir),
        repository=REPO,
        identity=("adp-agent[bot]", "adp-agent[bot]@users.noreply.github.com"),
    )


# ---------------------------------------------------------------------------
# Startup -> materialize -> local edit -> publish -> pull request
# ---------------------------------------------------------------------------


async def test_startup_materializes_commits_and_opens_a_pull_request(worker_helper, mediated_gateway, tmp_path):
    """The acceptance path: from an empty disk to a published pull request, no token.

    Each step goes through the real route and the real `authorize_operation`, so the
    commit and the pull request are published under authority derived from protected
    records at the moment of each effect.
    """
    work_dir = tmp_path / "repo"

    # Startup. The work tree comes from an authorized archive, not a clone — there
    # is no credential here to clone with.
    materialized = await _materialize(worker_helper, work_dir)
    assert materialized["remote_head"] == REMOTE_HEAD
    assert materialized["branch"] == BRANCH, "the gateway must report the branch it derived, not one the worker chose"
    assert (work_dir / "src/app.py").read_bytes() == b"value = 1\n", "the archive's content did not survive materialization"
    assert (work_dir / ".git").is_dir(), "materialization did not produce a real git work tree"
    # Not a clone: the local commit is a different object from the provider's.
    assert materialized["local_head"] != REMOTE_HEAD

    # The agent's actual work: a real edit in the real tree.
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")

    published = await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value")
    assert published.sha == NEW_COMMIT
    assert published.branch == BRANCH, "publication did not target the gateway-derived branch"

    pull = await _offload(worker_helper.upsert_pull_request, title="Adjust the value", body="Explanation.")
    assert pull["number"] == 7

    # The field name, pinned across the whole boundary. `html_url` is what the
    # provider returns, what the documented operation contract promises, and what
    # the agent's injected prompt tells it to print. GitHub also returns a `url`
    # (the API address, not the browsable page), so a rename to the shorter name
    # would keep every layer's tests green while handing the agent an api.github.com
    # link to report as its result — visible only to whoever reads the final report.
    # Nothing else asserts the spelling, so this is the one place it can be caught.
    assert pull["html_url"] == "https://github.test/pr/7", "the route must pass the provider's `html_url` through under that exact name"
    assert "url" not in pull, "the browsable link must not be renamed to `url`, which GitHub uses for the API address"

    calls = RecordingProvider.all_calls()
    assert any(call.startswith("archive:") for call in calls), calls
    assert any(call.startswith("commit:src/app.py:Adjust the value:") for call in calls), calls
    assert "pr:Adjust the value" in calls, calls

    # The commit carried the PROVIDER's head as `expected_head`, not the local SHA.
    # A materialized tree's local commit is a different object, so sending it would
    # read as a stale expectation and turn every first publication into a conflict.
    assert RecordingProvider.commits()[0].endswith(f":{REMOTE_HEAD}"), RecordingProvider.commits()

    # Minted per operation and narrowed to this repository, and only the publishing
    # operation ever held `contents: write`.
    archive_mint, commit_mint, pull_mint = mediated_gateway.minted
    assert {mint["repository"] for mint in mediated_gateway.minted} == {REPO}
    assert archive_mint["permissions"]["contents"] == "read", "materializing a work tree must not require write authority"
    assert commit_mint["permissions"]["contents"] == "write"
    assert pull_mint["permissions"]["contents"] == "read"


async def test_merge_is_refused_at_the_route_for_the_assignment_that_just_published(worker_helper, mediated_gateway, tmp_path):
    """After a real publication, the same run still cannot merge.

    Ordered deliberately: a merge refusal proves little alone, since a run that can
    do nothing also cannot merge. Publishing first establishes that this
    assignment's authority is live, and only then that merge lies outside it.

    The helper exposes no merge function, so this posts the operation directly. The
    property is that authorization refuses it, not that the helper declines to
    offer it.
    """
    work_dir = tmp_path / "repo"
    await _materialize(worker_helper, work_dir)
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")
    await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value")
    assert RecordingProvider.commits(), "the publication this test builds on did not happen"

    response = await mediated_gateway.client.post(
        MEDIATED_GITHUB_OPERATION_PATH,
        json={"operation": "merge_pull_request", "pull_number": 7, "expected_head": NEW_COMMIT},
        headers=PROOFS,
    )

    assert response.status_code == 404, response.text
    assert not any(call.startswith("merge:") for call in RecordingProvider.all_calls()), (
        "a merge reached the provider for an assignment whose policy gates merge to a human"
    )


async def test_a_worker_cannot_redirect_the_effect_by_asserting_another_target(worker_helper, tmp_path):
    """Assertions are compared against the protected record, never adopted.

    Exercised through the shipped helper's own `repository`/`branch` parameters, so
    what is proven is that the worker API cannot be used to write somewhere else —
    not merely that the service function would have refused.
    """
    work_dir = tmp_path / "repo"
    await _materialize(worker_helper, work_dir)
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")

    for override in ({"repository": "attacker/elsewhere"}, {"branch": "main"}):
        with pytest.raises(worker_helper.MediatedRefused):
            await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value", **override)
    assert not RecordingProvider.commits(), "a redirected assertion still reached the provider"


async def test_a_grant_shorter_than_the_token_floor_still_publishes(assignment, worker_helper, mediated_gateway, tmp_path):
    """The cohort no token can serve: a 10-minute grant, publishing normally.

    `test_the_policy_refuses_the_token_and_permits_mediation` proves the decision;
    this proves the effect, because a permitted decision that still could not
    publish would leave the capability just as unavailable.
    """
    mediated_gateway.runtime.grant = replace(assignment.grant, expires_at=datetime.now(UTC) + timedelta(minutes=10))

    work_dir = tmp_path / "repo"
    await _materialize(worker_helper, work_dir)
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")
    published = await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value")

    assert published.sha == NEW_COMMIT
    assert published.branch == BRANCH


async def test_authority_withdrawn_mid_operation_stops_the_effect(assignment, worker_helper, mediated_gateway, tmp_path):
    """The window between "authorized" and "effect", which is the whole design.

    Authorization runs before every provider mutation rather than once per request,
    so a grant revoked *during* an operation must stop it. That cannot be shown by
    counting `reauthorize` calls: a callback that returns immediately is counted
    just the same, and a mutation replacing its body with `return` passed a
    call-counting assertion. The only evidence is behavioural — change authority in
    that window and observe the effect not landing.

    `withdraw_authority` fires inside the provider, after the route has authorized
    and committed to publishing, at the moment the real code re-checks.
    """
    work_dir = tmp_path / "repo"
    await _materialize(worker_helper, work_dir)
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")

    def revoke():
        mediated_gateway.runtime.grant = replace(assignment.grant, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    RecordingProvider.withdraw_authority = revoke

    with pytest.raises(worker_helper.MediatedRefused):
        await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value")
    assert not RecordingProvider.commits(), (
        "the commit landed even though authority was withdrawn before the effect; re-authorization ran but did not gate the mutation"
    )


async def test_an_expired_grant_stops_the_next_publication(assignment, worker_helper, mediated_gateway, tmp_path):
    """Authority is observed at the effect, so expiry lands between operations.

    This is what makes the synchronous design stronger than issuing the worker
    something to present: nothing in the worker's hands outlives the authority it
    was derived from, so there is no window in which a stale artifact still works.
    """
    work_dir = tmp_path / "repo"
    await _materialize(worker_helper, work_dir)
    (work_dir / "src/app.py").write_bytes(b"value = 2\n")
    await _offload(worker_helper.publish_commit, repo=str(work_dir), message="Adjust the value")
    assert len(RecordingProvider.commits()) == 1

    mediated_gateway.runtime.grant = replace(assignment.grant, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    (work_dir / "src/app.py").write_bytes(b"value = 3\n")
    with pytest.raises(worker_helper.MediatedRefused):
        await _offload(worker_helper.publish_commit, repo=str(work_dir), message="A second change")
    assert len(RecordingProvider.commits()) == 1, "a publication landed after the grant had expired"
