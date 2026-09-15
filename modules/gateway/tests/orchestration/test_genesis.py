"""Tests for engine genesis (issue #4204, ruling D-R12).

**AC-30 lives here, and the negative tests matter more than the happy path.**
This story adds a genesis path to a component that deliberately had none, so the
tests that earn the change are the ones proving it refuses: a `decision_id` that
does not exist, one whose actor is a `service`, and one belonging to another org.
Each must be refused with nothing dispatched.

The positive test is almost incidental by comparison — "a real gate approval roots
a chain" is one assertion. "Nothing else does" takes the rest of the file, and
that asymmetry is deliberate.

Also asserted here at source level: nothing in the genesis or dispatch path reads
authority from an envelope (R-O5d). That is a structural claim about the code, not
about one input, so a behavioural test cannot cover it — a future refactor could
add a `persona` parameter and every behavioural test would still pass.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import dispatch as dispatch_module
from src.orchestration import genesis as genesis_module
from src.orchestration.genesis import (
    APPROVAL_DECISION_KINDS,
    EngineGenesis,
    GenesisRefusedError,
    resolve_engine_genesis,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationFlow,
)
from src.orchestration.state import ActorKind
from src.shared.logging import get_json_formatter
from src.shared.models.base import Base

GENESIS_LOGGER = "bedrockgateway.orchestration.genesis"

# The stable token an operator and an evaluation both match on (issue #4321).
GENESIS_EVENT = "engine_genesis"

ORG_A = "org-alpha"
ORG_B = "org-beta"

APPROVER = "cognito-sub-alice"


@pytest.fixture
async def engine():
    """In-memory SQLite engine, following `test_tick.py`'s fixture exactly.

    The two pysqlite hooks are not incidental: without them the driver manages
    transactions implicitly, and a second session's view of an uncommitted write
    is not what the deployed asyncpg path would show.
    """
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
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = "flow-1") -> OrchestrationFlow:
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="Demo flow")
    session.add(flow)
    await session.flush()
    return flow


async def _make_decision(
    session: AsyncSession,
    flow: OrchestrationFlow | None = None,
    *,
    kind: str = DecisionKind.GATE_APPROVED.value,
    actor_kind: str = ActorKind.HUMAN.value,
    actor_id: str = APPROVER,
    actor_role: str = "org_admin",
    org_id: str | None = None,
) -> OrchestrationDecision:
    # Most cases do not care which flow the decision hangs off, so the helper
    # makes one. Cases that DO care (cross-org) pass their own.
    if flow is None:
        flow = await _make_flow(session, org_id=org_id or ORG_A, slug=f"flow-for-{kind}-{actor_kind}")
    decision = OrchestrationDecision(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        kind=kind,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_kind=actor_kind,
        reason="approved at the wave gate",
    )
    session.add(decision)
    await session.flush()
    return decision


class TestGenesisResolvesARealApproval:
    """The happy path: an SSO-attributed gate approval roots the chain."""

    async def test_gate_approval_roots_the_chain_in_the_approver(self, session):
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        result = await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert result.root_human_id == APPROVER
        assert result.decision_id == decision.id
        assert result.flow_id == flow.id
        assert result.org_id == ORG_A
        assert result.is_human_rooted is True

    async def test_approver_role_is_the_snapshot_from_the_row(self, session):
        """Attribution reflects authority held at decision time, not now."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, actor_role="wave_approver")

        result = await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert result.root_human_role == "wave_approver"

    @pytest.mark.parametrize("kind", sorted(APPROVAL_DECISION_KINDS))
    async def test_every_approval_kind_can_root_a_dispatch(self, session, kind):
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, kind=kind)

        result = await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert result.kind == kind


class TestGenesisIsRefused:
    """AC-30, the load-bearing half. Every refusal leaves nothing dispatched."""

    async def test_nonexistent_decision_id_is_refused(self, session):
        await _make_flow(session)

        with pytest.raises(GenesisRefusedError, match="does not exist"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id="no-such-decision")

    async def test_service_actor_decision_cannot_root_a_chain(self, session):
        """The central check: the engine cannot bootstrap human authority.

        The tick writes `TRANSITION_REJECTED` rows as a SERVICE actor, so without
        this check the engine could point genesis at one of its OWN decisions and
        manufacture a human root from nothing.
        """
        flow = await _make_flow(session)
        decision = await _make_decision(
            session,
            flow,
            kind=DecisionKind.GATE_APPROVED.value,
            actor_kind=ActorKind.SERVICE.value,
            actor_id="system:orchestration-tick",
        )

        with pytest.raises(GenesisRefusedError, match="actor_kind"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

    async def test_another_orgs_decision_is_refused(self, session):
        """Cross-org: filtered in SQL, so it is indistinguishable from absent."""
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        decision = await _make_decision(session, flow_b, org_id=ORG_B)

        with pytest.raises(GenesisRefusedError, match="does not exist"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

    async def test_cross_org_refusal_is_not_an_existence_oracle(self, session):
        """A real id in another org and a fabricated id must be indistinguishable.

        If the two produced different messages, the endpoint would let a caller
        enumerate other tenants' decision ids by comparing error text.
        """
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        real_in_other_org = await _make_decision(session, flow_b, org_id=ORG_B)

        with pytest.raises(GenesisRefusedError) as cross_org:
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=real_in_other_org.id)
        with pytest.raises(GenesisRefusedError) as fabricated:
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id="fabricated-id")

        # Same shape of message, differing only in the echoed id.
        assert str(cross_org.value).replace(real_in_other_org.id, "X") == str(fabricated.value).replace("fabricated-id", "X")

    @pytest.mark.parametrize(
        "kind",
        [
            DecisionKind.GATE_REJECTED.value,
            DecisionKind.TRANSITION_REJECTED.value,
            DecisionKind.HALT_OVERRIDDEN.value,
        ],
    )
    async def test_non_approval_decision_kinds_cannot_root_a_dispatch(self, session, kind):
        """A human REFUSING a gate must not authorise dispatching that work."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, kind=kind)

        with pytest.raises(GenesisRefusedError, match="not an approval"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

    async def test_empty_decision_id_is_refused(self, session):
        with pytest.raises(GenesisRefusedError, match="requires a decision_id"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id="")

    async def test_missing_org_id_is_refused_rather_than_unscoped(self, session):
        """An absent tenant must never mean 'search every tenant'."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with pytest.raises(GenesisRefusedError, match="requires an org_id"):
            await resolve_engine_genesis(session, org_id="", decision_id=decision.id)

    async def test_human_decision_with_no_actor_id_is_refused(self, session):
        """A root that identifies nobody is worse than no root."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, actor_id="")

        with pytest.raises(GenesisRefusedError, match="no actor_id"):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

    async def test_refusal_is_an_exception_not_a_falsy_return(self, session):
        """A caller that forgets to check a return value must not dispatch unrooted.

        This pins the API shape deliberately: `resolve_engine_genesis` has no
        `None` return path, so there is no way to accidentally proceed without a
        genesis object.
        """
        signature = inspect.signature(resolve_engine_genesis)
        assert "None" not in str(signature.return_annotation), (
            "resolve_engine_genesis must not have a None return path — a falsy return is ignorable, an exception is not"
        )

    async def test_a_refused_genesis_writes_nothing(self, session):
        """Refusal is read-only: no decision row is appended by a failed lookup."""
        flow = await _make_flow(session)
        await _make_decision(session, flow, actor_kind=ActorKind.SERVICE.value)
        before = len((await session.execute(select(OrchestrationDecision))).scalars().all())

        with pytest.raises(GenesisRefusedError):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id="nope")

        after = len((await session.execute(select(OrchestrationDecision))).scalars().all())
        assert after == before


def _genesis_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every captured record carrying the stable `engine_genesis` token.

    Matched on the STRUCTURED field rather than by searching message text, so a
    test cannot pass merely because the token appears in some unrelated line.
    """
    return [record for record in caplog.records if getattr(record, "event", None) == GENESIS_EVENT]


def _as_deployed_json(record: logging.LogRecord) -> dict:
    """Render a record through the REAL deployed formatter.

    The point of the issue is that the fields are queryable in CloudWatch, and
    what decides that is `StructuredJsonFormatter` (`src/shared/logging.py`) —
    the same formatter `configure_logging` installs in the tick Lambda. Asserting
    against `record.__dict__` would prove the attributes were attached but not
    that they survive into the emitted document, which is the actual contract.
    """
    return json.loads(get_json_formatter().format(record))


class TestGenesisEmitsAStructuredHumanRootedLogLine:
    """Issue #4321: an operator can confirm human authorization from logs alone.

    Observability only. These tests assert what is VISIBLE, never that the log
    grants anything — authority stays sourced from `EngineGenesis`.
    """

    async def test_success_emits_exactly_one_genesis_event(self, session, caplog):
        """One dispatch, one line — the CloudWatch ingest cost is bounded by that."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert len(_genesis_events(caplog)) == 1

    async def test_the_emitted_document_carries_every_rooting_field(self, session, caplog):
        """The fields land as TOP-LEVEL JSON keys, i.e. queryable, not embedded."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            genesis = await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        emitted = _as_deployed_json(_genesis_events(caplog)[0])

        assert emitted["event"] == GENESIS_EVENT
        assert emitted["is_human_rooted"] is True
        assert emitted["decision_id"] == decision.id
        assert emitted["kind"] == DecisionKind.GATE_APPROVED.value
        assert emitted["flow_id"] == flow.id
        assert emitted["org_id"] == ORG_A
        # The opaque internal identity from the decision row, and nothing richer.
        assert emitted["root_human_id"] == APPROVER
        # The log must agree with the authority object rather than assert its own
        # version of events.
        assert emitted["root_human_id"] == genesis.root_human_id
        assert emitted["is_human_rooted"] == genesis.is_human_rooted

    async def test_message_text_is_greppable_for_the_token_and_the_flag(self, session, caplog):
        """The smoke test greps a JSON stream, where `"is_human_rooted": true` would not match.

        `aws logs tail ... | grep engine_genesis` has to return a line an operator
        can read the answer off, so the token and the flag are in the message text
        too — not only in the structured fields.
        """
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        message = _genesis_events(caplog)[0].getMessage()
        assert GENESIS_EVENT in message
        assert "is_human_rooted=true" in message

    async def test_the_existing_searchable_fields_still_appear(self, session, caplog):
        """Current log consumers must not break: the old substring is intact."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        message = _genesis_events(caplog)[0].getMessage()
        assert (
            f"orchestration genesis: resolved decision_id={decision.id} kind={DecisionKind.GATE_APPROVED.value} flow={flow.id} org={ORG_A}" in message
        )

    async def test_the_human_identifier_is_a_structured_field_not_message_text(self, session, caplog):
        """The privacy control: the identity is queryable but not smeared into prose.

        Keeping `root_human_id` out of the message means a consumer that ships only
        message text (or a truncating one) does not carry the identity along, while
        an operator who needs it can still query the field.
        """
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        record = _genesis_events(caplog)[0]
        assert APPROVER not in record.getMessage()
        assert _as_deployed_json(record)["root_human_id"] == APPROVER

    async def test_no_profile_or_credential_data_is_logged(self, session, caplog):
        """Only the opaque id may appear — never an email or a role-bearing profile."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, actor_id="cognito-sub-bob", actor_role="org_admin")

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        emitted = _as_deployed_json(_genesis_events(caplog)[0])

        assert emitted["root_human_id"] == "cognito-sub-bob"
        assert "@" not in emitted["root_human_id"]
        # `root_human_role` is carried on the authority object for attribution, but
        # it is not part of the logged projection.
        assert "root_human_role" not in emitted
        serialised = json.dumps(emitted)
        for forbidden in ("email", "password", "token", "credential", "secret"):
            assert forbidden not in serialised.lower(), f"{forbidden!r} must not appear in the genesis log document"


class TestRefusalsEmitNoHumanRoot:
    """The load-bearing negative half of #4321.

    A refusal that named the approver would turn the refusal into an oracle for
    who approved what — the thing the resolver deliberately avoids. Adding a log
    line must not reintroduce it, and it must not log human-root SUCCESS on a
    path that refused.
    """

    async def test_absent_decision_refusal_emits_no_genesis_event(self, session, caplog):
        await _make_flow(session)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            with pytest.raises(GenesisRefusedError):
                await resolve_engine_genesis(session, org_id=ORG_A, decision_id="no-such-decision")

        assert _genesis_events(caplog) == []

    async def test_service_actor_refusal_emits_no_genesis_event_and_no_identity(self, session, caplog):
        """The central refusal: the engine cannot log itself a human root."""
        flow = await _make_flow(session)
        decision = await _make_decision(
            session,
            flow,
            actor_kind=ActorKind.SERVICE.value,
            actor_id="system:orchestration-tick",
        )

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            with pytest.raises(GenesisRefusedError):
                await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert _genesis_events(caplog) == []
        # No line may claim a human root on a path that refused.
        for record in caplog.records:
            assert "is_human_rooted=true" not in record.getMessage()
            assert getattr(record, "is_human_rooted", None) is None

    async def test_cross_org_refusal_never_echoes_the_approver(self, session, caplog):
        """A cross-tenant probe must learn nothing about who approved anything."""
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        decision = await _make_decision(session, flow_b, org_id=ORG_B)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            with pytest.raises(GenesisRefusedError) as refusal:
                await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert _genesis_events(caplog) == []
        assert APPROVER not in str(refusal.value)
        for record in caplog.records:
            assert APPROVER not in record.getMessage()
            # Not in the emitted document either — an attached attribute would
            # reach CloudWatch even though it is absent from the message.
            assert APPROVER not in json.dumps(_as_deployed_json(record))

    @pytest.mark.parametrize(
        "kind",
        [
            DecisionKind.GATE_REJECTED.value,
            DecisionKind.TRANSITION_REJECTED.value,
            DecisionKind.HALT_OVERRIDDEN.value,
        ],
    )
    async def test_non_approval_refusal_emits_no_genesis_event(self, session, caplog, kind):
        """A human's refusal is still a refusal: nothing may be logged as rooted."""
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, kind=kind)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            with pytest.raises(GenesisRefusedError):
                await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        assert _genesis_events(caplog) == []
        for record in caplog.records:
            assert APPROVER not in record.getMessage()


class TestTheLogIsNotAnAuthoritySource:
    """The log is a projection of `EngineGenesis`, not a second source of truth.

    The issue names "log line treated as the authority source" as a bug class, so
    this pins the ordering structurally: resolution happens first, the log reads
    off the resolved object, and the returned object is that same object.
    """

    async def test_the_logged_fields_are_read_off_the_returned_object(self, session, caplog):
        flow = await _make_flow(session)
        decision = await _make_decision(session, flow, kind=DecisionKind.PLAN_ACCEPTED.value)

        with caplog.at_level(logging.INFO, logger=GENESIS_LOGGER):
            genesis = await resolve_engine_genesis(session, org_id=ORG_A, decision_id=decision.id)

        emitted = _as_deployed_json(_genesis_events(caplog)[0])

        # Every logged rooting field matches the authority object exactly, so the
        # log cannot drift into asserting something the resolver did not.
        assert emitted["decision_id"] == genesis.decision_id
        assert emitted["kind"] == genesis.kind
        assert emitted["flow_id"] == genesis.flow_id
        assert emitted["org_id"] == genesis.org_id
        assert emitted["root_human_id"] == genesis.root_human_id
        assert emitted["is_human_rooted"] == genesis.is_human_rooted

    def test_the_success_log_is_emitted_after_the_genesis_is_constructed(self):
        """Structural: the log reads a resolved object, it does not compute a root.

        A behavioural test cannot catch a future refactor that rebuilds the fields
        independently in the logging call, which is how a log quietly becomes a
        parallel authority path. The source ordering is the guarantee.
        """
        source = Path(inspect.getfile(genesis_module)).read_text()
        tree = ast.parse(source)
        resolver = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "resolve_engine_genesis")

        construction = [
            node.lineno for node in ast.walk(resolver) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "EngineGenesis"
        ]
        genesis_logs = [
            node.lineno
            for node in ast.walk(resolver)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and any(isinstance(arg, ast.Constant) and GENESIS_EVENT in str(arg.value) for arg in node.args)
        ]

        assert len(construction) == 1, "EngineGenesis must be constructed exactly once in the resolver"
        assert genesis_logs, f"the resolver must emit the {GENESIS_EVENT!r} token on success"
        assert min(genesis_logs) > construction[0], "the genesis log must be emitted AFTER resolution, from the resolved object"


class TestAuthorityIsNeverReadFromAnEnvelope:
    """R-O5d, asserted structurally — a behavioural test cannot cover this.

    Every persona's pod presents an identical ARN (one shared role, one service
    account, one registry entry), so authority derived from `persona`,
    `AGENT_TYPE` or a caller ARN is authority derived from a value the caller
    controls or that cannot discriminate. The guarantee is that these modules
    never read them at all, which is a property of the source.
    """

    ENVELOPE_FIELDS = ("persona", "AGENT_TYPE", "caller_arn", "userArn", "bot_kind")

    @pytest.mark.parametrize("module", [genesis_module, dispatch_module], ids=["genesis", "dispatch"])
    def test_module_never_names_an_envelope_authority_field(self, module):
        source = Path(inspect.getfile(module)).read_text()
        tree = ast.parse(source)

        # Docstrings legitimately DISCUSS these fields (explaining why they are not
        # read), so only non-docstring string constants and identifiers count.
        docstrings = {
            ast.get_docstring(node) for node in ast.walk(tree) if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }

        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in self.ENVELOPE_FIELDS:
                offenders.append(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in self.ENVELOPE_FIELDS:
                offenders.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings:
                offenders.extend(field for field in self.ENVELOPE_FIELDS if field == node.value)

        assert offenders == [], (
            f"{module.__name__} reads envelope-supplied authority fields {sorted(set(offenders))}. "
            "R-O5d: authority comes only from the server-resolved decision row."
        )

    def test_dispatch_node_takes_no_persona_or_actor_parameter(self):
        """A parameter that exists will eventually be trusted, so there is none."""
        params = set(inspect.signature(dispatch_module.dispatch_node).parameters)
        forbidden = params & {"persona", "agent_type", "actor", "actor_kind", "caller_arn", "root_human_id"}
        assert forbidden == set(), (
            f"dispatch_node accepts caller-supplied authority parameters {sorted(forbidden)}. "
            "Authority must be resolved server-side from the decision row."
        )

    def test_engine_genesis_cannot_be_constructed_from_a_bare_root_human_id(self):
        """The only way to get an EngineGenesis is by reference to a real row.

        `EngineGenesis` requires the decision it derives from, so a caller cannot
        fabricate one from a chosen human id alone — that would be exactly the
        client-supplied root D-R12 forbids.
        """
        required = {name for name, param in inspect.signature(EngineGenesis).parameters.items() if param.default is inspect.Parameter.empty}
        assert "decision_id" in required, "EngineGenesis must require the decision it derives from"

    def test_genesis_does_not_import_the_marker_verification_path(self):
        """The genesis path is additive: it is not reachable by presenting a marker.

        If genesis imported or wrapped marker verification, a forged marker would
        become a way in. It resolves a database row and nothing else.
        """
        source = Path(inspect.getfile(genesis_module)).read_text()
        tree = ast.parse(source)
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not any("marker" in (mod or "") for mod in imported), (
            "genesis.py imports marker verification; the genesis path must not be reachable by presenting a marker"
        )
