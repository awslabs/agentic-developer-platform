"""The real handoff route, with the real store, mocking only the authority boundary.

Issue #5144. These tests are the worker↔gateway *contract*: the last test drives the
actual worker client against the actual route's response, because a client and a route
that were each tested against a mock of the other is how a contract breaks in
production while both suites stay green.

The properties pinned here, and why each matters:

1. **Both proofs, always.** A run credential alone, a workload token alone, or the
   shared platform identity plus a self-declared run id are each insufficient. Every
   hosted worker assumes the same platform role, so without the second proof the
   authorization reduces to "whatever the caller typed".

2. **The worker names no work and selects no flow.** `extra="forbid"` makes any
   identifying field a 422 rather than a silently ignored one, so the refusal is loud
   at exactly the point where a silent ignore would look like it worked.

3. **Idempotency over the wire.** A repeat returns the same receipt with no second row
   state, and a refusal writes nothing at all — asserted on the database, not on the
   response.

4. **Refusals do not leak.** Authorization failures collapse to 404 so a caller cannot
   learn whether a run exists or how many attempts it has had. Handoff refusals are
   200-with-outcome, deliberately, because the worker must be able to distinguish
   "another attempt owns this" from "your credential is bad" in order to report
   accurately.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.registration_routes import (
    RegistrationRuntime,
    get_registration_runtime,
    router,
)
from src.agentauth.routes import require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.workload import WorkloadRefusedError
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity, ExecutionStatus, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_claims import OwnerKind
from src.shared.models.organization import Organization

REPO = "aws-e/adp"
ORG = "handoff-tenant"
INSTALLATION = 4242
ISSUE = 5144
REPO_ID = 987654321
PLAN_VERSION = 4
CLAIM_ID = "claim-5144-route"
URL = "/internal/v1/agent/self/handoff"
HEADERS = {"X-Adp-Run-Credential": "verified-run-credential", "X-Adp-Workload-Token": "verified-pod-token"}


async def _story(session, issue: int, *, claim_id: str = CLAIM_ID, generation: int = 1):
    """A dispatched story with the accepted plan, held claim and execution row."""
    flow = OrchestrationFlow(org_id=ORG, slug=f"flow-{issue}", title="Delivery", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        org_id=ORG,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref=f"story-{issue}",
        kind="story",
        state="running",
        title="Implement",
        issue_ref=str(issue),
        attempts=1,
    )
    session.add(node)
    await session.flush()
    run = attempt_run_id(node.id, 1)
    session.add(
        OrchestrationDecision(
            org_id=ORG,
            flow_id=flow.id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="engine",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps(
                {
                    "run_id": run,
                    "attempt": 1,
                    "repo": REPO,
                    "issue": issue,
                    "pr_binding_required": True,
                    "handoff_required": True,
                }
            ),
        )
    )
    session.add(OrchestrationAcceptedPlan(org_id=ORG, flow_id=flow.id, version=PLAN_VERSION, plan_document={}, plan_hash=f"plan-{issue}"))
    session.add(
        OrchestrationWorkClaim(
            id=claim_id,
            org_id=ORG,
            provider_repository_id=REPO_ID,
            issue_number=issue,
            owner_kind=OwnerKind.ENGINE_FLOW.value,
            owner_ref=flow.id,
            state=ClaimState.HELD.value,
            generation=generation,
        )
    )
    await session.flush()
    outcome = await create_execution(
        session,
        identity=ExecutionIdentity(
            org_id=ORG,
            node_id=node.id,
            cycle=1,
            accepted_plan_version=PLAN_VERSION,
            claim_id=claim_id,
            claim_generation=generation,
        ),
        flow_id=flow.id,
    )
    assert outcome.kind is OutcomeKind.APPLIED
    await session.commit()
    return node, run


def _authenticate(runtime, node, run):
    caller = SimpleNamespace(invocation_id=run, tenant_id=ORG, principal=f"{run}#1", persona="developer")
    record = SimpleNamespace(invocation_id=run, tenant_id=ORG, repo=REPO, flow_id=node.flow_id)
    grant = SimpleNamespace(authority=SimpleNamespace(kind="gate_decision"), repo_scope={REPO}, flow_id=node.flow_id)
    runtime.authenticate.return_value = (SimpleNamespace(uid="verified-pod"), caller, record, grant)


@pytest.fixture
async def handoff(db_session_factory, monkeypatch):
    async with db_session_factory() as session:
        session.add(Organization(id=ORG, name=ORG, github_installation_ids=[str(INSTALLATION)]))
        node, run = await _story(session, ISSUE)
    agent_runtime = MagicMock()
    agent_runtime.validate_flow = AsyncMock()
    _authenticate(agent_runtime, node, run)
    # The real RegistrationRuntime wrapping a mocked AgentRuntime, so the route is
    # exercised through the transport it actually ships on — a mocked
    # RegistrationRuntime would verify nothing about the wiring being changed here.
    runtime = RegistrationRuntime(service=MagicMock(), runtime=agent_runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_registration_runtime] = lambda: runtime
    app.dependency_overrides[require_agent_transport] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        # `runtime` is the AgentRuntime mock: it owns `authenticate`/`validate_flow`,
        # which is what the credential and workload assertions below inspect.
        yield SimpleNamespace(client=client, runtime=agent_runtime, sessions=db_session_factory, node=node, run=run)


async def _rows(handoff) -> list[OrchestrationExecution]:
    async with handoff.sessions() as session:
        return list((await session.scalars(select(OrchestrationExecution))).all())


async def _row(handoff) -> OrchestrationExecution:
    rows = await _rows(handoff)
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# The receipt and the continuation, over the wire
# ---------------------------------------------------------------------------


async def test_handoff_commits_a_receipt_and_a_due_continuation(handoff):
    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["outcome"] == "committed"
    assert body["receipt_ref"].startswith("handoff:")
    row = await _row(handoff)
    assert row.handoff_receipt_ref == body["receipt_ref"]
    # Non-terminal and still due: a handoff is not lane completion.
    assert row.next_check_at is not None
    assert ExecutionStatus(row.status) is ExecutionStatus.AWAITING_EXTERNAL
    handoff.runtime.authenticate.assert_called_with(HEADERS["X-Adp-Run-Credential"], HEADERS["X-Adp-Workload-Token"])
    handoff.runtime.validate_flow.assert_awaited()


async def test_repeat_returns_the_same_receipt_and_does_not_advance_the_row(handoff):
    """A retry, or a lost response, converges. Not a second receipt, not a counter."""
    first = await handoff.client.post(URL, headers=HEADERS, json={})
    revision_after_first = (await _row(handoff)).revision

    repeat = await handoff.client.post(URL, headers=HEADERS, json={})

    assert first.status_code == 201
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["outcome"] == "already_committed"
    assert repeat.json()["accepted"] is True
    assert repeat.json()["receipt_ref"] == first.json()["receipt_ref"]
    assert (await _row(handoff)).revision == revision_after_first


async def test_many_concurrent_style_repeats_never_mint_a_second_receipt(handoff):
    """Sequential here (SQLite has no real locking); the invariant is still asserted.

    The genuinely-simultaneous case needs PostgreSQL and lives in
    `tests/orchestration/test_handoff_postgres.py`. This one catches an implementation
    that made the repeat path conditional on something per-request.
    """
    receipts = set()
    for _ in range(5):
        response = await handoff.client.post(URL, headers=HEADERS, json={})
        assert response.json()["accepted"] is True
        receipts.add(response.json()["receipt_ref"])

    assert len(receipts) == 1
    assert len(await _rows(handoff)) == 1


async def test_summary_is_accepted_but_grants_no_authority(handoff):
    """Diagnostics only: it cannot change what is committed."""
    plain = await handoff.client.post(URL, headers=HEADERS, json={})
    async with handoff.sessions() as session:
        await session.execute(select(OrchestrationExecution))
    with_summary = await handoff.client.post(URL, headers=HEADERS, json={"summary": "opened PR #5293"})

    assert with_summary.json()["receipt_ref"] == plain.json()["receipt_ref"]


# ---------------------------------------------------------------------------
# The body names no work, and there is no field for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "org_id",
        "tenant_id",
        "node_id",
        "cycle",
        "accepted_plan_version",
        "claim_id",
        "claim_generation",
        "execution_id",
        "action_id",
        "run_id",
        "status",
        "phase",
        "action",
        "outcome",
        "receipt_ref",
    ],
)
async def test_any_identifying_or_authority_field_is_refused(handoff, field):
    """422, not silently ignored.

    A silently-ignored field looks to the sender like it worked, which is how a caller
    comes to believe it selected its own flow. The loudest possible refusal is correct
    for an attempt to.
    """
    response = await handoff.client.post(URL, headers=HEADERS, json={field: "anything"})

    assert response.status_code == 422
    assert (await _row(handoff)).handoff_receipt_ref is None


async def test_oversized_summary_is_refused_without_writing(handoff):
    response = await handoff.client.post(URL, headers=HEADERS, json={"summary": "x" * 9000})

    assert response.status_code == 422
    assert (await _row(handoff)).handoff_receipt_ref is None


# ---------------------------------------------------------------------------
# Both proofs are required, and refusals leave nothing written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [CredentialError("invalid"), WorkloadRefusedError("wrong pod"), ExecutionStateError("superseded")],
)
async def test_invalid_run_or_workload_proof_commits_nothing(handoff, error):
    handoff.runtime.authenticate.side_effect = error

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 404
    # The durable outcome: no receipt, and the work is still due.
    row = await _row(handoff)
    assert row.handoff_receipt_ref is None
    assert row.next_check_at is not None


async def test_shared_worker_identity_and_run_id_are_not_sufficient(handoff):
    """The shared platform role proves only "a platform worker", never which run."""
    response = await handoff.client.post(URL, headers={"X-Caller-Identity": "shared-worker", "X-Agent-RunId": "forged"}, json={})

    assert response.status_code == 404
    handoff.runtime.authenticate.assert_not_called()
    assert (await _row(handoff)).handoff_receipt_ref is None


async def test_missing_workload_token_still_reaches_the_verifier(handoff):
    """Absent is passed through as empty, so TokenReview refuses it — not this route.

    Short-circuiting here would put a second, weaker admission decision in front of the
    verifier, which is how the two drift apart.
    """
    handoff.runtime.authenticate.side_effect = WorkloadRefusedError("no token")

    response = await handoff.client.post(URL, headers={"X-Adp-Run-Credential": "c"}, json={})

    assert response.status_code == 404
    handoff.runtime.authenticate.assert_called_with("c", "")


async def test_revoked_flow_authority_cannot_commit(handoff):
    handoff.runtime.validate_flow.side_effect = BootstrapRefusedError("flow halted")

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 404
    assert (await _row(handoff)).handoff_receipt_ref is None


async def test_tenant_mismatch_between_credential_and_work_is_refused(handoff):
    """The credential's tenant must agree with the resolved node's tenant."""
    caller = SimpleNamespace(invocation_id=handoff.run, tenant_id="other-tenant", principal="p", persona="developer")
    _, _, record, grant = handoff.runtime.authenticate.return_value
    handoff.runtime.authenticate.return_value = (SimpleNamespace(uid="pod"), caller, record, grant)

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 404
    assert (await _row(handoff)).handoff_receipt_ref is None


async def test_flow_disagreement_between_grant_and_target_is_refused(handoff):
    """A grant for another flow cannot commit against this one."""
    pod, caller, record, grant = handoff.runtime.authenticate.return_value
    handoff.runtime.authenticate.return_value = (
        pod,
        caller,
        record,
        SimpleNamespace(authority=grant.authority, repo_scope=grant.repo_scope, flow_id="some-other-flow"),
    )

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 404
    assert (await _row(handoff)).handoff_receipt_ref is None


async def test_unknown_run_is_indistinguishable_from_an_unauthorized_one(handoff):
    """Uniform 404: a distinguishable answer would let a caller enumerate runs."""
    caller = SimpleNamespace(invocation_id="orch:node-nonexistent#9", tenant_id=ORG, principal="p", persona="developer")
    _, _, record, grant = handoff.runtime.authenticate.return_value
    handoff.runtime.authenticate.return_value = (SimpleNamespace(uid="pod"), caller, record, grant)

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 404
    assert response.json()["detail"] == "not found"


# ---------------------------------------------------------------------------
# A handoff refusal is answered, not hidden
# ---------------------------------------------------------------------------


async def test_superseded_generation_is_answered_200_with_a_refusal(handoff):
    """The worker must be able to tell "another attempt owns this" from "bad credential".

    Collapsing this to 404 would make an accurate report impossible: the worker could
    not distinguish a lost lane from a broken credential, and would have to guess.
    """
    await handoff.client.post(URL, headers=HEADERS, json={})
    async with handoff.sessions() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM_ID)
        claim.generation = 2
        await session.commit()

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert body["receipt_ref"] is None
    assert body["outcome"] in {"superseded", "stale", "refused"}


async def test_a_refusal_carries_no_receipt_the_worker_could_mistake_for_one(handoff):
    """`receipt_ref` is None on refusal, so a reason string cannot be read as a receipt."""
    async with handoff.sessions() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM_ID)
        claim.generation = 7
        await session.commit()

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.json()["receipt_ref"] is None
    assert response.json()["accepted"] is False


async def test_superseded_attempt_cannot_commit_after_a_retry_advances_the_node(handoff):
    """The reviewer's reproducer: an old caller must not get 201/accepted after a retry.

    The node is re-dispatched (``attempts`` advances) after the worker authenticated
    but before it reports. Resolving the *newest* execution here is what made this
    caller succeed: it received a 201 and a receipt for a cycle it was never
    dispatched to, so a superseded run's clean exit read as delivery of live work.

    The receipt must also not exist for the new cycle afterwards — otherwise the
    current attempt would find a receipt it never committed and skip its own handoff.
    """
    async with handoff.sessions() as session:
        node = await session.get(OrchestrationNode, handoff.node.id)
        node.attempts = node.attempts + 1
        await session.commit()

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    # 404, not 200-with-refusal: a run that is not the current attempt is in the
    # authorization class, indistinguishable from an unknown run.
    assert response.status_code == 404, response.text
    rows = await _rows(handoff)
    assert [row.handoff_receipt_ref for row in rows] == [None]


async def test_a_receipt_is_never_minted_for_a_cycle_the_caller_was_not_dispatched_to(handoff):
    """Binding is to the caller's own cycle, not to whichever execution is newest.

    The discriminating case, and the reason it is written this way: the node's
    ``attempts`` is left at 1, so this caller IS the current attempt and
    ``resolve_registration_target`` admits it. Only the *cycle* resolution is under
    test. Resolving the newest execution row instead gives this legitimate caller
    cycle 2's fences, so it commits a receipt for work it was never dispatched to
    while its own cycle stays unhanded-off — the failure is a wrong receipt, not a
    refusal, which is why a test asserting only 404 cannot see it.
    """
    async with handoff.sessions() as session:
        # A newer cycle's execution, as a repair cycle's dispatch would create.
        outcome = await create_execution(
            session,
            identity=ExecutionIdentity(
                org_id=ORG,
                node_id=handoff.node.id,
                cycle=2,
                accepted_plan_version=PLAN_VERSION,
                claim_id=CLAIM_ID,
                claim_generation=1,
            ),
            flow_id=handoff.node.flow_id,
        )
        assert outcome.kind is OutcomeKind.APPLIED
        await session.commit()

    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["cycle"] == 1, f"caller was dispatched to cycle 1, got {body['cycle']}"
    assert "cycle=1" in body["receipt_ref"], body["receipt_ref"]
    async with handoff.sessions() as session:
        rows = list((await session.scalars(select(OrchestrationExecution).order_by(OrchestrationExecution.cycle))).all())
    assert [row.cycle for row in rows] == [1, 2]
    # The caller's own cycle is handed off; the cycle it was never dispatched to is
    # untouched, so the current attempt cannot find a receipt it did not commit.
    assert rows[0].handoff_receipt_ref is not None
    assert rows[1].handoff_receipt_ref is None


async def test_the_current_attempt_still_commits_and_the_receipt_names_its_cycle(handoff):
    """The fence refuses stale callers without blocking the legitimate one."""
    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted"] is True
    # The echoed fences come from protected state, so the worker can confirm the
    # receipt covers the work it actually did.
    assert body["node_id"] == handoff.node.id
    assert body["cycle"] == 1
    assert f"cycle={body['cycle']}" in body["receipt_ref"]


async def test_response_is_not_cacheable(handoff):
    """A cached handoff answer would be a receipt for the wrong attempt."""
    response = await handoff.client.post(URL, headers=HEADERS, json={})

    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# The contract: the real worker client against the real route
# ---------------------------------------------------------------------------


async def test_real_worker_client_accepts_the_real_route_response(handoff, monkeypatch):
    """The actual worker parser, driven by the actual route's bytes and status.

    This is the test that a mock on either side cannot replace. It also asserts both
    proofs are on the wire, which is the property the whole authorization rests on.
    """
    response = await handoff.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 201

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory" / "agent-worker-image"))
    from botocore.credentials import Credentials
    from lib import handoff_client, status_gateway_client

    monkeypatch.setenv("ADP_HANDOFF_REQUIRED", "true")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
    monkeypatch.setattr(status_gateway_client, "read_workload_token", lambda: "workload-token")
    monkeypatch.setattr(status_gateway_client, "_read_credential", lambda: "adpr1.run.signature")
    monkeypatch.setattr(status_gateway_client.botocore.session, "get_session", MagicMock())
    monkeypatch.setattr(
        "adp_trigger.transport_identity.worker_credentials",
        lambda _: Credentials("platform-key", "secret", "token"),
    )
    monkeypatch.setattr("adp_trigger.transport_identity.gateway_signing_region", lambda _: "us-east-1")
    wire = MagicMock(status_code=response.status_code)
    wire.__enter__.return_value = wire
    wire.raw.read.side_effect = lambda *_a, **_k: BytesIO(response.content).read()
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = wire
    monkeypatch.setattr(status_gateway_client.requests, "Session", lambda: http)

    note = handoff_client.handoff_note(summary="opened PR #5293")

    assert "Delivery handoff recorded" in note
    assert response.json()["receipt_ref"] in note
    assert "not recorded" not in note
    headers = http.post.call_args.kwargs["headers"]
    assert headers["X-Adp-Run-Credential"] == "adpr1.run.signature"
    assert headers["X-Adp-Workload-Token"] == "workload-token"
    assert "Credential=platform-key/" in headers["Authorization"]


async def test_real_worker_client_reports_a_route_refusal_as_not_accepted(handoff, monkeypatch):
    """The other half of the contract: a refusal must not read as a handoff.

    Without this, a client that treated any 200 as success would pass the test above
    and still mis-report every superseded lane in production.
    """
    async with handoff.sessions() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM_ID)
        claim.generation = 3
        await session.commit()
    response = await handoff.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 200
    assert response.json()["accepted"] is False

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory" / "agent-worker-image"))
    from botocore.credentials import Credentials
    from lib import handoff_client, status_gateway_client

    monkeypatch.setenv("ADP_HANDOFF_REQUIRED", "true")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
    monkeypatch.setattr(status_gateway_client, "read_workload_token", lambda: "workload-token")
    monkeypatch.setattr(status_gateway_client, "_read_credential", lambda: "adpr1.run.signature")
    monkeypatch.setattr(status_gateway_client.botocore.session, "get_session", MagicMock())
    monkeypatch.setattr(
        "adp_trigger.transport_identity.worker_credentials",
        lambda _: Credentials("platform-key", "secret", "token"),
    )
    monkeypatch.setattr("adp_trigger.transport_identity.gateway_signing_region", lambda _: "us-east-1")
    wire = MagicMock(status_code=response.status_code)
    wire.__enter__.return_value = wire
    wire.raw.read.side_effect = lambda *_a, **_k: BytesIO(response.content).read()
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = wire
    monkeypatch.setattr(status_gateway_client.requests, "Session", lambda: http)

    note = handoff_client.handoff_note(summary="opened PR #5293")

    assert "not accepted" in note
    assert "Delivery handoff recorded" not in note


async def test_an_older_gateway_without_this_route_is_reported_not_recorded(handoff, monkeypatch):
    """Staged deployment, the unsafe direction: new worker against an older server.

    The worker must report the handoff as NOT recorded rather than assuming it worked —
    that is what keeps the engine holding the work instead of a 404 reading as success.
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory" / "agent-worker-image"))
    from botocore.credentials import Credentials
    from lib import handoff_client, status_gateway_client

    monkeypatch.setenv("ADP_HANDOFF_REQUIRED", "true")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
    monkeypatch.setattr(status_gateway_client, "read_workload_token", lambda: "workload-token")
    monkeypatch.setattr(status_gateway_client, "_read_credential", lambda: "adpr1.run.signature")
    monkeypatch.setattr(status_gateway_client.botocore.session, "get_session", MagicMock())
    monkeypatch.setattr(
        "adp_trigger.transport_identity.worker_credentials",
        lambda _: Credentials("platform-key", "secret", "token"),
    )
    monkeypatch.setattr("adp_trigger.transport_identity.gateway_signing_region", lambda _: "us-east-1")
    wire = MagicMock(status_code=404)
    wire.__enter__.return_value = wire
    wire.raw.read.side_effect = lambda *_a, **_k: BytesIO(b'{"detail":"not found"}').read()
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = wire
    monkeypatch.setattr(status_gateway_client.requests, "Session", lambda: http)

    note = handoff_client.handoff_note(summary="opened PR #5293")

    assert "not recorded" in note
    assert "Delivery handoff recorded" not in note
