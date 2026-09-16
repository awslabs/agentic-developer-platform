"""The design loop's story, captured at registration — issue #4885.

A flow row carried the plan's *shape* and nothing about the design conversation
that produced it, so the flows list (#4869) could say what a plan was doing but
not what it was *for*, nor which of the five AIDLC design gates ran. Two capture
fields fix that: `description` (the intent issue's plain-terms opening) and
`design_history` (which gates ran, which were skipped by scope, which is open).

Design-loop *spend* is deliberately not here — it is blocked on #4898, because
`usage_logs.graph_address` has no writer at all, so there is no attribution path
to hook into yet.

Ordered by how much damage each failure does:

  - **A fabricated design history is worse than an absent one.** `NULL` means "we
    do not know", and for every flow registered before this feature that is the
    honest value. A synthetic history renders as a real record of gates that never
    happened, and it looks authoritative. So the absent case is asserted first and
    from several directions.
  - **`skipped` and `not_reached` must not merge.** Scope decides which stages run
    at all (`poc` skips reverse-engineering); rendering a skipped stage as pending
    reads as unfinished work that is never coming.
  - **An invalid stage name must fail at write time**, not become a permanently
    unrenderable chip nothing downstream can tell from a real stage.
  - **An over-length description is rejected, never truncated.** It rides every row
    of the list response, and silently cutting a sentence ships text whose author
    cannot tell it was altered.
  - **The new fields must not change `plan_hash`.** It is what idempotency
    compares, so including them would break the worker's fail-soft retry across
    this deploy and refuse it as a plan-of-record rewrite.

The session fixture mirrors `test_registration.py`'s (working SAVEPOINTs on
in-memory SQLite) rather than importing it, because pytest fixtures are not
importable across modules and the alternative is a conftest change that would
touch every other test in the package.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import AdminRole
from src.orchestration.compile import ApprovalContext, plan_hash
from src.orchestration.models import DecisionKind
from src.orchestration.proposal import (
    DESCRIPTION_MAX_LEN,
    DESIGN_STAGES,
    DesignHistory,
    LoopProposal,
    ProposedEdge,
    ProposedNode,
)
from src.orchestration.registration import register_draft_proposal
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind
from src.shared.models.base import Base

ORG_A = "aws-e"
AGENT_USER_ID = "scaledjob-worker"
FLOW = "aidlc-delivery-loop-4885"
SPEC_REVISION = "issue-4885-r1"

# The five canonical stages, all approved, as a well-formed history.
FULLY_APPROVED_STAGES = [{"name": name, "state": "approved", "approved_at": "2026-09-02T00:13:13Z"} for name in DESIGN_STAGES]


@pytest_asyncio.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs."""
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
    async with factory() as opened:
        yield opened
    await engine.dispose()


@pytest.fixture
def registrar():
    """The server-resolved context the draft route builds for a registering agent."""
    return ApprovalContext(
        org_id=ORG_A,
        actor_id=AGENT_USER_ID,
        actor_role=AdminRole.MEMBER.value,
        actor_kind=ActorKind.SERVICE,
        reason="AIDLC authoring completed; registering the compiled loop proposal.",
    )


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def proposal(**overrides) -> LoopProposal:
    """A minimal single-wave proposal. Design fields absent unless overridden."""
    payload = {
        "flow_slug": FLOW,
        "title": "Delivery loop for #4885",
        "org_id": ORG_A,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4885",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4885"),
            ProposedNode(address=address("eval-w1"), kind="eval", title="Wave 1 eval"),
        ],
        "edges": [ProposedEdge(from_address=address("story-a"), to_address=address("eval-w1"))],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


async def register(session, document, registrar):
    """Register and return the resulting flow row."""
    result, _ = await register_draft_proposal(session, document, registrar)
    repo = OrchestrationRepository(session)
    return await repo.get_flow(org_id=ORG_A, flow_id=result.flow_id)


# ---------------------------------------------------------------------------
# NULL means "we do not know", and it must stay reachable
# ---------------------------------------------------------------------------


class TestAbsenceIsPreserved:
    """The guardrail with the worst failure mode: never invent a design history."""

    @pytest.mark.asyncio
    async def test_a_flow_with_no_design_fields_stores_null_for_both(self, session, registrar):
        """A hand-authored proposal has no design loop behind it.

        Not `""` and not `{}` — either would render as a description that is blank
        or a history with no stages, both of which assert something. `None` asserts
        nothing, which is the truth.
        """
        flow = await register(session, proposal(), registrar)
        assert flow.description is None
        assert flow.design_history is None

    @pytest.mark.asyncio
    async def test_an_omitted_design_history_is_not_defaulted_to_an_empty_object(self, session, registrar):
        """`{}` would be a design history with no stages — a claim, not an absence.

        Asserted separately from the test above because the two failure shapes are
        different: an empty-dict default is falsy in Python and would pass a naive
        `assert not flow.design_history`, while reaching the frontend as a non-null
        object that renders an empty stage strip. The UI keys off `null`, so the
        distinction is the whole contract.
        """
        flow = await register(session, proposal(description="A use case."), registrar)
        assert flow.design_history is None, f"expected NULL, got {flow.design_history!r}"

    @pytest.mark.asyncio
    async def test_a_description_without_a_history_is_allowed(self, session, registrar):
        """The two fields are independent — one known does not require the other.

        An author who can state the use case but whose loop predates stage
        recording must be able to supply the half they know.
        """
        flow = await register(session, proposal(description="Capture the design loop's story."), registrar)
        assert flow.description == "Capture the design loop's story."
        assert flow.design_history is None

    def test_the_proposal_model_defaults_both_fields_to_none(self):
        """Defaulted at the schema, so every caller that omits them gets NULL.

        This is what makes the absent case structural rather than dependent on a
        code path in the registration helper that someone could later "improve".
        """
        document = proposal()
        assert document.description is None
        assert document.design_history is None


# ---------------------------------------------------------------------------
# The capture path
# ---------------------------------------------------------------------------


class TestRegistrationWritesTheDesignStory:
    """An AIDLC-originated flow carries both fields."""

    @pytest.mark.asyncio
    async def test_both_fields_are_written_for_an_aidlc_originated_flow(self, session, registrar):
        document = proposal(
            description="Delivery plans showed what they were doing but not what they were for.",
            design_history={"scope": "auto", "stages": FULLY_APPROVED_STAGES},
        )
        flow = await register(session, document, registrar)

        assert flow.description == "Delivery plans showed what they were doing but not what they were for."
        assert flow.design_history["scope"] == "auto"
        assert [stage["name"] for stage in flow.design_history["stages"]] == list(DESIGN_STAGES)

    @pytest.mark.asyncio
    async def test_the_history_is_stored_as_plain_json_not_a_pydantic_model(self, session, registrar):
        """The column holds JSON, so `approved_at` must round-trip as an ISO string.

        Handing the Pydantic model to the column would store datetimes SQLite
        cannot serialise, and on Postgres would depend on the driver's JSON
        encoder accepting them — a difference that would surface only in dev.
        """
        document = proposal(design_history={"scope": "poc", "stages": FULLY_APPROVED_STAGES})
        flow = await register(session, document, registrar)

        assert isinstance(flow.design_history, dict)
        approved_at = flow.design_history["stages"][0]["approved_at"]
        assert isinstance(approved_at, str), f"approved_at is {type(approved_at).__name__}, not an ISO string"
        assert approved_at.startswith("2026-09-02T00:13:13")

    @pytest.mark.asyncio
    async def test_an_open_gate_is_recorded_as_open_not_as_approved(self, session, registrar):
        """The card must not claim design finished while a human is being waited on.

        That is the exact confusion #4869 exists to remove, so a history whose last
        stage is `open` must survive as `open`.
        """
        stages = [
            {"name": "intent-capture", "state": "approved", "approved_at": "2026-09-02T00:13:13Z"},
            {"name": "requirements-analysis", "state": "approved", "approved_at": "2026-09-02T00:24:11Z"},
            {"name": "delivery-planning", "state": "approved", "approved_at": "2026-09-02T00:44:12Z"},
            {"name": "loop-proposal", "state": "open"},
        ]
        flow = await register(session, proposal(design_history={"scope": "auto", "stages": stages}), registrar)

        by_name = {stage["name"]: stage for stage in flow.design_history["stages"]}
        assert by_name["loop-proposal"]["state"] == "open"
        # An open gate carries no approval time — nobody has answered it yet.
        assert by_name["loop-proposal"]["approved_at"] is None
        assert by_name["intent-capture"]["approved_at"] is not None


class TestScopeDecidesWhichStagesRun:
    """`skipped` and `not_reached` are different states and must not be merged."""

    @pytest.mark.asyncio
    async def test_poc_scope_records_reverse_engineering_as_skipped(self, session, registrar):
        """`poc` legitimately skips reverse-engineering — it is not pending work.

        Stored as `skipped`, so the card can strike it through rather than show it
        as a gate still to come. This is the required test the approved design
        calls out by name.
        """
        stages = [
            {"name": "intent-capture", "state": "approved", "approved_at": "2026-09-02T00:13:13Z"},
            {"name": "reverse-engineering", "state": "skipped"},
            {"name": "requirements-analysis", "state": "approved", "approved_at": "2026-09-02T00:24:11Z"},
            {"name": "delivery-planning", "state": "approved", "approved_at": "2026-09-02T00:44:12Z"},
            {"name": "loop-proposal", "state": "open"},
        ]
        flow = await register(session, proposal(design_history={"scope": "poc", "stages": stages}), registrar)

        states = {stage["name"]: stage["state"] for stage in flow.design_history["stages"]}
        assert states["reverse-engineering"] == "skipped"
        assert states["reverse-engineering"] != "not_reached"

    @pytest.mark.asyncio
    async def test_skipped_and_not_reached_round_trip_as_distinct_values(self, session, registrar):
        """Both present in one history, still distinguishable after storage."""
        stages = [
            {"name": "reverse-engineering", "state": "skipped"},
            {"name": "loop-proposal", "state": "not_reached"},
        ]
        flow = await register(session, proposal(design_history={"scope": "poc", "stages": stages}), registrar)

        states = {stage["name"]: stage["state"] for stage in flow.design_history["stages"]}
        assert states["reverse-engineering"] == "skipped"
        assert states["loop-proposal"] == "not_reached"

    def test_all_four_states_are_accepted_and_none_is_an_alias(self):
        """The vocabulary is exactly four members, each meaning something different."""
        for state in ("approved", "open", "skipped", "not_reached"):
            payload = {"name": "loop-proposal", "state": state}
            if state == "approved":
                payload["approved_at"] = "2026-09-02T00:13:13Z"
            history = DesignHistory.model_validate({"scope": "auto", "stages": [payload]})
            assert history.stages[0].state == state


# ---------------------------------------------------------------------------
# Validation at write time
# ---------------------------------------------------------------------------


class TestInvalidHistoryIsRejectedAtWriteTime:
    """A typo must fail now, not become a permanently unrenderable chip."""

    def test_a_stage_name_outside_the_canonical_five_is_rejected(self):
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": [{"name": "intent-captrue", "state": "open"}]})

    def test_the_canonical_five_are_exactly_the_aidlc_stage_names(self):
        """Pinned against `rules/personas/aidlc.md`, so a rename fails here loudly.

        The names are a contract with the AIDLC persona that authors them; a silent
        divergence would reject every real history the agent emits.
        """
        assert DESIGN_STAGES == (
            "intent-capture",
            "reverse-engineering",
            "requirements-analysis",
            "delivery-planning",
            "loop-proposal",
        )

    def test_an_unknown_state_is_rejected(self):
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": [{"name": "loop-proposal", "state": "pending"}]})

    def test_an_unknown_scope_is_rejected(self):
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "yolo", "stages": [{"name": "loop-proposal", "state": "open"}]})

    def test_approved_without_a_timestamp_is_rejected(self):
        """An approval with no time is not a usable record of an approval."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": [{"name": "loop-proposal", "state": "approved"}]})

    def test_a_timestamp_on_a_non_approved_stage_is_rejected(self):
        """The card would render it as an approval time for a gate nobody answered."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate(
                {"scope": "auto", "stages": [{"name": "loop-proposal", "state": "open", "approved_at": "2026-09-02T00:13:13Z"}]}
            )

    def test_a_duplicate_stage_is_rejected(self):
        """Two entries for one gate make "N of 5 approved" ambiguous."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate(
                {
                    "scope": "auto",
                    "stages": [
                        {"name": "loop-proposal", "state": "open"},
                        {"name": "loop-proposal", "state": "approved", "approved_at": "2026-09-02T00:13:13Z"},
                    ],
                }
            )

    def test_more_than_five_stages_is_rejected(self):
        """There are five gates; a sixth entry is a bug in whatever emitted it."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": [*FULLY_APPROVED_STAGES, {"name": "loop-proposal", "state": "open"}]})

    def test_an_empty_stage_list_is_rejected(self):
        """A history with no stages says nothing; the honest form of that is NULL."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": []})

    def test_an_unknown_key_is_rejected_rather_than_silently_dropped(self):
        """`extra="forbid"`, so a misspelled field surfaces instead of vanishing."""
        with pytest.raises(ValidationError):
            DesignHistory.model_validate({"scope": "auto", "stages": [{"name": "loop-proposal", "state": "open", "aproved_at": "x"}]})

    @pytest.mark.asyncio
    async def test_an_invalid_history_is_refused_before_any_row_is_written(self, session, registrar):
        """Validation is the type, so the refusal happens at the API boundary.

        Registration is never reached, so there is no partially-written flow to
        clean up — asserted, because a validation error that arrived mid-transaction
        would leave a flow row with no design history and no way to tell why.
        """
        with pytest.raises(ValidationError):
            proposal(design_history={"scope": "auto", "stages": [{"name": "not-a-stage", "state": "open"}]})

        repo = OrchestrationRepository(session)
        assert await repo.list_flows(org_id=ORG_A) == []


class TestDescriptionIsCappedNotTruncated:
    """It rides every row of the list response, so the cap is enforced on write."""

    def test_a_description_at_the_cap_is_accepted(self):
        document = proposal(description="x" * DESCRIPTION_MAX_LEN)
        assert len(document.description) == DESCRIPTION_MAX_LEN

    def test_a_description_over_the_cap_is_rejected(self):
        with pytest.raises(ValidationError):
            proposal(description="x" * (DESCRIPTION_MAX_LEN + 1))

    @pytest.mark.asyncio
    async def test_an_over_length_description_is_never_silently_truncated(self, session, registrar):
        """Rejection, not truncation.

        A truncated description ships text whose author cannot tell it was altered,
        and the fix — write a shorter one — belongs with them. Asserted by proving
        no flow was created at all, which is stronger than checking the stored
        length: a truncating implementation would store a 500-char value here.
        """
        with pytest.raises(ValidationError):
            proposal(description="y" * 5000)

        repo = OrchestrationRepository(session)
        assert await repo.list_flows(org_id=ORG_A) == []

    def test_the_cap_is_five_hundred_characters(self):
        """Pinned: the approved design specifies 500, and it bounds the list response."""
        assert DESCRIPTION_MAX_LEN == 500


# ---------------------------------------------------------------------------
# The fields must not change a plan's identity
# ---------------------------------------------------------------------------


class TestPlanIdentityIsUnaffected:
    """`plan_hash` is what idempotency compares — the new fields must not enter it."""

    def test_two_documents_differing_only_in_description_hash_identically(self):
        """They describe the same graph, so they are the same plan.

        If they hashed differently, the worker's fail-soft retry across the #4885
        deploy would not match its own in-force plan: it would fall through the
        idempotency return and be refused 409 as a plan-of-record rewrite, turning
        a dropped connection into a permanent failure.
        """
        assert plan_hash(proposal()) == plan_hash(proposal(description="A use case."))

    def test_two_documents_differing_only_in_design_history_hash_identically(self):
        assert plan_hash(proposal()) == plan_hash(proposal(design_history={"scope": "poc", "stages": FULLY_APPROVED_STAGES}))

    def test_a_document_differing_in_its_graph_still_hashes_differently(self):
        """The exclusion is narrow: it must not weaken idempotency generally."""
        other = proposal(nodes=[ProposedNode(address=address("story-z"), kind="story", title="Story Z")], edges=[])
        assert plan_hash(proposal()) != plan_hash(other)

    @pytest.mark.asyncio
    async def test_a_retry_whose_description_differs_is_still_an_idempotent_retry(self, session, registrar):
        """The exact case the exclusion exists for, end to end.

        A worker retrying after a dropped connection may re-read the artifact and
        send a description the first attempt did not carry. That is the same plan,
        and it must return `already_compiled` rather than 409.
        """
        first, _ = await register_draft_proposal(session, proposal(), registrar)
        assert first.already_compiled is False

        second, _ = await register_draft_proposal(session, proposal(description="Added on the retry."), registrar)
        assert second.already_compiled is True, "a retry differing only in description was treated as a new plan"
        assert second.flow_id == first.flow_id
        assert second.plan_hash == first.plan_hash

    @pytest.mark.asyncio
    async def test_the_stored_plan_document_still_records_the_captured_fields(self, session, registrar):
        """Excluded from the HASH, not from the stored document.

        The accepted-plan row holds the document verbatim, so the capture fields
        remain auditable there even though they do not participate in identity.
        """
        document = proposal(description="A use case.", design_history={"scope": "poc", "stages": FULLY_APPROVED_STAGES})
        result, _ = await register_draft_proposal(session, document, registrar)

        repo = OrchestrationRepository(session)
        plan = await repo.get_accepted_plan(org_id=ORG_A, flow_id=result.flow_id)
        assert plan.plan_document["description"] == "A use case."
        assert plan.plan_document["design_history"]["scope"] == "poc"


class TestCaptureHappensOnCreationOnly:
    """`_resolve_flow` resolves a flow; it does not reconcile one."""

    @pytest.mark.asyncio
    async def test_registration_still_records_the_draft_decision(self, session, registrar):
        """Regression guard: the capture fields did not disturb the draft contract.

        A draft is inert because it records `PLAN_DRAFTED`, which is absent from
        the approval kinds that root a dispatch. Asserted here because this file
        exercises the registration path with new arguments.
        """
        result, gate_address = await register_draft_proposal(session, proposal(description="A use case."), registrar)

        repo = OrchestrationRepository(session)
        decisions = await repo.list_decisions(org_id=ORG_A, flow_id=result.flow_id)
        assert [decision.kind for decision in decisions] == [DecisionKind.PLAN_DRAFTED.value]
        assert gate_address.startswith(FLOW)
