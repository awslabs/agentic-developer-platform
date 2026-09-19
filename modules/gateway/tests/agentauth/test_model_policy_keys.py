"""Public discovery and explicit retirement of the superseded raw chat harness."""

from unittest.mock import AsyncMock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import update

from src.agentauth import model_policy_keys
from src.agentauth.envelope import SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV
from src.agentauth.runtime_posture import reset_posture_cache
from src.shared.database import get_db
from src.shared.models.persona_models import PersonaModelPolicySetting


async def test_discovery_exposes_only_public_key_and_legacy_is_refused_on_enforcement(report_only_db, db_session, monkeypatch):
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setenv(SIGNING_KEY_ID_ENV, "discovery-test")
    monkeypatch.setenv(SIGNING_KEY_ENV, private)
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    app = FastAPI()
    app.include_router(model_policy_keys.router)
    app.dependency_overrides[get_db] = report_only_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        discovery = await client.get("/internal/v1/agent/model-policy-keys")
        assert discovery.json()["keys"] == {
            "discovery-test": key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        }
        assert "PRIVATE" not in discovery.text
        assert discovery.headers["Cache-Control"] == "no-store"
        path = "/internal/v1/agent/legacy-chat-preflight"
        assert (await client.post(path)).status_code == 403
        headers = {"X-Caller-Identity": "chat-role"}
        assert (await client.post(path, headers=headers)).json() == {"legacy_permitted": True, "posture": "report_only"}
        await db_session.execute(update(PersonaModelPolicySetting).values(enforcement_posture="enforcing", posture_revision=2))
        await db_session.commit()
        reset_posture_cache()
        assert (await client.post(path, headers=headers)).status_code == 409
