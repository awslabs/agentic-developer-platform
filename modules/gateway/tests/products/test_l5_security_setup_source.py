"""Setup document capture uses the saved selection and rechecks live authority."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.setup import SetupIntent

from src.internal import l5_security_setup_source as module
from src.internal.l5_credential_routes import CredentialSessionRequest, L5WorkPermit
from src.l5_service_identity import WORKER_IDS
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "not_configured", "wrong_work", "revoked_during_capture", "source_changed"])
async def test_setup_captures_only_configured_document(test_client, db_session, app, source_member, monkeypatch, failure):
    grant, source_request, captured, _, _, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    grant.service_id = WORKER_IDS["security"]
    grant.operation_id = grant.attempt_id = grant.lease_fence = None
    await db_session.commit()
    source = source_request.source_ref.model_dump()
    version = SimpleNamespace(hash="a" * 64, settings={"capabilities": ["threat_model"], "source_refs": [source]})
    if failure == "not_configured":
        version.settings["source_refs"] = []
    intent = SimpleNamespace(service_id="security", configuration_id="config", request={"action": "check", "configuration_revision": 1})
    get = db_session.get

    async def lookup(model, key, **options):
        if model is SetupIntent:
            return intent
        if model is ConfigurationVersion:
            return version
        return await get(model, key, **options)

    monkeypatch.setattr(db_session, "get", lookup)
    actor = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-worker",
            "agent_registry_id": grant.service_id,
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )
    monkeypatch.setattr(module, "service_actor", lambda _: actor)
    monkeypatch.setattr(module, "require_service_operation", lambda *_: None)
    body = CredentialSessionRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        operation_id="setup-operation",
        attempt_id="setup-work",
        lease_owner="owner",
        lease_fence=1,
        connection_ref={**source, "source_connection_id": grant.credential_id, "record_id": grant.credential_id, "record_type": "credential"},
    )
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id=body.operation_id,
        attempt_id=body.attempt_id,
        lease_owner="owner",
        lease_fence=1,
        binding_generation=grant.binding_generation,
        expires_at=clock["now"] + timedelta(minutes=5),
        session_policy="{}",
        setup_verified={"configuration_id": "config"},
    )
    authorize = AsyncMock(return_value=permit)
    if failure == "wrong_work":
        authorize.side_effect = HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    content = b"original document bytes"
    objects = SimpleNamespace(get=lambda _: content)
    monkeypatch.setattr(module, "get_snapshot_objects", lambda: objects)

    async def capture(binding, mapping, request, store):
        assert request.source_ref.model_dump() == source
        assert request.max_files == 1 and request.exclusions == []
        if failure == "revoked_during_capture":
            grant.revoked_at = clock["now"]
            await db_session.commit()
        if failure == "source_changed":
            binding.observed = {"source_revision": "r2"}
            await db_session.commit()
        return captured.model_copy(update={"expires_at": request.retain_until, "exclusions": []})

    capture = AsyncMock(side_effect=capture)
    monkeypatch.setattr(module, "capture_source", capture)
    response = Response()
    if failure:
        with pytest.raises(HTTPException):
            await module.setup_document(body, None, response, db_session, authorize)
        if failure in {"not_configured", "wrong_work"}:
            capture.assert_not_awaited()
    else:
        result = await module.setup_document(body, None, response, db_session, authorize)
        assert result["snapshot"]["source_ref"] == source
        assert result["snapshot"]["content"]["sha256"] == captured.content.sha256
        assert response.headers["Cache-Control"] == "no-store"
        replay = await module.setup_document(body, None, Response(), db_session, authorize)
        assert replay == result
        assert capture.await_count == 1
