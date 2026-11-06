"""Explicit provider resources stay bound to the current Product connection."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from l5_services.configuration_population import populate_settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_wa_provider_selection as selection
from src.products.models import ProductSourceBinding
from src.shared.models.base import Base
from src.shared.models.vault import UserCredential

ACCOUNT = "000000000000"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/SourceReader"
BUCKET = "example-wa-sources"
CONNECTION = {
    "tenant_id": "example-tenant",
    "project_id": "example-project",
    "source_connection_id": "example-credential",
    "record_type": "connection",
    "record_id": "example-credential",
    "record_revision": "example-version",
}
REQUEST = {"connection_ref": CONNECTION, "region": "us-east-1", "execution_role_arn": ROLE, "source_bucket": BUCKET}


@pytest.fixture
def provider():
    iam, s3 = Mock(), Mock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": ROLE,
            "RoleId": "AROEXAMPLE",
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"Service": "wellarchitected.amazonaws.com"},
                        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
                    }
                ],
            },
        }
    }
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {
                "EvalActionName": "s3:GetObjectVersion",
                "EvalResourceName": f"arn:aws:s3:::{BUCKET}/l5/well_architected/work/*",
                "EvalDecision": "allowed",
                "MissingContextValues": [],
            }
        ]
    }
    s3.get_bucket_location.return_value = {"LocationConstraint": None}
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    s3.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
    }
    clients = {"iam": iam, "s3": s3}
    return SimpleNamespace(client=Mock(side_effect=lambda name, **_: clients[name]), iam=iam, s3=s3)


@pytest.mark.parametrize(
    "change", ["trust", "missing_account", "extra_trust", "foreign_role", "denied", "missing_context", "truncated", "region", "versioning", "public"]
)
def test_resource_readback_rejects_incomplete_or_unsafe_selection(provider, change):
    statement = provider.iam.get_role.return_value["Role"]["AssumeRolePolicyDocument"]["Statement"][0]
    if change == "trust":
        statement["Principal"] = {"AWS": "*"}
    elif change == "missing_account":
        statement["Condition"] = {}
    elif change == "extra_trust":
        statement["NotAction"] = "iam:*"
    elif change == "foreign_role":
        provider.iam.get_role.return_value["Role"]["Arn"] = ROLE + "Other"
    elif change in {"denied", "missing_context"}:
        evaluation = provider.iam.simulate_principal_policy.return_value["EvaluationResults"][0]
        evaluation["EvalDecision" if change == "denied" else "MissingContextValues"] = (
            "implicitDeny" if change == "denied" else ["aws:PrincipalTag/owner"]
        )
    elif change == "truncated":
        provider.iam.simulate_principal_policy.return_value["IsTruncated"] = True
    elif change == "region":
        provider.s3.get_bucket_location.return_value = {"LocationConstraint": "us-west-2"}
    elif change == "versioning":
        provider.s3.get_bucket_versioning.return_value = {"Status": "Suspended"}
    elif change == "public":
        provider.s3.get_public_access_block.return_value["PublicAccessBlockConfiguration"]["RestrictPublicBuckets"] = False
    with pytest.raises(ValueError):
        selection.read_resources(provider, selection.ProviderSelection.model_validate(REQUEST), ACCOUNT)
    assert all(call.args[0] in {"iam", "s3"} for call in provider.client.call_args_list)


@pytest.fixture
async def fixture(monkeypatch, provider):
    monkeypatch.setenv("L5_ARCHIVE_BUCKET", BUCKET)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        credential = UserCredential(
            id="example-credential",
            org_id="example-tenant",
            user_id="example-owner",
            label="Example",
            secret_arn="example-private-ref",
            service="aws",
            credential_type="aws_role",
            aws_verified_version_id="example-version",
            scopes={"account_id": ACCOUNT, "role_arn": f"arn:aws:iam::{ACCOUNT}:role/Broker"},
        )
        environment = ProductSourceBinding(
            tenant_id="example-tenant",
            project_id="example-project",
            kind="environment",
            provider="aws",
            credential_id=credential.id,
            namespace=ACCOUNT,
            native_id="us-east-1",
            desired={},
            status="healthy",
            observed={"source_revision": "example-version"},
        )
        db.add_all([credential, environment])
        await db.commit()
        selected_provider = AsyncMock(
            return_value=(provider, {"account_id": ACCOUNT, "region": "us-east-1", "role_arn": credential.scopes["role_arn"]})
        )
        monkeypatch.setattr(selection.WellArchitectedService, "selected_provider", selected_provider)
        monkeypatch.setattr(selection, "verified_connection_evidence", lambda selected: selected.aws_verified_version_id)
        backend = SimpleNamespace(
            db=db, actor=SimpleNamespace(org_id="example-tenant", user_id="example-owner", account_type="human"), profile=AsyncMock()
        )
        yield SimpleNamespace(backend=backend, credential=credential, environment=environment, selected_provider=selected_provider, provider=provider)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "foreign_project",
        "foreign_tenant",
        "other_owner",
        "service",
        "stale_connection",
        "foreign_account",
        "broker_as_execution",
        "revoked_environment",
        "other_environment",
        "stale_environment",
        "other_store",
    ],
)
async def test_authority_denials_happen_before_provider_access(fixture, change):
    request = deepcopy(REQUEST)
    project = "example-project"
    if change == "foreign_project":
        project = "other-project"
    elif change == "foreign_tenant":
        request["connection_ref"]["tenant_id"] = "other-tenant"
    elif change == "other_owner":
        fixture.backend.actor.user_id = "other-owner"
    elif change == "service":
        fixture.backend.actor.account_type = "service"
    elif change == "stale_connection":
        request["connection_ref"]["record_revision"] = "other-version"
    elif change == "foreign_account":
        request["execution_role_arn"] = ROLE.replace(ACCOUNT, "000000000001")
    elif change == "broker_as_execution":
        request["execution_role_arn"] = fixture.credential.scopes["role_arn"]
    elif change == "revoked_environment":
        fixture.environment.status = "revoked"
    elif change == "other_environment":
        fixture.environment.project_id = "other-project"
    elif change == "stale_environment":
        fixture.environment.observed = {"source_revision": "other-version"}
    elif change == "other_store":
        request["source_bucket"] = "another-source-store"
    await fixture.backend.db.commit()
    with pytest.raises(HTTPException):
        await selection.provider_selection(fixture.backend, project, selection.ProviderSelection.model_validate(request))
    fixture.selected_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_returns_current_refs_without_qualifying_native_work(fixture):
    app = FastAPI()
    app.include_router(selection.create_provider_selection_router(lambda: fixture.backend))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://example.test") as client:
        response = await client.post(
            "/v1/products/example-product/projects/example-project/services/well_architected/configuration/provider-selection", json=REQUEST
        )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    fixture.backend.profile.assert_awaited_once_with("example-product", "example-project")
    result = response.json()
    assert result["native_qualified"] is False and result["provider_contacted"] is True
    assert result["execution_role_ref"]["record_id"] == ROLE
    assert result["source_store_ref"]["record_id"] == BUCKET
    assert len(result["execution_role_ref"]["record_revision"]) == 64
    policy = fixture.selected_provider.await_args.kwargs["session_policy"]
    assert [row["Resource"] for row in policy["Statement"]] == [ROLE, f"arn:aws:s3:::{BUCKET}"]
    assert not any("Create" in action or "Put" in action or action.endswith("*") for row in policy["Statement"] for action in row["Action"])
    fixture.provider.s3.get_bucket_versioning.assert_called_once_with(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)


@pytest.mark.asyncio
async def test_resource_refs_populate_draft_and_detect_provider_drift(fixture, monkeypatch):
    observed = await selection.provider_selection(fixture.backend, "example-project", selection.ProviderSelection.model_validate(REQUEST))
    goal = {**CONNECTION, "record_type": "document", "record_id": "example-goal", "record_revision": "example-blob"}
    settings = {
        "service_id": "well_architected",
        "connection_ref": CONNECTION,
        "region": "us-east-1",
        "enabled": False,
        "capabilities": ["architecture_review"],
        "source_refs": [goal],
        "well_architected": {
            "review_mode": "source_only",
            "pillars": ["SECURITY"],
            "goal_refs": [goal],
            "execution_role_ref": observed["execution_role_ref"],
            "source_store_ref": observed["source_store_ref"],
        },
    }
    settings["overrides"] = [
        {"field": field, "value": settings[field], "reason": "Explicit Product selection."} for field in ("connection_ref", "region", "source_refs")
    ]
    references = await selection.verify_provider_references(fixture.backend, "example-project", settings)
    available = {
        "coverage": "complete",
        "next_cursor": None,
        "regions": ["us-east-1"],
        "items": [{"connection_ref": {"kind": "credential", "id": "example-credential"}, "record_ref": CONNECTION, "resources": [goal]}],
    }
    profile = {
        "tenant_id": "example-tenant",
        "project_id": "example-project",
        "product_id": "example-product",
        "revision": 1,
        "configuration": {
            "selected_capabilities": [{"capability": "well_architected", "ownership_mode": "integrated"}],
            "basic": {"documents": [goal]},
        },
    }
    profile_ref = {**CONNECTION, "record_type": "product", "record_id": "example-product", "record_revision": "1"}
    with pytest.raises(ValueError, match="currently authorized"):
        populate_settings(profile, profile_ref, available, settings, "well_architected")
    populated, _ = populate_settings(profile, profile_ref, selection.add_provider_resources(available, references), settings, "well_architected")
    assert populated["well_architected"] == settings["well_architected"] and populated["enabled"] is False
    assert populated["source_refs"] == [goal]
    from src import l5_integration

    backend = l5_integration.GatewayConfigurationBackend(fixture.backend.db, fixture.backend.actor)
    backend.source_rights = AsyncMock(return_value={(CONNECTION["record_id"], goal["record_id"]), (CONNECTION["record_id"], "us-east-1")})
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda selected: selected.aws_verified_version_id)
    monkeypatch.setattr(
        l5_integration,
        "authorized_connections",
        AsyncMock(
            return_value=[
                (
                    {"provider": "aws", "connection_ref": {"kind": "credential", "id": CONNECTION["record_id"]}},
                    fixture.credential,
                )
            ]
        ),
    )
    assert await backend.authorize_settings(settings, "example-project", "well_architected") == references
    fixture.provider.iam.get_role.return_value["Role"]["RoleId"] = "AROREPLACED"
    with pytest.raises(HTTPException) as caught:
        await selection.verify_provider_references(fixture.backend, "example-project", settings)
    assert caught.value.detail["code"] == "provider_selection_stale"
    fixture.selected_provider.reset_mock()
    await backend.authorize_sources(SimpleNamespace(settings=settings), "example-project")
    fixture.selected_provider.assert_not_awaited()
