from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.admin.persona_models import catalogue_service, dispatch_selection, service
from src.internal import persona_model_selection
from src.shared.database import get_db
from src.shared.models.organization import User
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference

SONNET = "global.anthropic.claude-sonnet-4-6"
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture
async def mapped(db_session, monkeypatch):
    monkeypatch.setenv("PERSONA_MODEL_MAPPING_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    db_session.add_all(
        [
            User(id="human-root", org_id="tenant-a", team_id="team-a", email="root@example.test", cognito_sub="root-sub"),
            User(id="other-human", org_id="tenant-a", team_id="team-a", email="other@example.test", cognito_sub="other-sub"),
            PersonaModelPolicySetting(compatibility_class="claude-agent-sdk", active_default_model_id=SONNET, revision=2),
            PersonaModelPreference(
                org_id="tenant-a",
                principal_kind="human",
                principal_source="self",
                principal_id="human-root",
                persona_key="developer",
                canonical_model_id=HAIKU,
                revision=1,
                updated_by="human-root",
                updated_by_source="self",
            ),
            PersonaModelPreference(
                org_id="tenant-a",
                principal_kind="human",
                principal_source="self",
                principal_id="other-human",
                persona_key="developer",
                canonical_model_id=SONNET,
                revision=1,
                updated_by="other-human",
                updated_by_source="self",
            ),
        ]
    )
    await db_session.commit()
    return db_session


def envelope(persona="developer"):
    return {
        "tenant_id": "tenant-a",
        "persona": persona,
        "actor": {"user_id": "other-human"},
        "correlation": {"is_human_rooted": True, "root_human_id": "root-sub"},
    }


@pytest.mark.parametrize("channel", ["github", "agent_trigger", "orchestration"])
async def test_each_path_uses_root_preference_not_executing_actor(mapped, channel):
    original = {**envelope(), "channel": channel}
    selected = await dispatch_selection.apply_dispatch_selection(mapped, original)
    assert selected["model_resolved"] == HAIKU
    assert selected["model_selection"]["principal_id"] == "human-root"
    assert selected["model_selection"]["source"] == "principal-mapping"
    assert "model_resolved" not in original


async def test_each_persona_has_its_own_choice_and_direct_override_is_local(mapped):
    direct = await dispatch_selection.apply_dispatch_selection(mapped, {**envelope(), "model_requested": SONNET})
    child = await dispatch_selection.apply_dispatch_selection(mapped, envelope())
    reviewer = await dispatch_selection.apply_dispatch_selection(mapped, envelope("reviewer"))
    assert direct["model_resolved"] == reviewer["model_resolved"] == SONNET
    assert direct["model_selection"]["source"] == "explicit-direct"
    assert child["model_resolved"] == HAIKU
    assert reviewer["model_selection"]["source"] == "system-default"


@pytest.mark.parametrize("change", [{"tenant_id": "tenant-b"}, {"persona": "invented-persona"}, {"model_requested": "invented-model"}])
async def test_invalid_identity_persona_or_explicit_choice_never_falls_back(mapped, change):
    with pytest.raises(service.PreferenceRejectedError):
        await dispatch_selection.apply_dispatch_selection(mapped, {**envelope(), **change})


async def test_saving_and_catalogue_do_not_need_daily_paid_probes(mapped):
    row = await service.set_preference(
        mapped,
        org_id="tenant-a",
        principal_kind="human",
        principal_source="self",
        principal_id="human-root",
        persona_key="reviewer",
        model=HAIKU,
        expected_revision=None,
        actor_id="human-root",
        actor_source="self",
    )
    assert row.canonical_model_id == HAIKU
    models = await catalogue_service.build_model_catalogue(
        mapped, persona_key="developer", canonical_principal_id="human-root", require_evidence=False
    )
    choice = next(m for m in models if m.canonical_model_id == HAIKU)
    assert choice.selectable and choice.invocable is None and choice.evidence is None
    denied = await catalogue_service.validate_selection(
        mapped,
        org_id="tenant-a",
        principal_kind="human",
        canonical_principal_id="human-root",
        persona_key="developer",
        model=HAIKU,
        tenant_allowed_patterns=[],
        require_evidence=False,
    )
    assert denied.reason == "not_permitted"


@pytest.mark.parametrize("flag,value", [("PERSONA_MODEL_MAPPING_ENABLED", "false"), ("AGENT_AUTHORITY_ENABLED", "true")])
async def test_disabled_or_protected_path_is_unchanged(mapped, monkeypatch, flag, value):
    monkeypatch.setenv(flag, value)
    original = envelope()
    assert await dispatch_selection.apply_dispatch_selection(mapped, original) is original


async def test_producer_endpoint_binds_entire_request_and_rejects_unauthenticated_callers(mapped, monkeypatch):
    import hashlib

    monkeypatch.setenv("PERSONA_MODEL_PRODUCER_ROLES", "arn:aws:iam::123456789012:role/ingress")
    app = FastAPI()
    app.include_router(persona_model_selection.router)

    async def db():
        yield mapped

    app.dependency_overrides[get_db] = db
    proof = AsyncMock(side_effect=HTTPException(403, "forbidden"))
    monkeypatch.setattr(persona_model_selection, "verify_producer", proof)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"tenant_id": "tenant-a", "user_id": "root-sub", "persona": "developer"}
        rejected = await client.post("/internal/v1/agent/persona-model/resolve", json=body)
        assert rejected.status_code == 403
        proof.side_effect = None
        response = await client.post("/internal/v1/agent/persona-model/resolve", json=body, headers={"X-Adp-Producer-Proof": "proof"})
        assert response.status_code == 200, response.text
        assert response.json()["model"] == HAIKU
        assert proof.call_args.args == ("proof", hashlib.sha256(response.request.content).hexdigest())
        assert proof.call_args.kwargs["allowed_roles"] == {"arn:aws:iam::123456789012:role/ingress"}
        extra = await client.post("/internal/v1/agent/persona-model/resolve", json={**body, "allowed_models": ["*"]})
        assert extra.status_code == 422
