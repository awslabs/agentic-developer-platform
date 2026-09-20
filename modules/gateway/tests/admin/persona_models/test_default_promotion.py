"""Default promotion must use fresh exact SDK evidence and platform authority."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

from src.admin.persona_models.catalogue import catalogue_lookup
from src.admin.persona_models.catalogue_service import compute_request_shape_sha256
from src.admin.persona_models.default_routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.audit import AuditLog
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.organization import Organization, User
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
from src.shared.models.persona_models import PersonaModelPolicySetting
from src.shared.schemas.auth import TokenContext

MODEL = "us.anthropic.claude-sonnet-4-6"
CLASS = "claude-agent-sdk"
PATH = f"/admin/persona-models/default/{CLASS}"
BODY = {"canonical_model_id": MODEL, "expected_revision": 1, "reason": "bounded live proof"}


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


async def test_promote_and_refuse_stale_revision(session, prepared):
    async with client(session) as http:
        before = await http.get(PATH)
        assert before.json()["active_default_model_id"] is None
        response = await http.put(PATH, json=BODY)
        assert response.status_code == 200, response.text
        assert response.json()["active_default_model_id"] == MODEL
        assert response.json()["revision"] == 2
        stale = await http.put(PATH, json=BODY)
        assert stale.status_code == 409
    row = await session.get(PersonaModelPolicySetting, CLASS)
    assert (row.enforcement_posture, row.posture_revision) == ("report_only", 1)
    audit = await session.scalar(select(AuditLog).where(AuditLog.event_type == "persona_model_default_changed"))
    assert audit.actor_id == "admin"
    assert audit.details["provider_request_id"] == "real-provider-receipt-fixture"
    assert audit.details["before_model"] is None


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_tenant_user_cannot_administer_default(session, prepared, method):
    async with client(session, admin=False) as http:
        response = await http.request(method, PATH, json=BODY if method == "PUT" else None)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "dimension,value,reason",
    [
        ("account_id", "222222222222", "model_unproven"),
        ("region", "us-west-2", "model_unproven"),
        ("compatibility_class", "codex-sdk", "model_unproven"),
        ("canonical_model_id", "us.anthropic.claude-opus-4-6-v1", "model_unproven"),
        ("harness_contract_revision", "old-sdk", "model_unproven"),
        ("request_shape_sha256", "0" * 64, "model_unproven"),
        ("outcome", "refused", "model_unproven"),
        ("provider_request_id", "", "model_unproven"),
        ("expires_at", datetime.now(UTC) - timedelta(hours=1), "evidence_stale"),
    ],
)
async def test_nonmatching_or_stale_proof_cannot_promote(session, prepared, dimension, value, reason):
    evidence = await session.scalar(select(ModelInvocabilityEvidence))
    setattr(evidence, dimension, value)
    await session.commit()
    async with client(session) as http:
        response = await http.put(PATH, json=BODY)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == reason
    row = await session.get(PersonaModelPolicySetting, CLASS)
    assert row.active_default_model_id is None
    assert row.revision == 1
    assert await session.scalar(select(AuditLog).where(AuditLog.event_type == "persona_model_default_changed")) is None


async def test_no_promotion_when_actor_is_unregistered(session, prepared):
    async with client(session, subject="unregistered") as http:
        response = await http.put(PATH, json=BODY)
    assert response.status_code == 422
    audit = await session.scalar(select(AuditLog))
    assert audit.actor_id is None


async def test_customer_destination_is_not_platform_proof(session, prepared):
    destination = await session.get(BedrockDestinationRegistry, "platform")
    destination.is_platform_registered = False
    destination.owner_org_id = "customer"
    await session.commit()
    async with client(session) as http:
        response = await http.put(PATH, json=BODY)
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "platform_destination_unavailable"


async def test_audit_failure_cannot_commit_a_default(session, prepared):
    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO security_audit_logs"):
            raise RuntimeError("audit unavailable")

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", fail_audit)
    try:
        async with client(session) as http:
            with pytest.raises(RuntimeError, match="audit unavailable"):
                await http.put(PATH, json=BODY)
    finally:
        event.remove(engine, "before_cursor_execute", fail_audit)
        await session.rollback()
    session.expire_all()
    row = await session.get(PersonaModelPolicySetting, CLASS)
    assert (row.active_default_model_id, row.revision) == (None, 1)


@pytest.mark.parametrize("revision", [True, "1", 0])
async def test_revision_is_strict(session, prepared, revision):
    async with client(session) as http:
        response = await http.put(PATH, json={**BODY, "expected_revision": revision})
    assert response.status_code == 422
