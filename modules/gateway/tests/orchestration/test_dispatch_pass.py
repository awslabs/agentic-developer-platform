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
    assert report.undispatchable == 10
    assert report.dispatched == 1
    assert report.pending[0].node_id == story.id
