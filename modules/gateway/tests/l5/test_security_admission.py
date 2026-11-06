"""Security admission validates current Product scope before capture."""

from datetime import UTC, datetime
from types import SimpleNamespace as Namespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src import l5_integration


@pytest.mark.asyncio
async def test_security_admission_authority(monkeypatch):
    source = dict(
        tenant_id="tenant", project_id="project", source_connection_id="source", record_type="document", record_id="document", record_revision="r1"
    )
    connection = {**source, "source_connection_id": "credential", "record_type": "connection", "record_id": "credential", "record_revision": "v1"}
    trigger = dict(kind="manual", enabled=True, capability="threat_model", binding_id="binding", source_refs=[source])
    settings = dict(
        enabled=True,
        source_refs=[source],
        connection_ref=connection,
        triggers=[trigger],
        profile_ref=source,
        limits={
            "deadline_seconds": 3600,
            "max_provider_attempts": 3,
            "max_polls": 30,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": {**connection, "record_type": "policy", "record_id": "example-budget", "record_revision": "budget-policy-v1"},
        },
        visibility="only_me",
        classification="internal",
        purpose="Review",
        region="us-east-1",
        capabilities=["threat_model"],
    )
    configuration = Namespace(
        tenant_id="tenant",
        product_id="product",
        project_id="project",
        configuration_id="config",
        owner_id="owner",
        relationship_revision="1",
        handoff_state="linked",
    )
    version = Namespace(revision=1, state="ready", gaps=[], settings=settings, hash="a" * 64, inherited_fields=[])
    scope = dict(tenant_id="tenant", product_id="product", project_id="project", relationship_revision="1", owner_id="owner", visibility="only_me")
    observed_at = datetime.now(UTC).isoformat()
    native = {**connection, "record_type": "security_space", "record_id": "space-one"}
    model = {**native, "record_type": "threat_model", "record_id": "model-one"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected", "region": "us-east-1"}
    binding = Namespace(
        configuration_id="config",
        revision=1,
        state="ready",
        gaps=[],
        connection_ref=connection,
        scope=scope,
        generation=2,
        binding_id="binding",
        ownership="managed",
        readiness=[
            {
                "kind": "code_review_source_registration",
                "observed_at": observed_at,
                "registration": {"service_role": "arn:aws:iam::123456789012:role/Reader", "bucket": "example-source-bucket"},
            },
            {"kind": "threat_model", "observed_at": observed_at, "native_model_ref": model},
        ],
        native_resource_ref=native,
        provider_identity=identity,
    )
    credential = Namespace(
        org_id="tenant", user_id="owner", id="credential", aws_verified_version_id="v1", service="aws", credential_type="aws_role", scopes=identity
    )
    grant = Namespace(
        # This legacy admission fixture predates owner-retained execution scope.
        execution_scope=None,
        binding_generation=2,
        operations=["source:pin", "credential:session"],
        capabilities=["threat_model"],
        sources=[{**connection, "record_type": "credential"}, source],
        id="grant",
        revision=1,
    )
    db = Namespace(bind=None, get=AsyncMock(return_value=credential), scalars=AsyncMock())
    db.scalars.return_value.all = lambda: [grant]
    client = Namespace(pin_source=AsyncMock())
    backend = l5_integration.GatewayConfigurationBackend(
        db, Namespace(org_id="tenant", user_id="owner", account_type="human"), publish=AsyncMock(), security_source_client=client
    )
    backend.current = AsyncMock(return_value=(configuration, version))
    backend.source_rights = AsyncMock(return_value={("source", "document")})
    backend.binding_store = AsyncMock(return_value=Namespace(binding=AsyncMock(return_value=binding)))
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda credential: None)
    run = dict(binding_id="binding", capability="threat_model", sources=[source])
    verified, selected = await backend.admission_authority("product", "project", "security", run)
    assert selected is client and verified["scope"] == scope and verified["delegation_id"] == "grant"
    assert verified["native_model_ref"] == model
    binding.readiness = binding.readiness[:1]
    with pytest.raises(HTTPException) as unqualified:
        await backend.admission_authority("product", "project", "security", run)
    assert unqualified.value.status_code == 409
    binding.readiness.append({"kind": "threat_model", "observed_at": observed_at, "native_model_ref": model})
    backend.source_rights.return_value = set()
    with pytest.raises(HTTPException) as denied:
        await backend.admission_authority("product", "project", "security", run)
    assert denied.value.status_code == 404
    backend.source_rights.return_value = {("source", "document")}
    binding.state = "needs_input"
    with pytest.raises(HTTPException) as stale:
        await backend.admission_authority("product", "project", "security", run)
    assert stale.value.status_code == 409
    binding.state = "ready"
    db.scalars.return_value.all = lambda: []
    with pytest.raises(HTTPException) as revoked:
        await backend.admission_authority("product", "project", "security", run)
    assert revoked.value.status_code == 403
    db.scalars.return_value.all = lambda: [grant]
    backend.security_source_client = None
    with pytest.raises(HTTPException) as unmounted:
        await backend.admission_authority("product", "project", "security", run)
    assert unmounted.value.status_code == 503
    client.pin_source.assert_not_awaited()
