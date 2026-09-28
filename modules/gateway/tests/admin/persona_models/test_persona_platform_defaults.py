"""Default promotion must use fresh exact SDK evidence and platform authority."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.admin.persona_models.catalogue import catalogue_lookup
from src.admin.persona_models.catalogue_service import compute_request_shape_sha256
from src.admin.persona_models.persona_default_routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.audit import AuditLog
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.organization import Organization, User
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaPlatformDefault
from src.shared.schemas.auth import TokenContext

MODEL = "us.anthropic.claude-sonnet-4-6"
CLASS = "claude-agent-sdk"
PATH = "/admin/persona-defaults/developer"
BODY = {"canonical_model_id": MODEL, "expected_revision": 0, "reason": "bounded live proof"}


@pytest.fixture
async def prepared(session, monkeypatch):
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "111111111111")
    monkeypatch.setenv("BG_AWS_REGION", "us-east-1")
    now = datetime.now(UTC)
    model = catalogue_lookup(MODEL)
    session.add_all(
        [
            Organization(id="org", name="Test"),
            User(id="admin", cognito_sub="admin-sub", org_id="org", team_id="team", email="admin@example.com"),
            PersonaModelPolicySetting(compatibility_class=CLASS, revision=1, posture_revision=1, enforcement_posture="report_only"),
            BedrockDestinationRegistry(
                id="platform",
                account_id="111111111111",
                region="us-east-1",
                role_arn="arn:aws:iam::111111111111:role/probe",
                is_platform_registered=True,
                routing_capable=True,
                verified_at=now,
                label="Platform",
                registered_by_user_id="admin",
            ),
            ModelInvocabilityEvidence(
                account_id="111111111111",
                region="us-east-1",
                canonical_model_id=MODEL,
                compatibility_class=CLASS,
                harness_contract_revision=model.harness_contract_revision,
                request_shape_sha256=compute_request_shape_sha256(MODEL),
                outcome="proven",
                provider_request_id="real-provider-receipt-fixture",
                verified_at=now,
                expires_at=now + timedelta(hours=1),
                updated_at=now,
            ),
        ]
    )
    await session.commit()
    yield


def client(session, *, admin=True, subject="admin-sub"):
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    async def database():
        yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id=subject,
        org_id="org",
        team_id="team",
        department_id="",
        account_type="human",
        is_admin=admin,
        auth_source="jwt",
        expires_at=date(2099, 1, 1),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_replay_update_reset_and_conflict(session, prepared):
    from uuid import uuid4

    async with client(session) as http:
        body = {**BODY, "operation_id": str(uuid4())}
        response = await http.put(PATH, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["canonical_model_id"] == MODEL
        assert response.json()["revision"] == 1
        replay = await http.put(PATH, json=body)
        assert replay.json() == response.json()
        stale = await http.put(PATH, json=BODY)
        assert stale.status_code == 409
        reset = await http.put(PATH, json={**BODY, "expected_revision": 1, "canonical_model_id": None})
        assert reset.status_code == 200, reset.text
        assert reset.json()["canonical_model_id"] is None
        assert reset.json()["revision"] == 2
    row = await session.get(PersonaPlatformDefault, "developer")
    assert row.canonical_model_id is None
    audits = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "persona_platform_default_changed")))
    assert len(audits) == 2
    assert audits[0].actor_id == "admin"


@pytest.mark.parametrize("method,path", [("GET", "/admin/persona-defaults"), ("PUT", PATH)])
async def test_non_admin_denied(session, prepared, method, path):
    async with client(session, admin=False) as http:
        response = await http.request(method, path, json=BODY if method == "PUT" else None)
    assert response.status_code == 403


@pytest.mark.parametrize("model", ["openai.gpt-6-astra", "unknown"])
async def test_incompatible_or_unknown_model_refused(session, prepared, model):
    async with client(session) as http:
        response = await http.put(PATH, json={**BODY, "canonical_model_id": model})
    assert response.status_code == 422
    assert await session.get(PersonaPlatformDefault, "developer") is None


async def test_stale_evidence_refused(session, prepared):
    evidence = await session.scalar(select(ModelInvocabilityEvidence))
    evidence.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    async with client(session) as http:
        response = await http.put(PATH, json=BODY)
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "evidence_stale"


async def test_list_and_unknown_persona(session, prepared):
    async with client(session) as http:
        response = await http.get("/admin/persona-defaults")
        assert response.status_code == 200, response.text
        assert any(row["persona_key"] == "developer" for row in response.json()["entries"])
        response = await http.put("/admin/persona-defaults/invented", json=BODY)
        assert response.status_code == 422


async def test_codex_missing_contract_is_audited_refusal(session, prepared):
    async with client(session) as http:
        response = await http.put(
            "/admin/persona-defaults/agent-codex-developer",
            json={
                **BODY,
                "canonical_model_id": "openai.gpt-6-sol",
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "probe_contract_unavailable"
    assert await session.get(PersonaPlatformDefault, "agent-codex-developer") is None
    audit = await session.scalar(select(AuditLog).where(AuditLog.event_type == "persona_platform_default_rejected"))
    assert audit.details["reason"] == "probe_contract_unavailable"
