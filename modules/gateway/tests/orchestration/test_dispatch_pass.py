"""Tests for the engine dispatch pass (issue #4313).

The pass is the caller wave 4 deliberately did not ship, so these tests carry the
acceptance criteria of the ruling in
`docs/design-notes/4303-engine-genesis-transport.md`. Three of them are
**negative** — they prove the ruling was followed rather than worked around:

- AC 6: an `EngineGenesis` built from a caller-supplied `root_human_id` is
  impossible to express (`TestGenesisIsUnforgeable`).
- AC 7: two nodes sharing an `issue_ref` produce two distinct
  `MessageDeduplicationId` values (`TestDeduplicationId`, guards hazard 1).
- AC 8: a node with `issue_ref = NULL` never produces a `tenant##`
  `MessageGroupId` (`TestMessageGroupId`, guards hazard 2).

AC 5 (IAM scoped to the queue ARN, no `Resource: "*"` for SQS) is asserted against
the Terraform source in `TestTickIAMPolicy`, following the
`test_stall.py::test_pod_deadline_mirrors_the_terraform_value` precedent for
pinning an invariant that lives in Terraform.

The remaining ACs are structural and asserted elsewhere by construction: no
`decision_id` in any HTTP body or SQS message (AC 1 — nothing here builds a
request, and `TestEnvelope` asserts the message shape), a zero-line diff to
`test_internal_plane_guard.py` (AC 2) and to `test_genesis.py` (AC 9), and no
change under `webhook-ingress/` (ACs 3 and 4) — all verified by `git diff` rather
than by a test that could drift from what it claims.

Concurrency is modelled as **two passes that both observed the same prior state**,
which is what overlapping ticks actually produce, rather than as OS threads whose
interleaving would make failures flaky rather than informative. Same reasoning as
`test_tick.py` and `test_dispatch.py`.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import dispatch_pass as dispatch_pass_module
from src.orchestration.dispatch_pass import (
    DEFAULT_MAX_DISPATCHES_PER_TICK,
    DispatchPassConfig,
    DispatchPassConfigError,
    message_deduplication_id,
    message_group_id,
    publish_pending,
    run_dispatch_pass,
)
from src.orchestration.genesis import EngineGenesis
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from tests.agentauth.conftest import report_only_db as report_only_db_fixture
from tests.agentauth.conftest import test_engine  # noqa: F401

report_only_db = report_only_db_fixture

ORG_A = "org-alpha"
ORG_B = "org-beta"
APPROVER = "cognito-sub-alice"
INSTALLATION_A = 55_501
INSTALLATION_B = 55_502
REPO = "aws-e/adp"


# ---------------------------------------------------------------------------
# Fixtures — same SQLite-in-memory shape as test_dispatch.py
# ---------------------------------------------------------------------------


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
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


class FakeRunStore:
    def __init__(self):
        self.rows = {}

    def register(self, envelope):
        self.rows[envelope["message_id"]] = envelope


@pytest.fixture(autouse=True)
def run_store(monkeypatch):
    from src.orchestration.run_store import EngineRunStore

    store = FakeRunStore()
    monkeypatch.setattr(EngineRunStore, "from_env", lambda: store)
    return store


class FakeSQS:
    """Records `send_message` calls so the FIFO keys can be asserted on.

    A double rather than a real client because the dedup id and group id are three
    of this issue's acceptance criteria — they have to be inspectable, and a real
    AWS call would make them unobservable.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated SQS failure")
        self.calls.append(kwargs)
        return {"MessageId": f"msg-{len(self.calls)}"}

    @property
    def dedup_ids(self) -> list[str]:
        return [c["MessageDeduplicationId"] for c in self.calls]

    @property
    def group_ids(self) -> list[str]:
        return [c["MessageGroupId"] for c in self.calls]

    def envelope(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.calls[index]["MessageBody"])


def _config(**overrides: Any) -> DispatchPassConfig:
    defaults: dict[str, Any] = {
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo",
        "repo": REPO,
    }
    defaults.update(overrides)
    return DispatchPassConfig(**defaults)


async def _make_org(session: AsyncSession, *, org_id: str = ORG_A, installations: list[int] | None = None) -> Organization:
    org = Organization(
        id=org_id,
        name=f"Org {org_id}",
        github_installation_ids=[str(i) for i in (installations if installations is not None else [INSTALLATION_A])],
    )
    session.add(org)
    await session.flush()
    return org


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = "flow-1") -> OrchestrationFlow:
    flow = OrchestrationFlow(execution_paused=False, org_id=org_id, slug=slug, title="Demo flow")
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
    org_id: str | None = None,
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-4",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
        issue_ref=issue_ref,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_approval(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    org_id: str | None = None,
    kind: str = DecisionKind.GATE_APPROVED.value,
    actor_kind: str = ActorKind.HUMAN.value,
    actor_id: str = APPROVER,
) -> OrchestrationDecision:
    existing_user = await session.get(User, actor_id)
    if existing_user is not None and existing_user.org_id != (org_id or flow.org_id):
        actor_id = f"{actor_id}:{org_id or flow.org_id}"
    if actor_kind == ActorKind.HUMAN.value and await session.get(User, actor_id) is None:
        session.add(
            User(id=actor_id, org_id=org_id or flow.org_id, team_id="team-test", email=f"{actor_id}@example.com", cognito_sub=f"sub:{actor_id}")
        )
        await session.flush()
    decision = OrchestrationDecision(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        kind=kind,
        actor_id=actor_id,
        actor_role="org_admin",
        actor_kind=actor_kind,
        reason="approved at the wave gate",
    )
    session.add(decision)
    await session.flush()
    return decision


async def _ready_story(session: AsyncSession, **node_kwargs: Any) -> tuple[OrchestrationFlow, OrchestrationNode, OrchestrationDecision]:
    """The standard happy-path fixture: org + flow + approval + one ready story."""
    await _make_org(session)
    flow = await _make_flow(session)
    decision = await _make_approval(session, flow)
    node = await _make_node(session, flow, **node_kwargs)
    return flow, node, decision


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


@pytest.fixture
def work_claims_enabled(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=12345))


async def test_ownership_enabled_admits_one_of_two_ready_nodes_on_same_issue(session, work_claims_enabled):
    from src.orchestration.models import OrchestrationWorkClaim

    flow, _, _ = await _ready_story(session)
    await _make_node(session, flow, node_ref="another-node")
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1
    assert len(report.pending) == 1
    row = (await session.scalars(select(OrchestrationWorkClaim))).one()
    assert row.active_run_id == report.pending[0].envelope["message_id"]
    assert row.owner_ref == flow.id
    assert report.pending[0].envelope["source_ref"]["provider_repository_id"] == 12345
    assert report.pending[0].envelope["work_claim_required"] is True


async def test_webhook_claim_prevents_engine_attempt_consumption(session, work_claims_enabled):
    from src.orchestration.work_admission import admit
    from src.orchestration.work_claims import ClaimOwner, OwnerKind

    _, node, _ = await _ready_story(session)
    node_id = node.id
    await admit(
        session,
        org_id=ORG_A,
        repository_id=12345,
        issue=4196,
        owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "human-event"),
        invocation_id="webhook-run",
    )
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 0
    assert report.pending == []
    assert await _state_of(session, node_id) == NodeState.READY.value
    assert (await session.get(OrchestrationNode, node_id)).attempts == 0


async def test_policy_refusal_does_not_leak_an_ownership_claim(session, work_claims_enabled, monkeypatch):
    from unittest.mock import AsyncMock

    from src.orchestration.execution_policy import Decision, DenyReason
    from src.orchestration.models import OrchestrationWorkClaim

    await _ready_story(session)
    monkeypatch.setattr(
        dispatch_pass_module, "authorize_node_dispatch", AsyncMock(return_value=Decision.block(DenyReason.ACTION_NOT_PERMITTED, "test denial"))
    )
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 0
    assert report.policy_blocked == 1
    assert list((await session.scalars(select(OrchestrationWorkClaim))).all()) == []


# ---------------------------------------------------------------------------
# AC 7 (negative) — dedup key must not collapse two nodes on one issue
# ---------------------------------------------------------------------------


class TestDeduplicationId:
    """Hazard 1: the webhook path's key shape would silently discard dispatches.

    `sqs_publisher.py` builds `f"{arrived_at}_{repo}_{issue}"` and the queue sets
    `content_based_deduplication = true`. The FIFO dedup window is 5 minutes and
    the tick runs every 5 minutes, so a key that is not unique per node lets SQS
    accept a message, return a MessageId, and discard it — a dispatch that commits
    `running` and never runs.
    """

    def test_two_nodes_sharing_an_issue_ref_get_distinct_dedup_ids(self):
        # AC 7, at the key level. Same decision, same issue — different nodes.
        first = message_deduplication_id(node_id="node-1", decision_id="dec-1", attempt=1)
        second = message_deduplication_id(node_id="node-2", decision_id="dec-1", attempt=1)
        assert first != second, "two distinct nodes must never share a MessageDeduplicationId"

    async def test_two_nodes_sharing_an_issue_ref_publish_two_messages(self, session):
        # AC 7, end to end: the thing that actually matters is that BOTH reach SQS.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        # Same issue_ref on purpose — this is the exact collision hazard 1 names.
        await _make_node(session, flow, node_ref="s1", issue_ref="4196")
        await _make_node(session, flow, node_ref="s2", issue_ref="4196")

        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 2

        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        assert len(sqs.calls) == 2
        assert len(set(sqs.dedup_ids)) == 2, f"both nodes collapsed to one dedup id: {sqs.dedup_ids}"

    def test_dedup_id_is_not_the_webhook_paths_key_shape(self):
        # The webhook key is `{arrived_at}_{repo}_{issue}`. A timestamp-derived key
        # would change on every attempt and defeat dedup entirely; a repo/issue key
        # is not unique per node. Assert the node id is what carries uniqueness.
        dedup = message_deduplication_id(node_id="node-abc", decision_id="dec-1", attempt=1)
        assert "node-abc" in dedup
        assert REPO not in dedup
        assert "4196" not in dedup

    def test_attempt_number_distinguishes_a_resumed_re_dispatch(self):
        # A human resume increments `attempts`. Without it in the key, the second
        # dispatch of the same node would be swallowed as a duplicate of the first.
        first = message_deduplication_id(node_id="node-1", decision_id="dec-1", attempt=1)
        retry = message_deduplication_id(node_id="node-1", decision_id="dec-1", attempt=2)
        assert first != retry

    def test_a_new_approval_produces_a_new_dedup_id(self):
        # A re-plan that re-approves the work is a new dispatch, not a duplicate.
        first = message_deduplication_id(node_id="node-1", decision_id="dec-1", attempt=1)
        replanned = message_deduplication_id(node_id="node-1", decision_id="dec-2", attempt=1)
        assert first != replanned

    def test_dedup_id_respects_the_sqs_length_cap(self):
        dedup = message_deduplication_id(node_id="n" * 200, decision_id="d" * 200, attempt=1)
        assert len(dedup) <= 128


# ---------------------------------------------------------------------------
# AC 8 (negative) — group id must not collapse to `tenant##`
# ---------------------------------------------------------------------------


class TestMessageGroupId:
    """Hazard 2: a per-issue group sends every issue-less node to `tenant##`.

    `OrchestrationNode.issue_ref` is nullable, so under the webhook path's
    `tenant#repo#issue` grouping every gate and eval node in a tenant would share
    one group — reintroducing the tenant-wide head-of-line blocking that
    `sqs_publisher.py:3-8` says the per-run group was chosen to avoid.
    """

    def test_group_id_is_per_node(self):
        first = message_group_id(org_id=ORG_A, node_id="node-1")
        second = message_group_id(org_id=ORG_A, node_id="node-2")
        assert first != second, "two nodes in one tenant must not share a MessageGroupId"

    def test_a_null_issue_ref_never_produces_the_tenant_double_hash_group(self):
        # AC 8, stated exactly as the issue words it. The node id is always present
        # (it is the primary key), so the group can never degrade to `tenant##`.
        group = message_group_id(org_id=ORG_A, node_id="node-1")
        assert group != f"{ORG_A}##"
        assert not group.endswith("##")

    async def test_published_group_ids_are_per_node_not_per_issue(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        await _make_node(session, flow, node_ref="s1", issue_ref="4196")
        await _make_node(session, flow, node_ref="s2", issue_ref="4196")

        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        assert len(set(sqs.group_ids)) == 2, f"nodes shared a FIFO group: {sqs.group_ids}"
        for group in sqs.group_ids:
            assert not group.endswith("##")

    def test_group_id_respects_the_sqs_length_cap(self):
        assert len(message_group_id(org_id="o" * 200, node_id="n" * 200)) <= 128


# ---------------------------------------------------------------------------
# AC 6 (negative) — a caller-supplied root human must be unrepresentable
# ---------------------------------------------------------------------------


class TestGenesisIsUnforgeable:
    """AC 6: genesis must be obtainable ONLY via `resolve_engine_genesis`.

    The requirement is that a caller-supplied `root_human_id` is *impossible to
    express*, not merely unused. Asserted at source level, following the
    `persona`/`actor` ban pattern `test_dispatch.py` already uses, because a
    behavioural test can only show that today's code does not do it — an AST test
    fails the build when tomorrow's code starts.
    """

    @staticmethod
    def _module_ast() -> ast.Module:
        return ast.parse(Path(inspect.getfile(dispatch_pass_module)).read_text())

    def test_the_pass_never_constructs_an_engine_genesis(self):
        # The forgery path would be `EngineGenesis(root_human_id=...)`. Nothing in
        # this module may call that constructor at all — genesis arrives only as the
        # return value of `resolve_engine_genesis`.
        tree = self._module_ast()
        constructions = [
            node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EngineGenesis"
        ]
        assert not constructions, (
            f"dispatch_pass.py constructs EngineGenesis directly at line(s) {[n.lineno for n in constructions]} "
            "— genesis must only be RESOLVED (AC 6)"
        )

    def test_the_pass_never_names_root_human_id_as_a_keyword(self):
        # A `root_human_id=` keyword anywhere in this module would mean some call
        # accepts a caller-supplied root. Reading `genesis.root_human_id` is fine
        # (that is an attribute access on a resolved object); passing one is not.
        tree = self._module_ast()
        offenders = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) for kw in node.keywords if kw.arg == "root_human_id"]
        assert not offenders, (
            f"dispatch_pass.py passes root_human_id as a keyword at line(s) {offenders} — a resolved root must never be supplied (D-R12, AC 6)"
        )

    def test_resolve_engine_genesis_is_imported_from_the_single_declared_module(self):
        tree = self._module_ast()
        sources = {
            (node.level, node.module)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and any(a.name == "resolve_engine_genesis" for a in node.names)
        }
        assert sources == {(1, "genesis")}, f"resolve_engine_genesis must come from the sibling .genesis module; got {sources}"

    def test_engine_genesis_is_frozen(self):
        # Frozen is what stops a resolved root being mutated into a supplied one
        # after the fact.
        genesis = EngineGenesis(
            root_human_id=APPROVER,
            root_human_role="org_admin",
            decision_id="dec-1",
            flow_id="flow-1",
            org_id=ORG_A,
            kind=DecisionKind.GATE_APPROVED.value,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            genesis.root_human_id = "cognito-sub-attacker"  # type: ignore[misc]

    def test_the_pass_never_calls_spawn_persona(self):
        # Hazard 4. `spawn_persona` bundles the webhook path's enforcement (loop
        # detection, MAX_CHAIN_DEPTH, DynamoDB correlation-pointer writes). Pointer
        # provenance is agent-writable (#4304), so the engine must not source
        # authority from it. Only the envelope CONTRACT is reused.
        source = Path(inspect.getfile(dispatch_pass_module)).read_text()
        tree = self._module_ast()
        calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "spawn_persona"
        ]
        assert not calls, f"dispatch_pass.py calls spawn_persona at line(s) {calls} — forbidden by hazard 4"
        assert "import spawn_persona" not in source
        assert "from common" not in source, "the gateway image does not contain webhook-ingress lambda code"


# ---------------------------------------------------------------------------
# The happy path and the envelope contract
# ---------------------------------------------------------------------------


class TestSuccessfulDispatch:
    async def test_a_ready_story_node_is_dispatched_and_published(self, session):
        _flow, node, _decision = await _ready_story(session)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 1
        assert report.dispatches_attempted == 1
        assert report.success
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)
        assert len(sqs.calls) == 1

    async def test_the_envelope_satisfies_the_workers_required_fields(self, session):
        # `parse_envelope` in agent-worker-image/entrypoint.py hard-requires these.
        # An envelope missing any of them is rejected by the worker AFTER the node
        # has already committed to `running` — an invisible dispatch.
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        envelope = sqs.envelope()
        for key in ("tenant_id", "persona", "source_ref"):
            assert key in envelope, f"worker requires envelope.{key}"
        for key in ("installation_id", "repo", "issue"):
            assert key in envelope["source_ref"], f"worker requires source_ref.{key}"

        assert envelope["source_ref"]["installation_id"] == INSTALLATION_A
        assert envelope["source_ref"]["repo"] == REPO
        assert envelope["source_ref"]["issue"] == 4196
        assert envelope["tenant_id"] == ORG_A

    async def test_the_envelope_carries_the_resolved_approver_as_attribution(self, session):
        # The honest claim: these fields are attribution for the run's audit trail,
        # NOT a credential. What the test pins is that the value is the RESOLVED
        # approver — nothing else could have put it there.
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        correlation = sqs.envelope()["correlation"]
        assert correlation["root_human_id"] == APPROVER
        assert correlation["is_human_rooted"] is True

    async def test_no_decision_id_appears_in_the_published_message(self, session):
        # AC 1's message half. `decision_id` never leaves the gateway as a
        # *reference to be re-resolved*; the envelope's `root_decision_id` is the
        # audit pointer to an already-committed row, which is the whole point of
        # resolution having happened in-process.
        _flow, _node, decision = await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        def keys(obj: Any) -> set[str]:
            if isinstance(obj, dict):
                return set(obj) | {k for v in obj.values() for k in keys(v)}
            if isinstance(obj, list):
                return {k for v in obj for k in keys(v)}
            return set()

        envelope = sqs.envelope()
        assert "decision_id" not in keys(envelope), "the envelope must not carry a bare `decision_id` for a consumer to re-resolve"
        # The audit pointer is present under its own explicit name — a pointer to an
        # already-committed row, not a reference the consumer resolves into authority.
        assert envelope["orchestration"]["root_decision_id"] == decision.id

    async def test_the_envelope_reports_its_own_channel(self, session):
        # Not "github": nothing here came from a GitHub event, and labelling it so
        # would make an engine dispatch indistinguishable from a webhook trigger in
        # every downstream log.
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        envelope = sqs.envelope()
        assert envelope["channel"] == "orchestration"
        assert envelope["intent"]["trigger"] == "engine_dispatch"

    async def test_the_envelope_carries_the_graph_address(self, session):
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        assert sqs.envelope()["orchestration"]["graph_address"] == "flow-1/4191/wave-4/s7"

    async def test_a_plan_accepted_decision_also_roots_a_dispatch(self, session):
        # `APPROVAL_DECISION_KINDS` is shared with genesis.py rather than restated,
        # so all three approval kinds work without this module knowing the list.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow, kind=DecisionKind.PLAN_ACCEPTED.value)
        await _make_node(session, flow)

        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 1


# ---------------------------------------------------------------------------
# Idempotency (R-NF2) — the cost-critical invariant
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_two_passes_over_the_same_ready_node_produce_one_run(self, session):
        # R-NF2. Duplicate dispatch means duplicate agent runs, duplicate Bedrock
        # spend and duplicate PRs, so "at most once" has to be structural — the
        # second pass sees `running`, which is not a candidate state.
        _flow, node, _decision = await _ready_story(session)

        first = await run_dispatch_pass(session, _config())
        second = await run_dispatch_pass(session, _config())

        assert first.dispatched == 1
        assert second.dispatched == 0, "a second pass must not re-dispatch a running node"
        assert second.nodes_examined == 0, "a running node is not even a candidate"
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_two_passes_publish_exactly_one_message(self, session):
        await _ready_story(session)
        sqs = FakeSQS()

        first = await run_dispatch_pass(session, _config())
        publish_pending(first, _config(), client=sqs)
        second = await run_dispatch_pass(session, _config())
        publish_pending(second, _config(), client=sqs)

        assert len(sqs.calls) == 1, "one ready node must yield exactly one queued run"

    async def test_a_node_already_running_is_never_a_candidate(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        await _make_node(session, flow, state=NodeState.RUNNING)

        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 0
        assert report.nodes_examined == 0


# ---------------------------------------------------------------------------
# Genesis refusal — fail-closed, publishes nothing
# ---------------------------------------------------------------------------


class TestGenesisRefusal:
    async def test_a_flow_with_no_approval_is_not_dispatched(self, session):
        # Nothing human authorised this work, so there is no root. Refusing is the
        # only fail-closed reading (D-R12).
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow)  # no approval decision

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.genesis_refused == 1
        assert not report.pending, "a refused genesis must publish nothing"
        assert await _state_of(session, node.id) == NodeState.READY.value, "the node must stay ready for a later tick"

    async def test_a_service_decision_cannot_root_a_dispatch(self, session):
        # The load-bearing check. The tick writes SERVICE decision rows itself, so
        # without this the engine could root a dispatch in its own prior decision
        # and bootstrap human authority out of nothing.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow, actor_kind=ActorKind.SERVICE.value, actor_id="system:orchestration-tick")
        node = await _make_node(session, flow)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.genesis_refused == 1
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_a_human_rejection_does_not_root_a_dispatch(self, session):
        # Rooting a dispatch in a human's REFUSAL would have the engine dispatch
        # work a human just declined — an inversion, not a gap.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow, kind=DecisionKind.GATE_REJECTED.value)
        await _make_node(session, flow)

        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 0
        assert report.genesis_refused == 1

    async def test_a_refusal_publishes_nothing_even_when_another_node_succeeds(self, session):
        # Per-node containment: one flow's missing approval must not stop another
        # flow's legitimate dispatch, and must not smuggle a message out either.
        await _make_org(session)
        good_flow = await _make_flow(session, slug="flow-good")
        await _make_approval(session, good_flow)
        await _make_node(session, good_flow, node_ref="s1")

        bad_flow = await _make_flow(session, slug="flow-bad")
        await _make_node(session, bad_flow, node_ref="s2")  # no approval

        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        assert report.dispatched == 1
        assert report.genesis_refused == 1
        assert len(sqs.calls) == 1


# ---------------------------------------------------------------------------
# Publish failure after commit — the named recovery path is #4211
# ---------------------------------------------------------------------------


class TestPublishFailure:
    async def test_a_failed_publish_leaves_the_node_running(self, session):
        # Commit-then-publish (hazard 3): the node is `running` with no run. That is
        # recoverable by #4211's stall detector, which is merged. The alternative
        # ordering would leave a run with no node, which deviation.py flags as
        # off-graph work.
        _flow, node, _decision = await _ready_story(session)

        report = await run_dispatch_pass(session, _config())
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

        publish_pending(report, _config(), client=FakeSQS(fail=True))

        assert report.publish_failed == 1
        assert await _state_of(session, node.id) == NodeState.RUNNING.value, "the node must stay running so the stall detector can find it"

    async def test_a_failed_publish_forces_a_non_success_report(self, session):
        # A dispatch that committed `running` and never reached the queue is the
        # exact invisible failure this issue exists to end. It must never look green.
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        assert report.success

        publish_pending(report, _config(), client=FakeSQS(fail=True))
        assert not report.success

    async def test_the_stall_detector_can_recover_a_failed_publish(self, session):
        # Names the recovery path concretely rather than asserting it in prose: the
        # node is left in a state `stall.py` treats as a candidate.
        from src.orchestration.stall import STALLABLE_STATES

        _flow, node, _decision = await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        publish_pending(report, _config(), client=FakeSQS(fail=True))

        state = await _state_of(session, node.id)
        assert NodeState(state) in STALLABLE_STATES, "a node stranded by a publish failure must be visible to the stall detector"

    async def test_one_failed_publish_does_not_stop_the_others(self, session):
        class FlakySQS(FakeSQS):
            def send_message(self, **kwargs: Any) -> dict[str, Any]:
                if len(self.calls) == 0:
                    self.calls.append(kwargs)
                    raise RuntimeError("first send fails")
                return super().send_message(**kwargs)

        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        await _make_node(session, flow, node_ref="s1")
        await _make_node(session, flow, node_ref="s2")

        report = await run_dispatch_pass(session, _config())
        publish_pending(report, _config(), client=FlakySQS())

        assert report.publish_failed == 1
        assert report.dispatched == 2


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_each_node_is_dispatched_under_its_own_orgs_context(self, session):
        # `org_id` comes from the pass's own query context, never from a message.
        await _make_org(session, org_id=ORG_A, installations=[INSTALLATION_A])
        await _make_org(session, org_id=ORG_B, installations=[INSTALLATION_B])

        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        await _make_approval(session, flow_a)
        await _make_node(session, flow_a, node_ref="s1")

        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        await _make_approval(session, flow_b)
        await _make_node(session, flow_b, node_ref="s2")

        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)

        tenants = {sqs.envelope(i)["tenant_id"] for i in range(len(sqs.calls))}
        assert tenants == {ORG_A, ORG_B}
        # Each envelope carries ITS OWN org's installation — never the other's.
        by_tenant = {sqs.envelope(i)["tenant_id"]: sqs.envelope(i)["source_ref"]["installation_id"] for i in range(len(sqs.calls))}
        assert by_tenant == {ORG_A: INSTALLATION_A, ORG_B: INSTALLATION_B}

    async def test_an_approval_in_another_org_cannot_root_a_dispatch(self, session):
        # The cross-tenant attack: a real approval exists, but in a different org.
        # The decision lookup is filtered by org_id in SQL, so it resolves to
        # nothing and the dispatch is refused rather than cross-attributed.
        await _make_org(session, org_id=ORG_A)
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        # Approval belongs to ORG_B but names ORG_A's flow.
        await _make_approval(session, flow_a, org_id=ORG_B)
        node = await _make_node(session, flow_a)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.genesis_refused == 1
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_per_org_counters_never_aggregate_one_org_into_another(self, session):
        await _make_org(session, org_id=ORG_A, installations=[INSTALLATION_A])
        await _make_org(session, org_id=ORG_B, installations=[INSTALLATION_B])
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        await _make_approval(session, flow_a)
        await _make_node(session, flow_a, node_ref="s1")
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        await _make_node(session, flow_b, node_ref="s2")  # no approval

        report = await run_dispatch_pass(session, _config())

        assert report.per_org[ORG_A]["dispatched"] == 1
        assert report.per_org[ORG_B]["dispatched"] == 0
        assert report.per_org[ORG_B]["genesis_refused"] == 1


# ---------------------------------------------------------------------------
# Scope: story nodes only, and the source_ref completeness gate
# ---------------------------------------------------------------------------


class TestDispatchScope:
    """Story-nodes-only, per the scope decision this issue asks to be explicit.

    The gate is stricter than `issue_ref IS NOT NULL` because the worker requires a
    complete `source_ref` and the graph carries no repo or installation.
    """

    @pytest.mark.parametrize("kind", [NodeKind.GATE.value])
    async def test_non_story_nodes_are_not_dispatched(self, session, kind):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        node = await _make_node(session, flow, kind=kind, issue_ref=None)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.nodes_examined == 0, "gate/eval nodes are filtered in SQL, not skipped in Python"
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_a_story_node_with_no_issue_is_undispatchable_not_published(self, session):
        # Publishing a malformed envelope would have the worker reject it AFTER the
        # node committed to `running` — the invisible dispatch by another route.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        node = await _make_node(session, flow, issue_ref=None)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.undispatchable == 1
        assert not report.pending
        assert await _state_of(session, node.id) == NodeState.READY.value, "the node must not be moved to running"

    async def test_a_non_numeric_issue_ref_is_undispatchable(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        await _make_node(session, flow, issue_ref="not-a-number")

        report = await run_dispatch_pass(session, _config())
        assert report.undispatchable == 1
        assert report.dispatched == 0

    async def test_a_hash_prefixed_issue_ref_is_accepted(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        await _make_node(session, flow, issue_ref="#4196")

        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)
        assert sqs.envelope()["source_ref"]["issue"] == 4196

    @pytest.mark.parametrize("installations", [[], [INSTALLATION_A, INSTALLATION_B]])
    async def test_an_ambiguous_installation_is_undispatchable(self, session, installations):
        # Fail closed on both zero and many. Guessing would dispatch into a
        # repository nobody asked for.
        await _make_org(session, installations=installations)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        node = await _make_node(session, flow)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert report.undispatchable == 1
        assert await _state_of(session, node.id) == NodeState.READY.value

    @pytest.mark.parametrize(
        "state",
        [NodeState.PENDING, NodeState.AWAITING_GATE, NodeState.PASSED, NodeState.FAILED, NodeState.HALTED, NodeState.SUPERSEDED],
    )
    async def test_only_ready_nodes_are_dispatched(self, session, state):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        node = await _make_node(session, flow, state=state)

        report = await run_dispatch_pass(session, _config())

        assert report.dispatched == 0
        assert await _state_of(session, node.id) == state.value

    async def test_a_halted_node_is_never_resurrected_by_the_pass(self, session):
        # `halted` is terminal for the engine (R-Q9c). A dispatch pass that could
        # pick one up would be a bound the engine can lift, i.e. no bound at all.
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        node = await _make_node(session, flow, state=NodeState.HALTED)

        report = await run_dispatch_pass(session, _config())
        assert report.dispatched == 0
        assert await _state_of(session, node.id) == NodeState.HALTED.value


# ---------------------------------------------------------------------------
# The per-tick cap — the deliberately bounded surface
# ---------------------------------------------------------------------------


class TestPerTickCap:
    async def test_the_cap_is_honoured(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        for i in range(5):
            await _make_node(session, flow, node_ref=f"s{i}", issue_ref=str(4200 + i))

        report = await run_dispatch_pass(session, _config(max_dispatches_per_tick=2))

        assert report.dispatched == 2
        assert report.capped is True

    async def test_a_capped_pass_reports_that_it_was_capped(self, session):
        # "We ran out of budget" must never read as "there was nothing left to do".
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        for i in range(3):
            await _make_node(session, flow, node_ref=f"s{i}", issue_ref=str(4200 + i))

        capped = await run_dispatch_pass(session, _config(max_dispatches_per_tick=1))
        assert capped.capped is True

        uncapped = await run_dispatch_pass(session, _config(max_dispatches_per_tick=10))
        assert uncapped.capped is False

    async def test_the_cap_delays_work_it_does_not_drop_it(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        await _make_approval(session, flow)
        for i in range(3):
            await _make_node(session, flow, node_ref=f"s{i}", issue_ref=str(4200 + i))

        total = 0
        for _ in range(3):
            report = await run_dispatch_pass(session, _config(max_dispatches_per_tick=1))
            total += report.dispatched
        assert total == 3, "successive ticks must drain the backlog, not skip it"

    async def test_a_cap_below_one_is_rejected_at_construction(self):
        for cap in (0, -1):
            with pytest.raises(DispatchPassConfigError, match="at least 1"):
                _config(max_dispatches_per_tick=cap)

    def test_the_default_cap_is_bounded(self):
        assert DEFAULT_MAX_DISPATCHES_PER_TICK == 10
        assert _config().max_dispatches_per_tick == DEFAULT_MAX_DISPATCHES_PER_TICK


# ---------------------------------------------------------------------------
# Unconfigured environments must be visible, not silent
# ---------------------------------------------------------------------------


class TestUnconfigured:
    @pytest.mark.parametrize("overrides", [{"queue_url": ""}, {"repo": ""}])
    async def test_an_unconfigured_pass_dispatches_nothing(self, session, overrides):
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config(**overrides))

        assert report.dispatched == 0
        assert report.enabled is False
        assert report.undispatchable == 1, "ready nodes must be counted, so an unwired env is not mistaken for an idle one"

    async def test_an_unconfigured_pass_leaves_nodes_ready(self, session):
        _flow, node, _decision = await _ready_story(session)
        await run_dispatch_pass(session, _config(queue_url=""))
        assert await _state_of(session, node.id) == NodeState.READY.value

    def test_publish_with_nothing_pending_is_a_no_op(self):
        from src.orchestration.dispatch_pass import DispatchPassReport

        sqs = FakeSQS()
        report = publish_pending(DispatchPassReport(), _config(), client=sqs)
        assert not sqs.calls
        assert report.success

    def test_config_from_env_reads_the_documented_variables(self, monkeypatch):
        monkeypatch.setenv("BG_ORCH_DISPATCH_QUEUE_URL", "https://example/queue.fifo")
        monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "owner/name")
        monkeypatch.setenv("BG_ORCH_DISPATCH_PERSONA", "architect")
        monkeypatch.setenv("BG_ORCH_DISPATCH_MAX_PER_TICK", "3")

        config = DispatchPassConfig.from_env()
        assert config.queue_url == "https://example/queue.fifo"
        assert config.repo == "owner/name"
        assert config.persona == "architect"
        assert config.max_dispatches_per_tick == 3
        assert config.configured

    def test_config_from_env_is_unconfigured_when_unset(self, monkeypatch):
        for var in ("BG_ORCH_DISPATCH_QUEUE_URL", "BG_ORCH_DISPATCH_REPO"):
            monkeypatch.delenv(var, raising=False)
        assert DispatchPassConfig.from_env().configured is False

    def test_a_malformed_cap_falls_back_to_the_default(self, monkeypatch):
        # An unparseable number must not take the whole tick down, and the default
        # is the conservative value anyway.
        monkeypatch.setenv("BG_ORCH_DISPATCH_MAX_PER_TICK", "not-a-number")
        assert DispatchPassConfig.from_env().max_dispatches_per_tick == DEFAULT_MAX_DISPATCHES_PER_TICK


# ---------------------------------------------------------------------------
# AC 5 — the IAM grant must be scoped to the queue ARN
# ---------------------------------------------------------------------------


class TestTickIAMPolicy:
    """AC 5: `sqs:SendMessage` scoped to the queue ARN, never `Resource: "*"`.

    Asserted against the Terraform source, following
    `test_stall.py::test_pod_deadline_mirrors_the_terraform_value`. A wildcard here
    would let the tick produce onto any queue in the account, which is a materially
    different grant from the one the ruling's producer-set argument depends on.
    """

    @staticmethod
    def _iam_tf() -> str:
        path = Path(__file__).resolve().parents[3] / "gateway" / "infra" / "modules" / "orchestration-tick" / "iam.tf"
        assert path.exists(), f"tick IAM policy not found at {path}"
        return path.read_text()

    def test_the_tick_is_granted_send_message(self):
        assert '"sqs:SendMessage"' in self._iam_tf()

    def test_the_sqs_statement_is_not_wildcard_scoped(self):
        # Parse the SendMessage statement specifically: the policy legitimately uses
        # `Resource = "*"` for PutMetricData, ENI management and X-Ray, none of
        # which support resource-level permissions. SQS does.
        source = self._iam_tf()
        start = source.index("PublishEngineDispatch")
        # The statement ends at the next `},` at statement indentation.
        end = source.index("\n      },", start)
        statement = source[start:end]

        assert "sqs:SendMessage" in statement
        assert 'Resource = "*"' not in statement, 'SQS SendMessage must never be granted on Resource = "*" (AC 5)'
        assert "var.agent_submit_queue_arn" in statement, "the grant must be scoped to the queue ARN passed in as a variable"

    def test_the_fallback_resource_is_still_a_scoped_arn(self):
        # When the ARN variable is unset the policy falls back to a conventional
        # queue name — still a specific ARN, never a wildcard.
        source = self._iam_tf()
        start = source.index("PublishEngineDispatch")
        end = source.index("\n      },", start)
        statement = source[start:end]
        assert "agent-submit.fifo" in statement

    def test_no_kms_grant_was_added_speculatively(self):
        # The queue is SSE-SQS (`sqs.tf` sets no kms_master_key_id), so a KMS grant
        # would be unnecessary privilege. The ruling says explicitly not to add one.
        source = self._iam_tf()
        start = source.index("PublishEngineDispatch")
        end = source.index("\n      },", start)
        assert "kms" not in source[start:end].lower()


# ---------------------------------------------------------------------------
# Structural guards on the module itself
# ---------------------------------------------------------------------------


class TestTickNetworkPath:
    """#4316: the tick must be able to REACH the queue it is authorised to write.

    The IAM tests above prove the grant; these prove the network path. Both are
    needed — #4316 shipped with a correct IAM policy and no reachability, so every
    dispatch hung until the Lambda timeout killed the invocation AFTER the
    `ready -> running` commit. That produced a durable state change, dispatch
    counters reading 0, no `tick_report` line at all, and no alarm.

    Asserted against the Terraform source in the same style as `TestTickIAMPolicy`.
    """

    @staticmethod
    def _main_tf() -> str:
        path = Path(__file__).resolve().parents[3] / "gateway" / "infra" / "modules" / "orchestration-tick" / "main.tf"
        assert path.exists(), f"tick module not found at {path}"
        return path.read_text()

    def test_ingress_is_opened_on_the_vpc_endpoint_sg(self):
        # The tick's own egress already allows 443, but the SQS interface endpoint
        # has private_dns_enabled=true, so there is no public path to fall back to
        # and the endpoint SG must admit the tick explicitly.
        source = self._main_tf()
        assert 'resource "aws_security_group_rule" "tick_to_vpc_endpoints"' in source, (
            "the tick must be granted 443 ingress on the VPC interface endpoint SG, or SQS SendMessage hangs (#4316)"
        )

    def test_the_endpoint_rule_is_scoped_to_the_tick_sg_on_443(self):
        source = self._main_tf()
        marker = 'resource "aws_security_group_rule" "tick_to_vpc_endpoints"'
        assert marker in source, "endpoint ingress rule missing (see the preceding test)"
        start = source.index(marker)
        statement = source[start : source.index("\n}", start)]

        assert "source_security_group_id = aws_security_group.tick.id" in statement, (
            "the rule must reference the tick's SG, not a CIDR — widening the endpoint SG would grant every "
            "workload in the VPC access to the private AWS endpoints (#4316 'what is not the fix')"
        )
        assert "cidr_blocks" not in statement, "the endpoint ingress must never be CIDR-scoped"
        assert "from_port                = 443" in statement
        assert "to_port                  = 443" in statement

    def test_the_timeout_was_not_raised_instead(self):
        # Negative check. A longer timeout only makes the hang take longer to fail;
        # #4316 names it explicitly as not-the-fix. 120s is the documented default.
        path = Path(__file__).resolve().parents[3] / "gateway" / "infra" / "modules" / "orchestration-tick" / "variables.tf"
        source = path.read_text()
        start = source.index('variable "tick_timeout"')
        assert "default     = 120" in source[start : source.index("\n}", start)], (
            "tick_timeout must stay at 120s — raising it masks a reachability failure instead of fixing it (#4316)"
        )

    def test_the_function_waits_for_the_endpoint_rule(self):
        # Without this the first apply can create the function (and let a scheduled
        # tick fire) before the path it needs exists.
        source = self._main_tf()
        start = source.index("depends_on = [")
        assert "aws_security_group_rule.tick_to_vpc_endpoints" in source[start : source.index("]", start)]


class TestModuleStructure:
    @staticmethod
    def _module_ast() -> ast.Module:
        return ast.parse(Path(inspect.getfile(dispatch_pass_module)).read_text())

    def test_the_pass_never_writes_node_state_itself(self):
        # Every state change must go through `dispatch_node`, which is the single
        # audited seam (it consults `transition()` and records rejections). A second
        # write path here would bypass the authority guard.
        tree = self._module_ast()
        offenders = [
            node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update"
        ]
        assert not offenders, f"dispatch_pass.py issues its own UPDATE at line(s) {offenders} — state changes must go through dispatch_node"

    def test_the_pass_does_not_commit(self):
        # Commit-then-publish requires the CALLER to own the transaction. A commit
        # in here would make the ordering unenforceable from the handler.
        tree = self._module_ast()
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "commit"
        ]
        assert not offenders, f"dispatch_pass.py commits at line(s) {offenders} — the caller owns the transaction"

    def test_run_dispatch_pass_does_not_send_messages(self):
        # The DB half must not publish; that is what makes commit-then-publish
        # structural rather than a comment. `send_message` may appear only in
        # `publish_pending` and the client Protocol.
        source = Path(inspect.getfile(dispatch_pass_module)).read_text()
        db_half = source[source.index("async def run_dispatch_pass") : source.index("def publish_pending")]
        assert "send_message" not in db_half, "run_dispatch_pass must not publish — the send happens after the caller commits"

    def test_tick_py_is_not_imported_for_dispatch(self):
        # `tick.py`'s docstring says the tick performs NO dispatch, and that
        # sentence is load-bearing. This module must not reach into it.
        tree = self._module_ast()
        offenders = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module in {".tick", "src.orchestration.tick"}
        ]
        assert not offenders, "dispatch_pass.py must not import tick.py — the tick performs no dispatch"


@pytest.fixture
def protected_engine(monkeypatch):
    import boto3
    from moto import mock_aws

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.engine import EngineAuthorityWriter

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for table, pk, sk in [("authority", "pk", "sk"), ("events", "event_id", "arrived_at")]:
            ddb.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        store = BootstrapStore(table_name="authority", dynamodb_client=ddb)
        writer = EngineAuthorityWriter(store=store, events_table="events")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: writer)
        yield store, writer


@pytest.mark.parametrize("kind", [NodeKind.STORY.value, NodeKind.EVAL.value])
async def test_protected_engine_publishes_committed_identity_and_live_flow(session, protected_engine, kind):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import boto3

    from src.activity.service import ActivityService, _build_chain_tree
    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.engine import validate_engine_authority
    from src.agentauth.grants import AgentAction
    from src.agentauth.workload import VerifiedPod
    from src.orchestration.dispatch import graph_address
    from src.orchestration.results import observe_results
    from src.orchestration.run_store import EngineRunStore

    store, _ = protected_engine
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    node = await _make_node(session, flow, kind=kind)
    node.title = "Deploy the accepted production integration contracts"
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    pending = list(report.pending)
    committed = json.loads(
        (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value))).one().reason
    )
    runs = EngineRunStore(boto3.resource("dynamodb", region_name="us-east-1").Table("events"))
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs, run_store=runs)
    assert len(sqs.calls) == 1
    envelope = sqs.envelope()
    assert envelope["message_id"] == committed["run_id"]
    assert envelope["arrived_at"] == committed["arrived_at"]
    row = runs.get(committed["run_id"], committed["arrived_at"])
    assert row["engine_node_id"] == node.id
    assert row["engine_attempt"] == node.attempts
    assert row["actor_kind"] == "service"
    assert row["user_id"] == envelope["actor"]["user_id"]
    # Real dispatch -> persisted row -> both activity representations. Testing
    # only the envelope let engine runs ship with no topic in Agent Activity.
    activity = ActivityService._map_item(row)
    assert activity.topic == node.title
    assert activity.repo == REPO
    assert activity.issue_number == 4196
    assert activity.source_url == f"https://github.com/{REPO}/issues/4196"
    assert _build_chain_tree([row])[0].topic == node.title
    # Summary is a worker outcome, not an invented result at dispatch time.
    assert activity.summary is None
    record = store.bind(
        invocation_id=envelope["message_id"],
        digest=envelope_digest(envelope),
        pod=VerifiedPod("engine-pod", "engine-worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
        now=datetime.now(UTC),
    )
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id=ORG_A, attempt=1, now=datetime.now(UTC))
    execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{record.invocation_id}")
    attribution = await validate_engine_authority(session=session, execution=execution, grant=grant)
    # Issue #4898: the same authorization that admits the run also yields the graph
    # address its model calls are billed to. Asserted here, on a genuinely dispatched
    # node, because this is the only place the whole chain is real — a real dispatch
    # pass, a real committed identity, a real bound credential and the real store
    # record. The address must equal what the dispatch write and the cost readback
    # compose from the same helper, so all three agree by construction.
    assert attribution is not None
    assert attribution.address == graph_address(node, flow_slug=flow.slug)
    assert (attribution.org_id, attribution.flow_id) == (ORG_A, flow.id)
    assert (attribution.node_id, attribution.node_attempt) == (node.id, node.attempts)
    assert attribution.run_id == envelope["message_id"] == committed["run_id"]
    assert execution["orchestration_node_id"]["S"] == node.id
    assert grant.authority.human_id == APPROVER
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1
    assert "credential" not in envelope
    if kind == NodeKind.EVAL.value:
        assert envelope["persona"] == "operations"
        assert grant.allowed_actions == frozenset({AgentAction.MONITOR})
    runs.table.update_item(
        Key={"event_id": envelope["message_id"], "arrived_at": envelope["arrived_at"]},
        UpdateExpression="SET #status = :complete, transcript_key = :transcript, summary = :summary",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":complete": "complete", ":transcript": "test/transcript.json", ":summary": "Worker completed the task"},
    )
    report.pending = pending
    publish_pending(report, _config(), client=sqs, run_store=runs)
    assert report.publish_failed == 0
    assert sqs.envelope(1) == envelope
    persisted = runs.get(committed["run_id"], committed["arrived_at"])
    assert persisted["status"] == "complete"
    assert persisted["topic"] == node.title
    assert persisted["summary"] == "Worker completed the task"
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1
    await observe_results(session, run_store=runs, evidence=SimpleNamespace(merged_story=AsyncMock(return_value=None)))
    await session.refresh(node)
    assert node.state == (NodeState.AWAITING_GATE.value if kind == NodeKind.EVAL.value else NodeState.AWAITING_MERGE.value)


@pytest.mark.parametrize("title", [None, "", "   ", "A" * 512])
async def test_activity_topic_handles_legacy_envelopes_and_bounds_titles(session, title):
    from src.orchestration.run_store import EngineRunStore

    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    envelope = report.pending[0].envelope
    if title is None:
        envelope["orchestration"].pop("title")
    else:
        envelope["orchestration"]["title"] = title
    row = EngineRunStore.build_item(envelope)
    assert row["topic"] == ("A" * 120 if title and title.strip() else f"{REPO}#4196")


async def _protected_execution(session, store, *, node_kwargs=None):
    """One real dispatched, bound execution: the input `validate_engine_authority` reads.

    Built through the actual dispatch pass and the actual store bind rather than a
    hand-written dict, so the attribution assertions below run on the shape the
    engine really writes.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.workload import VerifiedPod

    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    node = await _make_node(session, flow, **(node_kwargs or {}))
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    envelope = sqs.envelope()
    record = store.bind(
        invocation_id=envelope["message_id"],
        digest=envelope_digest(envelope),
        pod=VerifiedPod("engine-pod", "engine-worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
        now=datetime.now(UTC),
    )
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id=ORG_A, attempt=1, now=datetime.now(UTC))
    execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{record.invocation_id}")
    return SimpleNamespace(flow=flow, node=node, execution=execution, grant=grant)


async def test_a_coordinator_is_not_attributed_to_the_node_it_coordinates(session, protected_engine):
    """A flow coordinator owns no single graph node, so its spend stays unattributed.

    The execution here deliberately still carries `orchestration_node_id`: that is
    the exact hazard the issue names — a coordinator must not be "arbitrarily
    charged to one of its children" just because a child node id is reachable. The
    coordinator branch returns before that field is ever read, and this asserts it,
    so the coordinator's own model calls persist a NULL address rather than
    inflating a child node's measured cost.
    """
    from src.agentauth.engine import validate_engine_authority

    store, _ = protected_engine
    work = await _protected_execution(session, store)
    work.flow.intent_ref = "4191"
    await session.commit()
    coordinator = {
        **work.execution,
        "coordinator_flow_id": {"S": work.grant.flow_id},
        "coordinator_intent_issue": {"N": "4191"},
        "persona": {"S": "operations"},
    }
    coordinator.pop("parent_principal", None)

    assert await validate_engine_authority(session=session, execution=coordinator, grant=work.grant) is None
    assert coordinator["orchestration_node_id"]["S"] == work.node.id


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda node: setattr(node, "state", NodeState.HALTED.value), "engine node is no longer authorized"),
        (lambda node: setattr(node, "attempts", 7), "engine node is no longer authorized"),
    ],
)
async def test_no_attribution_survives_a_node_that_is_no_longer_authorized(session, protected_engine, mutate, expected):
    """A completed, halted or reassigned node yields a refusal — never a stale address.

    This is why the attribution is composed at the END of the validated scope and
    captured before provider submission: attributing a node the caller is no longer
    authorized for at this attempt would report someone else's spend with the
    authority of a measurement, which is worse than reporting nothing.
    """
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.engine import validate_engine_authority

    store, _ = protected_engine
    work = await _protected_execution(session, store)
    mutate(work.node)
    await session.commit()

    with pytest.raises(BootstrapRefusedError, match=expected):
        await validate_engine_authority(session=session, execution=work.execution, grant=work.grant)


@pytest.mark.parametrize("tamper", ["run_id", "node_id", "attempt", "flow_id", "decision_id", "persona"])
async def test_protected_engine_refuses_changed_committed_identity(session, protected_engine, tamper):
    from copy import deepcopy
    from dataclasses import replace

    store, _ = protected_engine
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    await _make_node(session, flow)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    envelope = deepcopy(report.pending[0].envelope)
    if tamper == "run_id":
        envelope["message_id"] = "another-run"
    elif tamper == "persona":
        envelope["persona"] = "operations"
    else:
        key = "root_decision_id" if tamper == "decision_id" else tamper
        envelope["orchestration"][key] = 99 if tamper == "attempt" else "another-record"
    report.pending[0] = replace(report.pending[0], envelope=envelope)
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert not sqs.calls
    assert report.publish_failed == 1
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 0


async def test_engine_halt_blocks_bootstrap_refresh_http(session, session_factory, protected_engine, monkeypatch, report_only_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI

    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.routes import AgentRuntime, get_agent_runtime, router
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV
    from src.agentauth.workload import VerifiedPod

    store, _ = protected_engine
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    node = await _make_node(session, flow)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    envelope = sqs.envelope()
    pod = VerifiedPod("engine-pod", "engine-worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    runtime = AgentRuntime(store=store, workloads=SimpleNamespace(verify=lambda token: pod), env={CREDENTIAL_KEY_ENV: "gateway-key-test"})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    from src.shared.database import get_db

    app.dependency_overrides[get_db] = report_only_db
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    headers = {"X-Caller-Identity": "shared-worker-role", "X-Adp-Workload-Token": "verified-pod-proof"}
    body = {"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        first = await client.post("/internal/v1/agent/bootstrap", json=body, headers=headers)
        assert first.status_code == 200
        again = await client.post("/internal/v1/agent/bootstrap", json=body, headers=headers)
        assert again.status_code == 200
        assert again.json()["attempt"] == first.json()["attempt"] == 1
        node.state = NodeState.HALTED.value
        await session.commit()
        halted = await client.post("/internal/v1/agent/bootstrap", json=body, headers=headers)
        assert halted.status_code == 404
        assert "credential" not in halted.text
        auth_headers = {**headers, "X-Adp-Run-Credential": first.json()["credential"]}
        refused = await client.post(
            "/internal/v1/agent/dispatch",
            headers=auth_headers,
            json={"persona": "reviewer", "target": {"repo": REPO, "issue": 4196}, "request_id": "after-halt"},
        )
        assert refused.status_code == 404
        assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1


async def test_engine_refuses_missing_trusted_genesis_without_sqs(session, protected_engine):
    from dataclasses import replace

    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    await _make_node(session, flow)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    report.pending[0] = replace(report.pending[0], genesis=None)
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert not sqs.calls
    assert report.publish_failed == 1


async def test_unconfigured_evaluations_do_not_consume_the_dispatch_cap(session):
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    for i in range(10):
        node = await _make_node(session, flow, node_ref=f"eval-{i}", kind=NodeKind.EVAL.value, issue_ref=None)
        node.id = f"aaa-{i}"
    story = await _make_node(session, flow, node_ref="configured-story")
    story.id = "zzz-story"
    await session.flush()
    report = await run_dispatch_pass(session, _config(max_dispatches_per_tick=1))
    # Worker admission precedes evidence/misconfiguration reads. Reaching the
    # worker cap leaves unexamined evaluations for a later tick.
    assert report.undispatchable == 0
    assert report.dispatched == 1
    assert report.pending[0].node_id == story.id


@pytest.fixture(autouse=True)
def provider_repository_identity(monkeypatch):
    """Dispatch resolves immutable GitHub identity even with work claims off."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=12345))


async def test_new_story_requires_binding_without_enabling_work_claims(session, monkeypatch):
    from src.orchestration.models import OrchestrationWorkClaim

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1
    envelope = report.pending[0].envelope
    assert envelope["pr_binding_required"] is True
    assert envelope["source_ref"]["provider_repository_id"] == 12345
    assert not envelope.get("work_claim_required")
    assert list((await session.scalars(select(OrchestrationWorkClaim))).all()) == []
    dispatch = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value))).one()
    assert json.loads(dispatch.reason)["pr_binding_required"] is True


async def test_missing_repository_identity_refuses_before_dispatch(session, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(side_effect=RuntimeError("provider unavailable")))
    _, node, _ = await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    assert report.undispatchable == 1
    assert report.dispatched == 0
    assert not report.pending
    assert node.state == "ready"
    assert node.attempts == 0


@pytest.fixture
async def policy_bound_dispatch(monkeypatch):
    """The supporting services a policy-bound dispatch needs, and nothing more.

    A flow's spend allowance is only meaningful with a real atomic reservation
    backend, so an absent one denies with `budget_unavailable` — correctly, and
    unrelated to #5144. These are the same two fixtures `test_policy_admission.py`
    uses, reused rather than re-invented so a change in how budgets initialise cannot
    leave a stale copy here.
    """
    import fakeredis.aioredis

    from src.budget.reservations import ReservationStore
    from src.orchestration import flow_budget

    initialized: set[tuple[str, str]] = set()

    async def claim(*, org_id, flow_id, allow_create):
        key = (org_id, flow_id)
        if key in initialized:
            return False
        if not allow_create:
            raise RuntimeError("existing work requires reconciliation")
        initialized.add(key)
        return True

    monkeypatch.setattr("src.orchestration.flow_meter._claim_initialization", claim)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flow_budget, "_reservations", ReservationStore(redis_url=None, ttl_seconds=120, client=client))
    yield
    await client.aclose()


async def _accept_execution_policy(session: AsyncSession, flow: OrchestrationFlow, *, version: int = 1) -> None:
    """Persist an accepted plan carrying a real stamped execution policy (#5144).

    Uses the production `stamp_policy` rather than hand-writing the document, so these
    tests cannot pin a shape acceptance would never produce. Opting in is what makes a
    dispatch policy-bound, and only a policy-bound dispatch admits an execution — so
    without this the handoff marker is correctly absent.
    """
    from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits, stamp_policy
    from src.orchestration.models import OrchestrationAcceptedPlan

    policy = ExecutionPolicy(
        org_id=flow.org_id,
        repository_ids=[REPO],
        allowed_actions=[Action.DEVELOP, Action.REPAIR, Action.MERGE, Action.EVALUATE],
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        limits=PolicyLimits(
            max_wall_clock_seconds=86_400,
            max_spend_usd=Decimal("100"),
            max_attempts_per_node=10,
            max_concurrent_actions=5,
        ),
    )
    # A policy-bound dispatch checks the approver's CURRENT role, so the membership row
    # has to exist or admission denies with `role_revoked` — correctly. `_make_approval`
    # creates only the `users` row, which is enough for an unpolicied flow.
    from src.shared.models.onboarding import TenantMembership

    if not await session.scalar(select(TenantMembership).where(TenantMembership.user_id == APPROVER)):
        session.add(TenantMembership(user_id=APPROVER, tenant_id=flow.org_id, role="org_admin"))

    stamped = stamp_policy(policy, principal_id=APPROVER, org_id=flow.org_id)
    session.add(
        OrchestrationAcceptedPlan(
            org_id=flow.org_id,
            flow_id=flow.id,
            version=version,
            plan_document={"flow_slug": flow.slug, "execution_policy": stamped.model_dump(mode="json")},
            plan_hash=f"hash-v{version}",
        )
    )
    await session.flush()


async def _seed_conflicting_execution(session: AsyncSession, flow: OrchestrationFlow, node: OrchestrationNode, *, plan_version: int) -> str:
    """Make this node's next dispatch hit a real `create_execution` CONFLICT (#5144 F3).

    Reaching the refusal through production code rather than a monkeypatch, because
    the defect was a *branch* in `_admit_execution` and a patched loader would let that
    branch be asserted without ever proving the real one can reach it.

    How it works: a *released* claim row is seeded first, so the claim id this dispatch
    will be readmitted under is known here (`claim_work` reuses the row and advances
    its generation — it never inserts a replacement, see `_binding_conflict`). An
    execution for `(org, node, cycle)` is then written carrying that exact claim id and
    generation, but a DIFFERENT `accepted_plan_version`. The dispatch is admitted under
    the in-force plan, finds this row, and `_binding_conflict` answers
    `accepted_plan_version_mismatch` — the arm that makes `create_execution` return
    CONFLICT.

    Production reaches this state when the accepted plan is amended between an
    execution being admitted and the node being dispatched again. What matters for F3
    is that the flow is *unambiguously policy-bound* throughout: this is precisely the
    shape the old boolean flattened into "no policy applies" and published unmarked.

    Returns the seeded execution's id so a caller can assert it was left untouched.
    """
    from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus
    from src.orchestration.models import ClaimState, OrchestrationExecution, OrchestrationWorkClaim
    from src.orchestration.work_claims import OwnerKind

    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        # The id `work_claims_enabled` makes `resolve_repository_id` return, so the
        # dispatch's admission binds to THIS row instead of inserting its own.
        provider_repository_id=12345,
        issue_number=int(str(node.issue_ref).lstrip("#")),
        owner_kind=OwnerKind.ENGINE_FLOW.value,
        owner_ref=flow.id,
        # Released, so the dispatch is legitimately admitted rather than refused for
        # ownership — the refusal under test must be the execution admission's, not a
        # work-claim conflict wearing its clothes.
        state=ClaimState.RELEASED.value,
        generation=1,
        release_reason="completed",
    )
    session.add(claim)
    await session.flush()

    execution = OrchestrationExecution(
        org_id=flow.org_id,
        flow_id=flow.id,
        node_id=node.id,
        # `dispatch_node` increments `attempts` before the admission runs, so the
        # cycle this dispatch will ask for is one past what the node carries now.
        cycle=node.attempts + 1,
        phase=ExecutionPhase.ADMITTED.value,
        status=ExecutionStatus.RUNNABLE.value,
        revision=1,
        accepted_plan_version=plan_version,
        claim_id=claim.id,
        # Ordered reuse of a released claim advances the generation by one, so this is
        # the generation the dispatch will hold. Equal rather than older on purpose: an
        # older stored generation would be adopted, and a newer one would refuse as
        # `claim_generation_superseded` — neither is the plan-version arm under test.
        claim_generation=claim.generation + 1,
        next_check_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(execution)
    await session.flush()
    return execution.id


async def _decisions_of_kind(session: AsyncSession, kind: DecisionKind) -> list[OrchestrationDecision]:
    """Every recorded decision of one kind, in insertion order."""
    return list((await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == kind.value))).all())


async def test_opted_in_story_is_marked_as_owing_a_handoff_and_has_a_real_execution(session, work_claims_enabled, policy_bound_dispatch):
    """#5144: the marker rides the envelope AND the decision — and names a real execution.

    The second half is the point. An earlier revision of this test seeded only a ready
    story plus a work claim and asserted the marker, which passed while the receipt the
    marker demands had no producer at all: `create_execution` had no production call
    site, so the endpoint could never issue one and `results` would have held the story
    forever. So this asserts the execution row exists, under the claim generation the
    dispatch was admitted with — that is what makes the promise keepable.
    """
    from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow, version=7)

    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1
    assert report.pending[0].envelope["handoff_required"] is True
    dispatch = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value))).one()
    assert json.loads(dispatch.reason)["handoff_required"] is True

    # The producer the marker promises. Without this row the marker is a permanent hold.
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    assert execution.cycle == node.attempts
    assert execution.accepted_plan_version == 7
    # Bound to the generation this dispatch admitted, not merely to some claim.
    assert (execution.claim_id, execution.claim_generation) == (claim.id, claim.generation)

    # #5144 item 1: the fences the worker validates the receipt against. Without them
    # the worker can confirm the gateway said "yes" but not that the "yes" was about
    # its own dispatch, which is the readback's entire purpose.
    from src.orchestration.policy_admission import load_in_force_policy

    policy = (await load_in_force_policy(session, org_id=flow.org_id, flow_id=flow.id)).policy
    assert report.pending[0].envelope["handoff_expect"] == {
        "contract_version": 1,
        "execution_id": execution.id,
        "policy_id": policy.policy_id,
        "policy_hash": policy.policy_hash,
        "org_id": flow.org_id,
        "flow_id": flow.id,
        "node_id": node.id,
        "cycle": execution.cycle,
        "accepted_plan_version": 7,
        "claim_id": claim.id,
        "claim_generation": claim.generation,
    }


async def test_published_fences_match_the_committed_execution_exactly(session, work_claims_enabled, policy_bound_dispatch):
    """The envelope's fences and the stored execution are the same facts.

    Asserted as a whole-row comparison rather than field by field: the worker refuses
    on any disagreement, so a single drifting field here would refuse every legitimate
    handoff in production — a total delivery outage rather than a subtle bug. This is
    the test that catches it in CI instead.
    """
    from src.orchestration.models import OrchestrationExecution

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow, version=7)

    report = await run_dispatch_pass(session, _config())
    execution = (await session.scalars(select(OrchestrationExecution))).one()
    expect = report.pending[0].envelope["handoff_expect"]

    assert {k: expect[k] for k in ("org_id", "flow_id", "node_id", "cycle", "accepted_plan_version", "claim_id", "claim_generation")} == {
        "org_id": execution.org_id,
        "flow_id": execution.flow_id,
        "node_id": execution.node_id,
        "cycle": execution.cycle,
        "accepted_plan_version": execution.accepted_plan_version,
        "claim_id": execution.claim_id,
        "claim_generation": execution.claim_generation,
    }


# ---------------------------------------------------------------------------
# #5144 F3: a refusal is not "no policy". Only genuine absence keeps legacy.
# ---------------------------------------------------------------------------


async def test_an_unreadable_policy_refuses_the_dispatch_instead_of_publishing_it_unmarked(session, work_claims_enabled, policy_bound_dispatch):
    """The F3 defect, reproduced and closed.

    `_admit_execution` used to answer `False` for BOTH "this flow has no policy" and
    "a policy applies and could not be verified". The caller published the second case
    as an *unmarked* dispatch — so a policy-bound story went out able to complete with
    no continuation receipt at all, which is precisely the hole #5144 exists to shut.

    A refusal must therefore publish nothing, leave the node `ready` for a later pass,
    and record why. Asserted on all three, because publishing nothing while silently
    losing the reason would just move the invisibility.
    """
    from src.orchestration.models import OrchestrationExecution

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow, version=7)
    # The real refusal, reached by the real code path and no monkeypatch: an execution
    # row already exists for this (org, node, cycle) recorded under a DIFFERENT
    # accepted plan version, so `create_execution` answers CONFLICT —
    # `accepted_plan_version_mismatch`. The flow is unambiguously policy-bound here,
    # which is exactly the shape the old bool flattened into "no policy" before
    # publishing the story unmarked. Production reaches this when a plan is amended
    # between the execution being admitted and a re-dispatch.
    seeded_id = await _seed_conflicting_execution(session, flow, node, plan_version=6)

    report = await run_dispatch_pass(session, _config())

    # Nothing published, and it is reported as a refusal rather than as an idle pass.
    assert report.pending == []
    assert report.dispatched == 0
    assert report.admission_refused == 1
    # NOT counted as an error: a boundary refusing is the system working. Were this an
    # error every tick would report failure for as long as a policy stayed unreadable.
    assert report.errors == 0
    assert report.success is True

    # The node is still ready, so a later pass retries once the policy is readable.
    await session.refresh(node)
    assert node.state == NodeState.READY.value
    # No SECOND execution, and the conflicting one is untouched. The seeded row is the
    # *cause* of the refusal, so it is expected to survive — what must not happen is a
    # rival identity for the same cycle, or this dispatch overwriting the authority it
    # was just refused against. A bare "no executions exist" assertion would have been
    # unsatisfiable here and would have hidden both.
    executions = list((await session.scalars(select(OrchestrationExecution))).all())
    assert [e.id for e in executions] == [seeded_id]
    assert (executions[0].accepted_plan_version, executions[0].revision) == (6, 1)
    # No dispatch decision either: the attempt was never burned.
    assert await _decisions_of_kind(session, DecisionKind.NODE_DISPATCHED) == []

    # And the refusal is durable, typed, and names who resolves it.
    (rejected,) = await _decisions_of_kind(session, DecisionKind.TRANSITION_REJECTED)
    assert rejected.node_id == node.id
    # The node went nowhere; a recorded destination would read as an undone dispatch.
    assert rejected.to_state is None
    detail = json.loads(rejected.rejection_reason)
    assert detail["block_code"] == "authority_unverifiable"
    assert detail["owner"] == "platform-operator"
    assert detail["required_input"]


async def test_a_refusal_releases_the_work_claim_it_reserved(session, work_claims_enabled, policy_bound_dispatch):
    """An abandoned dispatch must not strand ownership of the issue.

    The refusal unwinds a savepoint that already admitted a claim. If that ownership
    survived, the story would be permanently unclaimable by the retry this refusal
    exists to allow — a refusal that deadlocks the work is worse than the defect.
    """
    from src.orchestration.models import ClaimState, OrchestrationWorkClaim

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow, version=7)
    await _seed_conflicting_execution(session, flow, node, plan_version=6)

    report = await run_dispatch_pass(session, _config())

    assert report.admission_refused == 1
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    # Rolled fully back to the released row the fixture seeded — not merely "not held".
    # The generation is asserted too: `claim_work` advances it on readmission, so a
    # surviving generation 2 would mean the reservation's write escaped the savepoint
    # even though its state did not, and the next retry would be fenced out of its own
    # execution by an authority nothing ever used.
    assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.RELEASED.value, 1, None)


async def test_a_refusal_leaves_no_dispatch_record_for_results_to_hold_a_story_on(session, work_claims_enabled, policy_bound_dispatch):
    """The other half of F3: a refusal must not be a *marked* dispatch either.

    Publishing with the marker and no admissible execution would hold the story forever
    waiting for a receipt nothing can issue — the opposite failure to the one above, and
    the reason the refusal abandons the dispatch rather than merely stamping it.

    Asserted through `handoff.handoff_required`, the production predicate `results`
    calls, rather than by inspecting the envelope: the marker `results` acts on lives on
    the `NODE_DISPATCHED` decision, and it is that reader — not the queue message — that
    decides whether a missing receipt holds the node.
    """
    from src.orchestration.handoff import handoff_required

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow, version=7)
    await _seed_conflicting_execution(session, flow, node, plan_version=6)

    report = await run_dispatch_pass(session, _config())

    assert (report.pending, report.admission_refused) == ([], 1)
    # No dispatch record at all, so there is nothing for `results` to read a marker off.
    assert await _decisions_of_kind(session, DecisionKind.NODE_DISPATCHED) == []
    # And the predicate itself refuses to hold a story on the refusal row that *does*
    # exist — a `TRANSITION_REJECTED` decision is evidence of a refusal, never of a
    # promised continuation.
    (rejected,) = await _decisions_of_kind(session, DecisionKind.TRANSITION_REJECTED)
    assert handoff_required(json.loads(rejected.rejection_reason)) is False


async def test_policy_absent_story_owes_no_handoff_and_admits_no_execution(session, work_claims_enabled, policy_bound_dispatch):
    """A flow with no accepted policy keeps legacy semantics exactly (#5128).

    A held work claim says who owns the issue; it does not make the flow policy-bound.
    Marking such a dispatch would hold it forever for evidence nothing can produce, so
    the marker must stay absent and no execution may be created.
    """
    from src.orchestration.models import OrchestrationExecution

    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1
    envelope = report.pending[0].envelope
    # Still bound by #5301's PR contract; just not by this one.
    assert envelope["pr_binding_required"] is True
    assert "handoff_required" not in envelope
    dispatch = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value))).one()
    assert json.loads(dispatch.reason)["handoff_required"] is False
    assert (await session.scalars(select(OrchestrationExecution))).all() == []


async def test_non_story_nodes_owe_no_handoff(session, work_claims_enabled):
    """An evaluation's completion boundary is the human gate, not a worker handoff.

    Left unpolicied deliberately so this pins the node-kind rule and nothing else: an
    accepted policy denies a machine-accepted evaluation for its own unrelated reason,
    which would make this test pass without ever exercising the kind check.
    """
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    await _make_node(session, flow, node_ref="eval-1", kind=NodeKind.EVAL.value)
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1
    assert "handoff_required" not in report.pending[0].envelope


@pytest.mark.parametrize("unreadable_policy", [False, True])
async def test_governed_dispatch_refuses_when_claims_are_disabled(session, monkeypatch, policy_bound_dispatch, unreadable_policy):
    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow)
    if unreadable_policy:
        from src.orchestration.models import OrchestrationAcceptedPlan

        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
        plan.plan_document = {"execution_policy": {"schema_version": "unreadable"}}
        await session.flush()
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 0 and not report.pending
    assert await _state_of(session, node.id) == NodeState.READY.value
    refusal = (
        await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == DecisionKind.TRANSITION_REJECTED.value,
            )
        )
    ).one()
    assert json.loads(refusal.rejection_reason)["block_code"] == "authority_unverifiable"


class TestSavedPersonaMapping:
    @pytest.mark.parametrize("unavailable", [False, True])
    async def test_selection_precedes_state_change_and_uses_approver(self, session, monkeypatch, unavailable):
        from src.admin.persona_models import dispatch_selection

        monkeypatch.setenv("PERSONA_MODEL_MAPPING_ENABLED", "true")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
        _flow, node, _decision = await _ready_story(session)

        async def select(db, *, org_id, user_id, persona):
            assert user_id == APPROVER
            assert org_id == ORG_A
            assert persona == "developer"
            assert await _state_of(db, node.id) == NodeState.READY.value
            if unavailable:
                raise RuntimeError("lookup unavailable")
            return {"model": "saved-model", "source": "principal-mapping"}

        monkeypatch.setattr(dispatch_selection, "select_for_dispatch", select)
        report = await run_dispatch_pass(session, _config())
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)
        if unavailable:
            assert report.dispatched == 0
            assert not sqs.calls
            assert await _state_of(session, node.id) == NodeState.READY.value
            assert report.policy_block_reasons["persona_model_selection_unavailable"] == 1
        else:
            assert report.dispatched == 1
            assert sqs.envelope()["model_resolved"] == "saved-model"


async def test_retry_dispatch_carries_provider_verified_pr_before_publish(session, monkeypatch):
    from unittest.mock import AsyncMock

    from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target

    _flow, node, _ = await _ready_story(session)
    first = await run_dispatch_pass(session, _config())
    assert first.dispatched == 1
    pr = PullRequestIdentity(12345, "PR_existing", REPO, 777, "a" * 40)
    target = await resolve_registration_target(session, run_id=first.pending[0].envelope["message_id"])
    original, _ = await register_binding(session, target=target, pr=pr, actor_id="worker", actor_kind=ActorKind.SERVICE)
    node.state = "ready"
    await session.flush()
    refreshed = PullRequestIdentity(12345, "PR_existing", REPO, 777, "b" * 40)
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", AsyncMock(return_value=refreshed))
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 1 and report.success
    assert node.attempts == 2
    assert original.attempt == 2 and original.head_sha == "b" * 40 and original.revision == 2
    assert report.pending[0].envelope["bound_pull_request"]["pr_number"] == 777
    assert report.pending[0].envelope["bound_pull_request"]["provider_pr_node_id"] == "PR_existing"


async def test_unverifiable_retry_pr_does_not_consume_attempt_or_publish(session, monkeypatch):
    from unittest.mock import AsyncMock

    from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target

    _flow, node, _ = await _ready_story(session)
    first = await run_dispatch_pass(session, _config())
    pr = PullRequestIdentity(12345, "PR_existing", REPO, 777, "a" * 40)
    target = await resolve_registration_target(session, run_id=first.pending[0].envelope["message_id"])
    await register_binding(session, target=target, pr=pr, actor_id="worker", actor_kind=ActorKind.SERVICE)
    node.state = "ready"
    await session.flush()
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", AsyncMock(side_effect=RuntimeError("provider unavailable")))
    report = await run_dispatch_pass(session, _config())
    assert report.dispatched == 0 and not report.pending
    assert node.state == "ready" and node.attempts == 1
    assert report.policy_block_reasons == {"repair_binding_unverifiable": 1}


async def test_paused_flows_do_not_consume_dispatch_capacity_or_attempts(session):
    await _make_org(session)
    paused = await _make_flow(session, slug="paused")
    paused.execution_paused = True
    await _make_approval(session, paused)
    waiting = await _make_node(session, paused, node_ref="waiting", issue_ref="4196")
    enabled = await _make_flow(session, slug="enabled")
    await _make_approval(session, enabled)
    ready = await _make_node(session, enabled, node_ref="ready", issue_ref="4197")
    result = await run_dispatch_pass(session, _config(max_dispatches_per_tick=1))
    assert result.dispatched == 1
    assert result.pending[0].node_id == ready.id
    await session.refresh(waiting)
    assert (waiting.state, waiting.attempts) == ("ready", 0)
