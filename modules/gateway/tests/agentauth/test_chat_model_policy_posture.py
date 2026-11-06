"""Chat model-policy posture (#7997): off / advisory / enforced at the live bootstrap boundary.

The chat decision route passes ``chat_posture`` to ``bootstrap_model_policy_live``.
``enforced`` keeps the fail-closed path (AC-02). ``advisory`` still resolves, signs and
journals the decision but no longer refuses a turn over rejected or unverified
invocability evidence: the persona's resolved model comes back with
``posture: "advisory"``, ``posture_verified: False`` and the rejection reason (AC-01).
``off`` reads no probe evidence at all. In every posture a model outside the tenant
allowlist stays refused (AC-03): a wrong result that must fail is an advisory decision
for such a model.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, verify_envelope
from src.agentauth.model_policy import (
    CHAT_MODEL_POLICY_POSTURES,
    CHAT_POSTURE_RUNTIME,
    bootstrap_model_policy_live,
    canonical_json,
)
from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.shared.models.organization import User
from tests.agentauth.test_model_policy import (
    HAIKU,
    SONNET,
    _add_invocability_evidence,
    _add_live_posture_setting,
    _assert_revisioned_refusal,
    _grant,
    _live_policy_record,
    active_allowlist_revision,
    snapshot,
)
from tests.agentauth.test_model_policy import policy_store as policy_store_fixture

policy_store = policy_store_fixture


def _signing_env():
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "policy-key"}, private.public_key()


async def _prepare(db_session, policy_store, monkeypatch, *, evidence: str | None, posture: str = "enforcing", email: str):
    current = datetime.now(UTC)
    policy = snapshot(
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
        allowlist_policy_revision=active_allowlist_revision(),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session, posture=posture)
    db_session.add(User(id="user-a", org_id="tenant-a", team_id="team-a", email=email, cognito_sub=email))
    if evidence == "fresh":
        _add_invocability_evidence(db_session, account_id="111111111111", outcome="proven", expires_at=current + timedelta(hours=1))
    elif evidence == "stale":
        _add_invocability_evidence(db_session, account_id="111111111111", outcome="proven", expires_at=current - timedelta(seconds=1))
    await db_session.flush()
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="user")),
    )
    return record


def _assert_signed(result: dict, record, public_key) -> None:
    """An advisory decision is signed exactly like an enforcing one; the audit trail is complete."""
    verified = verify_envelope(
        result["assertion"],
        public_keys={"policy-key": public_key},
        expected_run_id=record.invocation_id,
        expected_generation=record.current_attempt,
        expected_action="resolve_model",
        expected_command_id=result["decision"]["snapshot_digest"],
        request_body=canonical_json(result["decision"]),
        expected_audience=MODEL_POLICY_AUDIENCE,
        expected_chain_id=result["decision"]["correlation_id"],
    )
    assert verified.tenant_id == record.tenant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence,reason", [(None, "probing_disabled"), ("stale", "evidence_stale")])
async def test_advisory_issues_a_signed_decision_for_the_resolved_model_when_evidence_is_rejected(
    db_session, policy_store, monkeypatch, caplog, evidence, reason
):
    """AC-01: no (or stale) Claude invocability evidence, posture advisory -> a usable, signed, reasoned decision."""
    record = await _prepare(db_session, policy_store, monkeypatch, evidence=evidence, email="advisory@example.test")
    env, public_key = _signing_env()

    with caplog.at_level("WARNING", logger="bedrockgateway.agentauth.model_policy"):
        result = await bootstrap_model_policy_live(
            db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture="advisory"
        )

    assert result["status"] == "proposed"
    assert result["posture"] == "advisory"
    assert result["posture_verified"] is False
    assert result["rejection_reason"] == reason
    assert result["evidence"]["live_allowlist_policy_revision"], "the allowlist revision evidence travels with the reason"
    decision = result["decision"]
    assert decision["resolved_model_id"] == SONNET
    assert decision["runtime_posture"] == "report_only", "an advisory hop runs report-only, whatever the registry says"
    assert decision["posture_source"] in {"live", "cache"}, "the live posture was read; only the evidence check failed"
    assert decision["evidence_verified_at"] is None
    _assert_signed(result, record, public_key)
    degraded = [r for r in caplog.records if r.getMessage() == "Chat model policy decision issued despite a rejected check"]
    assert len(degraded) == 1 and degraded[0].reason == reason and degraded[0].chat_posture == "advisory"
    assert degraded[0].model_id == SONNET


@pytest.mark.asyncio
async def test_advisory_with_fresh_evidence_is_a_verified_advisory_decision(db_session, policy_store, monkeypatch):
    record = await _prepare(db_session, policy_store, monkeypatch, evidence="fresh", email="advisory-fresh@example.test")
    env, public_key = _signing_env()

    result = await bootstrap_model_policy_live(
        db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture="advisory"
    )

    assert result["status"] == "proposed" and result["posture"] == "advisory" and result["posture_verified"] is True
    assert "rejection_reason" not in result
    assert result["decision"]["runtime_posture"] == "report_only"
    assert result["decision"]["evidence_verified_at"] is not None
    assert result["decision"]["destination_account_id"] == "111111111111"
    _assert_signed(result, record, public_key)


@pytest.mark.asyncio
async def test_enforced_still_refuses_without_evidence(db_session, policy_store, monkeypatch):
    """AC-02: posture enforced, no evidence -> the unavailable proposal the route turns into 503 probing_disabled."""
    record = await _prepare(db_session, policy_store, monkeypatch, evidence=None, email="enforced@example.test")
    env, _ = _signing_env()

    for chat_posture in ("enforced", None):
        result = await bootstrap_model_policy_live(
            db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture=chat_posture
        )
        _assert_revisioned_refusal(result, "probing_disabled", posture="enforcing")


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_posture", ["advisory", "off"])
async def test_a_model_outside_the_tenant_allowlist_is_refused_in_every_posture(db_session, policy_store, monkeypatch, chat_posture):
    """AC-03: the wrong result that must fail is an advisory (or off) decision for a model the tenant does not permit."""
    record = await _prepare(db_session, policy_store, monkeypatch, evidence="fresh", email=f"allowlist-{chat_posture}@example.test")
    monkeypatch.setenv("BG_MODEL_ALLOWED_MODELS_CONFIG", json.dumps({"tenant-a": [HAIKU]}))
    env, _ = _signing_env()

    result = await bootstrap_model_policy_live(
        db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture=chat_posture
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "not_permitted"
    assert "decision" not in result and "assertion" not in result


@pytest.mark.asyncio
async def test_off_issues_a_decision_without_reading_evidence(db_session, policy_store, monkeypatch, caplog):
    record = await _prepare(db_session, policy_store, monkeypatch, evidence=None, email="off@example.test")
    env, public_key = _signing_env()

    with caplog.at_level("WARNING", logger="bedrockgateway.agentauth.model_policy"):
        result = await bootstrap_model_policy_live(
            db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture="off"
        )

    assert result["status"] == "proposed" and result["posture"] == "off"
    assert result["posture_verified"] is True, "the live posture was read; no evidence was required"
    assert "rejection_reason" not in result
    assert result["decision"]["runtime_posture"] == "disabled"
    assert result["decision"]["evidence_verified_at"] is None
    assert result["decision"]["resolved_model_id"] == SONNET
    assert "Live model-policy decision rejected" not in caplog.text
    _assert_signed(result, record, public_key)


@pytest.mark.asyncio
async def test_a_posture_read_failure_is_still_unavailable_under_advisory(db_session, policy_store, monkeypatch):
    """Advisory relaxes evidence only: without a registered posture nothing is signed."""
    current = datetime.now(UTC)
    policy = snapshot(issued_at=current - timedelta(minutes=1), expires_at=current + timedelta(hours=2))
    record = _live_policy_record(policy_store, policy)
    # No PersonaModelPolicySetting row: the per-hop live read cannot establish a posture.
    env, _ = _signing_env()

    result = await bootstrap_model_policy_live(
        db_session, store=policy_store, record=record, grant=_grant("github_event"), env=env, chat_posture="advisory"
    )

    assert result["status"] == "unavailable"
    assert result["posture_verified"] is False
    assert "decision" not in result


def test_posture_vocabulary_maps_onto_the_runtime_posture_vocabulary():
    assert CHAT_MODEL_POLICY_POSTURES == ("off", "advisory", "enforced")
    assert CHAT_POSTURE_RUNTIME == {"off": "disabled", "advisory": "report_only", "enforced": "enforcing"}
