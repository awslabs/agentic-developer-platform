"""Registration and selection for the optional Profile providers."""

import pytest
from pydantic import ValidationError

from src.products.models import ConnectorModuleAdmission
from src.products.source_routes import BindingInput
from src.shared.models.vault import UserCredential
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i08_api import mutation

PROVIDERS = {
    "sonarqube": ("quality_project", "api_key", "/", "team:api"),
    "argocd": ("deployment_application", "bearer", "team/ns", "api"),
    "pagerduty": ("incident_service", "api_key", "/", "P123"),
    "linear": ("tracker_team", "bearer", "/", "team-a"),
    "slack": ("communication_channel", "oauth_token", "/", "C123"),
}


@pytest.mark.asyncio
async def test_profile_provider_capabilities_and_off_flag(test_client, db_session, source_member, monkeypatch):
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    root = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    credentials = {}
    for provider, (_, credential_type, _, _) in PROVIDERS.items():
        credential = UserCredential(
            org_id=source_member.org_id,
            user_id=source_member.user_id,
            service=provider,
            credential_type=credential_type,
            label=f"Controlled {provider}",
            secret_arn="fixture-only",
            scopes={"oauth_scopes": ["channels:read", "groups:read"]} if provider == "slack" else None,
        )
        db_session.add(credential)
        credentials[provider] = credential
    db_session.add(ConnectorModuleAdmission(tenant_id=source_member.org_id, enabled=True))
    await db_session.commit()

    listed = await test_client.get(root + "/sources")
    assert listed.status_code == 200, listed.text
    visible = {item["provider"]: item for item in listed.json()["items"]}
    for provider, (kind, _, _, _) in PROVIDERS.items():
        assert visible[provider]["capabilities"] == [kind]
        assert visible[provider]["connection_ref"] == {"kind": "credential", "id": credentials[provider].id}

    assert (
        BindingInput.model_validate(
            {
                "provider": "slack",
                "kind": "communication_channel",
                "namespace": "T123",
                "native_id": "C123",
                "connection_ref": {"kind": "credential", "id": credentials["slack"].id},
            }
        ).namespace
        == "T123"
    )
    sonar = credentials["sonarqube"]
    linked = await test_client.post(
        root + "/bindings",
        json={
            "provider": "sonarqube",
            "kind": "quality_project",
            "namespace": "/",
            "native_id": "team:api",
            "connection_ref": {"kind": "credential", "id": sonar.id},
        },
        headers=mutation(1, "sonar_selection_123456"),
    )
    assert linked.status_code == 201 and linked.json()["status"] == "unverified", linked.text
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
    disabled = await test_client.get(root + "/sources")
    assert disabled.status_code == 200
    assert not (PROVIDERS.keys() & {source["provider"] for source in disabled.json()["items"]})
    bindings = await test_client.get(root + "/bindings")
    assert bindings.json()["items"][0]["status"] == "restricted"


@pytest.mark.parametrize("provider,kind,credential_type,namespace,native_id", [(provider, *selection) for provider, selection in PROVIDERS.items()])
def test_provider_selection_rejects_wrong_identity(provider, kind, credential_type, namespace, native_id):
    selection = {
        "provider": provider,
        "kind": kind,
        "namespace": namespace,
        "native_id": native_id,
        "connection_ref": {"kind": "credential", "id": "connection"},
    }
    assert BindingInput.model_validate(selection).kind == kind
    for invalid in (
        {**selection, "kind": "ci_job"},
        {**selection, "provider": "github"},
        {**selection, "connection_ref": {"kind": "github_installation", "id": "connection"}},
        {**selection, "namespace": "foreign" if namespace == "/" else "team"},
    ):
        with pytest.raises(ValidationError):
            BindingInput.model_validate(invalid)
