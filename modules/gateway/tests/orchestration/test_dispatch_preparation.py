"""PMM-07: the orchestration dispatch actually carries a model-policy snapshot.

This file replaces `test_engine_root_snapshot_gap.py`, which pinned the *absence*
of this behaviour. That file's assertions were only ever honest as a statement
about the code at the time, so leaving them in place after the ordering is fixed
would have meant a test suite that fails when the product gets better. What is
worth keeping from it is the reasoning, which survives here as the thing being
proved rather than the thing being lamented: a snapshot can only attach while the
protected execution is `pending`, and the post-commit/pre-publish window is the
only moment that is true.

Everything here runs the **real** composition: the real dispatch pass, the real
`EngineAuthorityWriter`, the real `ensure_snapshot_for_admission` and the real
snapshot reader. `test_tick_dispatch_preparation.py` is the companion that drives
the real `tick_handler._run()`, and it is the load-bearing one: a preparation
helper that nothing calls would satisfy every test in *this* file, and an unused
helper is exactly the failure mode PMM-07 inherited.

Only the edges are doubled: SQS (so the published bytes are inspectable),
DynamoDB (moto), and the provider repository lookup. No live AWS, no provider
calls, no paid model call -- the snapshot is built from database rows and the
catalogue, and nothing here invokes a model.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.model_policy import parse_snapshot, policy_digest
from src.orchestration.dispatch_pass import prepare_pending, publish_pending, run_dispatch_pass
from src.orchestration.models import NodeKind, OrchestrationNode

# Imported for their side effect on the shared metadata: the snapshot builder
# reads these tables, so they must exist in the fixture's schema.
from src.shared.models.persona_models import (  # noqa: F401
    PersonaModelPolicySetting,
    PersonaModelPreference,
)

# Reused verbatim from the dispatch pass's own tests, so the seam is exercised on
# the same shapes the pass is already pinned against rather than on a
# purpose-built fixture that could drift into agreeing with the code. Aliased on
# import and rebound below, which is this repo's idiom for borrowed fixtures (see
# `tests/agentauth/test_fan_out_dispatch.py`): a direct import would shadow each
# test's same-named parameter and trip ruff's F811.
from tests.orchestration.test_dispatch_pass import (
    APPROVER,
    ORG_A,
    ORG_B,
    FakeSQS,
    _config,
    _make_approval,
    _make_flow,
    _make_node,
    _make_org,
    _ready_story,
)
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import protected_engine as protected_engine_fixture
from tests.orchestration.test_dispatch_pass import provider_repository_identity as provider_repository_identity_fixture
from tests.orchestration.test_dispatch_pass import run_store as run_store_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_dispatch_pass import work_claims_enabled as work_claims_enabled_fixture

engine = engine_fixture
session = session_fixture
session_factory = session_factory_fixture
protected_engine = protected_engine_fixture
provider_repository_identity = provider_repository_identity_fixture
run_store = run_store_fixture
work_claims_enabled = work_claims_enabled_fixture

INSTALLATION_B = 55_502


def _snapshot_of(store, *, tenant_id: str, invocation_id: str):
    """Read the stored snapshot back through the real parser.

    Deliberately not `json.loads`: `parse_snapshot` is the reader every consumer
    uses, and it re-derives the digest and checks the audience and bindings. A
    snapshot that only survives a raw JSON read is not one a worker could use.
    """
    execution = store._read(f"TENANT#{tenant_id}", f"EXEC#{invocation_id}")
    return parse_snapshot(
        execution["model_policy_snapshot"]["S"],
        execution["model_policy_snapshot_digest"]["S"],
        tenant_id=tenant_id,
        policy_revision=execution["model_policy_revision"]["S"],
        correlation_id=execution["model_policy_correlation_id"]["S"],
        root_invocation_id=execution["model_policy_root_invocation_id"]["S"],
    )


async def test_a_dispatched_node_reaches_the_queue_with_a_snapshot_attached(session, protected_engine):
    """The behaviour PMM-07 exists for, end to end on one real dispatch.

    The old gap test asserted `[key for key in execution if "model_policy" in key] == []`.
    The same read is made here and must now be non-empty, which is the single
    clearest statement that the blocker is gone.
    """
    store, writer = protected_engine
    flow, node, _ = await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    intended = json.loads(json.dumps(report.pending[0].envelope, default=str))
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)

    receipt = report.model_policy_receipts[invocation_id]
    assert receipt["status"] == "available"
    assert receipt["root_invocation_id"] == invocation_id

    snapshot = _snapshot_of(store, tenant_id=ORG_A, invocation_id=invocation_id)
    assert receipt["snapshot_digest"] == policy_digest(snapshot.to_dict())
    # The snapshot describes the human who approved the wave, resolved
    # server-side from the approval row -- not anything a message carried.
    assert (snapshot.principal_kind, snapshot.principal_id) == ("human", APPROVER)
    assert snapshot.correlation_id == flow.id
    assert snapshot.source == "live"

    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert len(sqs.calls) == 1
    # Preparation did not rewrite the message. The envelope published is the one
    # the committed dispatch decided on, byte-for-byte, which is what keeps the
    # digest the protected record was keyed to valid.
    assert sqs.envelope() == intended
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["envelope_digest"]["S"] == envelope_digest(sqs.envelope())
    assert report.publish_failed == 0
    assert report.success
    assert (await session.scalar(select(OrchestrationNode.state).where(OrchestrationNode.id == node.id))) == "running"


async def test_the_snapshot_attaches_while_the_execution_is_still_pending(session, protected_engine):
    """The window, asserted as a window rather than as a comment.

    `_persist_snapshot` requires `status = pending`, and a published message is
    what lets a worker bind and flip it to `active`. So the property that makes
    this seam correct is that preparation completes before anything is sent --
    here, the execution is observed `pending` with its snapshot already stored,
    while the queue is still empty.
    """
    store, writer = protected_engine
    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)

    execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")
    assert execution["status"] == {"S": "pending"}
    assert "model_policy_snapshot" in execution


async def test_preparing_the_same_dispatch_twice_changes_nothing(session, protected_engine):
    """A retried tick must not double-provision, double-attach or double-publish.

    Both halves are genuinely idempotent rather than guarded by a flag we set:
    `provision_pending` resolves a conflicting write against the stored pointer,
    and `ensure_snapshot_for_admission` returns the existing snapshot instead of
    building a second one. This test is what makes it safe for `publish_pending`
    to keep its own `provision()` call.
    """
    store, writer = protected_engine
    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)
    first = dict(report.model_policy_receipts[invocation_id])
    snapshot_after_first = store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["model_policy_snapshot"]["S"]

    await prepare_pending(session, report, writer=writer)

    assert report.model_policy_receipts[invocation_id] == first
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["model_policy_snapshot"]["S"] == snapshot_after_first
    assert len(report.pending) == 1
    assert report.publish_failed == 0

    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert len(sqs.calls) == 1


async def test_unavailable_policy_evidence_does_not_change_what_executes(session, protected_engine, monkeypatch):
    """Report-only means a proposal defect costs the run nothing.

    The live tenant allowlist is made unavailable, which is a real failure mode of
    a config-sourced read and the one `build_root_snapshot` turns into
    `not_permitted`. The dispatch must still publish, the node must stay
    `running`, and the pass must still report success -- while the failure is
    recorded as evidence rather than hidden. Treating this as an error would mean
    a model-policy bug could stop the engine from working at all, which is
    precisely what report-only forbids.
    """
    store, writer = protected_engine
    await _ready_story(session)

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError("tenant policy source unavailable")

    monkeypatch.setattr("src.proxy.model_resolver.production_model_resolver", _unavailable)

    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)

    assert report.model_policy_receipts[invocation_id] == {"status": "unavailable", "reason": "not_permitted"}
    # No half-written snapshot: unavailable evidence stores nothing rather than
    # storing something a worker would then treat as authoritative.
    assert "model_policy_snapshot" not in store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")
    # And the authority gate it is unrelated to is untouched: the protected record
    # and its grant exist, so the run is as admissible as it was before.
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["status"] == {"S": "pending"}

    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert len(sqs.calls) == 1
    assert report.errors == 0
    assert report.publish_failed == 0
    assert report.success


async def test_a_protected_authority_failure_stops_that_dispatch_and_is_counted(session, protected_engine):
    """The other failure class, and it is deliberately NOT treated like evidence.

    A dispatch whose protected record cannot be created must not be published: it
    would reach a worker that has nothing to authorize it against. That is a real
    failure of the dispatch, so it counts `publish_failed` and forces a non-success
    report -- the same accounting `publish_pending` would have applied.
    """
    store, writer = protected_engine
    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    class Refusing:
        store = writer.store

        def provision(self, _pending):
            raise RuntimeError("authority store unavailable")

    await prepare_pending(session, report, writer=Refusing())

    assert report.publish_failed == 1
    assert report.per_org[ORG_A]["publish_failed"] == 1
    assert not report.success
    assert report.pending == []
    assert report.model_policy_receipts == {}
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}") is None

    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert sqs.calls == []


async def test_one_failing_node_does_not_stop_another_from_being_prepared(session, protected_engine):
    """Containment is per node, matching every other stage of the pass.

    Two tenants dispatch in one tick and the first one's provisioning fails. The
    second must still be provisioned, snapshotted and published: one tenant's
    authority-store trouble is not a reason to stall another tenant's work.
    """
    store, writer = protected_engine
    await _make_org(session)
    flow_a = await _make_flow(session)
    await _make_approval(session, flow_a)
    await _make_node(session, flow_a, node_ref="s-a", issue_ref="4196")
    await _make_org(session, org_id=ORG_B, installations=[INSTALLATION_B])
    flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
    await _make_approval(session, flow_b, org_id=ORG_B)
    await _make_node(session, flow_b, node_ref="s-b", issue_ref="4197", org_id=ORG_B)

    report = await run_dispatch_pass(session, _config())
    await session.commit()
    assert report.dispatched == 2
    doomed, survivor = report.pending[0], report.pending[1]

    class FailingFirst:
        store = writer.store

        def provision(self, pending):
            if pending.node_id == doomed.node_id:
                raise RuntimeError("authority store unavailable for this tenant")
            return writer.provision(pending)

    await prepare_pending(session, report, writer=FailingFirst())

    assert report.publish_failed == 1
    assert [p.node_id for p in report.pending] == [survivor.node_id]
    assert list(report.model_policy_receipts) == [survivor.invocation_id()]
    assert report.model_policy_receipts[survivor.invocation_id()]["status"] == "available"
    assert store._read(f"TENANT#{survivor.org_id}", f"EXEC#{survivor.invocation_id()}")["model_policy_snapshot"]["S"]

    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert [json.loads(c["MessageBody"])["message_id"] for c in sqs.calls] == [survivor.invocation_id()]


async def test_the_invocation_identity_is_the_committed_one_and_is_not_recomputed(session, protected_engine):
    """Root, envelope and snapshot identity all agree, by derivation not by copy.

    `PendingPublish.invocation_id()` is the single definition, and the writer
    independently recomputes the same value from `(node_id, attempt)` and refuses a
    mismatch. So this asserts the chain: committed decision row -> envelope
    `message_id` -> protected record key -> snapshot `root_invocation_id`.
    """
    store, writer = protected_engine
    _, node, _ = await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    pending = report.pending[0]

    await prepare_pending(session, report, writer=writer)

    invocation_id = pending.invocation_id()
    assert pending.envelope["message_id"] == invocation_id
    assert pending.envelope["orchestration"]["node_id"] == node.id
    assert pending.envelope["orchestration"]["attempt"] == pending.node_attempt
    snapshot = _snapshot_of(store, tenant_id=ORG_A, invocation_id=invocation_id)
    assert snapshot.root_invocation_id == invocation_id
    assert snapshot.tenant_id == ORG_A == pending.envelope["tenant_id"]
    # The prepared envelope is the committed one: preparation returns what the
    # writer provisioned against, and that must equal what dispatch decided.
    assert report.pending[0].envelope == pending.envelope


async def test_preparation_is_inert_when_nothing_was_dispatched(session, protected_engine):
    """An idle tick must not touch the authority store at all."""
    _, writer = protected_engine
    report = await run_dispatch_pass(session, _config())
    assert report.pending == []

    class Forbidden:
        store = writer.store

        def provision(self, _pending):
            raise AssertionError("an idle tick must not provision anything")

    await prepare_pending(session, report, writer=Forbidden())
    assert report.model_policy_receipts == {}
    assert report.success


async def test_unprotected_environments_are_left_exactly_as_they_were(session, protected_engine, monkeypatch):
    """No protected record means nothing to attach to -- and nothing to fail on.

    `AGENT_AUTHORITY_ENABLED=false` is a deployment without the protected store,
    where `publish_pending` registers the run itself and never provisions. That
    path must be untouched: no receipt, no error, and the dispatch still publishes.
    """
    _, writer = protected_engine
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()

    class Forbidden:
        store = writer.store

        def provision(self, _pending):
            raise AssertionError("an unprotected deployment has no record to provision")

    await prepare_pending(session, report, writer=Forbidden())

    assert report.model_policy_receipts == {}
    assert report.success
    sqs = FakeSQS()
    publish_pending(report, _config(), client=sqs)
    assert len(sqs.calls) == 1


@pytest.mark.parametrize("kind", [NodeKind.STORY.value, NodeKind.EVAL.value])
async def test_both_dispatchable_node_kinds_carry_a_snapshot(session, protected_engine, kind):
    """An evaluation node is dispatched as `operations`, and is not a special case.

    Covered because the writer's persona allowlist differs by node kind, so a
    preparation that happened to work only for the story persona would still look
    complete against every other test here.
    """
    store, writer = protected_engine
    await _make_org(session)
    flow = await _make_flow(session)
    await _make_approval(session, flow)
    await _make_node(session, flow, kind=kind)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)

    assert report.model_policy_receipts[invocation_id]["status"] == "available"
    persona = "operations" if kind == NodeKind.EVAL.value else "developer"
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["persona"] == {"S": persona}
    snapshot = _snapshot_of(store, tenant_id=ORG_A, invocation_id=invocation_id)
    assert persona in snapshot.persona_contracts


async def test_work_claims_and_the_snapshot_coexist_on_one_dispatch(session, protected_engine, work_claims_enabled):
    """Preparation must not disturb the ownership reservation the tick committed.

    With work claims on, the claim row is written inside the tick transaction and
    committed before preparation runs. The snapshot build reads the same session,
    so a careless read could abort that transaction -- hence the savepoints inside
    `build_root_snapshot`. Asserted here on the real claim row.
    """
    from src.orchestration.models import OrchestrationWorkClaim

    _, writer = protected_engine
    flow, _, _ = await _ready_story(session)
    report = await run_dispatch_pass(session, _config())
    await session.commit()
    invocation_id = report.pending[0].invocation_id()

    await prepare_pending(session, report, writer=writer)

    assert report.model_policy_receipts[invocation_id]["status"] == "available"
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    assert claim.active_run_id == invocation_id
    assert claim.owner_ref == flow.id
