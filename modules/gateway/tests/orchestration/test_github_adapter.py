"""The GitHub-comment input adapter is a first-class, equally-strong door (#4209).

What is asserted here, in order of how much it would hurt to get wrong:

1. **Not a weaker door.** A commenter without `plan:approve` is refused, and a
   commenter from another tenant is refused *without disclosing* whether the node
   exists. These are the adversarial cases: an input adapter that skipped an
   authorization step would be privilege escalation dressed as convenience.
2. **Refusals are recorded** (R-N2b) — for in-org principals, where recording
   discloses nothing. Cross-tenant refusals deliberately write *nothing*.
3. **One decision shape.** The row a comment writes and the row the dashboard
   writes are identical except the input-path field. Asserted by normalising that
   one field and comparing the whole record.
4. **Gate semantics are the state machine's**, not the adapter's: a node that is
   not at a gate is refused by `transition()`, and two concurrent answers produce
   one transition and one decision row.
5. **No deprecation signal** anywhere in the adapter (ruling D-R20).

Authorization is exercised against **real rows** — `users`, `tenant_memberships`,
`user_identities` — through the real `AccessControl`, not a stubbed permission
check. A mocked check would assert that the adapter calls something, not that an
unauthorized commenter is actually refused, and the latter is the property.

Concurrency is modelled as **two attempts that both observed the same prior state**
(what two people answering the same gate produces), rather than OS threads whose
interleaving would make failures flaky rather than informative — the same reasoning
as `test_dispatch.py`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.orchestration.adapters import github_comments as adapter_module
from src.orchestration.adapters.github_comments import (
    GateAnswer,
    GateAnswerStatus,
    InputPath,
    apply_gate_answer,
    as_dashboard_decision,
    build_gate_decision,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

ORG_A = "org-alpha"
ORG_B = "org-beta"

# GitHub numeric account ids (as strings) — never logins, which are renameable.
GH_APPROVER = "100001"  # org_admin in ORG_A: holds plan:approve
GH_MEMBER = "100002"  # member in ORG_A: does NOT hold plan:approve
GH_OUTSIDER = "100003"  # exists only in ORG_B
GH_UNLINKED = "999999"  # no platform identity anywhere


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
    async with factory() as sess:
        yield sess


async def _seed_principal(
    session: AsyncSession,
    *,
    org_id: str,
    github_user_id: str,
    role: str,
) -> str:
    """Create a real user + membership + GitHub identity. Returns `users.id`.

    All three rows are required for the permission check to resolve: the role
    comes from `tenant_memberships` (the DB is the authority for authority), and
    the adapter reaches it through `user_identities`.
    """
    user = User(
        id=f"user-{github_user_id}",
        org_id=org_id,
        team_id=f"team-{org_id}",
        email=f"{github_user_id}@example.test",
        cognito_sub=f"cognito-{github_user_id}",
    )
    session.add(user)
    session.add(TenantMembership(user_id=user.id, tenant_id=org_id, role=role, is_active=True))
    session.add(
        UserIdentity(
            org_id=org_id,
            user_id=user.id,
            team_id=f"team-{org_id}",
            provider="github",
            provider_user_id=github_user_id,
            provider_username=f"login-{github_user_id}",
            verification_method="oauth",
        )
    )
    await session.flush()
    return user.id


async def _seed_gate_node(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    state: str = NodeState.AWAITING_GATE.value,
) -> OrchestrationNode:
    flow = OrchestrationFlow(id=f"flow-{node_id}", org_id=org_id, slug=f"slug-{node_id}", title="A flow")
    session.add(flow)
    node = OrchestrationNode(
        id=node_id,
        org_id=org_id,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref=node_id,
        kind="gate",
        state=state,
        title="A gate",
    )
    session.add(node)
    await session.flush()
    return node


@pytest.fixture
def access(session):
    return AccessControl(db=session)


async def _decisions(session: AsyncSession, org_id: str) -> list[OrchestrationDecision]:
    stmt = select(OrchestrationDecision).where(OrchestrationDecision.org_id == org_id)
    return list((await session.execute(stmt)).scalars().all())


class TestHappyPath:
    """An authorized commenter can answer a gate from GitHub."""

    async def test_approve_moves_the_node_and_records_the_decision(self, session, access):
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-1")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True, reason="looks good"),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.APPLIED
        assert outcome.applied is True

        await session.refresh(node)
        assert node.state == NodeState.PASSED.value

        rows = await _decisions(session, ORG_A)
        assert len(rows) == 1
        assert rows[0].kind == DecisionKind.GATE_APPROVED.value
        assert rows[0].node_id == node.id
        # A gate answer is a HUMAN act — the human-only edges in state.py depend on it.
        assert rows[0].actor_kind == ActorKind.HUMAN.value
        assert rows[0].from_state == NodeState.AWAITING_GATE.value
        assert rows[0].to_state == NodeState.PASSED.value
        # Provenance is on the row, and the operator's reason survives verbatim.
        assert "input-path=github_comment" in rows[0].reason
        assert "looks good" in rows[0].reason

    async def test_reject_moves_the_node_to_rejected_at_gate(self, session, access):
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-2")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=False, reason="needs work"),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.APPLIED
        await session.refresh(node)
        assert node.state == NodeState.REJECTED_AT_GATE.value
        rows = await _decisions(session, ORG_A)
        assert [row.kind for row in rows] == [DecisionKind.GATE_REJECTED.value]

    async def test_the_platform_identity_is_resolved_server_side(self, session, access):
        """The recorded actor is the platform user id, not the GitHub id.

        A comment must never be able to name its own actor: the adapter looks the
        identity up, so the audit row says who this GitHub account *is* on the
        platform.
        """
        user_id = await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-3")

        await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        rows = await _decisions(session, ORG_A)
        assert rows[0].actor_id == user_id
        # The recorded actor is the platform `users.id`, not the raw GitHub id the
        # comment arrived with. (The seeded id embeds the GitHub id, so this is an
        # inequality against the raw value rather than a substring check.)
        assert rows[0].actor_id != GH_APPROVER
        assert rows[0].actor_role == "org_admin"


class TestAuthzIsNotWeakerThanTheDashboard:
    """The adversarial half. These are the tests that matter most."""

    async def test_commenter_without_the_permission_is_refused_and_recorded(self, session, access):
        """A plain member holds no `plan:approve`, so the gate does not move.

        Recorded, not swallowed: an in-org principal attempting an approval they
        do not hold is exactly the off-plan-activity evidence R-N2b exists for.
        """
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_MEMBER, role="member")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-4")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_MEMBER, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_NO_PERMISSION
        assert outcome.applied is False

        await session.refresh(node)
        assert node.state == NodeState.AWAITING_GATE.value, "an unauthorized comment moved the gate"

        rows = await _decisions(session, ORG_A)
        assert len(rows) == 1
        assert rows[0].kind == DecisionKind.TRANSITION_REJECTED.value
        assert "plan:approve" in rows[0].rejection_reason
        assert rows[0].actor_id == "user-" + GH_MEMBER

    async def test_another_orgs_identity_is_refused_with_no_existence_disclosure(self, session, access):
        """A commenter linked only in ORG_B cannot answer an ORG_A gate.

        And cannot learn that the gate exists: the message must be byte-identical
        to the one a *nonexistent* node produces, or commenting becomes a graph
        enumeration oracle.
        """
        await _seed_principal(session, org_id=ORG_B, github_user_id=GH_OUTSIDER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-5")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_OUTSIDER, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_UNKNOWN_IDENTITY

        await session.refresh(node)
        assert node.state == NodeState.AWAITING_GATE.value

        # Nothing written: a decision row for an outsider would itself be a leak
        # into the tenant's audit trail.
        assert await _decisions(session, ORG_A) == []
        assert await _decisions(session, ORG_B) == []

    async def test_cross_org_and_absent_node_are_indistinguishable(self, session, access):
        """The two refusals must not be tellable apart from outside."""
        await _seed_principal(session, org_id=ORG_B, github_user_id=GH_OUTSIDER, role="org_admin")
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-6")

        cross_org = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_OUTSIDER, approve=True),
            access=access,
        )
        absent_node = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id="no-such-node", github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        assert absent_node.status is GateAnswerStatus.REFUSED_NOT_FOUND
        assert cross_org.message == absent_node.message, "the refusal messages differ — an outsider can probe for node existence"

    async def test_unlinked_github_account_is_refused_and_writes_nothing(self, session, access):
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-7")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_UNLINKED, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_UNKNOWN_IDENTITY
        await session.refresh(node)
        assert node.state == NodeState.AWAITING_GATE.value
        assert await _decisions(session, ORG_A) == []

    async def test_identity_linked_in_another_org_does_not_leak_across_tenants(self, session, access):
        """The same GitHub account linked in both orgs acts only in the right one.

        `user_identities` is unique per (provider, provider_user_id, org_id), so
        this is a legitimate state. Resolving without the org filter would let a
        comment act in whichever tenant sorted first.
        """
        await _seed_principal(session, org_id=ORG_B, github_user_id=GH_OUTSIDER, role="org_admin")
        # Same GitHub id, second tenant, deliberately unprivileged there.
        session.add(
            UserIdentity(
                org_id=ORG_A,
                user_id="user-" + GH_OUTSIDER,
                team_id=f"team-{ORG_A}",
                provider="github",
                provider_user_id=GH_OUTSIDER,
                provider_username=f"login-{GH_OUTSIDER}",
                verification_method="oauth",
            )
        )
        await session.flush()
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-8")

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_OUTSIDER, approve=True),
            access=access,
        )

        # The ORG_B membership grants nothing in ORG_A: the scope check in
        # check_permission refuses, and the gate holds.
        assert outcome.status is GateAnswerStatus.REFUSED_NO_PERMISSION
        await session.refresh(node)
        assert node.state == NodeState.AWAITING_GATE.value

    async def test_a_comment_can_never_mint_a_platform_admin(self, session, access):
        """`is_admin` is hardcoded False, so no comment gets platform authority.

        Source-level as well as behavioural: platform admin bypasses every
        `target_org_id` scope check in `check_permission`, so a `True` here would
        make one comment authoritative in every tenant at once.
        """
        source = Path(inspect.getfile(adapter_module)).read_text()
        assert "is_admin=False" in source
        assert "is_admin=True" not in source, "the adapter names is_admin=True — a comment must never mint a platform admin"

        tree = ast.parse(source)
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "TokenContext"):
                continue
            admin_kwargs = [kw for kw in call.keywords if kw.arg == "is_admin"]
            assert admin_kwargs, "TokenContext built without an explicit is_admin — the default must not be relied on"
            for kw in admin_kwargs:
                assert isinstance(kw.value, ast.Constant) and kw.value.value is False, "is_admin must be the literal False, never computed"


class TestOneDecisionShape:
    """A gate answered by comment and by dashboard produce the same record."""

    def test_records_differ_only_in_the_input_path_field(self):
        common = {
            "org_id": ORG_A,
            "flow_id": "flow-1",
            "node_id": "node-1",
            "actor_id": "user-1",
            "actor_role": "org_admin",
            "approve": True,
            "from_state": NodeState.AWAITING_GATE.value,
            "reason": "ship it",
        }
        via_comment = build_gate_decision(input_path=InputPath.GITHUB_COMMENT, **common)
        via_dashboard = build_gate_decision(input_path=InputPath.DASHBOARD, **common)

        assert via_comment != via_dashboard, "the input path must be recorded, or provenance is lost"
        # Normalise the one field that is allowed to differ; everything else must match.
        assert as_dashboard_decision(via_comment) == via_dashboard

    def test_persisted_rows_differ_only_in_the_input_path_marker(self):
        common = {
            "org_id": ORG_A,
            "flow_id": "flow-1",
            "node_id": "node-1",
            "actor_id": "user-1",
            "actor_role": "org_admin",
            "approve": True,
            "from_state": NodeState.AWAITING_GATE.value,
            "reason": "ship it",
        }
        comment_kwargs = build_gate_decision(input_path=InputPath.GITHUB_COMMENT, **common).to_append_kwargs()
        dashboard_kwargs = build_gate_decision(input_path=InputPath.DASHBOARD, **common).to_append_kwargs()

        assert comment_kwargs["reason"] != dashboard_kwargs["reason"]
        differing = {key for key in comment_kwargs if comment_kwargs[key] != dashboard_kwargs[key]}
        assert differing == {"reason"}, f"rows differ in more than the provenance-carrying field: {differing}"

    def test_both_paths_record_the_same_kind_and_actor_kind(self):
        for path in InputPath:
            record = build_gate_decision(
                org_id=ORG_A,
                flow_id="flow-1",
                node_id="node-1",
                actor_id="user-1",
                actor_role="org_admin",
                input_path=path,
                approve=True,
                from_state=NodeState.AWAITING_GATE.value,
            )
            assert record.kind is DecisionKind.GATE_APPROVED
            assert record.actor_kind is ActorKind.HUMAN

    async def test_the_adapter_writes_the_shape_build_gate_decision_produces(self, session, access):
        """The persisted row matches the shared builder, not a second construction."""
        user_id = await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-9")

        await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True, reason="ok"),
            access=access,
        )

        expected = build_gate_decision(
            org_id=ORG_A,
            flow_id=node.flow_id,
            node_id=node.id,
            actor_id=user_id,
            actor_role="org_admin",
            input_path=InputPath.GITHUB_COMMENT,
            approve=True,
            from_state=NodeState.AWAITING_GATE.value,
            reason="ok",
        ).to_append_kwargs()

        rows = await _decisions(session, ORG_A)
        actual = {
            "org_id": rows[0].org_id,
            "flow_id": rows[0].flow_id,
            "node_id": rows[0].node_id,
            "kind": rows[0].kind,
            "actor_id": rows[0].actor_id,
            "actor_role": rows[0].actor_role,
            "actor_kind": rows[0].actor_kind,
            "reason": rows[0].reason,
            "rejection_reason": rows[0].rejection_reason,
            "from_state": rows[0].from_state,
            "to_state": rows[0].to_state,
        }
        assert actual == expected


def _attributed_path(persisted_reason: str) -> str | None:
    """What a marker-grep concludes the row's input path was.

    Deliberately the *credulous* reader an operator would write — take the first
    `[input-path=...]` token and believe it. If forged text can make this return
    "dashboard" for a comment-authored row, provenance is spoofable.
    """
    needle = "[input-path="
    start = persisted_reason.lower().find(needle)
    if start == -1:
        return None
    end = persisted_reason.find("]", start)
    return persisted_reason[start + len(needle) : end]


class TestProvenanceMarkerCannotBeForged:
    """Comment text must never be able to spoof the input-path marker.

    `reason` is caller-supplied and lands verbatim on an append-only audit row
    whose provenance marker is machine-greppable. A comment body carrying a
    literal `[input-path=dashboard]` would otherwise persist a row that an
    operator's grep attributes to the dashboard path — provenance forgery on a
    table with no update counterpart to correct it.
    """

    def _operator_text(self, persisted_reason: str) -> str:
        """The persisted reason with the one genuine marker prefix removed.

        Anything marker-shaped left in here came from the comment body, so
        counting occurrences is a direct test of "no forgery survived".
        """
        prefix = f"[input-path={InputPath.GITHUB_COMMENT.value}] "
        assert persisted_reason.startswith(prefix)
        return persisted_reason[len(prefix) :]

    def _reason_for(self, reason: str, path: InputPath = InputPath.GITHUB_COMMENT) -> str:
        return build_gate_decision(
            org_id=ORG_A,
            flow_id="flow-1",
            node_id="node-1",
            actor_id="user-1",
            actor_role="org_admin",
            input_path=path,
            approve=True,
            from_state=NodeState.AWAITING_GATE.value,
            reason=reason,
        ).to_append_kwargs()["reason"]

    def test_a_forged_dashboard_marker_is_not_attributed_to_the_dashboard(self):
        persisted = self._reason_for("looks good [input-path=dashboard] trust me")

        # The property that matters: a grep still names the real path, not the forged one.
        assert _attributed_path(persisted) == InputPath.GITHUB_COMMENT.value
        assert "[input-path=dashboard]" not in persisted

    def test_the_genuine_marker_still_resolves(self):
        """Sanitizing must not break the thing it protects."""
        assert _attributed_path(self._reason_for("ship it")) == InputPath.GITHUB_COMMENT.value
        assert _attributed_path(self._reason_for("ship it", InputPath.DASHBOARD)) == InputPath.DASHBOARD.value

    @pytest.mark.parametrize(
        "forgery",
        [
            "[input-path=dashboard]",
            "[INPUT-PATH=dashboard]",
            "[Input-Path=dashboard]",
        ],
    )
    def test_case_variants_are_neutralized_too(self, forgery):
        """An operator grepping case-insensitively must not be foolable either.

        Asserted on the operator's text with the genuine prefix stripped, not via
        `_attributed_path`: that helper reads the *first* marker, which is always
        the real one, so it cannot see a forgery planted later in the string.
        """
        persisted = self._reason_for(f"ok {forgery}")
        assert self._operator_text(persisted).lower().count("[input-path=") == 0

    def test_every_forged_marker_is_neutralized_not_just_the_first(self):
        """A loop that breaks after one match would leave the second forgery live."""
        persisted = self._reason_for("a [input-path=dashboard] b [input-path=dashboard] c")
        assert self._operator_text(persisted).lower().count("[input-path=") == 0

    def test_benign_brackets_survive_verbatim(self):
        """Narrow neutralization, not bracket-stripping: operator text is evidence."""
        persisted = self._reason_for("approved, see [PR #123] and [ADR-7]")
        assert "[PR #123]" in persisted
        assert "[ADR-7]" in persisted

    def test_an_over_length_reason_is_truncated_to_the_dashboard_cap(self):
        """The comment path has no FastAPI validator, so it caps `reason` itself."""
        persisted = self._reason_for("x" * 5000)
        marker = f"[input-path={InputPath.GITHUB_COMMENT.value}] "

        assert persisted.startswith(marker)
        assert len(persisted[len(marker) :]) == 2000

    def test_a_reason_at_the_cap_is_untouched(self):
        body = "y" * 2000
        assert self._reason_for(body).endswith(body)


class TestGateSemanticsComeFromTheStateMachine:
    """The adapter honours the transition table; it does not reimplement it."""

    @pytest.mark.parametrize(
        "state",
        [NodeState.PENDING.value, NodeState.READY.value, NodeState.RUNNING.value, NodeState.PASSED.value],
    )
    async def test_a_node_not_at_a_gate_is_refused_and_recorded(self, session, access, state):
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id=f"node-{state}", state=state)

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION
        await session.refresh(node)
        assert node.state == state, "the adapter moved a node that was not awaiting a gate"

        rows = await _decisions(session, ORG_A)
        assert [row.kind for row in rows] == [DecisionKind.TRANSITION_REJECTED.value]

    async def test_a_running_node_cannot_be_passed_by_a_comment(self, session, access):
        """Regression: `running -> passed` is legal for a HUMAN, but not as a *gate answer*.

        That edge exists so a green **evaluation** can promote a node that needs no
        gate. It is reachable by a HUMAN actor, so delegating the whole decision to
        `transition()` would let a comment mark work that is still in flight as
        passed with no gate ever raised — an approval of something nobody was asked
        to approve. The adapter narrows which edge it may request; this pins it.
        """
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-inflight", state=NodeState.RUNNING.value)

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION
        await session.refresh(node)
        assert node.state == NodeState.RUNNING.value, "a comment promoted in-flight work past a gate that was never raised"

    async def test_two_answers_to_the_same_gate_produce_one_transition(self, session, access):
        """Both callers observed `awaiting_gate`; the second must lose cleanly.

        The conditional UPDATE is the guard. Without it the second answer would
        overwrite the first and the audit trail would claim the gate was answered
        twice with different outcomes.
        """
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-race")

        first = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )
        # A second answer that still believes the node is awaiting a gate. Its
        # `transition()` call is legal (it observes the stale state), so what
        # stops it is the state-conditional UPDATE.
        second = await adapter_module._gate_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            observed_state=NodeState.AWAITING_GATE.value,
            approve=False,
            reason="stale view",
        )

        assert first.status is GateAnswerStatus.APPLIED
        rows_affected, allowed, _ = second
        assert allowed is True and rows_affected == 0, "a stale second answer rewrote the gate"

        await session.refresh(node)
        assert node.state == NodeState.PASSED.value
        assert len(await _decisions(session, ORG_A)) == 1

    async def test_a_gate_answered_mid_flight_reports_already_answered(self, session, access, monkeypatch):
        """The lost-race path, driven through the public entry point.

        The interleaving is forced at the one instant that matters — after the
        adapter has read the node as `awaiting_gate` but before its UPDATE lands —
        by having the (real) `transition()` call move the row first. Everything
        under test stays real: the state-conditional UPDATE is what matches 0 rows,
        and the assertion is that this surfaces as `ALREADY_ANSWERED` with **no
        second decision row**, because a second row would make the audit trail
        claim one gate was answered twice.
        """
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-midflight")

        real_transition = adapter_module.transition
        landed_first: list[bool] = []

        def _transition_after_someone_else_answers(*args, **kwargs):
            if not landed_first:
                landed_first.append(True)
                # Another approver's answer lands between our read and our write.
                node.state = NodeState.PASSED.value
                session.add(node)
            return real_transition(*args, **kwargs)

        monkeypatch.setattr(adapter_module, "transition", _transition_after_someone_else_answers)

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        assert outcome.status is GateAnswerStatus.ALREADY_ANSWERED
        assert outcome.applied is False
        assert outcome.decision is None
        await session.refresh(node)
        assert node.state == NodeState.PASSED.value
        assert await _decisions(session, ORG_A) == [], "the losing answer wrote a decision row for a gate it did not move"

    async def test_already_answered_is_reported_without_a_second_decision_row(self, session, access):
        await _seed_principal(session, org_id=ORG_A, github_user_id=GH_APPROVER, role="org_admin")
        node = await _seed_gate_node(session, org_id=ORG_A, node_id="node-answered", state=NodeState.PASSED.value)

        outcome = await apply_gate_answer(
            session,
            GateAnswer(org_id=ORG_A, node_id=node.id, github_user_id=GH_APPROVER, approve=True),
            access=access,
        )

        # `passed -> passed` is not in the table, so this surfaces as an illegal
        # transition rather than silently succeeding.
        assert outcome.status is GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION
        await session.refresh(node)
        assert node.state == NodeState.PASSED.value


class TestOneGuardedWriteSeam:
    """Source-level, mirroring `test_dispatch.py`: one seam, always guarded."""

    @staticmethod
    def _tree() -> ast.Module:
        return ast.parse(Path(inspect.getfile(adapter_module)).read_text())

    @staticmethod
    def _called_names(node: ast.AST) -> set[str]:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_every_function_that_updates_state_also_calls_transition(self):
        offenders = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            called = self._called_names(node)
            if "update" in called and "transition" not in called:
                offenders.append(node.name)
        assert offenders == [], f"these functions write node state without consulting transition(): {offenders}"

    def test_transition_is_imported_from_the_single_declared_module(self):
        """R-N2a: one vocabulary, one guard — no local copy of the table."""
        sources = {
            (node.level, node.module)
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom) and any(alias.name == "transition" for alias in node.names)
        }
        assert sources == {(2, "state")}, f"transition() must come from ..state; got {sources}"

    def test_no_raw_sql_in_the_module(self):
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
                pytest.fail("the adapter uses sqlalchemy text() — raw SQL can bypass the transition guard")

    def test_the_adapter_never_records_a_service_actor_for_a_gate_answer(self):
        """A gate answer is a human act; SERVICE here would be the AC-15 gate skip."""
        source = Path(inspect.getfile(adapter_module)).read_text()
        assert "ActorKind.SERVICE" not in source, "the adapter names ActorKind.SERVICE — a gate answer is always a human act"


class TestAdditiveOnly:
    """AC-27 / D-R20: the GitHub path is not deprecated and not degraded.

    Every check here inspects **executable code**, never docstrings or comments.
    That distinction is the point rather than a convenience: this module's
    documentation must be free to *discuss* legacy mode — it is a supported product
    mode and explaining it is how a future contributor learns not to remove it —
    while the running code must emit no signal that the path is going away. A
    whole-file substring scan would conflate the two and force the docs to go
    quiet, which is the opposite of what ruling D-R20 wants.
    """

    @staticmethod
    def _runtime_strings() -> list[str]:
        """Every string literal in the module that is NOT a docstring."""
        tree = ast.parse(Path(inspect.getfile(adapter_module)).read_text())
        docstring_ids = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstring_ids.add(id(first.value))
        return [
            literal.value
            for literal in ast.walk(tree)
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and id(literal) not in docstring_ids
        ]

    @staticmethod
    def _called_attribute_paths() -> set[str]:
        """Dotted names of everything called in the module, e.g. `re.compile`."""
        tree = ast.parse(Path(inspect.getfile(adapter_module)).read_text())
        paths = set()
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            parts, node = [], call.func
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            if parts:
                paths.add(".".join(reversed(parts)))
        return paths

    def test_no_deprecation_signal_in_runtime_code(self):
        """No warning call, no "deprecated"/"legacy" string the user could see."""
        source = Path(inspect.getfile(adapter_module)).read_text()
        assert "DeprecationWarning" not in source
        assert "warnings.warn" not in source

        for literal in self._runtime_strings():
            lowered = literal.lower()
            for signal in ("deprecat", "legacy", "will be removed", "sunset", "no longer supported"):
                assert signal not in lowered, f"runtime string signals the GitHub path is going away ({signal!r}): {literal!r}"

    def test_no_deprecation_decorator_or_warning_import(self):
        tree = ast.parse(Path(inspect.getfile(adapter_module)).read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert "warnings" not in imported, "the adapter imports `warnings` — the GitHub path must emit no deprecation signal"

    def test_the_adapter_does_not_render_trackers_or_parse_comment_markup(self):
        """Scope guard for AC-27, enforced rather than asserted in prose.

        Tracker rendering, comment parsing, sentinel handling and branch artifacts
        belong to the GitHub-driven flow. If this module ever starts doing any of
        them, "no observable change" stops being provable by reading the diff.

        Checked as *calls and imports* rather than raw text, so the docstring may
        name these concerns in order to declare them out of scope.
        """
        called = self._called_attribute_paths()
        for forbidden in ("re.compile", "re.match", "re.search", "requests.get", "requests.post", "httpx.get", "httpx.post"):
            assert forbidden not in called, f"the adapter calls {forbidden!r} — parsing/HTTP belong to the GitHub flow, not the engine"

        tree = ast.parse(Path(inspect.getfile(adapter_module)).read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        for forbidden in ("re", "requests", "httpx", "github"):
            assert forbidden not in imported, f"the adapter imports {forbidden!r} — comment parsing and GitHub I/O are the caller's concern"

        # No HTML/markdown comment markers in runtime strings: those would mean the
        # module is rendering or matching a tracker block.
        for literal in self._runtime_strings():
            assert "<!--" not in literal, f"runtime string contains a comment marker — tracker rendering is out of scope: {literal!r}"

    def test_the_input_path_is_never_read_as_authority(self):
        """Branching on the input path would recreate the second-class path.

        The permission check must not be reachable only on one branch, so
        `input_path` must never appear in a conditional test.
        """
        tree = ast.parse(Path(inspect.getfile(adapter_module)).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.If | ast.IfExp):
                continue
            names = {sub.id for sub in ast.walk(node.test) if isinstance(sub, ast.Name)}
            attrs = {sub.attr for sub in ast.walk(node.test) if isinstance(sub, ast.Attribute)}
            assert "input_path" not in names | attrs, "the adapter branches on input_path — one path would become weaker than the other"

    def test_both_input_paths_are_declared_first_class(self):
        """Neither member is marked as a fallback in the vocabulary."""
        assert set(InputPath) == {InputPath.DASHBOARD, InputPath.GITHUB_COMMENT}
        assert InputPath.GITHUB_COMMENT.value == "github_comment"
