"""Gateway admission tests for the faithful PMM-03 harness probe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.admin.persona_models.catalogue import PLATFORM_MODEL_CATALOGUE
from src.internal.persona_model_probe_routes import router, verify_model_probe_irsa
from src.internal.persona_model_probe_service import ProbeConflictError, claim_probe, complete_probe, start_probe
from src.proxy.bedrock_signing import DestinationCredentials
from src.shared.database import get_db
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence, ModelProbeCycle, ModelProbeSlot


def _enable(monkeypatch, *, slots: int = 2, cycle_budget: str = "0.02", attempt_budget: str = "0.01") -> None:
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "111111111111")
    monkeypatch.setenv("BG_MODEL_PROBE_ENABLED", "true")
    monkeypatch.setenv("BG_MODEL_PROBE_MAX_SLOTS_PER_CYCLE", str(slots))
    monkeypatch.setenv("BG_MODEL_PROBE_BUDGET_USD_PER_CYCLE", cycle_budget)
    monkeypatch.setenv("BG_MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT", attempt_budget)


def _destination() -> BedrockDestinationRegistry:
    now = datetime.now(UTC)
    return BedrockDestinationRegistry(
        id="probe-destination",
        account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/probe",
        credential_id=None,
        owner_org_id=None,
        is_platform_registered=True,
        routing_capable=True,
        verified_at=now,
        region="us-east-1",
        label="probe",
        registered_by_user_id="operator",
        created_at=now,
        updated_at=now,
    )


def test_gateway_manifest_is_the_sdk_generated_artifact():
    gateway_manifest = Path(__file__).parents[2] / "src/admin/persona_models/request-shape-manifest.json"
    sdk_manifest = Path(__file__).parents[3] / "agent-factory/agent/src/invocability-probe/request-shape-manifest.json"
    parsed = json.loads(gateway_manifest.read_text())
    assert parsed == json.loads(sdk_manifest.read_text())
    assert set(parsed["models"]) == {
        model.canonical_model_id for model in PLATFORM_MODEL_CATALOGUE if model.compatibility_class == "claude-agent-sdk"
    }


@pytest.mark.asyncio
async def test_default_configuration_is_inert(db_session):
    result = await claim_probe(db_session)
    assert result == result.__class__(claimed=False, reason="disabled")
    assert await db_session.scalar(select(ModelProbeCycle.id)) is None


@pytest.mark.asyncio
async def test_customer_destination_is_not_implicitly_consented_for_probing(db_session, monkeypatch):
    _enable(monkeypatch)
    customer = _destination()
    customer.id = "customer-destination"
    customer.account_id = "000000000001"
    customer.role_arn = "arn:aws:iam::000000000001:role/customer"
    customer.is_platform_registered = False
    customer.owner_org_id = "customer-org"
    customer.credential_id = "customer-credential"
    db_session.add_all([customer, _destination()])
    await db_session.commit()
    result = await claim_probe(db_session)
    assert result.claimed and result.slot.destination_id == "probe-destination"
    assert await db_session.scalar(select(ModelProbeSlot.id).where(ModelProbeSlot.destination_id == customer.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "different_account", "malformed_region", "destination_retired"])
async def test_start_rechecks_admission_before_releasing_credentials(db_session, monkeypatch, change):
    _enable(monkeypatch)
    destination = _destination()
    if change == "malformed_region":
        destination.region = "x@evil.com"
    db_session.add(destination)
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.claimed and claimed.slot and claimed.lease_token
    if change == "disabled":
        monkeypatch.setenv("BG_MODEL_PROBE_ENABLED", "false")
    elif change == "different_account":
        monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "222222222222")
    elif change == "destination_retired":
        destination.routing_capable = False
        await db_session.commit()
    with patch("src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials", new_callable=AsyncMock) as credentials:
        with pytest.raises(ProbeConflictError):
            await start_probe(
                db_session, slot_id=claimed.slot.id, lease_token=claimed.lease_token, request_shape_sha256=claimed.slot.expected_request_shape_sha256
            )
        credentials.assert_not_awaited()
    assert (await db_session.get(ModelProbeSlot, claimed.slot.id)).status == "reserved"


@pytest.mark.asyncio
async def test_claim_atomically_reserves_gateway_selected_candidate(db_session, monkeypatch):
    _enable(monkeypatch, slots=1, cycle_budget="0.01")
    db_session.add(_destination())
    await db_session.commit()

    first = await claim_probe(db_session)
    second = await claim_probe(db_session)

    assert first.claimed is True
    assert first.slot is not None
    assert first.lease_token is not None
    assert first.slot.lease_token_sha256 != first.lease_token
    assert len(first.slot.lease_token_sha256) == 64
    assert first.slot.destination_id == "probe-destination"
    assert len(first.slot.expected_request_shape_sha256) == 64
    assert second.claimed is False
    assert second.reason == "cycle_budget_exhausted"
    cycle = (await db_session.scalars(select(ModelProbeCycle))).one()
    assert cycle.reserved_usd == Decimal("0.010000")
    assert cycle.started_usd == Decimal("0.000000")


@pytest.mark.asyncio
async def test_operator_scope_selects_only_catalogue_models(db_session, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("BG_MODEL_PROBE_MODEL_ALLOWLIST", '["not-a-catalogue-model", "us.anthropic.claude-sonnet-4-6"]')
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.claimed and claimed.slot.canonical_model_id == "us.anthropic.claude-sonnet-4-6"
    assert (await claim_probe(db_session)).reason == "no_candidates"
    monkeypatch.setenv("BG_MODEL_PROBE_MODEL_ALLOWLIST", '["not-a-catalogue-model"]')
    with patch("src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials", new_callable=AsyncMock) as credentials:
        with pytest.raises(ProbeConflictError, match="outside the configured"):
            await start_probe(
                db_session,
                slot_id=claimed.slot.id,
                lease_token=claimed.lease_token,
                request_shape_sha256=claimed.slot.expected_request_shape_sha256,
            )
        credentials.assert_not_awaited()
    assert (await db_session.get(ModelProbeSlot, claimed.slot.id)).status == "reserved"


@pytest.mark.asyncio
async def test_start_is_durable_before_credentials_are_released(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.slot is not None
    assert claimed.lease_token is not None

    async def _credentials(db, target, *, user_id):
        stored = await db.get(ModelProbeSlot, claimed.slot.id)
        assert stored.status == "started"
        cycle = await db.get(ModelProbeCycle, stored.cycle_id)
        assert cycle.started_usd == Decimal("0.010000")
        assert target.destination_id == "probe-destination"
        assert user_id == "model-invocability-probe"
        return DestinationCredentials(
            access_key_id="AKIA",
            secret_access_key="secret",
            session_token="token",
            expiration=datetime.now(UTC) + timedelta(minutes=10),
            region="us-east-1",
        )

    with patch(
        "src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials",
        side_effect=_credentials,
    ):
        started = await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )
    assert started.slot.status == "started"


@pytest.mark.asyncio
async def test_complete_upserts_exact_key_and_identical_replay_is_idempotent(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.slot is not None
    assert claimed.lease_token is not None

    credentials = DestinationCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=datetime.now(UTC) + timedelta(minutes=10),
        region="us-east-1",
    )
    with patch(
        "src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials",
        return_value=credentials,
    ):
        await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )

    args = dict(
        slot_id=claimed.slot.id,
        lease_token=claimed.lease_token,
        outcome="proven",
        request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        provider_request_id="aws-request-1",
        error_code=None,
    )
    first = await complete_probe(db_session, **args)
    replay = await complete_probe(db_session, **args)
    evidence = (await db_session.scalars(select(ModelInvocabilityEvidence))).one()
    assert first.evidence_recorded is True
    assert replay.evidence_recorded is True
    assert evidence.account_id == "111111111111"
    assert evidence.request_shape_sha256 == claimed.slot.expected_request_shape_sha256
    assert evidence.provider_request_id == "aws-request-1"

    with pytest.raises(ProbeConflictError, match="different evidence"):
        await complete_probe(db_session, **{**args, "provider_request_id": "aws-request-2"})


@pytest.mark.asyncio
async def test_expired_started_slot_becomes_indeterminate_and_is_not_retried(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.slot is not None
    assert claimed.lease_token is not None
    credentials = DestinationCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=datetime.now(UTC) + timedelta(minutes=10),
        region="us-east-1",
    )
    with patch(
        "src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials",
        return_value=credentials,
    ):
        await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )
    claimed.slot.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(ProbeConflictError, match="will not be retried"):
        await complete_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            outcome="error",
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
            provider_request_id=None,
            error_code="timeout",
        )
    assert (await db_session.get(ModelProbeSlot, claimed.slot.id)).status == "indeterminate"
    next_claim = await claim_probe(db_session)
    assert next_claim.slot is not None
    assert next_claim.slot.canonical_model_id != claimed.slot.canonical_model_id


@pytest.mark.asyncio
async def test_expired_reservation_moves_to_next_candidate_without_unique_collision(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    first = await claim_probe(db_session)
    assert first.slot is not None
    first.slot.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    second = await claim_probe(db_session)
    assert second.slot is not None
    assert first.slot.status == "expired"
    assert second.slot.canonical_model_id != first.slot.canonical_model_id


@pytest.mark.asyncio
async def test_no_request_emitted_completes_without_fabricating_evidence(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.slot is not None
    assert claimed.lease_token is not None
    credentials = DestinationCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=datetime.now(UTC) + timedelta(minutes=10),
        region="us-east-1",
    )
    with patch(
        "src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials",
        return_value=credentials,
    ):
        await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )
    result = await complete_probe(
        db_session,
        slot_id=claimed.slot.id,
        lease_token=claimed.lease_token,
        outcome="error",
        request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        provider_request_id=None,
        error_code="no_request_emitted.Error",
    )
    assert result.slot.status == "completed"
    assert result.evidence_recorded is False
    assert await db_session.scalar(select(ModelInvocabilityEvidence)) is None


@pytest.mark.asyncio
async def test_routes_forbid_worker_selected_target_and_default_to_no_work(db_session, monkeypatch):
    monkeypatch.delenv("BG_MODEL_PROBE_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[verify_model_probe_irsa] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/v1/persona-model-probes/claim", json={})
        forbidden_target = await client.post(
            "/internal/v1/persona-model-probes/claim",
            json={"model_id": "attacker-model", "destination_id": "attacker-destination"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "claimed": False,
        "task_probe_json": None,
        "reason": "disabled",
        "slot_id": None,
        "lease_token": None,
        "model_id": None,
        "compatibility_class": None,
        "harness_contract_revision": None,
        "expected_request_shape_sha256": None,
        "max_budget_usd": None,
        "timeout_seconds": None,
        "lease_expires_at": None,
    }
    assert forbidden_target.status_code == 422


@pytest.mark.asyncio
async def test_shared_secret_cannot_access_probe_routes(db_session, monkeypatch):
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "legacy-secret")
    app = FastAPI()
    app.include_router(router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/persona-model-probes/claim",
            json={},
            headers={"X-Internal-Api-Key": "legacy-secret"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "irsa_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal",
    [
        "scaledjob-worker",
        "authority-worker",
        "agent-codex-reviewer",
        "deploy-runner",
    ],
)
async def test_other_internal_irsa_principal_cannot_release_probe_credentials(principal):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/persona-model-probes/claim",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )

    async def _verified_as_other_principal(request, **_kwargs):
        request.state.token_context = SimpleNamespace(
            user_id=principal,
            agent_registry_id=principal,
            org_id="__platform__",
            scope="internal",
        )

    with (
        patch(
            "src.internal.persona_model_probe_routes.verify_internal_or_irsa",
            new=AsyncMock(side_effect=_verified_as_other_principal),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await verify_model_probe_irsa(request, x_caller_identity="arn:aws:sts::111111111111:assumed-role/deploy/run")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "probe_worker_required"


@pytest.mark.asyncio
async def test_dedicated_probe_irsa_is_the_only_accepted_principal():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/persona-model-probes/claim",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )

    async def _verified_as_probe(request, **_kwargs):
        request.state.token_context = SimpleNamespace(
            user_id="persona-model-probe",
            agent_registry_id="persona-model-probe",
            org_id="__platform__",
            scope="internal",
        )

    with patch(
        "src.internal.persona_model_probe_routes.verify_internal_or_irsa",
        new=AsyncMock(side_effect=_verified_as_probe),
    ):
        await verify_model_probe_irsa(
            request,
            x_caller_identity="arn:aws:sts::111111111111:assumed-role/persona-model-probe/session",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_registry_id", "org_id", "scope"),
    [
        ("tenant-generated-uuid", "__platform__", "internal"),
        ("persona-model-probe", "tenant-org", "internal"),
        ("persona-model-probe", "__platform__", "shared"),
    ],
)
async def test_probe_name_lookalike_cannot_release_credentials(agent_registry_id, org_id, scope):
    """A matching mutable agent_name is not the dedicated probe identity."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/persona-model-probes/claim",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )

    async def _verified_as_lookalike(request, **_kwargs):
        request.state.token_context = SimpleNamespace(
            user_id="persona-model-probe",
            agent_registry_id=agent_registry_id,
            org_id=org_id,
            scope=scope,
        )

    with (
        patch(
            "src.internal.persona_model_probe_routes.verify_internal_or_irsa",
            new=AsyncMock(side_effect=_verified_as_lookalike),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await verify_model_probe_irsa(
            request,
            x_caller_identity="arn:aws:sts::111111111111:assumed-role/lookalike/session",
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "probe_worker_required"


@pytest.mark.asyncio
async def test_forged_lease_token_cannot_start_or_replay_completion(db_session, monkeypatch):
    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    claimed = await claim_probe(db_session)
    assert claimed.slot is not None
    assert claimed.lease_token is not None

    with pytest.raises(ProbeConflictError) as start_exc:
        await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token="forged-token" * 4,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )
    assert start_exc.value.reason == "invalid_lease_token"
    assert (await db_session.get(ModelProbeSlot, claimed.slot.id)).status == "reserved"

    credentials = DestinationCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=datetime.now(UTC) + timedelta(minutes=10),
        region="us-east-1",
    )
    with patch(
        "src.internal.persona_model_probe_service.bedrock_destination_signer.get_credentials",
        return_value=credentials,
    ):
        await start_probe(
            db_session,
            slot_id=claimed.slot.id,
            lease_token=claimed.lease_token,
            request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        )

    completion = dict(
        slot_id=claimed.slot.id,
        lease_token=claimed.lease_token,
        outcome="proven",
        request_shape_sha256=claimed.slot.expected_request_shape_sha256,
        provider_request_id="aws-request-token-test",
        error_code=None,
    )
    await complete_probe(db_session, **completion)
    with pytest.raises(ProbeConflictError) as replay_exc:
        await complete_probe(db_session, **{**completion, "lease_token": "forged-token" * 4})
    assert replay_exc.value.reason == "invalid_lease_token"


def test_task_profiles_match_worker_and_do_not_change_legacy_cycle():
    import hashlib

    from src.internal.persona_model_probe_service import _cycle_key
    from src.tasks.personas import TASK_PERSONAS

    worker = Path(__file__).parents[3] / "agent-factory/agent/src/invocability-probe/task-profiles.json"
    profiles = json.loads(worker.read_text())
    for persona, profile in TASK_PERSONAS.items():
        assert profiles[persona] == {
            "revision": profile.harness_contract_revision,
            "body": profile.probe_json,
            "digest": profile.request_shape_sha256,
        }
        assert profile.probe_body["max_tokens"] in (16, 64)
    legacy = json.loads((Path(__file__).parents[2] / "src/admin/persona_models/request-shape-manifest.json").read_text())
    fingerprint = hashlib.sha256(json.dumps(legacy["models"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert _cycle_key(datetime(2026, 9, 26, tzinfo=UTC)) == f"2026-09-26:{fingerprint}"


@pytest.mark.asyncio
async def test_task_probe_opt_in_shares_legacy_spend_envelope(db_session, monkeypatch):
    from src.tasks.personas import TASK_PERSONAS

    _enable(monkeypatch, slots=2, cycle_budget="0.02")
    db_session.add(_destination())
    await db_session.commit()
    legacy = await claim_probe(db_session)
    task = await claim_probe(db_session, task_persona="agent-task-cyber")
    assert legacy.slot.compatibility_class == "claude-agent-sdk"
    assert task.slot.compatibility_class == "anthropic_messages"
    assert task.slot.expected_request_shape_sha256 == TASK_PERSONAS["agent-task-cyber"].request_shape_sha256
    assert task.slot.cycle_id == legacy.slot.cycle_id
    assert (await claim_probe(db_session, task_persona="agent-task-investigator")).reason == "cycle_budget_exhausted"
    cycle = (await db_session.scalars(select(ModelProbeCycle))).one()
    assert cycle.reserved_usd == Decimal("0.02")


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["agent-task-investigator", "agent-task-cyber", "agent-task-claude-developer", "agent-task-codex-developer"])
async def test_task_probe_exact_profile_and_deduplication(db_session, monkeypatch, persona):
    from src.tasks.personas import TASK_PERSONAS

    _enable(monkeypatch)
    monkeypatch.setenv("BG_MODEL_PROBE_MODEL_ALLOWLIST", '["us.anthropic.claude-sonnet-4-6"]')
    db_session.add(_destination())
    await db_session.commit()
    task = await claim_probe(db_session, task_persona=persona)
    assert task.slot.harness_contract_revision == TASK_PERSONAS[persona].harness_contract_revision
    assert (await claim_probe(db_session, task_persona=persona)).reason == "no_candidates"


@pytest.mark.asyncio
async def test_claim_route_explicit_task_profile_returns_exact_body(db_session, monkeypatch):
    from src.tasks.personas import TASK_PERSONAS

    _enable(monkeypatch)
    db_session.add(_destination())
    await db_session.commit()
    app = FastAPI()
    app.include_router(router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[verify_model_probe_irsa] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.post("/internal/v1/persona-model-probes/claim", json={"task_persona": "unknown"})
        response = await client.post("/internal/v1/persona-model-probes/claim", json={"task_persona": "agent-task-cyber"})
    assert rejected.status_code == 422
    assert response.status_code == 200
    assert response.json()["task_probe_json"] == TASK_PERSONAS["agent-task-cyber"].probe_json
