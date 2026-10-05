"""Tests for the execution read model: `GET /orchestration/flows/{flow_id}/execution`.

Issue #5145 (ENGINE-K4, parent #5122). The endpoint exists so an operator can see
*why* delivery is waiting and who acts next, so every assertion here is about a way
that answer could be silently wrong rather than about field plumbing:

  - **Tenant isolation is a 404**, not a 403 and not an empty list. A 403 confirms
    the id exists somewhere and lets a caller enumerate flows by status code; an
    empty list reads as "no delivery work", which is a different and more
    misleading answer than "not found". The empty-list case is asserted separately
    because it is the plausible bug: the ledger query is org-scoped on its own, so
    an implementation that skipped the flow resolution would return 200 with no
    executions for another tenant's flow.
  - **The permission gate runs before any read**, so a denied caller cannot learn
    whether the flow exists.
  - **Blocked renders as blocked**, carrying its typed code, owner, required input
    and outstanding gates — not as an error and not as progress.
  - **`progressed_at` survives blocking.** The store deliberately does not reset it
    when a row blocks, because it is the clock that separates "stuck for a minute"
    from "stuck since Tuesday". A read that recomputed or overwrote it would destroy
    the only evidence of how long something has been stuck.
  - **An unobserved action stays unknown**, and `resolved` is false for it. This is
    the assertion that stops a green worker status from hiding an outstanding gate.
  - **A legacy flow is 200 + `legacy=true`**, never a 404 and never an implied
    success — "no durable execution record" is not "delivered".
  - **References are sanitised**, and a hostile one is dropped rather than rendered.
  - **The read is bounded**: actions come from one grouped query, and a capped list
    reports `action_overflow` instead of presenting itself as complete.
  - **No claim binding is published.** `claim_id`/`claim_generation` must not appear
    in a browser payload: they are what the store's authority fence tests.

Session, app and client fixtures mirror `test_read_api.py`, including its two
pysqlite hooks, and gate on `USAGE_READ` for the same reason — this is a read, and
requiring approval authority to see where delivery stands would grant more than the
operation needs. The ledger rows are written directly rather than through
`execution_store`, because the store requires a work-claim identity that an
operator read deliberately does not hold; what is under test here is the read.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import Permission
from src.orchestration.execution_read import (
    MAX_ACTIONS_PER_EXECUTION,
    MAX_EXECUTIONS_PER_PAGE,
    _safe_ref,
    load_flow_execution_view,
)
from src.orchestration.execution_state import (
    ActionStatus,
    BlockCode,
    ExecutionPhase,
    ExecutionStatus,
)
from src.orchestration.models import (
    NodeKind,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import NodeState
from src.shared.models.base import Base
from src.shared.schemas.auth import TokenContext

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW_SLUG = "delivery-loop"
USER_ID = "cognito-sub-operator"

# A fixed instant so age-style assertions are reproducible.
NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

# The authority binding the store's fence tests. Seeded onto rows so its ABSENCE
# from the response can be asserted — publishing it would put the values that
# satisfy the next authority check into a browser payload.
CLAIM_ID = "claim-secret-0001"
CLAIM_GENERATION = 7


def route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/execution"


# ---------------------------------------------------------------------------
# Every phase and status projects truthfully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "status"),
    [
        (ExecutionPhase.ADMITTED, ExecutionStatus.RUNNABLE),
        (ExecutionPhase.PREPARING, ExecutionStatus.RUNNABLE),
        (ExecutionPhase.DELIVERING, ExecutionStatus.RUNNABLE),
        (ExecutionPhase.SUBMITTING, ExecutionStatus.AWAITING_EXTERNAL),
        (ExecutionPhase.AWAITING_REVIEW, ExecutionStatus.AWAITING_EXTERNAL),
        (ExecutionPhase.REPAIRING, ExecutionStatus.BLOCKED),
        (ExecutionPhase.SETTLING, ExecutionStatus.BLOCKED),
        (ExecutionPhase.CONCLUDED, ExecutionStatus.CONCLUDED),
        (ExecutionPhase.CONCLUDED, ExecutionStatus.SUPERSEDED),
    ],
)
async def test_every_phase_and_status_is_rendered_as_itself(session, app_with_router, phase, status):
    """Each phase/status pair survives the projection unchanged.

    Parametrized over the whole vocabulary rather than spot-checked because the
    failure this catches is a mapping table with a hole in it — a phase that falls
    through to a default reads as a *different* stage of delivery, which is exactly
    the misreport this endpoint exists to remove.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    blocked = status is ExecutionStatus.BLOCKED
    await seed_execution(
        session,
        flow,
        node,
        phase=phase,
        status=status,
        # A terminal row carries no next check by construction; a live one must.
        next_check_at=None if status in (ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED) else NOW,
        block_code=BlockCode.HUMAN_GATE_REQUIRED.value if blocked else None,
        block_owner="platform-operator" if blocked else None,
        block_required_input="approve the wave gate" if blocked else None,
    )

    response = client_for(app_with_router).get(route(flow.id))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["legacy"] is False
    assert len(body["executions"]) == 1
    execution = body["executions"][0]
    assert execution["phase"] == phase.value
    assert execution["status"] == status.value
    # A blocked row reports a block; a non-blocked row reports none. The pairing is
    # asserted here because a view that showed a stale block on a row that had moved
    # on would send an operator to resolve something already resolved.
    assert (execution["block"] is not None) is blocked


@pytest.mark.asyncio
async def test_blocked_execution_reports_owner_input_and_gates(session, app_with_router):
    """A block is routable from the response alone.

    The point of the typed block: an operator reading only this payload must know
    who acts and what they supply. A bare "blocked" flag would send them to logs
    that expire, which is the failure the ledger's block columns exist to remove.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a", state=NodeState.RUNNING.value)
    await seed_execution(
        session,
        flow,
        node,
        phase=ExecutionPhase.SETTLING,
        status=ExecutionStatus.BLOCKED,
        block_code=BlockCode.HUMAN_GATE_REQUIRED.value,
        block_owner="platform-operator",
        block_required_input="approve the wave gate for story-a",
        block_remaining_gates='["gate:security-review", "gate:cost-approval"]',
        block_detail="gate opened by the engine; awaiting a human decision",
    )

    body = client_for(app_with_router).get(route(flow.id)).json()
    block = body["executions"][0]["block"]

    assert block["code"] == "human_gate_required"
    assert block["owner"] == "platform-operator"
    assert block["required_input"] == "approve the wave gate for story-a"
    # Both gates, in order, decoded from the stored JSON text.
    assert block["remaining_gates"] == ["gate:security-review", "gate:cost-approval"]
    assert block["detail"] == "gate opened by the engine; awaiting a human decision"
    # Blocked is not failed: the status is its own value, and the response carries no
    # error framing for it.
    assert body["executions"][0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_blocked_row_keeps_its_last_progress_time(session, app_with_router):
    """`progressed_at` is the last REAL progress, not the moment of blocking.

    The store deliberately does not touch `progressed_at` when a row blocks. A read
    that stamped it "now", or derived it from `updated_at`, would reset the clock an
    operator uses to tell "stuck for a minute" from "stuck since Tuesday" — and the
    reset happens on exactly the rows where the answer matters most.
    """
    stuck_since = NOW - timedelta(hours=3)
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(
        session,
        flow,
        node,
        phase=ExecutionPhase.SETTLING,
        status=ExecutionStatus.BLOCKED,
        progressed_at=stuck_since,
        # Written well after the last progress, as a later blocking write would.
        updated_at=NOW,
        block_code=BlockCode.CREDENTIAL_UNAVAILABLE.value,
        block_owner="requesting-user",
        block_required_input="connect an AWS account in settings",
    )

    execution = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]

    assert execution["progressed_at"] == stuck_since.isoformat()
    # Carried on the block too, so an operator reading only the block can date it.
    assert execution["block"]["progressed_at"] == stuck_since.isoformat()
    # And it is NOT the later write time.
    assert execution["progressed_at"] != NOW.isoformat()


@pytest.mark.asyncio
async def test_server_time_is_explicit_so_age_is_not_computed_from_a_client_clock(session, app_with_router):
    """`server_time` is stamped on the response.

    Without it a client computes "blocked for three hours" against its own clock,
    which may be wrong or in another zone — so the rendered age would be wrong on
    exactly the field an operator acts on. With it, both instants come from one
    source.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(session, flow, node)

    body = client_for(app_with_router).get(route(flow.id)).json()

    stamped = datetime.fromisoformat(body["server_time"])
    # Timezone-aware, so a client cannot parse it into a local-time guess.
    assert stamped.tzinfo is not None
    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


@pytest.mark.asyncio
async def test_next_check_time_is_served_for_live_work_and_absent_when_terminal(session, app_with_router):
    """The "next check" answers "is anything going to happen, and when?".

    A live row must carry it — the store refuses to write a non-terminal row without
    one, because such a row is invisible to pickup. A terminal row must NOT, because
    a wake-up time on a concluded execution would imply work still to come.
    """
    due = NOW + timedelta(minutes=15)
    flow = await seed_flow(session)
    live_node = await seed_node(session, flow, node_ref="story-live")
    done_node = await seed_node(session, flow, node_ref="story-done")
    await seed_execution(
        session,
        flow,
        live_node,
        phase=ExecutionPhase.AWAITING_REVIEW,
        status=ExecutionStatus.AWAITING_EXTERNAL,
        next_check_at=due,
        pending_action_key="pr:story-live:cycle-1",
    )
    await seed_execution(
        session,
        flow,
        done_node,
        phase=ExecutionPhase.CONCLUDED,
        status=ExecutionStatus.CONCLUDED,
        next_check_at=None,
    )

    executions = {e["node_id"]: e for e in client_for(app_with_router).get(route(flow.id)).json()["executions"]}

    assert executions[live_node.id]["next_check_at"] == due.isoformat()
    # What a recovering process must go and ask about — the field that makes
    # "awaiting external" actionable rather than merely opaque.
    assert executions[live_node.id]["pending_action_key"] == "pr:story-live:cycle-1"
    assert executions[done_node.id]["next_check_at"] is None


# ---------------------------------------------------------------------------
# Evidence: unknown stays unknown, missing stays pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "resolved"),
    [
        (ActionStatus.PREPARED, False),
        (ActionStatus.DISPATCHED, False),
        (ActionStatus.SUCCEEDED, True),
        (ActionStatus.FAILED, True),
        # The one that matters: an observer looked and could not tell. Settled as a
        # record, unresolved as evidence.
        (ActionStatus.UNKNOWN, False),
    ],
)
async def test_action_resolution_never_upgrades_an_unobserved_outcome(session, app_with_router, status, resolved):
    """`resolved` is served, and `unknown` is not resolved.

    Served rather than left to the client because the client-side derivation has a
    trap: testing `status != "prepared"` treats an outcome nobody observed as
    resolved evidence, which is how a green worker status ends up hiding an
    outstanding gate.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    await seed_action(session, execution, operation_key="pr:story-a:cycle-1", status=status)

    action = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]["actions"][0]

    assert action["status"] == status.value
    assert action["resolved"] is resolved


@pytest.mark.asyncio
async def test_missing_receipt_is_null_rather_than_implied_success(session, app_with_router):
    """An action with no receipt reference reports null, so the client says "pending".

    The failure this prevents: a view that renders nothing for a missing receipt
    looks identical to one that has nothing to report, so "the deployment receipt
    never arrived" becomes indistinguishable from "there was no deployment".
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    await seed_action(
        session,
        execution,
        operation_key="deploy:story-a:cycle-1",
        kind="deployment",
        status=ActionStatus.DISPATCHED,
        receipt_ref=None,
    )

    action = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]["actions"][0]

    assert action["receipt_ref"] is None
    assert action["observed_at"] is None
    assert action["resolved"] is False


@pytest.mark.asyncio
async def test_receipt_kinds_are_rendered_generically_for_all_four_evidence_types(session, app_with_router):
    """Review, merge, deployment and evaluation receipts use ONE shape.

    This child owns the common evidence presentation for its sibling acceptance
    parents, so the contract is asserted to be generic: later phase handlers
    populate `kind` and `receipt_ref` and light up this display rather than each
    needing a dashboard of its own. A per-kind special case here would be four
    divergent renderings later.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    kinds = {
        "review": "pr/PR_kwDOreview1",
        "merge": "pr/PR_kwDOmerge1",
        "deployment": "deploy/run-4417",
        "evaluation": "eval/report-88",
    }
    for index, (kind, receipt) in enumerate(kinds.items()):
        await seed_action(
            session,
            execution,
            operation_key=f"{kind}:story-a:cycle-1",
            kind=kind,
            status=ActionStatus.SUCCEEDED,
            receipt_ref=receipt,
            created_at=NOW + timedelta(seconds=index),
        )

    actions = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]["actions"]

    assert {action["kind"] for action in actions} == set(kinds)
    for action in actions:
        # Same field set for every kind — no kind-specific branch in the contract.
        assert action["receipt_ref"] == kinds[action["kind"]]
        assert set(action) == {
            "id",
            "operation_key",
            "kind",
            "status",
            "attempt",
            "resolved",
            "artifact_ref",
            "receipt_ref",
            "created_at",
            "observed_at",
        }


@pytest.mark.asyncio
async def test_worker_success_with_outstanding_gates_is_not_accepted_delivery(session, app_with_router):
    """A succeeded action plus a live block reads as still-blocked.

    The exact case the issue names: the worker finished, and review/merge/deploy are
    outstanding. If the response let the successful action imply completion, an
    operator would read delivery as done while a human gate is still waiting.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a", state=NodeState.AWAITING_MERGE.value)
    execution = await seed_execution(
        session,
        flow,
        node,
        phase=ExecutionPhase.AWAITING_REVIEW,
        status=ExecutionStatus.BLOCKED,
        block_code=BlockCode.HUMAN_GATE_REQUIRED.value,
        block_owner="platform-operator",
        block_required_input="approve the merge gate",
        block_remaining_gates='["gate:merge-approval"]',
    )
    # The worker's own step succeeded.
    await seed_action(
        session,
        execution,
        operation_key="pr:story-a:cycle-1",
        status=ActionStatus.SUCCEEDED,
        receipt_ref="pr/PR_kwDOABCD1234",
        observed_at=NOW,
    )

    body = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]

    assert body["actions"][0]["status"] == "succeeded"
    # ...and delivery is still blocked, with the gate named.
    assert body["status"] == "blocked"
    assert body["block"]["remaining_gates"] == ["gate:merge-approval"]
    # Nothing in the payload asserts conclusion.
    assert body["phase"] != ExecutionPhase.CONCLUDED.value


# ---------------------------------------------------------------------------
# Legacy flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_flow_with_no_ledger_rows_is_200_and_flagged_not_404(session, app_with_router):
    """No execution record is reported as exactly that — not 404, not success.

    Every flow delivered before this ledger existed has no rows, permanently. A 404
    would claim the flow does not exist (it does), and an unflagged empty list would
    let a client render "nothing outstanding", which is the absence-means-success
    error the issue calls out explicitly.
    """
    flow = await seed_flow(session)
    await seed_node(session, flow, node_ref="story-legacy", state=NodeState.PASSED.value)

    response = client_for(app_with_router).get(route(flow.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legacy"] is True
    assert body["executions"] == []
    assert body["total"] == 0
    assert body["flow_id"] == flow.id


@pytest.mark.asyncio
async def test_legacy_is_false_on_an_empty_page_of_a_populated_flow(session, app_with_router):
    """`legacy` keys on the flow's total, not on this page being empty.

    Page two of a one-page result is empty without the flow being legacy. Keying on
    the page would make a paging client announce "no durable execution record" about
    a flow that has one.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(session, flow, node)

    body = client_for(app_with_router).get(route(flow.id), params={"offset": 50}).json()

    assert body["executions"] == []
    assert body["total"] == 1
    assert body["legacy"] is False


# ---------------------------------------------------------------------------
# Tenant isolation, uniform denial, permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_tenants_flow_is_404_not_403_and_not_an_empty_list(session, app_with_router):
    """A cross-tenant flow id gets the router's uniform 404.

    Three wrong answers are excluded at once. A 403 confirms the id exists somewhere
    and lets a caller enumerate flows by status code. A 200 with an empty list — the
    plausible bug, since the ledger query is org-scoped on its own and would return
    nothing without the flow resolution — reads as "this flow has no delivery work".
    And a 200 with the rows would be the disclosure itself.
    """
    other_flow = await seed_flow(session, org_id=ORG_B, slug="other-tenant-loop")
    other_node = await seed_node(session, other_flow, node_ref="story-secret", org_id=ORG_B)
    await seed_execution(session, other_flow, other_node, org_id=ORG_B)

    # Caller authenticated in ORG_A asking for ORG_B's flow.
    response = client_for(app_with_router, org_id=ORG_A).get(route(other_flow.id))

    assert response.status_code == 404
    assert "story-secret" not in response.text
    assert other_node.id not in response.text


@pytest.mark.asyncio
async def test_unknown_flow_is_the_same_404_as_another_tenants(session, app_with_router):
    """Absent and forbidden are indistinguishable by status code and body shape.

    If they differed, the difference itself would be the enumeration oracle the 404
    exists to remove.
    """
    other_flow = await seed_flow(session, org_id=ORG_B, slug="other-tenant-loop")
    client = client_for(app_with_router, org_id=ORG_A)

    forbidden = client.get(route(other_flow.id))
    absent = client.get(route("00000000-0000-0000-0000-000000000000"))

    assert forbidden.status_code == absent.status_code == 404
    # Same detail template, differing only in the id the caller already supplied.
    assert forbidden.json()["detail"].replace(other_flow.id, "X") == absent.json()["detail"].replace("00000000-0000-0000-0000-000000000000", "X")


@pytest.mark.asyncio
async def test_a_tenants_own_flow_returns_only_its_own_executions(session, app_with_router):
    """The ledger read is org-scoped in its own predicate, not merely via the flow.

    Belt and braces on purpose: a filter that depends on an earlier query for its
    safety is one refactor away from being wrong, and the wrong direction here is a
    cross-tenant read.
    """
    mine = await seed_flow(session)
    my_node = await seed_node(session, mine, node_ref="story-mine")
    await seed_execution(session, mine, my_node, progress_note="mine")

    theirs = await seed_flow(session, org_id=ORG_B, slug="their-loop")
    their_node = await seed_node(session, theirs, node_ref="story-theirs", org_id=ORG_B)
    await seed_execution(session, theirs, their_node, org_id=ORG_B, progress_note="theirs")

    body = client_for(app_with_router, org_id=ORG_A).get(route(mine.id)).json()

    assert body["total"] == 1
    assert [e["node_id"] for e in body["executions"]] == [my_node.id]
    assert "theirs" not in response_text(body)


@pytest.mark.asyncio
async def test_action_read_carries_its_own_tenant_predicate(session, app_with_router):
    """The action query is org-scoped independently of the execution ids it filters on.

    The execution-id filter alone looks sufficient — ids are opaque, so an action can
    only attach to an execution the caller may already see. That safety is *derived*,
    not stated: it holds only while the id list is produced by an org-scoped query
    three statements earlier. A refactor that widened the execution page, took ids
    from a caller-supplied parameter, or reused this loader for a different scope
    would silently turn it into a cross-tenant action read with no test objecting.

    So the seeded row is deliberately impossible-by-construction — an ORG_B action
    hanging off an ORG_A execution — because a corrupt or mis-tenanted write is
    exactly the case the redundant predicate exists to contain. Asserting it keeps
    the defence load-bearing rather than decorative.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    await seed_action(
        session,
        execution,
        operation_key="pr:story-a:cycle-1",
        status=ActionStatus.SUCCEEDED,
        receipt_ref="pr/PR_kwDOmine",
    )
    await seed_action(
        session,
        execution,
        operation_key="pr:leaked-from-org-b:cycle-1",
        status=ActionStatus.SUCCEEDED,
        receipt_ref="pr/PR_kwDOtheirs",
        org_id=ORG_B,
    )

    response = client_for(app_with_router, org_id=ORG_A).get(route(flow.id))
    actions = response.json()["executions"][0]["actions"]

    assert [action["operation_key"] for action in actions] == ["pr:story-a:cycle-1"]
    assert "PR_kwDOtheirs" not in response.text


@pytest.mark.asyncio
async def test_denied_caller_cannot_learn_whether_the_flow_exists(session, app_with_router):
    """The permission gate runs before any read, so denial precedes existence.

    Asserted against a flow that DOES exist: if the check ran after resolution, a
    denied caller would get different behaviour for a real id than a fabricated one,
    which is the same enumeration oracle in a different coat.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(session, flow, node)

    denied = client_for(app_with_router, permitted=False)
    existing = denied.get(route(flow.id))
    fabricated = denied.get(route("00000000-0000-0000-0000-000000000000"))

    assert existing.status_code == fabricated.status_code == 403
    assert Permission.USAGE_READ.value in existing.text
    # No ledger content leaks through the denial.
    assert node.id not in existing.text


@pytest.mark.asyncio
async def test_progress_visibility_does_not_grant_pause_resume_permission(session, app_with_router, monkeypatch):
    """Reading progress remains available without the separate control authority."""
    from unittest.mock import AsyncMock

    from src.admin.exceptions import AccessDeniedError

    flow = await seed_flow(session)
    client = client_for(app_with_router)
    path = route(flow.id)
    check = AsyncMock(
        side_effect=AccessDeniedError(
            message="Permission 'plan:approve' is required for this operation",
            required_permission=Permission.PLAN_APPROVE.value,
            user_role="member",
        )
    )
    monkeypatch.setattr("src.orchestration.flow_controls.AccessControl.check_permission", check)

    assert client.get(path).status_code == 200
    assert client.post(path, json={"paused": False}).status_code == 403
    check.assert_awaited_once()
    assert check.call_args.args[1] == Permission.PLAN_APPROVE
    await session.refresh(flow)
    assert flow.execution_paused is True
    assert client.patch(path, json={}).status_code == 405
    assert client.delete(path).status_code == 405


@pytest.mark.asyncio
async def test_the_claim_binding_is_never_published(session, app_with_router):
    """Neither the work claim nor the acceptance record reaches the payload.

    `claim_id`/`claim_generation` are the authority binding the store's fence tests,
    and the store withholds them from a refused caller precisely so a refusal cannot
    disclose what would satisfy it. Serving them to a browser would undo that.

    `accepted_plan_version` is excluded for a different reason, and this assertion
    exists because an earlier draft of this route DID serve it:
    `test_internal_plane_guard.py` requires `PLAN_APPROVE` of any handler touching an
    acceptance record, and "which approved plan authorized this delivery" is exactly
    that. The two escapes were both wrong — relax the guard, or make merely viewing
    delivery progress demand approval authority. So the field is gone, and this test
    keeps it gone: re-adding it would reopen the escalation at a point where the
    router guard is the only thing watching.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(session, flow, node, accepted_plan_version=4)

    response = client_for(app_with_router).get(route(flow.id))
    execution = response.json()["executions"][0]

    assert "claim_id" not in execution
    assert "claim_generation" not in execution
    assert CLAIM_ID not in response.text
    # The acceptance record stays on the plans route, under the permission that
    # governs it.
    assert "accepted_plan_version" not in execution


# ---------------------------------------------------------------------------
# Reference sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Reference-shaped values pass through unchanged.
        ("pr/PR_kwDOABCD1234", "pr/PR_kwDOABCD1234"),
        ("s3://adp-artifacts/node-1/cycle-1/patch.diff", "s3://adp-artifacts/node-1/cycle-1/patch.diff"),
        ("issue-comment/5142#c9", "issue-comment/5142#c9"),
        ("ses/0190a7c4f1d2", "ses/0190a7c4f1d2"),
        # Script-bearing schemes are refused: these are the ones that turn a
        # reference rendered as a link into execution in an operator's browser.
        ("javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("vbscript:msgbox", None),
        ("data:text/html;base64,PHNjcmlwdD4=", None),
        # Not reference-shaped at all.
        ("has whitespace in it", None),
        ("", None),
        (None, None),
        # A transcript-sized blob is refused before any pattern reasoning.
        ("a" * 900, None),
    ],
)
def test_reference_sanitisation_fails_closed(raw, expected):
    """Only reference-shaped values survive; anything else becomes None.

    Fail-closed is the deliberate direction. A dropped reference degrades to
    "pending" in the UI, which is recoverable and honest. A rendered hostile one has
    no revocation path once it is in an operator's browser.
    """
    assert _safe_ref(raw) == expected


@pytest.mark.asyncio
async def test_a_hostile_stored_reference_is_dropped_from_the_response(session, app_with_router):
    """Sanitisation is wired into the route, not merely available as a helper.

    A unit-tested sanitiser that the projection forgets to call is worse than none,
    because the tests report it as covered.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(
        session,
        flow,
        node,
        notification_receipt_ref="javascript:alert('notify')",
        handoff_receipt_ref="issue-comment/5142#c9",
    )
    await seed_action(
        session,
        execution,
        operation_key="pr:story-a:cycle-1",
        status=ActionStatus.SUCCEEDED,
        artifact_ref="javascript:alert('artifact')",
        receipt_ref="pr/PR_kwDOABCD1234",
    )

    response = client_for(app_with_router).get(route(flow.id))
    body = response.json()["executions"][0]

    assert "javascript:" not in response.text
    assert body["notification_receipt_ref"] is None
    # The legitimate references alongside them are untouched.
    assert body["handoff_receipt_ref"] == "issue-comment/5142#c9"
    assert body["actions"][0]["artifact_ref"] is None
    assert body["actions"][0]["receipt_ref"] == "pr/PR_kwDOABCD1234"


# ---------------------------------------------------------------------------
# Bounded reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_are_capped_and_the_truncation_is_reported(session, app_with_router):
    """A capped action list says so instead of presenting itself as complete.

    "Never fetch every historical action per node" is a requirement, so the cap is
    real — and a silently truncated list would let an operator conclude a step never
    happened. `action_overflow` is the honest signal.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    total_actions = MAX_ACTIONS_PER_EXECUTION + 5
    for index in range(total_actions):
        await seed_action(
            session,
            execution,
            operation_key=f"step-{index}:story-a:cycle-1",
            status=ActionStatus.SUCCEEDED,
            created_at=NOW + timedelta(seconds=index),
        )

    body = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]

    assert len(body["actions"]) == MAX_ACTIONS_PER_EXECUTION
    assert body["action_overflow"] is True
    # Newest first: the recent actions are the ones that answer "what is pending".
    newest = f"step-{total_actions - 1}:story-a:cycle-1"
    assert body["actions"][0]["operation_key"] == newest


@pytest.mark.asyncio
async def test_the_action_bound_is_per_execution_and_does_not_starve_a_quiet_one(session, app_with_router):
    """A busy execution is capped without a quiet sibling losing its rows.

    This is the asymmetric case, and the asymmetry is the entire point: a
    single-execution regression passes against all three implementations of "cap the
    actions", including the two wrong ones.

    - Slicing in Python after the fetch bounds the RESPONSE while the QUERY still
      loads every historical action for every execution on the page — the unbounded
      read the issue forbids, wearing a bounded response as a disguise.
    - A global `LIMIT (cap + 1) * len(ids)` bounds the fetch but lets the busy
      execution consume the entire budget, so `quiet` returns ZERO rows. That is the
      worse failure: an empty action list reads as "nothing happened here", and
      `action_overflow` is False while it says so, so the view makes a positive false
      claim rather than admitting it withheld something.

    Only a per-execution bound satisfies both, which is why the assertion on `quiet`
    matters as much as the one on `busy`.

    **The seeded timestamps are what makes this discriminate, and the direction is
    counter-intuitive.** `quiet`'s actions are deliberately OLDER than every one of
    `busy`'s. A global limit must order newest-first to keep the cap meaningful, so it
    reads all of `busy` before reaching `quiet` and `quiet` is what gets truncated to
    nothing. Seeding `quiet` as newer protects it from the very bug under test: the
    global limit reaches its rows first, returns all three, and the test passes against
    the broken implementation. Verified by probe rather than assumed — the first
    version of this test had the direction backwards and passed against a global
    `LIMIT`.
    """
    flow = await seed_flow(session)
    busy_node = await seed_node(session, flow, node_ref="story-busy")
    quiet_node = await seed_node(session, flow, node_ref="story-quiet")
    busy = await seed_execution(session, flow, busy_node)
    quiet = await seed_execution(session, flow, quiet_node)

    quiet_count = 3
    for index in range(quiet_count):
        await seed_action(
            session,
            quiet,
            operation_key=f"quiet-{index}:story-quiet:cycle-1",
            status=ActionStatus.SUCCEEDED,
            # Oldest in the flow: a newest-first global limit reaches these LAST and
            # starves them, which is exactly the failure being asserted against.
            created_at=NOW + timedelta(seconds=index),
        )
    over_cap = MAX_ACTIONS_PER_EXECUTION + 30
    for index in range(over_cap):
        await seed_action(
            session,
            busy,
            operation_key=f"busy-{index}:story-busy:cycle-1",
            status=ActionStatus.SUCCEEDED,
            created_at=NOW + timedelta(seconds=quiet_count + index),
        )

    body = client_for(app_with_router).get(route(flow.id)).json()
    by_node = {execution["node_id"]: execution for execution in body["executions"]}

    busy_view = by_node[busy_node.id]
    assert len(busy_view["actions"]) == MAX_ACTIONS_PER_EXECUTION
    assert busy_view["action_overflow"] is True

    quiet_view = by_node[quiet_node.id]
    # The assertion a global limit fails: all three rows, and honest about it.
    assert len(quiet_view["actions"]) == quiet_count, "the quiet execution was starved of its actions"
    assert quiet_view["action_overflow"] is False


@pytest.mark.asyncio
async def test_the_action_cap_is_enforced_in_sql_not_after_the_fetch(session, app_with_router):
    """The database returns at most `cap + 1` per execution, not the whole history.

    Companion to the test above, and the one that distinguishes a bounded FETCH from a
    bounded RESPONSE — the two are indistinguishable from the payload alone, because
    Python slicing produces a byte-identical response while having already
    materialised every row in the gateway.

    Asserted by counting the rows the implementation's OWN action query returns, via
    the cursor. Re-issuing an equivalent query in the test would prove only that SQL
    can bound a fetch, not that this module's query does — the assertion has to observe
    the real statement.

    `cap + 1` is the expected ceiling rather than `cap`: the extra row is what
    `action_overflow` is derived from, so fetching exactly `cap` would make truncation
    undetectable.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    execution = await seed_execution(session, flow, node)
    seeded = MAX_ACTIONS_PER_EXECUTION + 40
    for index in range(seeded):
        await seed_action(
            session,
            execution,
            operation_key=f"step-{index}:story-a:cycle-1",
            status=ActionStatus.SUCCEEDED,
            created_at=NOW + timedelta(seconds=index),
        )
    await session.commit()

    # The action statement is captured and re-run on a SEPARATE connection to count
    # what it returns. It is deliberately NOT counted by draining the live cursor in an
    # `after_cursor_execute` hook: that consumes the rows the implementation is about to
    # read, so the code under test sees an empty result and the test measures its own
    # interference. (Observed — the first version of this test did exactly that and
    # failed with `action_overflow is False`.)
    captured: list[tuple[str, object]] = []

    @event.listens_for(session.bind.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, params, _context, _executemany):
        if "orchestration_actions" in statement.lower() and "GROUP BY" not in statement:
            captured.append((statement, params))

    try:
        view = await load_flow_execution_view(session, org_id=ORG_A, flow_id=flow.id)
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", _capture)

    assert len(captured) == 1, f"expected exactly one action query, saw {len(captured)}"
    statement, params = captured[0]
    # Re-run through the raw DBAPI: the captured statement carries the driver's own
    # positional placeholders and parameter tuple, which `text()` would try to parse as
    # named binds.
    raw = await session.connection()
    fetched = len((await raw.exec_driver_sql(statement, tuple(params))).fetchall())

    # The bound is in the DATABASE: far fewer rows crossed the wire than were seeded.
    assert fetched == MAX_ACTIONS_PER_EXECUTION + 1, f"action fetch was not bounded per execution: {fetched} rows fetched of {seeded} seeded"
    assert view.executions[0].action_overflow is True


@pytest.mark.asyncio
async def test_execution_read_carries_its_own_tenant_predicate(session, app_with_router):
    """The execution queries are org-scoped independently of the flow that resolved it.

    Mirrors `test_action_read_carries_its_own_tenant_predicate` one level up. The
    seeded row is impossible by construction — an ORG_B execution on ORG_A's flow —
    and it has to be, because a correctly-tenanted ORG_B execution on ORG_B's own flow
    is excluded by the `flow_id` predicate no matter what `org_id` does. Only a
    mis-tenanted row can show that the `org_id` predicate is load-bearing rather than
    decorative.

    To be precise about scope: there is no cross-tenant read at this head. The route
    resolves the flow under `current_user.org_id` and 404s otherwise, so these
    predicates are genuinely redundant today. What this test protects is that they
    still hold if that resolution is ever refactored — the same argument the
    action-level docstring makes, applied one level up.

    One test covers both statements because `total` and `executions` are computed
    separately: the `total` assertion is the only cover for the count query. That
    split is also a user-visible bug independent of tenancy — a predicate in one
    statement and not the other renders a permanently wrong count beside a correct
    list, which looks like a UI defect and gets debugged in the wrong place.
    """
    mine = await seed_flow(session)
    my_node = await seed_node(session, mine, node_ref="story-mine")
    await seed_execution(session, mine, my_node, progress_note="mine")

    their_node = await seed_node(session, mine, node_ref="story-theirs", org_id=ORG_B)
    await seed_execution(
        session,
        mine,
        their_node,
        org_id=ORG_B,
        cycle=2,
        progress_note="leaked-theirs",
    )

    body = client_for(app_with_router, org_id=ORG_A).get(route(mine.id)).json()

    assert body["total"] == 1, f"count query leaked: total={body['total']}"
    assert [execution["node_id"] for execution in body["executions"]] == [my_node.id]
    assert "leaked-theirs" not in response_text(body)


@pytest.mark.asyncio
async def test_page_is_bounded_and_total_reports_what_is_not_shown(session, app_with_router):
    """Paging is explicit, and `total` says how much the page omits.

    Without `total` a client showing 2 of 5 executions cannot tell the operator that
    3 are missing, which turns a page into a false claim about the flow.
    """
    flow = await seed_flow(session)
    for index in range(5):
        node = await seed_node(session, flow, node_ref=f"story-{index}")
        await seed_execution(session, flow, node)

    body = client_for(app_with_router).get(route(flow.id), params={"limit": 2, "offset": 0}).json()

    assert len(body["executions"]) == 2
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0


@pytest.mark.asyncio
async def test_an_over_bound_limit_is_422_rather_than_a_silent_clamp(session, app_with_router):
    """A caller asking for more than the ceiling is refused, not quietly clamped.

    A silent clamp lets a client believe it received everything and page as though
    it had — the same class of error as an unreported truncation.
    """
    flow = await seed_flow(session)

    response = client_for(app_with_router).get(route(flow.id), params={"limit": MAX_EXECUTIONS_PER_PAGE + 1})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cycles_of_one_node_are_separate_entries_in_order(session, app_with_router):
    """A repair cycle is its own execution, ordered after the original.

    Collapsing cycles would present a retry as the first attempt and lose the record
    of what the first cycle already did externally — which is the evidence a
    recovering process needs.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(session, flow, node, cycle=1, status=ExecutionStatus.SUPERSEDED, next_check_at=None)
    await seed_execution(session, flow, node, cycle=2, phase=ExecutionPhase.DELIVERING)

    body = client_for(app_with_router).get(route(flow.id)).json()

    assert body["total"] == 2
    assert [e["cycle"] for e in body["executions"]] == [1, 2]
    assert body["executions"][0]["status"] == "superseded"
    assert body["executions"][1]["status"] == "runnable"


@pytest.mark.asyncio
async def test_actions_for_a_page_are_fetched_in_one_grouped_query(session, app_with_router):
    """Action loading does not scale with execution count.

    Counted rather than asserted by inspection: an N+1 per-execution action query is
    invisible in a small test and is precisely the unbounded read the issue forbids.
    The query count must not grow when executions do.
    """
    flow = await seed_flow(session)
    for index in range(6):
        node = await seed_node(session, flow, node_ref=f"story-{index}")
        execution = await seed_execution(session, flow, node)
        await seed_action(session, execution, operation_key=f"pr:story-{index}:cycle-1", status=ActionStatus.SUCCEEDED)
    await session.commit()

    statements: list[str] = []

    @event.listens_for(session.bind.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    try:
        view = await load_flow_execution_view(session, org_id=ORG_A, flow_id=flow.id)
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", _record)

    assert len(view.executions) == 6
    assert all(execution.actions for execution in view.executions)
    # Count, execution page, bounded actions, grouped stage counts, and node counters.
    # Six executions must not mean six action queries.
    assert len(statements) == 5, statements


# ---------------------------------------------------------------------------
# Unknown vocabulary must not take the whole flow down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_from_a_newer_build_is_dropped_rather_than_failing_the_request(session, app_with_router):
    """One unreadable row must not 500 the whole flow view.

    The store raises on unknown phase/status because a *writer* must fail closed on
    a value it cannot reason about. A read is the opposite trade: an operator
    diagnosing node B must not be blocked because node A was written by a newer pod.
    The row is dropped and logged, and `total` still counts it — so the view reports
    fewer executions than it counted rather than silently renumbering.
    """
    flow = await seed_flow(session)
    good_node = await seed_node(session, flow, node_ref="story-good")
    future_node = await seed_node(session, flow, node_ref="story-future")
    await seed_execution(session, flow, good_node)
    await seed_execution(session, flow, future_node, phase="teleporting", status="quantum")

    response = client_for(app_with_router).get(route(flow.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["node_id"] for e in body["executions"]] == [good_node.id]
    # Counted, not hidden: the discrepancy between total and length is the signal.
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_an_unrecognised_block_code_stays_blocked(session, app_with_router):
    """An unknown block code reports as authority-unverifiable, never as unblocked.

    Fail-closed, matching the store: if a newer writer's block member read as "not
    blocked", an older pod's view would show work proceeding that was deliberately
    stopped.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(
        session,
        flow,
        node,
        status=ExecutionStatus.BLOCKED,
        block_code="invented_by_a_newer_build",
        block_owner="platform-operator",
        block_required_input="unknown",
    )

    execution = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]

    assert execution["block"] is not None
    assert execution["block"]["code"] == BlockCode.AUTHORITY_UNVERIFIABLE.value


@pytest.mark.asyncio
async def test_malformed_stored_gates_report_no_gates_rather_than_erroring(session, app_with_router):
    """A corrupt gate list must not make the record unreadable.

    The store's decoder answers "no gates" for unparseable JSON so a cosmetic
    storage problem cannot hide the record an operator is trying to diagnose. This
    read reuses that decoder rather than adding a second one, so the two cannot
    disagree about what a stored value means.
    """
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story-a")
    await seed_execution(
        session,
        flow,
        node,
        status=ExecutionStatus.BLOCKED,
        block_code=BlockCode.HUMAN_INPUT_REQUIRED.value,
        block_owner="requesting-user",
        block_required_input="answer the clarifying question",
        block_remaining_gates="{not json at all",
    )

    execution = client_for(app_with_router).get(route(flow.id)).json()["executions"][0]

    assert execution["block"]["remaining_gates"] == []
    # The block itself is still routable.
    assert execution["block"]["owner"] == "requesting-user"


# ---------------------------------------------------------------------------
# Fixtures and seeding
# ---------------------------------------------------------------------------


def response_text(body: dict) -> str:
    import json as _json

    return _json.dumps(body)


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See test_read_api.py."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _token_context(org_id: str, *, user_id: str = USER_ID) -> TokenContext:
    """An authenticated caller in `org_id`.

    `org_id` is an authenticated Cognito claim and is never writable by a request
    header, which is what lets the route use it as the tenant directly.
    """
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the orchestration router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and would
    make an authz assertion here depend on all of it.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import router as orchestration_router
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(orchestration_router)

    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool = True, org_id: str = ORG_A):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `USAGE_READ` because that is the permission this route
    gates on — asserting a `PLAN_APPROVE` denial would test a permission the route
    does not check.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from src.admin.access_control import AccessControl
    from src.admin.config import AdminRole
    from src.admin.exceptions import AccessDeniedError
    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import get_access_control

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message="Permission 'usage:read' is required for this operation",
                required_permission=Permission.USAGE_READ.value,
                user_role="member",
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole("org_admin"), org_id, None))

    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: _token_context(org_id)
    return TestClient(app, raise_server_exceptions=False)


async def seed_flow(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    slug: str = FLOW_SLUG,
    intent_ref: str | None = "5122",
) -> OrchestrationFlow:
    repo = OrchestrationRepository(session)
    return await repo.create_flow(org_id=org_id, slug=slug, title="Delivery loop", intent_ref=intent_ref)


async def seed_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str,
    kind: str = NodeKind.STORY.value,
    state: str = NodeState.PENDING.value,
    org_id: str = ORG_A,
) -> OrchestrationNode:
    repo = OrchestrationRepository(session)
    node = await repo.add_node(
        org_id=org_id,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        issue_ref=None,
    )
    node.state = state
    await session.flush()
    return node


async def seed_execution(
    session: AsyncSession,
    flow: OrchestrationFlow,
    node: OrchestrationNode,
    *,
    org_id: str = ORG_A,
    cycle: int = 1,
    phase: str | ExecutionPhase = ExecutionPhase.DELIVERING,
    status: str | ExecutionStatus = ExecutionStatus.RUNNABLE,
    revision: int = 3,
    accepted_plan_version: int = 4,
    attempts: int = 1,
    next_check_at: datetime | None = NOW,
    deadline_at: datetime | None = None,
    progressed_at: datetime | None = NOW,
    progress_note: str | None = None,
    updated_at: datetime | None = None,
    block_code: str | None = None,
    block_owner: str | None = None,
    block_required_input: str | None = None,
    block_remaining_gates: str | None = None,
    block_detail: str | None = None,
    pending_action_key: str | None = None,
    notification_receipt_ref: str | None = None,
    handoff_receipt_ref: str | None = None,
) -> OrchestrationExecution:
    """One ledger row, written directly.

    Deliberately NOT written through `execution_store`: every store entry point
    requires an `ExecutionIdentity` carrying a work claim, which is exactly the
    authority an operator read does not hold and must not need. Writing the row
    directly keeps these tests about the read path, and lets a test seed a
    phase/status this build does not recognise — which the store would (correctly)
    refuse to write.
    """
    row = OrchestrationExecution(
        org_id=org_id,
        flow_id=flow.id,
        node_id=node.id,
        cycle=cycle,
        phase=phase.value if isinstance(phase, ExecutionPhase) else phase,
        status=status.value if isinstance(status, ExecutionStatus) else status,
        revision=revision,
        accepted_plan_version=accepted_plan_version,
        claim_id=CLAIM_ID,
        claim_generation=CLAIM_GENERATION,
        attempts=attempts,
        next_check_at=next_check_at,
        deadline_at=deadline_at,
        progressed_at=progressed_at,
        progress_note=progress_note,
        block_code=block_code,
        block_owner=block_owner,
        block_required_input=block_required_input,
        block_remaining_gates=block_remaining_gates,
        block_detail=block_detail,
        pending_action_key=pending_action_key,
        notification_receipt_ref=notification_receipt_ref,
        handoff_receipt_ref=handoff_receipt_ref,
        created_at=NOW,
        updated_at=updated_at,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_action(
    session: AsyncSession,
    execution: OrchestrationExecution,
    *,
    operation_key: str,
    status: ActionStatus = ActionStatus.PREPARED,
    kind: str = "open_pull_request",
    attempt: int = 1,
    artifact_ref: str | None = "s3://adp-artifacts/node-1/cycle-1/patch.diff",
    receipt_ref: str | None = None,
    observed_at: datetime | None = None,
    created_at: datetime = NOW,
    org_id: str = ORG_A,
) -> OrchestrationAction:
    row = OrchestrationAction(
        org_id=org_id,
        execution_id=execution.id,
        operation_key=operation_key,
        kind=kind,
        status=status.value if isinstance(status, ActionStatus) else status,
        attempt=attempt,
        artifact_ref=artifact_ref,
        receipt_ref=receipt_ref,
        detail={"base": "main"},
        created_at=created_at,
        observed_at=observed_at,
    )
    session.add(row)
    await session.flush()
    return row


async def test_stage_counts_include_history_outside_the_execution_page(session, app_with_router):
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="stage-story")
    node.attempts = 3
    old = await seed_execution(session, flow, node, cycle=1, status=ExecutionStatus.CONCLUDED)
    current = await seed_execution(session, flow, node, cycle=3)
    for execution, key in [(old, "old-review"), (current, "current-review")]:
        action = await seed_action(session, execution, operation_key=key, kind="review_cycle_dispatch", status=ActionStatus.FAILED)
        action.detail = {"action": "review"}
    await session.commit()
    body = client_for(app_with_router).get(route(flow.id), params={"limit": 1, "offset": 1}).json()
    assert body["executions"][0]["stage_attempts"] == {"develop": 3, "review": 2}
