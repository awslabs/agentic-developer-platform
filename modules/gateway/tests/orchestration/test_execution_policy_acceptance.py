"""Tests for accepting an execution policy onto a plan of record (#5128).

`test_execution_policy.py` covers the schema and the pure decision rule. This file
covers the *acceptance path* — the part that needs a database, because the
properties under test are about what lands in the plan store and what does not:

  - **Legacy semantics are preserved.** A plan with no policy compiles exactly as
    it did before this field existed. This is the regression that matters most: the
    field is opt-in, so every existing flow must be untouched.
  - **A model may propose but never accept.** The draft-registration path compiles
    with `ActorKind.SERVICE`, and it must not be the way an agent gets a policy
    accepted.
  - **Server-stamped provenance.** The accepted document carries an id, hash and
    principal the caller did not supply and cannot influence.
  - **Retries stay idempotent.** The reason the stamped id is content-derived: a
    resubmitted identical document must remain a no-op rather than becoming a 409.
  - **Amendment produces a new accepted version** carrying the amended policy,
    while the prior version stays queryable.

The session fixture is `test_compile.py`'s, including both `event.listens_for`
hooks — they are required for `begin_nested()` to be a real SAVEPOINT under
pysqlite, and without them the atomicity assertions here would pass for the wrong
reason. See that file's fixture docstring.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.compile import (
    HASH_EXCLUDED_FIELDS,
    ApprovalContext,
    PolicyNotAcceptableError,
    ProposalRejectedError,
    compile_proposal,
    plan_hash,
)
from src.orchestration.execution_policy import (
    AcceptanceMode,
    Action,
    ExecutionPolicy,
    PolicyLimits,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode
from src.orchestration.state import ActorKind
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "demo-flow"
SPEC_REVISION = "issue-4120-r1"
REPO_A = "repo-alpha"
ENV_A = "conn-env-alpha"
HUMAN = "cognito-sub-123"
EXPIRY = datetime(2026, 12, 31, tzinfo=UTC)


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs (see module docstring)."""
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


@pytest.fixture
def approval():
    """A human acceptance context. `org_id` and `actor_id` are authoritative."""
    return ApprovalContext(
        org_id=ORG_A,
        actor_id=HUMAN,
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN,
        reason="Reviewed the plan and the authority it delegates.",
    )


@pytest.fixture
def service_approval():
    """A SERVICE acceptance context — the draft-registration path's shape.

    `draft_routes.py` builds exactly this so a registering agent's decision row
    cannot root a dispatch. It must equally not be able to accept a policy.
    """
    return ApprovalContext(
        org_id=ORG_A,
        actor_id="agent-principal",
        actor_role="member",
        actor_kind=ActorKind.SERVICE,
        reason="Registering a drafted plan.",
    )


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def a_policy(**overrides: object) -> ExecutionPolicy:
    base: dict[str, object] = {
        "org_id": ORG_A,
        "repository_ids": [REPO_A],
        "environment_connection_ids": [ENV_A],
        "allowed_actions": [Action.DEVELOP, Action.REVIEW, Action.EVALUATE],
        "evaluation_acceptance": {address("eval"): AcceptanceMode.MACHINE},
        "expires_at": EXPIRY,
        "limits": PolicyLimits(
            max_wall_clock_seconds=3600,
            max_spend_usd=Decimal("50.00"),
            max_attempts_per_node=3,
            max_concurrent_actions=4,
        ),
    }
    base.update(overrides)
    return ExecutionPolicy(**base)  # type: ignore[arg-type]


def valid_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """A well-formed two-wave proposal, matching `test_compile.py`'s shape."""
    payload = {
        "flow_slug": FLOW,
        "title": "Demo flow",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4196"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
            ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Human gate"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("gate", wave="wave-2")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


async def accepted_plan(session: AsyncSession, *, version: int | None = None) -> OrchestrationAcceptedPlan:
    stmt = sa.select(OrchestrationAcceptedPlan)
    if version is not None:
        stmt = stmt.where(OrchestrationAcceptedPlan.version == version)
    return (await session.execute(stmt.order_by(OrchestrationAcceptedPlan.version.desc()))).scalars().first()


async def count_rows(session: AsyncSession, model) -> int:
    return (await session.execute(sa.select(sa.func.count()).select_from(model.__table__))).scalar_one()


# ---------------------------------------------------------------------------
# Legacy semantics: the regression that matters most
# ---------------------------------------------------------------------------


class TestMissingPolicyPreservesLegacySemantics:
    """A plan with no policy behaves exactly as it did before this field existed.

    The issue's explicit requirement, and the reason the field is opt-in.
    """

    async def test_plan_without_a_policy_compiles(self, session: AsyncSession, approval: ApprovalContext) -> None:
        result = await compile_proposal(session, valid_proposal(), approval)
        assert result.nodes_created == 3
        assert result.plan_version == 1

    async def test_accepted_document_records_no_policy(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """`None`, not a defaulted permissive or restrictive policy.

        A default either silently widens every existing flow on deploy or silently
        halts them all; both are worse than absence.
        """
        await compile_proposal(session, valid_proposal(), approval)
        plan = await accepted_plan(session)
        assert plan.plan_document["execution_policy"] is None

    def test_hash_of_a_policyless_document_is_unchanged_by_the_new_field(self) -> None:
        """A plan authored before this field existed hashes as it always did.

        **The load-bearing backward-compatibility claim, and it is not free.**
        `model_dump` emits `"execution_policy": null` for a legacy plan, and that key
        alone changes the canonical JSON — so without the omission in `plan_hash`
        every pre-#5128 plan would hash differently after deploy and an in-flight
        fail-soft retry would be refused 409 as a plan-of-record rewrite (the #4885
        failure mode).

        Pinned against a hash recomputed here from a document with the key removed —
        i.e. what the pre-#5128 code would have hashed. Comparing an absent field
        against an explicit `None` would be vacuous: pydantic normalises those to the
        same document, so such a test passes even when the bug is present.
        """
        proposal = valid_proposal()
        legacy_document = {k: v for k, v in proposal.model_dump(mode="json", exclude=HASH_EXCLUDED_FIELDS).items() if k != "execution_policy"}
        legacy_hash = hashlib.sha256(json.dumps(legacy_document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        assert plan_hash(proposal) == legacy_hash

    def test_a_policy_bearing_document_does_hash_its_policy(self) -> None:
        """The omission above must not become "policies are excluded from identity".

        Guards the obvious over-fix: dropping the key unconditionally would restore
        legacy hashes *and* silently discard every policy amendment as a retry.
        """
        assert plan_hash(valid_proposal()) != plan_hash(valid_proposal(execution_policy=a_policy()))


# ---------------------------------------------------------------------------
# A model may propose, but may never accept
# ---------------------------------------------------------------------------


class TestOnlyAHumanAccepts:
    async def test_service_actor_cannot_accept_a_policy(self, session: AsyncSession, service_approval: ApprovalContext) -> None:
        """The draft-registration path's actor kind, refused.

        If a SERVICE compile could stamp a policy, an agent would be authorizing
        its own autonomous actions — the authority model inverted in one call.
        """
        with pytest.raises(PolicyNotAcceptableError, match="cannot accept an execution policy"):
            await compile_proposal(
                session,
                valid_proposal(execution_policy=a_policy()),
                service_approval,
                decision_kind=DecisionKind.PLAN_DRAFTED,
            )

    async def test_refused_service_acceptance_writes_nothing(self, session: AsyncSession, service_approval: ApprovalContext) -> None:
        """The refusal happens before any savepoint opens, so no rows land.

        A partial write here would leave nodes on the graph for a policy nobody
        accepted, which is the failure the atomicity design exists to prevent.
        """
        with pytest.raises(PolicyNotAcceptableError):
            await compile_proposal(
                session,
                valid_proposal(execution_policy=a_policy()),
                service_approval,
                decision_kind=DecisionKind.PLAN_DRAFTED,
            )
        assert await count_rows(session, OrchestrationNode) == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0

    async def test_service_actor_may_still_register_a_policyless_draft(self, session: AsyncSession, service_approval: ApprovalContext) -> None:
        """Propose-but-not-accept, demonstrated: the draft path still works.

        Proves the refusal above is scoped to the policy rather than breaking
        draft registration outright.
        """
        result = await compile_proposal(
            session,
            valid_proposal(),
            service_approval,
            decision_kind=DecisionKind.PLAN_DRAFTED,
        )
        assert result.nodes_created == 3

    async def test_refusal_is_catchable_as_a_rejected_proposal(self, session: AsyncSession, service_approval: ApprovalContext) -> None:
        """Subclassing is what gives the routes their 422 without a new handler."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, valid_proposal(execution_policy=a_policy()), service_approval)


# ---------------------------------------------------------------------------
# Server-stamped provenance
# ---------------------------------------------------------------------------


class TestAcceptanceStampsProvenance:
    async def test_accepted_policy_carries_a_server_stamped_id_and_hash(self, session: AsyncSession, approval: ApprovalContext) -> None:
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        stored = (await accepted_plan(session)).plan_document["execution_policy"]
        assert stored["policy_id"].startswith("pol_")
        assert len(stored["policy_hash"]) == 64

    async def test_principal_is_the_resolved_acceptor(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """Attribution comes from the acceptance context, not the document."""
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        stored = (await accepted_plan(session)).plan_document["execution_policy"]
        assert stored["principal_id"] == HUMAN

    async def test_caller_supplied_principal_is_refused(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """A policy that could name its own principal could name any principal."""
        with pytest.raises(PolicyNotAcceptableError, match="server-stamped"):
            await compile_proposal(
                session,
                valid_proposal(execution_policy=a_policy(principal_id="user-someone-else")),
                approval,
            )

    async def test_caller_supplied_policy_id_is_refused(self, session: AsyncSession, approval: ApprovalContext) -> None:
        with pytest.raises(PolicyNotAcceptableError, match="server-stamped"):
            await compile_proposal(session, valid_proposal(execution_policy=a_policy(policy_id="pol_attacker")), approval)

    async def test_policy_declaring_another_tenant_is_refused(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """Compared, never substituted — the plan document's own rule, restated.

        Note the *proposal* declares the correct org here, so this isolates the
        policy's tenant check rather than re-testing `TenantMismatchError`.
        """
        with pytest.raises(PolicyNotAcceptableError, match="never re-homed"):
            await compile_proposal(session, valid_proposal(execution_policy=a_policy(org_id=ORG_B)), approval)

    async def test_stamp_is_inside_the_hashed_document(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """The plan's stored hash covers the stamped policy, not the submitted one.

        Ordering proof: stamping runs before `plan_hash`. Were it the other way
        round, the store would hold a document whose hash was computed from a
        different document than the one persisted.
        """
        submitted = valid_proposal(execution_policy=a_policy())
        result = await compile_proposal(session, submitted, approval)
        plan = await accepted_plan(session)
        assert plan.plan_hash == result.plan_hash
        # The submitted document was unstamped, so its hash must differ from the
        # stored one — otherwise the stamp is not inside the hash.
        assert plan_hash(submitted) != plan.plan_hash


# ---------------------------------------------------------------------------
# The retry property the derived id exists to protect
# ---------------------------------------------------------------------------


class TestPolicyAcceptanceIsIdempotent:
    async def test_resubmitting_an_identical_policy_is_a_no_op(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """**The regression the content-derived id prevents.**

        A minted-per-acceptance id would make this second call hash differently,
        miss the idempotency return, and be refused as a plan-of-record rewrite —
        turning a dropped connection into a permanent failure.
        """
        first = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        second = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        assert second.already_compiled
        assert second.plan_version == first.plan_version
        assert second.plan_hash == first.plan_hash

    async def test_retry_does_not_write_a_second_plan_version(self, session: AsyncSession, approval: ApprovalContext) -> None:
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_a_second_acceptor_is_a_re_acceptance_not_a_retry(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """Attribution cannot be laundered by resubmitting someone else's policy.

        Idempotency is scoped to *the same* acceptor on purpose. `principal_id` is
        stamped into the hashed document, so a different human submitting identical
        policy content produces a new accepted version bound to them — their
        acceptance is recorded rather than collapsing into the first operator's
        grant.

        Note this is not in tension with `policy_hash` excluding the principal:
        that hash identifies the policy *content* (so a reviewer can see two
        versions authorize the same thing), while the plan hash identifies the
        accepted document, authority and attribution included.
        """
        first = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        other = ApprovalContext(org_id=ORG_A, actor_id="cognito-sub-999", actor_role="org_admin", actor_kind=ActorKind.HUMAN)
        second = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), other)

        assert not second.already_compiled
        assert second.plan_version == first.plan_version + 1
        assert (await accepted_plan(session, version=2)).plan_document["execution_policy"]["principal_id"] == "cognito-sub-999"

    async def test_re_acceptance_preserves_the_content_identity_of_the_policy(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """The flip side: the two versions authorize demonstrably the same thing.

        Without this, "who accepted" and "what was accepted" would be
        indistinguishable in the audit trail — a re-acceptance would look like a
        scope change.
        """
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        other = ApprovalContext(org_id=ORG_A, actor_id="cognito-sub-999", actor_role="org_admin", actor_kind=ActorKind.HUMAN)
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), other)

        v1 = (await accepted_plan(session, version=1)).plan_document["execution_policy"]
        v2 = (await accepted_plan(session, version=2)).plan_document["execution_policy"]
        assert v1["policy_hash"] == v2["policy_hash"]
        assert v1["policy_id"] == v2["policy_id"]

    async def test_widened_policy_is_not_mistaken_for_a_retry(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """**Why the policy is inside `plan_hash` at all.**

        Were it excluded like `design_history`, this widened submission would hash
        identically to the narrower plan in force, hit the idempotency return, and
        be silently discarded — the widening would appear to succeed and have no
        effect.
        """
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        widened = a_policy(allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.EVALUATE, Action.MERGE])
        second = await compile_proposal(session, valid_proposal(execution_policy=widened), approval)
        assert not second.already_compiled
        assert second.plan_hash != (await accepted_plan(session, version=1)).plan_hash

    async def test_raised_spend_cap_is_not_mistaken_for_a_retry(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """The same property for a limit rather than an action list."""
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        richer = a_policy(
            limits=PolicyLimits(
                max_wall_clock_seconds=3600,
                max_spend_usd=Decimal("5000.00"),
                max_attempts_per_node=3,
                max_concurrent_actions=4,
            )
        )
        assert not (await compile_proposal(session, valid_proposal(execution_policy=richer), approval)).already_compiled


# ---------------------------------------------------------------------------
# Amendment: a new accepted version carries the amended policy
# ---------------------------------------------------------------------------


class TestAmendmentProducesANewAcceptedVersion:
    @pytest.fixture
    def amender(self):
        return AmendmentContext(
            org_id=ORG_A,
            actor_id=HUMAN,
            actor_role="org_admin",
            actor_kind=ActorKind.HUMAN,
            reason="Widening the repository scope after review.",
        )

    async def test_amendment_stamps_the_new_policy(self, session: AsyncSession, approval: ApprovalContext, amender: AmendmentContext) -> None:
        compiled = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        amended = a_policy(repository_ids=[REPO_A, "repo-beta"])
        result = await amend_plan(session, compiled.flow_id, valid_proposal(execution_policy=amended), amender)

        assert result.plan_version == 2
        assert result.superseded_version == 1
        stored = (await accepted_plan(session, version=2)).plan_document["execution_policy"]
        assert stored["repository_ids"] == [REPO_A, "repo-beta"]
        assert stored["policy_id"].startswith("pol_")

    async def test_prior_version_stays_queryable_with_its_original_policy(
        self, session: AsyncSession, approval: ApprovalContext, amender: AmendmentContext
    ) -> None:
        """ "What was authorized at that gate" survives any number of amendments."""
        compiled = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        await amend_plan(
            session,
            compiled.flow_id,
            valid_proposal(execution_policy=a_policy(repository_ids=[REPO_A, "repo-beta"])),
            amender,
        )
        original = (await accepted_plan(session, version=1)).plan_document["execution_policy"]
        assert original["repository_ids"] == [REPO_A]

    async def test_amending_to_the_identical_policy_is_a_no_op(
        self, session: AsyncSession, approval: ApprovalContext, amender: AmendmentContext
    ) -> None:
        compiled = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        result = await amend_plan(session, compiled.flow_id, valid_proposal(execution_policy=a_policy()), amender)
        assert result.already_amended
        assert result.plan_version == 1

    async def test_amendment_cannot_be_used_to_accept_as_a_service(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """**The softer door, closed.**

        The amendment path shares `accept_execution_policy` rather than
        reimplementing it, so a SERVICE actor cannot get a policy accepted by
        amending instead of submitting. A second acceptance rule here would have
        been free to accept what the compile path refuses.
        """
        compiled = await compile_proposal(session, valid_proposal(), approval)
        service_amender = AmendmentContext(
            org_id=ORG_A,
            actor_id="agent-principal",
            actor_role="member",
            actor_kind=ActorKind.SERVICE,
            reason="Agent-initiated amendment.",
        )
        with pytest.raises(PolicyNotAcceptableError, match="cannot accept an execution policy"):
            await amend_plan(session, compiled.flow_id, valid_proposal(execution_policy=a_policy()), service_amender)

    async def test_amendment_may_add_a_policy_to_a_legacy_plan(
        self, session: AsyncSession, approval: ApprovalContext, amender: AmendmentContext
    ) -> None:
        """The migration path for an existing flow: amend to adopt a policy."""
        compiled = await compile_proposal(session, valid_proposal(), approval)
        result = await amend_plan(session, compiled.flow_id, valid_proposal(execution_policy=a_policy()), amender)
        assert result.plan_version == 2
        assert (await accepted_plan(session, version=2)).plan_document["execution_policy"] is not None

    async def test_amendment_may_remove_a_policy(self, session: AsyncSession, approval: ApprovalContext, amender: AmendmentContext) -> None:
        """Revocation by amendment: a new version with no policy.

        Reachable on purpose — an owner who wants to withdraw delegated authority
        should not have to author a policy that permits nothing.
        """
        compiled = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        result = await amend_plan(session, compiled.flow_id, valid_proposal(), amender)
        assert result.plan_version == 2
        assert (await accepted_plan(session, version=2)).plan_document["execution_policy"] is None


# ---------------------------------------------------------------------------
# Malformed policy -> 422, per the issue's API contract
# ---------------------------------------------------------------------------


class TestMalformedPolicyIsRejectedAtTheBoundary:
    def test_unbounded_policy_cannot_be_put_in_a_proposal(self) -> None:
        """422 at parse: the request body never becomes a `LoopProposal`.

        Asserted at the model rather than through the route because that is where
        FastAPI's validation happens — the route is the same code path for any
        malformed body.
        """
        with pytest.raises(Exception, match="max_spend_usd|validation"):
            LoopProposal(
                flow_slug=FLOW,
                title="Demo",
                org_id=ORG_A,
                spec_revision=SPEC_REVISION,
                execution_policy={  # type: ignore[arg-type]
                    "org_id": ORG_A,
                    "repository_ids": [REPO_A],
                    "allowed_actions": ["develop"],
                    "expires_at": EXPIRY.isoformat(),
                    "limits": {
                        "max_wall_clock_seconds": 60,
                        "max_spend_usd": 0,
                        "max_attempts_per_node": 1,
                        "max_concurrent_actions": 1,
                    },
                },
            )

    def test_unknown_action_cannot_be_put_in_a_proposal(self) -> None:
        with pytest.raises(Exception, match="allowed_actions|validation"):
            LoopProposal(
                flow_slug=FLOW,
                title="Demo",
                org_id=ORG_A,
                spec_revision=SPEC_REVISION,
                execution_policy={  # type: ignore[arg-type]
                    "org_id": ORG_A,
                    "repository_ids": [REPO_A],
                    "allowed_actions": ["exfiltrate"],
                    "expires_at": EXPIRY.isoformat(),
                    "limits": {
                        "max_wall_clock_seconds": 60,
                        "max_spend_usd": 1,
                        "max_attempts_per_node": 1,
                        "max_concurrent_actions": 1,
                    },
                },
            )


# ---------------------------------------------------------------------------
# The policy is readable back off the accepted plan
# ---------------------------------------------------------------------------


class TestAcceptedPolicyRoundTrips:
    async def test_stored_document_parses_back_to_an_equal_policy(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """The accepted document is self-describing: no join needed to read it.

        This is what `dispatch_pass` and #5122 will rely on, so a serialisation
        that could not round-trip would break enforcement rather than just
        reporting.
        """
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        stored = (await accepted_plan(session)).plan_document["execution_policy"]
        reparsed = ExecutionPolicy.model_validate(stored)
        assert reparsed.policy_id == stored["policy_id"]
        assert reparsed.principal_id == HUMAN
        assert reparsed.allowed_actions == [Action.DEVELOP, Action.REVIEW, Action.EVALUATE]
        assert reparsed.limits.max_spend_usd == Decimal("50.00")

    async def test_expiry_survives_the_round_trip(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """Expiry is what bounds the grant's lifetime, so it must not drift.

        A timezone lost in serialisation would make every expiry comparison in
        `authorize_action` wrong by the offset.
        """
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        stored = (await accepted_plan(session)).plan_document["execution_policy"]
        assert ExecutionPolicy.model_validate(stored).expires_at == EXPIRY

    async def test_evaluation_acceptance_survives_the_round_trip(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """The mapping that decides machine acceptance must survive verbatim."""
        await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        stored = (await accepted_plan(session)).plan_document["execution_policy"]
        assert ExecutionPolicy.model_validate(stored).evaluation_acceptance == {address("eval"): AcceptanceMode.MACHINE}


class TestPolicyIsNotWidenedByTheStore:
    async def test_expiry_extension_produces_a_new_version(self, session: AsyncSession, approval: ApprovalContext) -> None:
        """Extending a grant's lifetime is an amendment, not a silent refresh.

        A policy whose expiry could be extended without a new accepted version
        would be a permanent grant with extra steps.
        """
        compiled = await compile_proposal(session, valid_proposal(execution_policy=a_policy()), approval)
        amender = AmendmentContext(org_id=ORG_A, actor_id=HUMAN, actor_role="org_admin", actor_kind=ActorKind.HUMAN)
        extended = a_policy(expires_at=EXPIRY + timedelta(days=90))
        result = await amend_plan(session, compiled.flow_id, valid_proposal(execution_policy=extended), amender)
        assert result.plan_version == 2
        assert not result.already_amended


@pytest.mark.parametrize("selection", ["accessible", "inaccessible", "missing"])
async def test_v2_acceptance_checks_selected_vault_acl_and_preserves_idempotency(session, approval, selection):
    from src.orchestration.execution_policy import UserCredentialAuthority
    from src.shared.models.organization import Organization, User
    from src.shared.models.vault import UserCredential

    session.add(Organization(id=ORG_A, name="Credential approval fixture"))
    await session.flush()
    session.add(User(id=HUMAN, org_id=ORG_A, team_id="", email="owner@example.test"))
    session.add(User(id="other-owner", org_id=ORG_A, team_id="", email="other@example.test"))
    await session.flush()
    if selection != "missing":
        session.add(
            UserCredential(
                id="selected-key",
                org_id=ORG_A,
                user_id=HUMAN if selection == "accessible" else "other-owner",
                service="provider",
                label="default",
                credential_type="api_key",
                secret_arn="fixture-secret",
            )
        )
        await session.flush()
    authority = UserCredentialAuthority(
        permission_mode="user_configured", lifetime="provider_managed", vault_credential_ids=["selected-key"], actions=[Action.DEVELOP]
    )
    proposal = valid_proposal(execution_policy=a_policy(schema_version=2, user_credentials=authority))
    if selection != "accessible":
        with pytest.raises(PolicyNotAcceptableError, match="unavailable"):
            await compile_proposal(session, proposal, approval)
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0
        assert await count_rows(session, OrchestrationNode) == 0
        return
    first = await compile_proposal(session, proposal, approval)
    second = await compile_proposal(session, proposal, approval)
    assert second.already_compiled and second.plan_hash == first.plan_hash
    saved = await accepted_plan(session)
    assert saved.plan_document["execution_policy"]["user_credentials"] == authority.model_dump(mode="json")
    assert saved.plan_document["execution_policy"]["principal_id"] == HUMAN
