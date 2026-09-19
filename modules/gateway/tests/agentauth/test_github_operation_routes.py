"""The mediated GitHub operation route (#5223).

What is asserted here is the *route layer*: that both proofs are demanded, that no
request field can redirect the effect, that the claim generation is read rather
than accepted, that a refusal is indistinguishable from a missing route, and that
a conflict is distinguishable because the worker needs to act on it.

The authorization decisions themselves are asserted against real policy and claim
rows in `test_github_operation_service.py`; the provider's HTTP behaviour is
asserted in `test_github_provider.py`. This file deliberately stubs both so a
failure here means the *wiring* is wrong.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth import github_operation_routes as gor
from src.agentauth import routes
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.github_operations import OperationAssignment, OperationRefusedError
from src.agentauth.github_provider import PublishedCommit
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError

CREDENTIAL = "adpr1.eyJhIjoxfQ.deadbeef"
POD = VerifiedPod(uid="pod-uid-a", name="agent-a", namespace="adp-agents", service_account="agent-scaledjob-sa", ip="10.0.1.5")
REPOSITORY_ID = 987654
BRANCH = "agent/issue-5223"
HEAD = "a" * 40
NEW_COMMIT = "c" * 40
PATH = "/internal/v1/agent/self/github-operation"


def _assignment(**overrides) -> OperationAssignment:
    fields = {
        "tenant_id": "org-a",
        "invocation_id": "run-a",
        "attempt": 1,
        "workload_binding": POD.uid,
        "claim_generation": 3,
        "installation_id": 4242,
        "repository_id": REPOSITORY_ID,
        "repository": "acme/widgets",
        "branch": BRANCH,
        "default_branch": "main",
        "node_id": "node-1",
        "accepted_plan_version": 2,
        "not_after": datetime.now(UTC) + timedelta(minutes=10),
    }
    fields.update(overrides)
    return OperationAssignment(**fields)


class StubWorkloads:
    def __init__(self, *, pod=POD, refuse=False) -> None:
        self.pod = pod
        self.refuse = refuse
        self.tokens: list[str] = []

    def verify(self, token: str) -> VerifiedPod:
        self.tokens.append(token)
        if self.refuse or not token:
            raise WorkloadRefusedError("workload token refused")
        return self.pod


# The raw protected item, in the DynamoDB attribute-value shape the store holds and
# `authorize_worker_credential`/`build_assignment` read. Distinct from
# ExecutionRecord below on purpose: conflating the two is the defect these stubs now
# make impossible to reintroduce silently.
RAW_EXECUTION = {
    "tenant_id": {"S": "org-a"},
    "invocation_id": {"S": "run-a"},
    "flow_id": {"S": "flow-a"},
    "orchestration_node_id": {"S": "node-1"},
    "orchestration_node_attempt": {"N": "1"},
    "installation_id": {"N": "4242"},
    "repo": {"S": "acme/widgets"},
    "issue_number": {"N": "5223"},
    "default_branch": {"S": "main"},
}


class StubStore:
    """Stands in for the authority store's raw read only.

    `_read` is the same private accessor `broker_identity.verify_broker_worker` uses
    to obtain the raw item; the route reads through it for the same reason.
    """

    def __init__(self, item=None) -> None:
        self.item = RAW_EXECUTION if item is None else item
        self.reads: list[tuple[str, str]] = []

    def _read(self, partition_key, sort_key):
        self.reads.append((partition_key, sort_key))
        return self.item


class StubRuntime:
    """Authenticates like production does, including its RETURN TYPES.

    `authenticate` returns the real `ExecutionRecord` dataclass, because that is what
    `AgentRuntime.authenticate` returns. Returning a dict here previously hid a
    production `AttributeError`: the route called `.get()` on the record, which a
    frozen dataclass does not have, so every real request would have 500'd while the
    suite stayed green.
    """

    def __init__(self, workloads, *, store=None) -> None:
        self.workloads = workloads
        self.env = None
        self.store = StubStore() if store is None else store

    def authenticate(self, credential_token, workload_token):
        if not credential_token:
            raise WorkloadRefusedError("no run credential")
        pod = self.workloads.verify(workload_token)
        caller = SimpleNamespace(tenant_id="org-a", invocation_id="run-a", attempt=1, principal="run-a#1")
        record = ExecutionRecord(
            invocation_id="run-a",
            tenant_id="org-a",
            current_attempt=1,
            status=ExecutionStatus.ACTIVE,
            current_credential_epoch=1,
            min_acceptable_credential_epoch=1,
            workload_binding=POD.uid,
            flow_id="flow-a",
            repo="acme/widgets",
        )
        return pod, caller, record, SimpleNamespace(tenant_id="org-a")

    async def validate_flow(self, record, grant):
        # Production passes the ExecutionRecord here, so assert the route does too:
        # this is the value whose type the route must not confuse with the raw item.
        assert isinstance(record, ExecutionRecord)
        return None


class StubProvider:
    """Records the provider calls the route made, and can refuse mid-sequence."""

    instances: list[StubProvider] = []

    def __init__(self, *, token, assignment, client=None) -> None:
        self.token = token
        self.assignment = assignment
        self.calls: list[str] = []
        self.reauthorizations = 0
        StubProvider.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def read_repository(self):
        self.calls.append("read")
        return {"id": REPOSITORY_ID, "default_branch": "main"}

    async def publish_commit(self, *, changes, message, expected_head, reauthorize):
        await reauthorize()
        self.reauthorizations += 1
        self.calls.append(f"commit:{len(changes)}:{message}:{expected_head}")
        return PublishedCommit(sha=NEW_COMMIT, branch=self.assignment.branch, parent_sha=expected_head or HEAD)

    async def upsert_pull_request(self, *, title, body, reauthorize):
        await reauthorize()
        self.calls.append(f"pr:{title}")
        return {"number": 7, "url": "https://github.test/pr/7", "created": True}

    async def publish_review(self, *, pull_number, body, event, reauthorize):
        await reauthorize()
        self.calls.append(f"review:{pull_number}:{event}")
        return {"id": 11, "state": "COMMENTED"}

    async def merge_pull_request(self, *, pull_number, expected_head, reauthorize):
        await reauthorize()
        self.calls.append(f"merge:{pull_number}:{expected_head}")
        return {"merged": True, "sha": NEW_COMMIT}


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    """Replace the provider, the token mint and the DB session, keeping the route.

    The token mint is stubbed because a real one needs tenant App credentials; the
    session factory because the authorization it feeds is asserted elsewhere. What
    is NOT stubbed is `_perform` or the route body itself.
    """
    StubProvider.instances = []
    state = SimpleNamespace(
        authorizations=0,
        generation=3,
        authorize_error=None,
        generation_error=None,
        assignment=_assignment(),
        policy=SimpleNamespace(permits=lambda action: False),
        minted=[],
    )

    monkeypatch.setattr(gor, "GitHubProvider", StubProvider)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: Session)

    async def current_claim_generation(session, *, org_id, invocation_id):
        if state.generation_error is not None:
            raise state.generation_error
        return state.generation

    async def authorize_operation(session, **kwargs):
        state.authorizations += 1
        state.last_kwargs = kwargs
        if state.authorize_error is not None:
            raise state.authorize_error
        return SimpleNamespace(
            assignment=state.assignment,
            operation=kwargs["operation"],
            policy=state.policy,
            plan_version=2,
        )

    async def installation_token(*, org_id, installation_id, repository, permissions):
        state.minted.append(permissions)
        return "provider-token-value"

    monkeypatch.setattr(gor, "current_claim_generation", current_claim_generation)
    monkeypatch.setattr(gor, "authorize_operation", authorize_operation)
    monkeypatch.setattr(gor, "installation_token", installation_token)
    return state


@pytest.fixture
def harness(stubs):
    def build(*, workloads=None):
        app = FastAPI()
        # The coordinator router is mounted first, as app.py mounts it, so a route
        # shadowed by `/control/{run_id}/{action}` would show up here.
        app.include_router(routes.router)
        app.include_router(gor.router)
        stub_workloads = workloads or StubWorkloads()
        app.dependency_overrides[routes.require_agent_transport] = lambda: None
        app.dependency_overrides[gor.get_agent_runtime] = lambda: StubRuntime(stub_workloads)
        return TestClient(app, raise_server_exceptions=False), stub_workloads

    return build


def headers(*, credential=CREDENTIAL, workload="a-projected-workload-token"):
    sent = {}
    if credential is not None:
        sent["X-Adp-Run-Credential"] = credential
    if workload is not None:
        sent["X-Adp-Workload-Token"] = workload
    return sent


def commit_body(**overrides):
    body = {
        "operation": "publish_commit",
        "message": "fix the thing",
        "expected_head": HEAD,
        "changes": [{"path": "src/app.py", "content_base64": base64.b64encode(b"print(1)").decode()}],
    }
    body.update(overrides)
    return body


# --- The route is reachable and does the operation ---------------------------


def test_the_route_is_not_shadowed_and_publishes_a_commit(harness):
    client, _ = harness()

    response = client.post(PATH, json=commit_body(), headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["commit_sha"] == NEW_COMMIT
    assert payload["branch"] == BRANCH
    assert payload["idempotency_key"]
    assert StubProvider.instances[0].calls == [f"commit:1:fix the thing:{HEAD}"]


def test_the_server_refuses_the_complete_wire_body_before_authorization(harness, stubs, monkeypatch):
    """Per-field limits cannot account for JSON escaping and combined overhead."""
    client, _ = harness()
    monkeypatch.setattr(gor, "MAX_OPERATION_REQUEST_BYTES", 200)
    raw = json.dumps(
        commit_body(changes=[{"path": chr(0x1F600) * 20, "content_base64": base64.b64encode(b"x").decode()}]),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    assert len(raw) > gor.MAX_OPERATION_REQUEST_BYTES

    response = client.post(PATH, content=raw, headers={**headers(), "Content-Type": "application/json"})

    assert response.status_code == 413
    assert stubs.authorizations == 0


@pytest.mark.parametrize(
    ("operation", "extra", "expected"),
    [
        ("read_repository", {}, "read"),
        ("upsert_pull_request", {"title": "Fix", "body": "b"}, "pr:Fix"),
        ("publish_review", {"pull_number": 7, "body": "notes", "review_event": "COMMENT"}, "review:7:COMMENT"),
        ("merge_pull_request", {"pull_number": 7, "expected_head": HEAD}, f"merge:7:{HEAD}"),
    ],
)
def test_each_typed_operation_reaches_its_own_provider_call(harness, operation, extra, expected):
    client, _ = harness()

    response = client.post(PATH, json={"operation": operation, **extra}, headers=headers())

    assert response.status_code == 200
    assert StubProvider.instances[0].calls == [expected]


def test_the_token_is_minted_narrowly_per_operation_and_never_returned(harness, stubs):
    """A review must not receive the contents-write that also authorizes merge."""
    client, _ = harness()

    review = client.post(PATH, json={"operation": "publish_review", "pull_number": 7, "body": "notes"}, headers=headers())
    assert stubs.minted[-1]["contents"] == "read"
    assert "provider-token-value" not in review.text

    client.post(PATH, json=commit_body(), headers=headers())
    assert stubs.minted[-1]["contents"] == "write"


# --- Nothing in the request may redirect the effect -------------------------


def test_no_request_field_can_name_a_method_or_url(harness):
    """The absence of forwarding is the control, so an attempt to add one is a 422
    rather than a silently ignored field."""
    client, _ = harness()

    for extra in ({"method": "PUT"}, {"url": "https://api.github.com/x"}, {"installation_id": 1}, {"tenant_id": "other"}):
        response = client.post(PATH, json={**commit_body(), **extra}, headers=headers())
        assert response.status_code == 422


def test_an_unknown_operation_is_refused_by_the_closed_enum(harness):
    client, _ = harness()

    for operation in ("delete_branch", "dispatch_workflow", "force_push", "return_token", ""):
        response = client.post(PATH, json={"operation": operation}, headers=headers())
        assert response.status_code == 422
    assert StubProvider.instances == []


def test_assertions_are_passed_to_authorization_not_used_directly(harness, stubs):
    """`repository` and `branch` are compared against the protected record; the
    effect still targets the derived assignment."""
    client, _ = harness()

    response = client.post(PATH, json=commit_body(repository="attacker/repo", branch="main"), headers=headers())

    assert response.status_code == 200
    assert stubs.last_kwargs["asserted_repository"] == "attacker/repo"
    assert stubs.last_kwargs["asserted_branch"] == "main"
    # The provider was constructed with the DERIVED assignment regardless.
    assert StubProvider.instances[0].assignment.branch == BRANCH
    assert StubProvider.instances[0].assignment.repository == "acme/widgets"


def test_the_claim_generation_is_read_not_accepted_from_the_body(harness, stubs):
    """Accepting it would let a stale worker assert the generation it wished it
    still held — the thing the comparison exists to establish."""
    client, _ = harness()
    stubs.generation = 9

    response = client.post(PATH, json={**commit_body(), "claim_generation": 1}, headers=headers())
    assert response.status_code == 422

    assert client.post(PATH, json=commit_body(), headers=headers()).status_code == 200
    assert stubs.last_kwargs["claim_generation"] == 9


def test_the_workload_binding_comes_from_the_verified_pod(harness, stubs):
    client, _ = harness()

    client.post(PATH, json=commit_body(), headers=headers())

    assert stubs.last_kwargs["workload_binding"] == POD.uid


# --- Both proofs, every request --------------------------------------------


def test_a_missing_run_credential_is_refused(harness):
    client, _ = harness()

    response = client.post(PATH, json=commit_body(), headers=headers(credential=None))

    assert response.status_code == 404
    assert StubProvider.instances == []


def test_a_refused_workload_token_is_refused(harness):
    client, _ = harness(workloads=StubWorkloads(refuse=True))

    response = client.post(PATH, json=commit_body(), headers=headers())

    assert response.status_code == 404
    assert StubProvider.instances == []


def test_the_workload_token_is_verified_on_this_request_not_carried_from_bootstrap(harness):
    client, workloads = harness()

    client.post(PATH, json=commit_body(), headers=headers(workload="first-request"))
    client.post(PATH, json=commit_body(), headers=headers(workload="second-request"))

    # Each request verifies its own proof again before provider effects.
    assert set(workloads.tokens) == {"first-request", "second-request"}
    assert workloads.tokens.count("first-request") >= 2
    assert workloads.tokens.count("second-request") >= 2


# --- Authorization runs before the effect, and again per mutation -----------


def test_a_refusal_is_indistinguishable_from_a_missing_route(harness, stubs):
    client, _ = harness()
    stubs.authorize_error = OperationRefusedError("operation is not currently authorized")

    response = client.post(PATH, json=commit_body(), headers=headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    assert StubProvider.instances == []


def test_lost_work_ownership_refuses_before_any_provider_call(harness, stubs):
    client, _ = harness()
    stubs.generation_error = OperationRefusedError("this run no longer owns the work")

    assert client.post(PATH, json=commit_body(), headers=headers()).status_code == 404
    assert stubs.authorizations == 0
    assert StubProvider.instances == []


def test_the_provider_reauthorizes_through_the_route_closure(harness, stubs):
    """The closure the route hands the provider must re-run full authorization, not
    replay a cached decision. Two authorizations for one commit: the initial one and
    the provider's own."""
    client, _ = harness()

    client.post(PATH, json=commit_body(), headers=headers())

    assert stubs.authorizations == 2
    assert StubProvider.instances[0].reauthorizations == 1


def test_authority_withdrawn_mid_operation_surfaces_as_a_refusal(harness, stubs):
    client, _ = harness()
    calls = {"n": 0}
    original = gor.authorize_operation

    async def failing(session, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OperationRefusedError("authority withdrawn")
        return await original(session, **kwargs)

    gor.authorize_operation = failing
    try:
        response = client.post(PATH, json=commit_body(), headers=headers())
    finally:
        gor.authorize_operation = original

    assert response.status_code == 404


# --- Content that could perform a gated action -----------------------------


def test_workflow_content_is_refused_when_merge_is_gated(harness):
    """Merging a workflow definition gives repository automation an ability the
    human gate was holding. Branch naming is not authority for that."""
    client, _ = harness()

    response = client.post(
        PATH,
        json=commit_body(changes=[{"path": ".github/workflows/deploy.yml", "content_base64": base64.b64encode(b"on: push").decode()}]),
        headers=headers(),
    )

    assert response.status_code == 404
    assert StubProvider.instances[0].calls == []


def test_workflow_content_is_allowed_once_the_implied_actions_are_accepted(harness, stubs):
    client, _ = harness()
    stubs.policy = SimpleNamespace(permits=lambda action: True)

    response = client.post(
        PATH,
        json=commit_body(changes=[{"path": ".github/workflows/deploy.yml", "content_base64": base64.b64encode(b"on: push").decode()}]),
        headers=headers(),
    )

    assert response.status_code == 200


def test_a_traversing_path_is_refused(harness):
    client, _ = harness()

    response = client.post(
        PATH,
        json=commit_body(changes=[{"path": "../../etc/passwd", "content_base64": base64.b64encode(b"x").decode()}]),
        headers=headers(),
    )

    assert response.status_code == 404


def test_undecodable_content_is_refused_rather_than_published_as_garbage(harness):
    client, _ = harness()

    response = client.post(PATH, json=commit_body(changes=[{"path": "a.py", "content_base64": "not!base64"}]), headers=headers())

    assert response.status_code == 404
    assert StubProvider.instances[0].calls == []


def test_a_gitlink_mode_is_refused(harness):
    """A submodule pointer imports code nothing that reviewed the change has seen."""
    client, _ = harness()

    response = client.post(
        PATH,
        json=commit_body(changes=[{"path": "vendor/dep", "content_base64": base64.b64encode(b"x").decode(), "mode": "160000"}]),
        headers=headers(),
    )

    assert response.status_code == 404


def test_too_many_files_is_refused_at_parse_time(harness):
    client, _ = harness()
    changes = [{"path": f"f{index}.py", "content_base64": base64.b64encode(b"x").decode()} for index in range(501)]

    response = client.post(PATH, json=commit_body(changes=changes), headers=headers())

    assert response.status_code == 422
    assert StubProvider.instances == []


# --- Conflicts and provider trouble are distinguishable from refusals ------


def test_a_conflict_is_visible_so_the_worker_can_reconcile(harness, monkeypatch):
    """409, not 404: "the branch moved, reconcile and retry" is information the
    worker needs, and it is only reachable after full authorization."""

    class Conflicting(StubProvider):
        async def publish_commit(self, *, changes, message, expected_head, reauthorize):
            await reauthorize()
            from src.agentauth.github_provider import ProviderConflictError

            raise ProviderConflictError("the assigned branch moved")

    monkeypatch.setattr(gor, "GitHubProvider", Conflicting)
    client, _ = harness()

    response = client.post(PATH, json=commit_body(), headers=headers())

    assert response.status_code == 409


def test_a_timeout_reconciles_before_it_is_reported(harness, monkeypatch):
    """A timeout is not evidence that nothing happened, so the already-landed commit
    is returned rather than a failure the worker would retry into a duplicate."""

    class TimingOut(StubProvider):
        async def publish_commit(self, *, changes, message, expected_head, reauthorize):
            await reauthorize()
            from src.agentauth.github_provider import ProviderUnavailableError

            # The provider attaches the objects it had already built, which is what
            # lets reconciliation identify OUR commit rather than a similar one.
            raise ProviderUnavailableError("provider timed out", prepared_parent=HEAD, prepared_tree="t" * 40)

    seen = {}

    async def reconcile(provider, *, message, expected_parent=None, expected_tree=None):
        seen["parent"] = expected_parent
        seen["tree"] = expected_tree
        return PublishedCommit(sha=NEW_COMMIT, branch=BRANCH, parent_sha=HEAD)

    monkeypatch.setattr(gor, "GitHubProvider", TimingOut)
    monkeypatch.setattr(gor, "reconcile_commit", reconcile)
    client, _ = harness()

    response = client.post(PATH, json=commit_body(), headers=headers())

    assert response.status_code == 200
    assert response.json()["commit_sha"] == NEW_COMMIT
    # The route must forward the prepared identity, not just the message.
    assert seen == {"parent": HEAD, "tree": "t" * 40}


def test_a_timeout_that_reconciles_to_nothing_is_reported_as_unavailable(harness, monkeypatch):
    class TimingOut(StubProvider):
        async def publish_commit(self, *, changes, message, expected_head, reauthorize):
            await reauthorize()
            from src.agentauth.github_provider import ProviderUnavailableError

            raise ProviderUnavailableError("provider timed out")

    async def reconcile(provider, *, message, expected_parent=None, expected_tree=None):
        return None

    monkeypatch.setattr(gor, "GitHubProvider", TimingOut)
    monkeypatch.setattr(gor, "reconcile_commit", reconcile)
    client, _ = harness()

    assert client.post(PATH, json=commit_body(), headers=headers()).status_code == 503


def test_a_merge_without_a_reviewed_head_is_refused(harness):
    """Merging without naming the head it was reviewed at would merge whatever the
    branch happens to point at now."""
    client, _ = harness()

    response = client.post(PATH, json={"operation": "merge_pull_request", "pull_number": 7}, headers=headers())

    assert response.status_code == 404
    assert StubProvider.instances[0].calls == []


def test_a_review_without_a_pull_request_is_refused(harness):
    client, _ = harness()

    response = client.post(PATH, json={"operation": "publish_review", "body": "notes"}, headers=headers())

    assert response.status_code == 404


def test_the_router_is_registered_in_the_app(harness):
    """Registration in `UNIT_MODULES` is what makes the route reachable at all; the
    harness above mounts it directly and would pass on an unregistered router."""
    from src.app import UNIT_MODULES

    assert "src.agentauth.github_operation_routes" in UNIT_MODULES


def test_the_idempotency_key_is_bound_to_the_request_and_changes_with_it(harness):
    client, _ = harness()

    first = client.post(PATH, json=commit_body(), headers=headers()).json()["idempotency_key"]
    same = client.post(PATH, json=commit_body(), headers=headers()).json()["idempotency_key"]
    other = client.post(PATH, json=commit_body(message="a different change"), headers=headers()).json()["idempotency_key"]

    assert first == same
    assert first != other


def test_grant_revocation_between_provider_writes(harness, stubs, monkeypatch):
    live = SimpleNamespace(revoked=False, authentication_reads=0, published=False)

    class Runtime(StubRuntime):
        def authenticate(self, *args):
            live.authentication_reads += 1
            if live.revoked:
                raise BootstrapRefusedError("grant revoked in authority store")
            return super().authenticate(*args)

    class Provider(StubProvider):
        async def publish_commit(self, *, changes, message, expected_head, reauthorize):
            # Model revocation after an upload and before the visible write.
            live.revoked = True
            await reauthorize()
            live.published = True
            return PublishedCommit(NEW_COMMIT, self.assignment.branch, HEAD)

    client, _ = harness()
    runtime = Runtime(StubWorkloads())
    client.app.dependency_overrides[gor.get_agent_runtime] = lambda: runtime
    monkeypatch.setattr(gor, "GitHubProvider", Provider)
    response = client.post(PATH, json=commit_body(), headers=headers())
    assert (response.status_code, live.published) == (404, False), (response.status_code, live.published, live.authentication_reads)


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
def test_real_worker_review_payload_is_accepted_by_the_route(harness, monkeypatch, event):
    helper_path = Path(__file__).resolve().parents[3] / "agent-factory/agent-worker-image/lib/mediated_github.py"
    spec = importlib.util.spec_from_file_location("worker_mediated_route_contract", helper_path)
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, helper)
    spec.loader.exec_module(helper)
    client, _ = harness()

    def transport(payload):
        response = client.post(PATH, json=payload, headers=headers())
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(helper, "_request", transport)
    review = helper.publish_review(pull_number=7, body="review notes", event=event)
    assert review["id"] == 11
    assert StubProvider.instances[0].calls == [f"review:7:{event}"]
