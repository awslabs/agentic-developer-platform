"""Protected persona-policy and pricing evidence on usage rows (#5426)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.model_policy import (
    ModelPolicySnapshot,
    canonical_json,
    policy_digest,
)
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.model_policy_evidence import protected_usage_attribution
from src.usage.persona_attribution import PersonaUsageAttribution
from src.usage.service import UsageService

RUN = "run-1"
TENANT = "tenant-1"


def _context(**overrides):
    fields = dict(
        user_id="authority-worker",
        org_id="__platform__",
        attributed_org_id=TENANT,
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fields.update(overrides)
    return TokenContext(**fields)


def _attribution(**overrides):
    fields = dict(
        tenant_id=TENANT,
        invocation_id=RUN,
        root_invocation_id="root-1",
        chain_id="chain-1",
        persona_key="architect",
        compatibility_class="claude-agent-sdk",
        harness_contract_revision="0.3.220",
        principal_kind="service_account",
        principal_id="canonical-service-1",
        snapshot_digest="a" * 64,
        policy_revision="b" * 64,
        catalogue_revision="c" * 64,
        requested_model_id="sonnet46",
        resolved_model_id="global.anthropic.claude-sonnet-4-6",
        resolution_source="explicit-direct",
        runtime_posture="report_only",
        posture_revision=2,
    )
    fields.update(overrides)
    return PersonaUsageAttribution(**fields)


def _decision(**overrides):
    fields = dict(
        confidence="verified",
        estimate_reasons=(),
        to_dict=lambda: {"confidence": "verified"},
        source_kind="database",
        generation_id=12,
        pointer_revision=7,
        snapshot_version="2026-09-12.2",
        policy_version=2,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def _log(
    db_session,
    context,
    *,
    pricing_decision=None,
    request_id="request-1",
    model="global.anthropic.claude-sonnet-4-6",
):
    await UsageService(db_session).log_request(
        context=context,
        model=model,
        input_tokens=10,
        output_tokens=20,
        cost_usd=Decimal("0.001000"),
        latency_ms=100,
        status_code=200,
        request_id=request_id,
        agent_run_id=RUN,
        pricing_decision=pricing_decision,
    )


def test_constructor_cannot_inject_private_persona_attribution():
    context = _context(_persona_usage_attribution=_attribution(persona_key="forged"))
    assert context._persona_usage_attribution is None
    assert "_persona_usage_attribution" not in context.model_dump()


@pytest.mark.parametrize(
    "override",
    [
        {"tenant_id": "other"},
        {"invocation_id": "other"},
    ],
)
def test_cross_tenant_or_run_evidence_is_withheld_atomically(override):
    context = _context()
    context._persona_usage_attribution = _attribution(**override)
    evidence = UsageService._persona_evidence_for(context, RUN)
    assert set(evidence.values()) == {None}


async def test_writer_persists_protected_persona_chain_owner_and_pricing_tuple(db_session):
    context = _context()
    context._persona_usage_attribution = _attribution()
    await _log(db_session, context, pricing_decision=_decision(), model="legacy-runtime-model")
    row = (await db_session.scalars(select(UsageLog))).one()
    assert (row.persona_key, row.chain_id, row.root_invocation_id) == ("architect", "chain-1", "root-1")
    assert (row.preference_owner_kind, row.preference_owner_id) == ("service_account", "canonical-service-1")
    assert row.harness_contract_revision == "0.3.220"
    assert row.model_policy_snapshot_digest == "a" * 64
    assert row.model == "legacy-runtime-model"
    assert (
        row.requested_model_id,
        row.resolved_model_id,
        row.resolution_source,
        row.runtime_posture,
        row.posture_revision,
    ) == (
        "sonnet46",
        "global.anthropic.claude-sonnet-4-6",
        "explicit-direct",
        "report_only",
        2,
    )
    assert (
        row.pricing_source_kind,
        row.pricing_generation_id,
        row.pricing_pointer_revision,
        row.pricing_snapshot_version,
        row.pricing_policy_version,
    ) == ("database", 12, 7, "2026-09-12.2", 2)


async def test_unavailable_evidence_keeps_usage_row_and_writes_nulls(db_session):
    await _log(db_session, _context(), pricing_decision=object())
    row = (await db_session.scalars(select(UsageLog))).one()
    assert row.persona_key is None
    assert row.preference_owner_id is None
    assert row.pricing_source_kind is None
    assert row.pricing_generation_id is None
    assert row.input_tokens == 10


def test_bundled_pricing_tuple_is_distinct_from_not_captured():
    result = UsageService._pricing_revision_for(
        _decision(
            source_kind="bundled_snapshot",
            generation_id=None,
            pointer_revision=None,
        )
    )
    assert result == {
        "pricing_confidence": "verified",
        "pricing_estimate_reasons": "[]",
        "pricing_decision": {"confidence": "verified"},
        "pricing_source_kind": "bundled_snapshot",
        "pricing_generation_id": None,
        "pricing_pointer_revision": None,
        "pricing_snapshot_version": "2026-09-12.2",
        "pricing_policy_version": 2,
    }


@pytest.mark.parametrize(
    "decision",
    [
        _decision(generation_id=None),
        _decision(pointer_revision=0),
        _decision(source_kind="bundled_snapshot", generation_id=1),
        _decision(source_kind="unknown"),
        _decision(policy_version=0),
        _decision(snapshot_version=None),
    ],
)
def test_incomplete_pricing_decision_never_writes_a_partial_tuple(decision):
    assert set(UsageService._pricing_revision_for(decision).values()) == {None}


def protected_fixture():
    now = datetime.now(UTC)
    snapshot = ModelPolicySnapshot(
        schema_version=1,
        tenant_id=TENANT,
        principal_kind="human",
        principal_id="canonical-human-1",
        mappings={"architect": "global.anthropic.claude-sonnet-4-6"},
        class_defaults={
            "claude-agent-sdk": {
                "model_id": "global.anthropic.claude-sonnet-4-6",
                "posture": "report_only",
                "posture_revision": 2,
            }
        },
        persona_contracts={
            "architect": {
                "compatibility_class": "claude-agent-sdk",
                "harness_contract_revision": "0.3.220",
            }
        },
        policy_revision="p" * 64,
        allowlist_policy_revision="a" * 64,
        catalogue_revision="c" * 64,
        correlation_id="protected-chain",
        root_invocation_id="protected-root",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        audience="adp-agent-model-policy",
        source="live",
    )
    raw = canonical_json(snapshot.to_dict()).decode("ascii")
    digest = policy_digest(snapshot.to_dict())

    class Store:
        def _read(self, pk, sk):
            assert (pk, sk) == (f"TENANT#{TENANT}", f"EXEC#{RUN}")
            return {
                "persona": {"S": "architect"},
                "model_policy_snapshot": {"S": raw},
                "model_policy_snapshot_digest": {"S": digest},
            }

    record = ExecutionRecord(
        invocation_id=RUN,
        tenant_id=TENANT,
        current_attempt=1,
        status=ExecutionStatus.ACTIVE,
        current_credential_epoch=1,
        min_acceptable_credential_epoch=1,
    )
    return Store(), record


def test_projection_reads_only_the_protected_snapshot():
    store, record = protected_fixture()
    result = protected_usage_attribution(store=store, record=record)
    assert result is not None
    assert result.persona_key == "architect"
    assert result.chain_id == "protected-chain"
    assert result.principal_kind == "human"
    assert result.principal_id == "canonical-human-1"
    assert result.resolved_model_id is None  # no issued launch decision was supplied
    assert result.resolution_source is None
    assert result.runtime_posture is None


def test_partial_report_only_proposal_is_withheld_atomically():
    context = _context()
    context._persona_usage_attribution = _attribution(resolved_model_id=None)
    evidence = UsageService._persona_evidence_for(context, RUN)
    assert evidence["persona_key"] == "architect"
    assert {
        evidence["requested_model_id"],
        evidence["resolved_model_id"],
        evidence["resolution_source"],
        evidence["runtime_posture"],
        evidence["posture_revision"],
    } == {None}


@pytest.fixture
def issued_fixture():
    import json
    from unittest.mock import MagicMock

    from src.usage.model_policy_evidence import record_model_evidence

    base, record = protected_fixture()
    execution = base._read(f"TENANT#{TENANT}", f"EXEC#{RUN}")
    items = {}

    def put(**kwargs):
        item = kwargs["Item"]
        key = (item["pk"]["S"], item["sk"]["S"])
        assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
        if key in items:
            raise RuntimeError("conditional conflict")
        items[key] = item

    store = SimpleNamespace(table="protected", client=SimpleNamespace(put_item=MagicMock(side_effect=put)))
    store._read = lambda pk, sk: execution if (pk, sk) == (f"TENANT#{TENANT}", f"EXEC#{RUN}") else items.get((pk, sk))
    decision = dict(
        tenant_id=TENANT,
        invocation_id=RUN,
        persona="architect",
        snapshot_digest=execution["model_policy_snapshot_digest"]["S"],
        requested_model_id="sonnet46",
        resolved_model_id="issued-original",
        resolution_source="principal-mapping",
        runtime_posture="report_only",
        posture_revision=2,
    )
    result = dict(tenant_id=TENANT, invocation_id=RUN, attempt=1, nonce="a" * 64, model_policy=dict(posture_verified=True, decision=decision))
    record_model_evidence(store=store, record=record, result=result)
    assert json.loads(next(iter(items.values()))["result"]["S"]) == result
    return store, record, result, items


def test_usage_consumes_original_launch_and_keeps_concurrent_launches_separate(issued_fixture):
    import copy
    import json

    from src.usage.model_policy_evidence import record_model_evidence

    store, record, original, _ = issued_fixture
    second = copy.deepcopy(original)
    second["nonce"] = "b" * 64
    second["model_policy"]["decision"].update(resolved_model_id="later-model", posture_revision=9)
    record_model_evidence(store=store, record=record, result=second)
    duplicate = copy.deepcopy(second)
    duplicate["nonce"] = original["nonce"]
    record_model_evidence(store=store, record=record, result=duplicate)
    first = protected_usage_attribution(store=store, record=record, decision_id=original["nonce"], approving_human_id="approver")
    later = protected_usage_attribution(store=store, record=record, decision_id=second["nonce"])
    assert first.resolved_model_id == "issued-original" and first.posture_revision == 2
    assert later.resolved_model_id == "later-model" and later.posture_revision == 9
    assert json.loads(first.model_decision_json) == original
    assert first.approving_human_id == "approver"
    assert first.principal_id == "canonical-human-1"


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("invocation_id", "other"), ("attempt", 2), ("nonce", "b" * 64)])
def test_issued_response_identity_mismatch_cannot_relabel_usage(issued_fixture, field, value):
    import json

    store, record, result, items = issued_fixture
    result[field] = value
    next(iter(items.values()))["result"]["S"] = json.dumps(result)
    evidence = protected_usage_attribution(store=store, record=record, decision_id="a" * 64)
    assert evidence.persona_key == "architect"
    assert evidence.model_decision_id is None and evidence.resolved_model_id is None


@pytest.mark.parametrize("field,value", [("snapshot_digest", "other"), ("persona", "other"), ("tenant_id", "other"), ("invocation_id", "other")])
def test_candidate_binding_mismatch_cannot_relabel_usage(issued_fixture, field, value):
    import json

    store, record, result, items = issued_fixture
    result["model_policy"]["decision"][field] = value
    next(iter(items.values()))["result"]["S"] = json.dumps(result)
    evidence = protected_usage_attribution(store=store, record=record, decision_id="a" * 64)
    assert evidence.model_decision_id is None and evidence.resolved_model_id is None


def test_prior_attempt_nonce_is_not_read_in_current_attempt(issued_fixture):
    from dataclasses import replace

    store, record, _, _ = issued_fixture
    evidence = protected_usage_attribution(store=store, record=replace(record, current_attempt=2), decision_id="a" * 64)
    assert evidence.model_decision_id is None and evidence.resolved_model_id is None


def test_verified_unavailable_response_retains_original_evidence(issued_fixture):
    import json

    store, record, result, items = issued_fixture
    result["model_policy"]["decision"] = None
    result["model_policy"]["reason"] = "catalogue_unavailable"
    next(iter(items.values()))["result"]["S"] = json.dumps(result)
    evidence = protected_usage_attribution(store=store, record=record, decision_id="a" * 64)
    assert evidence.resolved_model_id is None
    assert json.loads(evidence.model_decision_json) == result
