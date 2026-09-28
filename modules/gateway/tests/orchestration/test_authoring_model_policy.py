"""Replan publication captures the verified requester's model choice before SQS.

Exercise the command flush, real authority writer and snapshot persistence with
SQLite/moto; no live AWS or model calls.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import update

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.grants import AUTHORITY_REPLAN_REQUEST, AgentAction, TargetRelationship
from src.agentauth.model_policy import resolve_decision
from src.orchestration.authoring_dispatch import recover_owed_authoring
from src.orchestration.engine_commands import EngineCommandReport
from src.orchestration.models import AmendmentRequestState, OrchestrationAmendmentRequest
from src.shared.models.organization import User
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference

from .test_authoring_authority_refusal import protected as protected_fixture
from .test_dispatch_preparation import _snapshot_of
from .test_pending_amendments import ORG_A
from .test_replan_authoring_dispatch import ASKER, INSTALLATION, REPO, FakeSQS, flow_with_asker, flush, replan
from .test_replan_authoring_dispatch import session as session_fixture

session = session_fixture
protected = protected_fixture

pytestmark = pytest.mark.asyncio

OPUS = "global.anthropic.claude-opus-5"
SONNET = "global.anthropic.claude-sonnet-4-6"
ASKER_SUB = "replan-requester-login-sub"


async def assignment(session):
    flow_id = await flow_with_asker(session)
    # Separate the login subject from users.id: preferences belong to the latter.
    await session.execute(update(User).where(User.id == ASKER).values(cognito_sub=ASKER_SUB))
    session.add(
        PersonaModelPreference(
            id="authoring-preference",
            org_id=ORG_A,
            principal_kind="human",
            principal_source="self",
            principal_id=ASKER,
            persona_key="aidlc",
            canonical_model_id=OPUS,
            requested_alias="opus",
            revision=1,
            updated_by=ASKER,
            updated_by_source="self",
        )
    )
    session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await session.commit()
    report = EngineCommandReport()
    applied, _ = await replan(session, report, flow_id=flow_id, user_id=ASKER_SUB)
    assert applied and len(report.pending_authoring) == 1
    return report, report.pending_authoring[0]


async def test_command_flush_persists_requesters_mapping_before_publish(session, protected, monkeypatch):
    store, _ = protected
    report, pending = await assignment(session)
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}") is None

    class InspectingSQS(FakeSQS):
        def send_message(self, **kwargs):
            execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}")
            assert execution["status"] == {"S": "pending"}
            snapshot = _snapshot_of(store, tenant_id=ORG_A, invocation_id=pending.author_run_id)
            assert (snapshot.principal_kind, snapshot.principal_id) == ("human", ASKER)
            assert snapshot.correlation_id == pending.flow_id
            assert snapshot.root_invocation_id == pending.author_run_id
            decision = resolve_decision(snapshot, invocation_id=pending.author_run_id, persona="aidlc")
            assert (decision.resolved_model_id, decision.resolution_source) == (OPUS, "principal-mapping")
            return super().send_message(**kwargs)

    sqs = InspectingSQS()
    await flush(session, report, sqs, monkeypatch)
    assert len(sqs.calls) == 1
    assert sqs.envelope() == pending.envelope
    execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}")
    assert execution["envelope_digest"]["S"] == envelope_digest(sqs.envelope())
    grant = store.live_grant(invocation_id=pending.author_run_id, tenant_id=ORG_A, attempt=1, now=datetime.now(UTC))
    assert grant.authority.kind == AUTHORITY_REPLAN_REQUEST
    assert grant.authority.human_id == ASKER_SUB
    assert grant.allowed_actions == frozenset({AgentAction.MONITOR})
    assert grant.target_relationships == frozenset({TargetRelationship.SELF})
    assert not grant.delegable_actions
    assert grant.max_chain_depth == grant.max_dispatch_concurrency == 0
    request = await session.get(OrchestrationAmendmentRequest, pending.request_id)
    assert request.state == AmendmentRequestState.DISPATCHED.value


async def test_failed_publish_retries_with_frozen_mapping(session, protected, monkeypatch):
    store, _ = protected
    first_publish_at = datetime.now(UTC)
    monkeypatch.setattr("src.orchestration.authoring_dispatch.utcnow", lambda: first_publish_at)
    report, pending = await assignment(session)
    await flush(session, report, FakeSQS(fail=True), monkeypatch)
    before = _snapshot_of(store, tenant_id=ORG_A, invocation_id=pending.author_run_id)
    request = await session.get(OrchestrationAmendmentRequest, pending.request_id)
    assert request.state == AmendmentRequestState.QUEUED.value
    await session.execute(
        update(PersonaModelPreference).where(PersonaModelPreference.id == "authoring-preference").values(canonical_model_id=SONNET, revision=2)
    )
    await session.commit()

    # Recovery runs on a later tick. Crossing a second must not change the
    # protected envelope digest even though the requester's preference changed.
    monkeypatch.setattr("src.orchestration.authoring_dispatch.utcnow", lambda: first_publish_at + timedelta(minutes=10))
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=INSTALLATION))
    report = EngineCommandReport()
    report.pending_authoring = await recover_owed_authoring(session, grace_seconds=0)
    assert len(report.pending_authoring) == 1
    await session.commit()
    sqs = FakeSQS()
    await flush(session, report, sqs, monkeypatch)
    assert len(sqs.calls) == 1
    after = _snapshot_of(store, tenant_id=ORG_A, invocation_id=pending.author_run_id)
    assert after.to_dict() == before.to_dict()
    assert after.mappings["aidlc"] == OPUS
    assert sqs.calls[0]["MessageDeduplicationId"] == pending.deduplication_id
    assert sqs.envelope() == pending.envelope


@pytest.mark.parametrize("failure", ["policy_read", "snapshot_write", "session_open"])
async def test_unavailable_snapshot_is_reported_without_blocking_report_only_publish(session, protected, monkeypatch, caplog, failure):
    store, _ = protected
    report, pending = await assignment(session)
    factory = session.info["factory"]
    if failure == "policy_read":

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("policy source offline")

        monkeypatch.setattr("src.proxy.model_resolver.production_model_resolver", unavailable)
    elif failure == "snapshot_write":
        original = store.client.update_item

        def unavailable(**kwargs):
            if "model_policy_snapshot =" in kwargs.get("UpdateExpression", ""):
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
            return original(**kwargs)

        monkeypatch.setattr(store.client, "update_item", unavailable)
    else:
        calls = 0

        def unavailable():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database connection unavailable")
            return factory()

        session.info["factory"] = unavailable

    sqs = FakeSQS()
    await flush(session, report, sqs, monkeypatch)
    assert len(sqs.calls) == 1
    assert sqs.envelope() == pending.envelope
    execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}")
    assert "model_policy_snapshot" not in execution
    assert "authoring model-policy evidence unavailable" in caplog.text
    request = await session.get(OrchestrationAmendmentRequest, pending.request_id)
    assert request.state == AmendmentRequestState.DISPATCHED.value


async def test_authority_failure_keeps_request_queued(session, protected, monkeypatch):
    store, writer = protected
    report, pending = await assignment(session)

    def unavailable(_pending):
        raise RuntimeError("authority unavailable")

    monkeypatch.setattr(writer, "provision_authoring", unavailable)
    sqs = FakeSQS()
    await flush(session, report, sqs, monkeypatch)
    assert sqs.calls == []
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}") is None
    request = await session.get(OrchestrationAmendmentRequest, pending.request_id)
    assert request.state == AmendmentRequestState.QUEUED.value


async def test_unprotected_publisher_does_not_prepare_a_snapshot(session, protected, monkeypatch):
    store, _ = protected
    report, pending = await assignment(session)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    sqs = FakeSQS()
    await flush(session, report, sqs, monkeypatch)
    assert len(sqs.calls) == 1
    assert sqs.envelope() == pending.envelope
    assert store._read(f"TENANT#{ORG_A}", f"EXEC#{pending.author_run_id}") is None
