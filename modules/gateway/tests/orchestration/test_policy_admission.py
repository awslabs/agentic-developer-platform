"""Tests for policy admission at dispatch (#5128).

`test_execution_policy.py` proves the *rule* is correct against literal facts. This
file proves the *fact resolution* is correct against real rows — which is where a
fail-closed design actually gets lost, because every mistake here has the same shape:
a fact that could not be read becomes a convenient default, and the rule then permits
on a fiction.

So the tests are organised around what each resolved fact is capable of getting
wrong:

  - **No policy still dispatches.** The legacy-preservation regression. Covered here
    end-to-end through `run_dispatch_pass` as well as at the unit boundary, because
    a refusal that only manifests in the full pass would be invisible otherwise.
  - **A refusal leaves the node exactly as it was.** `ready`, `attempts` unchanged,
    nothing queued. An attempt burned on unadmitted work is spend the owner's own
    limit then denies them.
  - **Revoked membership denies**, and the codebase's revocation *is* row absence,
    so the test deletes rows rather than setting a flag.
  - **Spend absence is read correctly in both directions**: a fresh flow (all nodes
    unmeasured) must still dispatch, while a node that ran without a ledger row must
    not. Getting the first wrong is a permanent engine outage; the second is the
    unbounded-spend hole.
  - **A scoped credential is required**, and "we could not check" is never `SCOPED`.

The harness deliberately mirrors `test_dispatch_pass.py` — same in-memory engine
shape, same `_make_*` builders — so a divergence between what dispatch does and what
these tests exercise cannot hide in a differently-built fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
from src.orchestration.execution_policy import (
    AcceptanceMode,
    Action,
    CredentialScope,
    DenyReason,
    ExecutionPolicy,
    PolicyLimits,
    stamp_policy,
)
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.policy_admission import (
    action_for_node_kind,
    authorize_node_dispatch,
    load_in_force_policy,
)
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.usage import UsageLog

ORG_A = "org-alpha"
APPROVER = "cognito-sub-alice"
INSTALLATION_A = 55_501
REPO = "aws-e/adp"
FLOW_SLUG = "flow-1"
TEAM_A = "team-test"
DEPT_A = "dept-test"
EXPIRY = datetime.now(UTC) + timedelta(days=30)


@pytest.fixture(autouse=True)
def policy_budget_initializers(monkeypatch):
    initialized = set()

    async def claim(*, org_id, flow_id, allow_create):
        key = (org_id, flow_id)
        if key in initialized:
            return False
        if not allow_create:
            raise RuntimeError("existing work requires reconciliation")
        initialized.add(key)
        return True

    monkeypatch.setattr("src.orchestration.flow_meter._claim_initialization", claim)


@pytest.fixture(autouse=True)
async def healthy_policy_reservations(monkeypatch):
    """Permitting-policy cases require real atomic holds, not an absent backend."""
    import fakeredis.aioredis

    from src.budget.reservations import ReservationStore
    from src.orchestration import flow_budget

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flow_budget, "_reservations", ReservationStore(redis_url=None, ttl_seconds=120, client=client))
    yield
    await client.aclose()


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


class FakeRunStore:
    """Same stand-in `test_dispatch_pass.py` uses — dispatch registers the envelope."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def register(self, envelope: dict[str, Any]) -> None:
        self.rows[envelope["message_id"]] = envelope


@pytest.fixture(autouse=True)
def run_store(monkeypatch):
    from src.orchestration.run_store import EngineRunStore

    store = FakeRunStore()
    monkeypatch.setattr(EngineRunStore, "from_env", lambda: store)
    return store


def _config(**overrides: Any) -> DispatchPassConfig:
    defaults: dict[str, Any] = {
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo",
        "repo": REPO,
    }
    defaults.update(overrides)
    return DispatchPassConfig(**defaults)


def _limits(**overrides: Any) -> PolicyLimits:
    base: dict[str, Any] = {
        "max_wall_clock_seconds": 3600,
        "max_spend_usd": Decimal("50.00"),
        "max_attempts_per_node": 3,
        "max_concurrent_actions": 4,
    }
    base.update(overrides)
    return PolicyLimits(**base)


def _policy(**overrides: Any) -> ExecutionPolicy:
    """A policy that permits the standard fixture's story node.

    Built permissive on purpose so each test breaks exactly one thing and the deny
    reason names that thing — a fixture that failed two checks at once would pass
    while asserting the wrong reason.
    """
    base: dict[str, Any] = {
        "org_id": ORG_A,
        "repository_ids": [REPO],
        "allowed_actions": [Action.DEVELOP, Action.MERGE, Action.EVALUATE],
        "expires_at": EXPIRY,
        "limits": _limits(),
    }
    base.update(overrides)
    return ExecutionPolicy(**base)


async def _make_org(session: AsyncSession, *, org_id: str = ORG_A) -> Organization:
    org = Organization(id=org_id, name=f"Org {org_id}", github_installation_ids=[str(INSTALLATION_A)])
    session.add(org)
    await session.flush()
    return org


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = FLOW_SLUG) -> OrchestrationFlow:
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="Demo flow")
    session.add(flow)
    await session.flush()
    return flow


async def _make_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str = "s7",
    state: NodeState | str = NodeState.READY,
    kind: str = NodeKind.STORY.value,
    issue_ref: str | None = "4196",
    attempts: int = 0,
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-4",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
        issue_ref=issue_ref,
        attempts=attempts,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_member(session: AsyncSession, *, org_id: str = ORG_A, user_id: str = APPROVER) -> User:
    """A real member: a `users` row, a tenant membership, and a team.

    All three because the resolver reads all three, and a fixture that only created
    the `users` row would make the team-scoped tests vacuous.
    """
    session.add(Department(id=DEPT_A, org_id=org_id, name="Test department"))
    await session.flush()
    session.add(Team(id=TEAM_A, org_id=org_id, department_id=DEPT_A, name="Test team"))
    user = User(id=user_id, org_id=org_id, team_id=TEAM_A, email=f"{user_id}@example.com", cognito_sub=f"sub:{user_id}")
    session.add(user)
    await session.flush()
    session.add(TenantMembership(user_id=user_id, tenant_id=org_id, role="org_admin"))
    session.add(TeamMembership(org_id=org_id, user_id=user_id, team_id=TEAM_A, role="member"))
    await session.flush()
    return user


async def _make_usage(
    session: AsyncSession,
    node: OrchestrationNode,
    *,
    flow_slug: str = FLOW_SLUG,
    cost_usd: str = "1.00",
) -> UsageLog:
    """A settled ledger row for a node, so its cost reads as measured.

    Needed whenever a test puts a node into an executed state for some *other*
    reason (concurrency, say): without a row that node is unreconciled spend, and the
    admission blocks on `SPEND_UNKNOWN` before ever reaching the limit under test.
    That interaction is real behaviour, not a fixture nuisance — which is why the
    spend tests assert it directly.
    """
    row = UsageLog(
        org_id=node.org_id,
        department_id=DEPT_A,
        team_id=TEAM_A,
        user_id=APPROVER,
        model="anthropic.claude-3-5-sonnet",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal(cost_usd),
        latency_ms=1200,
        status_code=200,
        graph_address=f"{flow_slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
    )
    session.add(row)
    await session.flush()
    return row


async def _make_approval(session: AsyncSession, flow: OrchestrationFlow) -> OrchestrationDecision:
    decision = OrchestrationDecision(
        org_id=flow.org_id,
        flow_id=flow.id,
        kind=DecisionKind.GATE_APPROVED.value,
        actor_id=APPROVER,
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN.value,
        reason="approved at the wave gate",
    )
    session.add(decision)
    await session.flush()
    return decision


async def _accept_policy(
    session: AsyncSession,
    flow: OrchestrationFlow,
    policy: ExecutionPolicy | None,
    *,
    version: int = 1,
) -> OrchestrationAcceptedPlan:
    """Persist an accepted plan carrying `policy`, stamped as acceptance would.

    Uses the real `stamp_policy` rather than hand-writing the stamped fields, so a
    test cannot accidentally pin a document shape that acceptance would never
    produce.
    """
    stamped = None if policy is None else stamp_policy(policy, principal_id=APPROVER, org_id=flow.org_id)
    plan = OrchestrationAcceptedPlan(
        org_id=flow.org_id,
        flow_id=flow.id,
        version=version,
        plan_document={
            "flow_slug": flow.slug,
            "execution_policy": None if stamped is None else stamped.model_dump(mode="json"),
        },
        plan_hash=f"hash-v{version}",
    )
    session.add(plan)
    await session.flush()
    return plan


async def _fixture(
    session: AsyncSession,
    *,
    policy: ExecutionPolicy | None,
    node_kwargs: dict[str, Any] | None = None,
) -> tuple[OrchestrationFlow, OrchestrationNode]:
    """Org + member + flow + accepted plan + approval + one ready story."""
    await _make_org(session)
    await _make_member(session)
    flow = await _make_flow(session)
    await _accept_policy(session, flow, policy)
    if policy is not None:
        from src.orchestration.flow_meter import prepare_flow_meter

        # Fixtures with historical work model a flow whose first admission already
        # initialized its meter. Fault tests may deliberately make this unavailable.
        await prepare_flow_meter(org_id=flow.org_id, flow_id=flow.id, policy=policy, nodes=[])
    await _make_approval(session, flow)
    node = await _make_node(session, flow, **(node_kwargs or {}))
    return flow, node


async def _authorize(session: AsyncSession, node: OrchestrationNode, **overrides: Any):
    kwargs: dict[str, Any] = {
        "principal_user_id": APPROVER,
        "target_repository": REPO,
        "installation_resolved": True,
    }
    kwargs.update(overrides)
    return await authorize_node_dispatch(session, node=node, **kwargs)


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


# ---------------------------------------------------------------------------
# Legacy preservation
# ---------------------------------------------------------------------------


class TestNoPolicyPreservesLegacyDispatch:
    async def test_absent_policy_permits(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=None)
        decision = await _authorize(session, node)
        assert decision.permitted

    async def test_absent_accepted_plan_entirely_permits(self, session: AsyncSession) -> None:
        """A flow with no accepted plan row at all — the oldest legacy shape."""
        await _make_org(session)
        await _make_member(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        assert (await _authorize(session, node)).permitted

    async def test_unpolicied_flow_still_dispatches_through_the_full_pass(self, session: AsyncSession) -> None:
        """End-to-end, because a refusal wired into the pass would not show up above.

        This is the test that would fail if admission were accidentally made to deny
        on a missing policy — the whole engine stopping, expressed as one assertion.
        """
        _, node = await _fixture(session, policy=None)
        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 1
        assert report.policy_blocked == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_a_permitting_policy_dispatches_through_the_full_pass(self, session: AsyncSession) -> None:
        """The other half: enforcement does not break a flow it should admit."""
        _, node = await _fixture(session, policy=_policy())
        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 1
        assert report.policy_blocked == 0

    async def test_load_in_force_policy_reports_absence_distinctly(self, session: AsyncSession) -> None:
        """`None` policy is not the same as a permissive policy (see `AdmissionInputs`)."""
        _, node = await _fixture(session, policy=None)
        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        assert inputs.policy is None
        assert inputs.plan_version == 1


# ---------------------------------------------------------------------------
# A refusal must cost nothing
# ---------------------------------------------------------------------------


class TestRefusalLeavesTheNodeUntouched:
    async def test_blocked_node_stays_ready(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        await run_dispatch_pass(session, _config())
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_blocked_node_does_not_burn_an_attempt(self, session: AsyncSession) -> None:
        """**Why admission runs before `dispatch_node`.**

        `dispatch_node` increments `attempts`. Admitting after it would spend an
        attempt against the policy's own `max_attempts_per_node` on work that was
        never authorized — so a repeatedly-refused node would eventually be denied
        for exhausting a limit it never actually used.
        """
        _, node = await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        await run_dispatch_pass(session, _config())
        refreshed = (await session.execute(select(OrchestrationNode.attempts).where(OrchestrationNode.id == node.id))).scalar_one()
        assert refreshed == 0

    async def test_blocked_node_queues_no_envelope(self, session: AsyncSession) -> None:
        await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        report = await run_dispatch_pass(session, _config())
        assert report.pending == []
        assert report.dispatched == 0

    async def test_block_is_counted_and_reasoned(self, session: AsyncSession) -> None:
        await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        report = await run_dispatch_pass(session, _config())
        assert report.policy_blocked == 1
        assert report.policy_block_reasons == {DenyReason.ACTION_NOT_PERMITTED.value: 1}

    async def test_block_is_not_an_error_and_not_undispatchable(self, session: AsyncSession) -> None:
        """A correctly-enforced boundary is not a defect.

        Folding it into `errors` would make every tick report failure while a policy
        legitimately withheld an action; folding it into `undispatchable` would make
        it look like a malformed graph an operator should go fix.
        """
        await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        report = await run_dispatch_pass(session, _config())
        assert report.errors == 0
        assert report.undispatchable == 0
        assert report.success

    async def test_per_org_counter_is_recorded(self, session: AsyncSession) -> None:
        await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        report = await run_dispatch_pass(session, _config())
        assert report.per_org[ORG_A]["policy_blocked"] == 1


# ---------------------------------------------------------------------------
# Identity: membership is verified live, and revocation is row absence
# ---------------------------------------------------------------------------


class TestMembershipIsVerifiedLive:
    async def test_revoked_member_is_blocked(self, session: AsyncSession) -> None:
        """Revocation here is a hard delete, so the test deletes rather than flags.

        The policy still *names* this principal — that is the point. Authority is
        re-derived from live membership at every admission rather than trusted from
        the accepted document, so removing someone stops their flows.
        """
        _, node = await _fixture(session, policy=_policy())
        await session.execute(delete(TeamMembership).where(TeamMembership.user_id == APPROVER))
        await session.execute(delete(TenantMembership).where(TenantMembership.user_id == APPROVER))
        await session.execute(delete(User).where(User.id == APPROVER))
        await session.flush()

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.MEMBERSHIP_REVOKED

    async def test_selected_workspace_flag_does_not_revoke(self, session: AsyncSession) -> None:
        """`tenant_memberships.is_active` is the *selected workspace*, not liveness.

        Filtering on it — the obvious-looking mistake — would deny every member who
        happened to be browsing another workspace, halting their flows for a reason
        that has nothing to do with authority.
        """
        _, node = await _fixture(session, policy=_policy())
        membership = (await session.execute(select(TenantMembership).where(TenantMembership.user_id == APPROVER))).scalar_one()
        membership.is_active = False
        await session.flush()
        assert (await _authorize(session, node)).permitted

    async def test_legacy_member_without_a_membership_row_is_permitted(self, session: AsyncSession) -> None:
        """A native user predating `tenant_memberships` is still a real member.

        `workspaces.py` makes the same fallback deliberately. Treating the missing
        row as revocation would lock out exactly the oldest accounts.
        """
        _, node = await _fixture(session, policy=_policy())
        await session.execute(delete(TenantMembership).where(TenantMembership.user_id == APPROVER))
        await session.flush()
        assert (await _authorize(session, node)).permitted

    async def test_team_scoped_policy_admits_a_team_member(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy(team_ids=[TEAM_A]))
        assert (await _authorize(session, node)).permitted

    async def test_team_scoped_policy_blocks_a_non_member_of_that_team(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy(team_ids=["team-other"]))
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.TEAM_NOT_PERMITTED


# ---------------------------------------------------------------------------
# Spend: the two-sided reading of absence
# ---------------------------------------------------------------------------


class TestSpendAbsenceIsReadCorrectly:
    async def test_a_fresh_flow_dispatches_despite_having_no_ledger_rows(self, session: AsyncSession) -> None:
        """**The deadlock this design has to avoid.**

        Every node of a brand-new flow is UNKNOWN because none has run. A literal
        "any unknown blocks" reading would refuse the first dispatch of every flow
        forever — an engine-wide outage that would look like a safety feature.
        """
        _, node = await _fixture(session, policy=_policy())
        assert (await _authorize(session, node)).permitted

    async def test_a_node_that_ran_without_a_usage_row_blocks(self, session: AsyncSession) -> None:
        """**The hole on the other side.** Executed work with no cost row is
        unreconciled spend, and admitting more would be minting allowance against an
        unmeasured total.
        """
        flow, node = await _fixture(session, policy=_policy())
        await _make_node(session, flow, node_ref="s8", state=NodeState.PASSED)

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.SPEND_UNKNOWN

    async def test_a_gate_node_that_passed_does_not_make_spend_unknown(self, session: AsyncSession) -> None:
        """A gate bills no model call, so its missing row is expected in any state.

        Without this carve-out every flow would block permanently the moment its
        first human gate was approved — which is to say, on every real flow.
        """
        flow, node = await _fixture(session, policy=_policy())
        await _make_node(session, flow, node_ref="g1", kind=NodeKind.GATE.value, state=NodeState.PASSED, issue_ref=None)
        assert (await _authorize(session, node)).permitted

    async def test_a_sibling_still_pending_does_not_make_spend_unknown(self, session: AsyncSession) -> None:
        flow, node = await _fixture(session, policy=_policy())
        await _make_node(session, flow, node_ref="s9", state=NodeState.PENDING)
        assert (await _authorize(session, node)).permitted


# ---------------------------------------------------------------------------
# Limits observed from engine-owned rows
# ---------------------------------------------------------------------------


class TestLimitsAreObservedFromEngineState:
    async def test_attempts_at_the_limit_block(self, session: AsyncSession) -> None:
        _, node = await _fixture(
            session,
            policy=_policy(limits=_limits(max_attempts_per_node=2)),
            node_kwargs={"attempts": 2},
        )
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.ATTEMPT_LIMIT_EXCEEDED

    async def test_concurrency_is_counted_from_running_nodes(self, session: AsyncSession) -> None:
        """Counted from engine-written state, never a worker-reported number.

        A worker-supplied count would let a run inflate its own headroom by
        under-reporting its siblings.

        The running sibling gets a usage row: a node that is running with no ledger
        row is unreconciled spend, which blocks earlier on `SPEND_UNKNOWN` and would
        make this test pass without ever exercising the concurrency limit.
        """
        flow, node = await _fixture(session, policy=_policy(limits=_limits(max_concurrent_actions=1)))
        running = await _make_node(session, flow, node_ref="s10", state=NodeState.RUNNING)
        await _make_usage(session, running)

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.CONCURRENCY_LIMIT_EXCEEDED

    async def test_spend_at_the_cap_blocks(self, session: AsyncSession) -> None:
        """Settled spend is compared against the owner's cap.

        Paired with the `SPEND_UNKNOWN` tests above: this proves a *known* total is
        actually enforced, not merely computed.
        """
        flow, node = await _fixture(session, policy=_policy(limits=_limits(max_spend_usd=Decimal("2.00"))))
        spent = await _make_node(session, flow, node_ref="s11", state=NodeState.PASSED)
        await _make_usage(session, spent, cost_usd="2.50")

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.SPEND_LIMIT_EXCEEDED

    async def test_spend_below_the_cap_permits(self, session: AsyncSession) -> None:
        flow, node = await _fixture(session, policy=_policy(limits=_limits(max_spend_usd=Decimal("50.00"))))
        spent = await _make_node(session, flow, node_ref="s12", state=NodeState.PASSED)
        await _make_usage(session, spent, cost_usd="1.50")
        assert (await _authorize(session, node)).permitted

    async def test_unspent_policy_cannot_admit_a_run_larger_than_its_allowance(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy(limits=_limits(max_spend_usd=Decimal("4.00"))))
        decision = await _authorize(session, node)
        assert decision.reason is DenyReason.SPEND_LIMIT_EXCEEDED

    async def test_expired_policy_blocks(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.POLICY_EXPIRED

    async def test_expired_policy_blocks_through_the_full_pass(self, session: AsyncSession) -> None:
        """Expiry must bite in the real pass, not only at the unit boundary."""
        _, node = await _fixture(session, policy=_policy(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 0
        assert report.policy_block_reasons == {DenyReason.POLICY_EXPIRED.value: 1}
        assert await _state_of(session, node.id) == NodeState.READY.value


# ---------------------------------------------------------------------------
# Scope: repository binding and the mandatory scoped credential
# ---------------------------------------------------------------------------


class TestScopeIsEnforced:
    async def test_repository_outside_the_policy_blocks(self, session: AsyncSession) -> None:
        """The dispatch target is compared against the policy verbatim.

        Two checks would both refuse this — the repository binding and the credential
        scope, since no narrow credential can be minted for a repo the policy does not
        name. The repository check runs first, and the reason asserted here is the
        more useful of the two: it tells an operator the policy does not cover this
        repository, rather than sending them to look at a credential service that is
        working fine.
        """
        _, node = await _fixture(session, policy=_policy(repository_ids=["aws-e/other"]))
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.REPOSITORY_NOT_PERMITTED

    async def test_unresolved_installation_is_never_treated_as_scoped(self, session: AsyncSession) -> None:
        """**"We could not check" must never become `SCOPED`.**

        This is the broad-token fallback the issue forbids. When the installation did
        not resolve, no narrow credential can be established, and the only correct
        outcome is a block — never a reach for a broader platform credential.
        """
        _, node = await _fixture(session, policy=_policy())
        decision = await _authorize(session, node, installation_resolved=False)
        assert not decision.permitted
        assert decision.reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE

    async def test_scoped_credential_permits(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy())
        assert (await _authorize(session, node)).permitted

    async def test_credential_scope_has_no_permitting_value_but_scoped(self) -> None:
        """Guards the enum itself: exactly one member may permit."""
        assert [s for s in CredentialScope if s is CredentialScope.SCOPED] == [CredentialScope.SCOPED]
        assert CredentialScope.UNKNOWN is not CredentialScope.SCOPED
        assert CredentialScope.UNSCOPABLE is not CredentialScope.SCOPED


# ---------------------------------------------------------------------------
# Human gates are never machine-clearable
# ---------------------------------------------------------------------------


class TestHumanGatesAreNeverAdmitted:
    async def test_a_gate_node_has_no_autonomous_action(self) -> None:
        """No `Action` exists for a gate, so none can be permitted for one.

        Structural rather than a runtime check: the absence of a mapping is what
        makes "a policy that clears a human gate" unrepresentable.
        """
        assert action_for_node_kind(NodeKind.GATE.value) is None
        assert action_for_node_kind(NodeKind.STORY.value) is Action.DEVELOP
        assert action_for_node_kind(NodeKind.EVAL.value) is Action.EVALUATE

    async def test_an_unknown_node_kind_is_refused_not_guessed(self, session: AsyncSession) -> None:
        _, node = await _fixture(session, policy=_policy())
        node.kind = "speculative-future-kind"
        await session.flush()
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.ACTION_NOT_PERMITTED

    async def test_declaring_a_human_gate_blocks_the_matching_action(self, session: AsyncSession) -> None:
        """An action the owner reserved for themselves is not autonomously admitted."""
        _, node = await _fixture(session, policy=_policy(human_gates=[Action.DEVELOP]))
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.HUMAN_GATE_REQUIRED

    async def test_machine_evaluation_acceptance_does_not_widen_other_actions(self, session: AsyncSession) -> None:
        """Machine acceptance is a mode for evaluations, not general authority.

        A policy may let a machine accept an evaluation and still not let it develop —
        the two must not be coupled.
        """
        _, node = await _fixture(
            session,
            policy=_policy(
                allowed_actions=[Action.EVALUATE],
                evaluation_acceptance={f"{FLOW_SLUG}/4191/wave-4/s7": AcceptanceMode.MACHINE},
            ),
        )
        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.ACTION_NOT_PERMITTED


# ---------------------------------------------------------------------------
# Tenant isolation on the policy read itself
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_a_policy_is_not_read_across_tenants(self, session: AsyncSession) -> None:
        """The plan read filters on `org_id` as well as `flow_id`.

        A flow id alone must not surface another tenant's accepted policy.
        """
        _, node = await _fixture(session, policy=_policy())
        inputs = await load_in_force_policy(session, org_id="org-beta", flow_id=node.flow_id)
        assert inputs.policy is None

    async def test_superseded_plan_is_not_the_one_enforced(self, session: AsyncSession) -> None:
        """Enforcement reads the version in force, not the newest or the oldest."""
        flow, node = await _fixture(session, policy=_policy(allowed_actions=[Action.REVIEW]))
        superseded = (await session.execute(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))).scalar_one()
        superseded.superseded_at = datetime.now(UTC)
        await _accept_policy(session, flow, _policy(), version=2)

        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        assert inputs.plan_version == 2
        assert (await _authorize(session, node)).permitted


class TestUnparseablePolicyRefuses:
    async def test_a_malformed_stored_policy_does_not_crash_the_pass(self, session: AsyncSession) -> None:
        """One flow's bad document must not stop every other flow's dispatch.

        The affected flow refuses admission; other flows retain their behavior.
        """
        flow, node = await _fixture(session, policy=None)
        plan = (await session.execute(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))).scalar_one()
        plan.plan_document = {"execution_policy": {"schema_version": 1, "nonsense": True}}
        await session.flush()

        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        assert inputs.policy is None
        assert inputs.refusal.reason is DenyReason.SCHEMA_UNSUPPORTED
        assert (await _authorize(session, node)).reason is DenyReason.SCHEMA_UNSUPPORTED


@pytest.mark.parametrize("mismatch", [None, "owner", "repository", "invocation", "released", "missing_identity"])
async def test_enforced_policy_checks_actual_issue_claim(session, monkeypatch, mismatch):
    from src.orchestration.work_admission import admit, maintain_worker_claim
    from src.orchestration.work_claims import ClaimOwner, OwnerKind

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    flow, node = await _fixture(session, policy=_policy())
    await admit(
        session,
        org_id=ORG_A,
        repository_id=123,
        issue=int(str(node.issue_ref).lstrip("#")),
        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "different-flow" if mismatch == "owner" else flow.id),
        invocation_id="reserved-run",
    )
    if mismatch == "released":
        await maintain_worker_claim(session, org_id=ORG_A, invocation_id="reserved-run", terminal=True)
    decision = await _authorize(
        session,
        node,
        provider_repository_id=None if mismatch == "missing_identity" else (321 if mismatch == "repository" else 123),
        expected_invocation_id="another-run" if mismatch == "invocation" else "reserved-run",
    )
    assert decision.permitted is (mismatch is None)
    if mismatch:
        assert decision.reason is DenyReason.WORK_NOT_OWNED
