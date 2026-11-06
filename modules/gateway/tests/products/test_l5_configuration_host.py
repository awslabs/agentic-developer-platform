"""Exercise the mounted L5 router and Product-owned conditional handoff."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

pytest.importorskip("l5_services")

from l5_services.adapters.devops import federation
from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work

from src.admin.membership_revocation import revoke_membership
from src.auth.aws_connection_authority import connection_binding
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.models.organization import Organization, User
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.membership import add_team_member
from tests.products.test_i05 import REQUEST


@pytest.fixture
async def l5_owner(app, db_session, valid_token_context, monkeypatch):
    db_session.add(Organization(id=valid_token_context.org_id, name="Fixture workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="l5-owner@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    from src import l5_integration

    valid_token_context = valid_token_context.model_copy(update={"token_use": "access"})
    monkeypatch.setattr(l5_integration, "require_native_launch_session", AsyncMock())
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


@pytest.mark.asyncio
async def test_mounted_options_map_authorized_product_bindings(test_client, test_engine, db_session, l5_owner, monkeypatch):
    from src import l5_integration
    from src.products import source_routes

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "options_product_00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    repository = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-repository",
        "record_type": "repository",
        "record_id": "example/repository",
        "record_revision": "source-revision-1",
    }
    environment = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-aws",
        "record_type": "environment",
        "record_id": "us-east-1",
        "record_revision": "version-1",
    }
    allowed = {("fixture-repository", "example/repository"), ("fixture-aws", "us-east-1")}
    rights = AsyncMock(side_effect=lambda *_: allowed.copy())
    monkeypatch.setattr(source_routes, "permitted_source_refs", rights)
    monkeypatch.setattr(l5_integration, "permitted_source_refs", rights)
    credential = UserCredential(
        id="fixture-aws",
        user_id=l5_owner.user_id,
        org_id=l5_owner.org_id,
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="version-1",
        label="test",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:example",
    )
    db_session.add(credential)
    await db_session.commit()

    async def connections(session, actor):
        selected_credential = await session.get(UserCredential, credential.id)
        return [
            ({"connection_ref": {"kind": "github_installation", "id": "fixture-repository"}, "provider": "github"}, None),
            ({"connection_ref": {"kind": "credential", "id": "fixture-aws"}, "provider": "aws"}, selected_credential),
        ]

    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda selected: selected.aws_verified_version_id)
    path = f"/v1/products/{product_id}/projects/{project_id}"
    patched = await test_client.patch(
        f"{path}/configuration",
        json={
            "configuration": {
                "basic": {"repositories": [repository]},
                "advanced": {"environments": [environment]},
                "selected_capabilities": [
                    {"capability": "code_review", "ownership_mode": "managed"},
                    {"capability": "devops", "ownership_mode": "managed"},
                    {"capability": "finops", "ownership_mode": "managed"},
                    {"capability": "refactor", "ownership_mode": "managed"},
                ],
            }
        },
        headers={"Idempotency-Key": "options_profile_00001", "If-Profile-Revision": "1"},
    )
    assert patched.status_code == 200, patched.text
    selected = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-aws",
        "record_type": "connection",
        "record_id": "fixture-aws",
        "record_revision": "version-1",
    }
    for service_id in ("security", "devops", "finops", "transform", "well_architected"):
        response = await test_client.get(f"{path}/services/{service_id}/configuration/options")
        assert response.status_code == 200, response.text
        options = response.json()
        assert options["regions"] == ["us-east-1"]
        assert options["connections"] == [selected]
        if service_id == "devops":
            assert options["sources"] == [environment]
        elif service_id == "finops":
            assert options["sources"] == []
        else:
            assert options["sources"] == [repository]
    drafted = await test_client.put(
        f"{path}/services/security/configuration",
        json={"capabilities": ["code_review"]},
        headers={"Idempotency-Key": "options_security_draft_00001", "If-None-Match": "*"},
    )
    assert drafted.status_code == 201, drafted.text
    assert drafted.json()["settings"]["source_refs"] == [repository]
    from l5_services.run_input import RunInput

    with pytest.raises(ValueError, match="source type does not match capability"):
        RunInput.model_validate({"configuration_revision": 1, "binding_id": "selected-binding", "capability": "code_review", "sources": [repository]})
    credential.aws_verified_version_id = "version-2"
    await db_session.commit()
    stale = await test_client.get(f"{path}/services/security/configuration/options")
    assert stale.status_code == 200
    assert stale.json()["regions"] == []
    allowed.clear()
    credential.aws_verified_version_id = None
    denied = await test_client.get(f"{path}/services/security/configuration/options")
    assert denied.status_code == 200
    assert denied.json()["connections"] == []
    assert denied.json()["sources"] == []
    assert denied.json()["regions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["security", "devops", "finops", "transform", "well_architected"])
async def test_project_verified_aws_reference_reaches_configuration_and_setup(db_session, l5_owner, monkeypatch, service_id):
    from src import l5_integration

    project_id = "example-project"
    reference = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "selected-aws",
        "record_type": "connection",
        "record_id": "selected-aws",
        "record_revision": "verified-version",
    }
    credential = UserCredential(
        id="selected-aws",
        user_id=l5_owner.user_id,
        org_id=l5_owner.org_id,
        service="aws",
        credential_type="aws_role",
        label="test",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:example",
        aws_verified_version_id="verified-version",
        scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected"},
    )
    db_session.add(credential)
    await db_session.commit()
    backend = l5_integration.GatewayConfigurationBackend(db_session, l5_owner)
    rights = {("selected-aws", "us-east-1")}
    backend.source_rights = AsyncMock(side_effect=lambda *_: rights.copy())

    async def connections(*_):
        return [({"provider": "aws", "connection_ref": {"kind": "credential", "id": "selected-aws"}}, credential)]

    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda selected: selected.aws_verified_version_id)
    monkeypatch.setattr(db_session, "get", AsyncMock(return_value=credential))
    selected = {"connection_ref": reference, "region": "us-east-1"}
    settings = {"visibility": "only_me", **selected}
    if service_id == "transform":
        settings = {"visibility": "only_me", "transform": selected}
    elif service_id in {"devops", "well_architected"}:
        settings[service_id] = {"hosting_connection_ref": reference}
    await backend.authorize_settings(settings, project_id)
    for changed in ({**reference, "project_id": "foreign-project"}, {**reference, "record_revision": "old-version"}):
        denied = (
            {**settings, "connection_ref": changed}
            if service_id != "transform"
            else {**settings, "transform": {**selected, "connection_ref": changed}}
        )
        with pytest.raises(HTTPException) as refusal:
            await backend.authorize_settings(denied, project_id)
        assert refusal.value.status_code == 404

    if service_id in {"security", "finops", "transform"}:
        configuration = SimpleNamespace(
            configuration_id="config-one",
            tenant_id=l5_owner.org_id,
            product_id="product-one",
            project_id=project_id,
            relationship_revision="1",
            owner_id=l5_owner.user_id,
            handoff_state="linked",
        )
        version = SimpleNamespace(revision=1, settings=settings, state="ready", inherited_fields=[], gaps=[], hash="a" * 64)
        backend.current = AsyncMock(return_value=(configuration, version))
        grant = SimpleNamespace(
            execution_scope=None,
            id="delegation-one",
            revision=1,
            operations=["credential:session"],
            sources=[{**reference, "record_type": "credential"}],
        )
        monkeypatch.setattr(db_session, "scalar", AsyncMock(return_value=grant))
        _, verified = await backend.binding_authority("product-one", project_id, service_id)
        assert verified["connection_ref"] == reference
        assert verified["authorization"]["delegation_id"] == grant.id
        assert verified["account_id"] == "123456789012"
        rights.clear()
        if service_id == "finops":
            await backend.authorize_settings(settings, project_id, service_id)
            with monkeypatch.context() as non_aws:
                non_aws.setattr(
                    l5_integration,
                    "authorized_connections",
                    AsyncMock(return_value=[({"provider": "github", "connection_ref": {"id": "selected-aws"}}, credential)]),
                )
                with pytest.raises(HTTPException) as refusal:
                    await backend.authorize_settings(settings, project_id, service_id)
                assert refusal.value.status_code == 404
            with pytest.raises(HTTPException) as refusal:
                await backend.authorize_settings(
                    {**settings, "source_refs": [{**reference, "record_type": "repository", "record_id": "foreign-repository"}]},
                    project_id,
                    service_id,
                )
            assert refusal.value.status_code == 404
            assert (await backend.binding_authority("product-one", project_id, service_id))[1]["connection_ref"] == reference
            monkeypatch.setattr(l5_integration, "authorized_connections", AsyncMock(return_value=[]))
        with pytest.raises(HTTPException) as refusal:
            await backend.binding_authority("product-one", project_id, service_id)
        assert refusal.value.status_code == 404


@pytest.mark.asyncio
async def test_mounted_configuration_and_profile_handoff(test_client, test_engine, db_session, l5_owner):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_l5_host_00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    product_path = f"/v1/products/{product_id}/projects/{project_id}"
    patched = await test_client.patch(
        f"{product_path}/configuration",
        json={
            "configuration": {
                "selected_capabilities": [
                    {"capability": "threat_model", "ownership_mode": "managed"},
                    {"capability": "Delivery", "ownership_mode": "integrated"},
                ]
            }
        },
        headers={"Idempotency-Key": "select_l5_capability_00001", "If-Profile-Revision": "1"},
    )
    assert patched.status_code == 200, patched.text
    path = f"{product_path}/services/security/configuration"
    result = await test_client.put(
        path,
        json={"capabilities": ["threat_model"]},
        headers={
            "Idempotency-Key": "create_l5_configuration_00001",
            "If-None-Match": "*",
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["state"] == "disabled"
    assert result.json()["handoff_state"] == "linked"
    assert result.json()["settings"]["purpose"] == REQUEST["purpose"]
    assert (await test_client.get(path)).json() == result.json()
    checked = await test_client.post(f"{path}/check")
    assert checked.status_code == 200
    assert checked.json()["ready"] is False and checked.json()["provider_contacted"] is False
    assert "profile_revision_stale" not in checked.json()["gaps"]
    assert "configuration_disabled" in checked.json()["gaps"]
    refused = await test_client.post(
        f"{product_path}/services/security/runs",
        json={
            "configuration_revision": 1,
            "binding_id": "unavailable",
            "capability": "threat_model",
            "sources": [
                {
                    "tenant_id": l5_owner.org_id,
                    "project_id": project_id,
                    "source_connection_id": "source-connection",
                    "record_type": "document",
                    "record_id": "source-document",
                    "record_revision": "revision-1",
                }
            ],
        },
        headers={"Idempotency-Key": "attempt_l5_without_authority_0001"},
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "configuration_disabled"
    db_session.expire_all()
    configuration = await db_session.scalar(select(Configuration))
    version = await db_session.scalar(select(ConfigurationVersion))
    project = await db_session.get(Project, (l5_owner.org_id, project_id))
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == product_id))).all()
    assert version.settings == result.json()["settings"]
    assert configuration.current_revision == 1
    assert project.configuration["selected_capabilities"][0]["configuration_ref"]["record_id"] == configuration.configuration_id
    assert project.configuration["selected_capabilities"][1] == {"capability": "Delivery", "ownership_mode": "integrated"}
    assert len(changes) == 3


@pytest.mark.asyncio
async def test_profile_conflict_retains_saved_configuration_and_retries_only_handoff(
    test_client,
    test_engine,
    db_session,
    l5_owner,
    monkeypatch,
):
    from fastapi import HTTPException

    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_l5_retry_00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    updated = await test_client.patch(
        f"{profile_path}/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}]}},
        headers={"Idempotency-Key": "select_retry_capability_00001", "If-Profile-Revision": "1"},
    )
    assert updated.status_code == 200
    path = f"{profile_path}/services/security/configuration"
    actual_link = l5_integration.link_l5_configuration
    attempts = []

    async def conflicting_link(*args):
        attempts.append("conditional-patch")
        raise HTTPException(412, detail={"code": "stale_revision"})

    monkeypatch.setattr(l5_integration, "link_l5_configuration", conflicting_link)
    saved = await test_client.put(
        path,
        json={"capabilities": ["threat_model"]},
        headers={
            "Idempotency-Key": "create_l5_retry_config_00001",
            "If-None-Match": "*",
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["handoff_state"] == "conflict"
    assert (await test_client.get(path)).json()["configuration_id"] == saved.json()["configuration_id"]
    assert (
        await test_client.post(
            f"{path}/handoff",
            headers={
                "Idempotency-Key": "retry_link_00000001",
                "If-Match": '"stale"',
            },
        )
    ).status_code == 412
    monkeypatch.setattr(l5_integration, "link_l5_configuration", actual_link)
    retried = await test_client.post(
        f"{path}/handoff",
        headers={
            "Idempotency-Key": "retry_link_00000001",
            "If-Match": saved.headers["etag"],
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["handoff_state"] == "linked"
    assert retried.json()["revision"] == saved.json()["revision"] == 1
    db_session.expire_all()
    stored = await db_session.scalar(select(Configuration))
    assert stored.current_revision == 1 and stored.handoff_state == "linked"
    assert attempts == ["conditional-patch"]


@pytest.mark.asyncio
async def test_mounted_profile_refresh_preserves_initial_version(test_client, test_engine, db_session, l5_owner):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_l5_refresh_00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.patch(
        f"{profile_path}/configuration",
        json={
            "configuration": {
                "selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}],
            }
        },
        headers={"Idempotency-Key": "select_refresh_capability_00001", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    path = f"{profile_path}/services/security/configuration"
    first = await test_client.put(
        path,
        json={"capabilities": ["threat_model"], "exclusions": ["legacy/"]},
        headers={
            "Idempotency-Key": "create_refresh_config_00001",
            "If-None-Match": "*",
        },
    )
    assert first.status_code == 201, first.text
    before = (await test_client.get(profile_path)).json()
    changed = await test_client.patch(
        f"{profile_path}/configuration",
        json={
            "configuration": {
                **before["configuration"],
                "governance": {"data_classification": "restricted"},
            }
        },
        headers={"Idempotency-Key": "change_profile_class_00001", "If-Profile-Revision": str(before["revision"])},
    )
    assert changed.status_code == 200, changed.text
    stale = await test_client.post(f"{path}/check")
    assert stale.status_code == 200
    assert "profile_revision_stale" in stale.json()["gaps"]
    new_ref = {**first.json()["settings"]["profile_ref"], "record_revision": str(changed.json()["revision"])}
    refreshed = await test_client.post(
        f"{path}/refresh",
        json={"profile_ref": new_ref, "choices": {"exclusions": ["legacy/", "generated/"]}},
        headers={
            "Idempotency-Key": "refresh_l5_config_00001",
            "If-Match": first.headers["etag"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"] == 2
    assert refreshed.json()["settings"]["classification"] == "restricted"
    assert refreshed.json()["settings"]["exclusions"] == ["legacy/", "generated/"]
    assert refreshed.json()["handoff_state"] == "linked"
    assert (await test_client.get(path)).json() == refreshed.json()
    checked = await test_client.post(f"{path}/check")
    assert checked.status_code == 200
    assert "profile_revision_stale" not in checked.json()["gaps"]
    assert checked.json()["provider_contacted"] is False
    old_read = await test_client.get(f"{path}/versions/1")
    assert old_read.status_code == 200
    assert old_read.json()["settings"].get("classification") is None
    assert old_read.headers["etag"] == first.headers["etag"]
    db_session.expire_all()
    old = await db_session.get(ConfigurationVersion, (l5_owner.org_id, first.json()["configuration_id"], 1))
    current = await db_session.get(ConfigurationVersion, (l5_owner.org_id, first.json()["configuration_id"], 2))
    assert old.settings.get("classification") is None
    assert old.settings["exclusions"] == ["legacy/"]
    assert current.settings["exclusions"] == ["legacy/", "generated/"]
    assert old.hash == first.json()["hash"]


@pytest.mark.asyncio
async def test_two_conditional_edits_have_one_winner_and_no_partial_version(test_client, test_engine, db_session, l5_owner):
    from sqlalchemy import func

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_l5_race_0000001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    patched = await test_client.patch(
        f"{profile_path}/configuration",
        json={
            "configuration": {
                "selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}],
            }
        },
        headers={"Idempotency-Key": "select_l5_race_0000001", "If-Profile-Revision": "1"},
    )
    assert patched.status_code == 200, patched.text
    path = f"{profile_path}/services/security/configuration"
    first = await test_client.put(
        path,
        json={"capabilities": ["threat_model"]},
        headers={
            "Idempotency-Key": "create_l5_race_config_00001",
            "If-None-Match": "*",
        },
    )
    assert first.status_code == 201, first.text
    observed_etag = first.headers["etag"]
    winner = await test_client.put(
        path,
        json={"exclusions": ["winner/"]},
        headers={
            "Idempotency-Key": "edit_l5_race_winner_00001",
            "If-Match": observed_etag,
        },
    )
    assert winner.status_code == 200, winner.text
    stale = await test_client.put(
        path,
        json={"exclusions": ["loser/"]},
        headers={
            "Idempotency-Key": "edit_l5_race_loser_000001",
            "If-Match": observed_etag,
        },
    )
    assert stale.status_code == 412, stale.text
    assert (await test_client.get(path)).json()["settings"]["exclusions"] == ["winner/"]
    assert (await test_client.get(f"{path}/versions/1")).json()["settings"]["exclusions"] == []
    async with db_session.begin():
        assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 2


@pytest.mark.asyncio
async def test_create_replay_returns_original_and_rejects_changed_payload(test_client, test_engine, db_session, l5_owner):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    product = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_replay_product_00001"})
    assert product.status_code == 201, product.text
    selected = await test_client.patch(
        f"/v1/products/{product.json()['product_id']}/projects/{product.json()['project_id']}/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}]}},
        headers={"Idempotency-Key": "select_replay_capability_0001", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    path = f"/v1/products/{product.json()['product_id']}/projects/{product.json()['project_id']}/services/security/configuration"
    headers = {"Idempotency-Key": "replayed_config_create_00001", "If-None-Match": "*"}
    settings = {"capabilities": ["threat_model"]}
    first = await test_client.put(path, json=settings, headers=headers)
    assert first.status_code == 201, first.text
    replay = await test_client.put(path, json=settings, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert (await test_client.put(path, json={"exclusions": ["different/"]}, headers=headers)).status_code == 409
    assert (await test_client.put(path, json=settings, headers={**headers, "Idempotency-Key": "second_create_key_00001"})).status_code == 412
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(Configuration)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 1
    assert (await test_client.get(path)).json() == first.json()


@pytest.mark.asyncio
async def test_foreign_scope_cannot_read_edit_refresh_or_replay(test_client, test_engine, db_session, app, l5_owner):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "foreign_config_product_0001"})
    assert created.status_code == 201
    selected = await test_client.patch(
        f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}]}},
        headers={"Idempotency-Key": "select_foreign_capability_0001", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/services/security/configuration"
    headers = {"Idempotency-Key": "foreign_replay_key_00001", "If-None-Match": "*"}
    original = await test_client.put(path, json={"capabilities": ["threat_model"]}, headers=headers)
    assert original.status_code == 201, original.text
    outsider = l5_owner.model_copy(update={"org_id": "foreign-config-tenant", "user_id": "foreign-config-user"})
    db_session.add_all(
        [
            Organization(id=outsider.org_id, name="Other fixture workspace"),
            User(id=outsider.user_id, org_id=outsider.org_id, team_id=outsider.team_id, email="other-config-owner@example.test", user_kind="human"),
        ]
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: outsider
    for method, url, kwargs in (
        (test_client.get, path, {}),
        (test_client.get, f"{path}/versions/1", {}),
        (test_client.post, f"{path}/check", {}),
        (test_client.put, path, {"json": {"capabilities": ["threat_model"]}, "headers": headers}),
        (test_client.put, path, {"json": {}, "headers": {"Idempotency-Key": "foreign_edit_key_00001", "If-Match": original.headers["etag"]}}),
        (
            test_client.post,
            f"{path}/refresh",
            {
                "json": {"profile_ref": original.json()["settings"]["profile_ref"]},
                "headers": {"Idempotency-Key": "foreign_refresh_key_00001", "If-Match": original.headers["etag"]},
            },
        ),
    ):
        response = await method(url, **kwargs)
        assert response.status_code == 404, response.text
        assert original.json()["configuration_id"] not in response.text
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 1


@pytest.mark.asyncio
async def test_revoked_saved_source_hides_configuration(test_client, test_engine, db_session, l5_owner, monkeypatch):
    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "source_config_product_0001"})
    assert created.status_code == 201
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    reference = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-connection",
        "record_type": "document",
        "record_id": "fixture-document",
        "record_revision": "rev-1",
    }
    allowed = {("fixture-connection", "fixture-document")}

    async def permitted(*args):
        return allowed

    async def connections(*args):
        return [({"connection_ref": {"kind": "credential", "id": "fixture-connection"}}, None)]

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)
    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    patch = await test_client.patch(
        f"/v1/products/{product_id}/projects/{project_id}/configuration",
        json={
            "configuration": {
                "basic": {"documents": [reference]},
                "selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}],
            }
        },
        headers={"Idempotency-Key": "profile_with_source_00001", "If-Profile-Revision": "1"},
    )
    assert patch.status_code == 200, patch.text
    path = f"/v1/products/{product_id}/projects/{project_id}/services/security/configuration"
    headers = {"Idempotency-Key": "saved_source_create_00001", "If-None-Match": "*"}
    first = await test_client.put(path, json={"capabilities": ["threat_model"]}, headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["settings"]["source_refs"] == [reference]
    assert (await test_client.get(path)).status_code == 200
    allowed.clear()
    for response in (
        await test_client.get(path),
        await test_client.get(f"{path}/versions/1"),
        await test_client.post(f"{path}/check"),
        await test_client.put(path, json={"capabilities": ["threat_model"]}, headers=headers),
    ):
        assert response.status_code == 404, response.text
        assert "fixture-document" not in response.text
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1


@pytest.mark.asyncio
async def test_transform_gateway_rejects_foreign_and_revoked_sources(test_client, test_engine, db_session, l5_owner, monkeypatch):
    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "transform-product-00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    repository = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "example-connection",
        "record_type": "repository_commit",
        "record_id": "example/repository",
        "record_revision": "a" * 40,
    }
    connection_ref = {**repository, "record_type": "connection", "record_id": "example-connection", "record_revision": "version-1"}
    rights = {("example-connection", "example/repository"), ("example-connection", "example-connection")}

    async def permitted(*args):
        return rights

    async def connections(*args):
        return [({"provider": "aws", "connection_ref": {"kind": "credential", "id": "example-connection"}, "record_ref": connection_ref}, None)]

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)
    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    publish = AsyncMock()
    monkeypatch.setattr(l5_integration, "publish_l5_work", publish)
    patched = await test_client.patch(
        f"/v1/products/{product_id}/projects/{project_id}/configuration",
        json={
            "configuration": {
                "basic": {"repositories": [repository]},
                "selected_capabilities": [{"capability": "refactor", "ownership_mode": "managed"}],
            }
        },
        headers={"Idempotency-Key": "transform-profile-00001", "If-Profile-Revision": "1"},
    )
    assert patched.status_code == 200, patched.text
    path = f"/v1/products/{product_id}/projects/{project_id}/services/transform/configuration"
    choices = {
        "service_id": "transform",
        "source_refs": [repository],
        "capabilities": ["refactor"],
        "enabled": False,
        "overrides": [{"field": "source_refs", "value": [repository], "reason": "Pin source"}],
        "transform": {
            "connection_ref": connection_ref,
            "executor_binding_ref": {**repository, "record_type": "executor_binding"},
            "test_policy_ref": {**repository, "record_type": "test_policy"},
            "region": "example-region",
            "read_paths": ["src"],
            "write_paths": ["src/module.py"],
            "excluded_paths": [],
            "goal_defaults": {"kind": "refactor", "summary": "Improve module"},
        },
    }
    for field, value in (
        ("source_refs", [{**repository, "tenant_id": "foreign-tenant"}]),
        ("source_refs", [{**repository, "record_id": "example/foreign"}]),
        (
            "transform",
            {**choices["transform"], "executor_binding_ref": {**choices["transform"]["executor_binding_ref"], "project_id": "foreign-project"}},
        ),
    ):
        attempt = {**choices, field: value}
        if field == "source_refs":
            attempt["overrides"] = [{"field": "source_refs", "value": value, "reason": "Not a grant"}]
        denied = await test_client.put(path, json=attempt, headers={"Idempotency-Key": "transform-denied-00001", "If-None-Match": "*"})
        assert denied.status_code == 404, denied.text
        assert "example/foreign" not in denied.text
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 0
    backend = l5_integration.GatewayConfigurationBackend(db_session, l5_owner)
    profile = await backend.profile(product_id, project_id)
    relation, profile_ref, available = await backend.profile_inputs(profile, product_id, project_id, "transform")
    await backend.store.create(product_id, project_id, "transform", relation.relationship_revision, profile, profile_ref, available, choices)
    from sqlalchemy.ext.asyncio import AsyncSession

    original_get = AsyncSession.get
    credential = SimpleNamespace(
        id="example-connection",
        user_id=l5_owner.user_id,
        org_id=l5_owner.org_id,
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="version-1",
        scopes={"account_id": "123456789012", "role_arn": "wrong-role"},
    )

    async def selected_credential(session, model, identity, **kwargs):
        if model is l5_integration.UserCredential:
            return credential
        return await original_get(session, model, identity, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", selected_credential)
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda observed: True)
    wrong_role = await test_client.get(path)
    assert wrong_role.status_code == 503, wrong_role.text
    assert "example/repository" not in wrong_role.text
    rights.clear()
    for response in (
        await test_client.get(path),
        await test_client.get(path + "/versions/1"),
        await test_client.put(path, json=choices, headers={"Idempotency-Key": "transform-denied-replay", "If-None-Match": "*"}),
    ):
        assert response.status_code == 404, response.text
        assert "example/repository" not in response.text
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_transform_selected_role_and_revoked_reference_deny_before_effect(db_session, l5_owner, monkeypatch):
    from src import l5_integration

    connection = {
        "tenant_id": l5_owner.org_id,
        "project_id": "example-project",
        "source_connection_id": "example-connection",
        "record_type": "connection",
        "record_id": "example-connection",
        "record_revision": "version-1",
    }
    repository = {**connection, "record_type": "repository_commit", "record_id": "example/repository"}
    settings = {"source_refs": [repository], "transform": {"connection_ref": connection, "region": "example-region"}}
    backend = l5_integration.GatewayConfigurationBackend(db_session, l5_owner)
    backend.source_rights = AsyncMock(return_value={("example-connection", "example/repository"), ("example-connection", "example-connection")})

    async def connections(*args):
        return [({"provider": "aws", "connection_ref": {"id": "example-connection"}, "record_ref": connection}, None)]

    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    credential = SimpleNamespace(
        id="example-connection",
        user_id=l5_owner.user_id,
        org_id=l5_owner.org_id,
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="version-1",
        scopes={"account_id": "123456789012", "role_arn": "wrong-role"},
    )
    get_credential = AsyncMock(return_value=credential)
    monkeypatch.setattr(db_session, "get", get_credential)
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda observed: True)
    with pytest.raises(HTTPException) as wrong_role_read:
        await backend.authorize_settings(settings, "example-project")
    assert wrong_role_read.value.status_code == 503
    configuration = SimpleNamespace(configuration_id="example-configuration", owner_id=l5_owner.user_id)
    version = SimpleNamespace(revision=1, settings=settings)
    backend.current = AsyncMock(return_value=(configuration, version))
    with pytest.raises(HTTPException) as wrong_role:
        await backend.binding_authority("example-product", "example-project", "transform")
    assert wrong_role.value.status_code == 503
    backend.source_rights = AsyncMock(return_value=set())
    get_credential.reset_mock()
    with pytest.raises(HTTPException) as revoked:
        await backend.authorize_settings(settings, "example-project")
    assert revoked.value.status_code == 404
    get_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_outage_preserves_saved_version_and_recovers(
    test_client,
    test_engine,
    db_session,
    app,
    l5_owner,
    monkeypatch,
):
    import httpx
    from fastapi import HTTPException

    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "outage_product_create_0001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.patch(
        f"{profile_path}/configuration",
        json={
            "configuration": {
                "selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}],
            }
        },
        headers={"Idempotency-Key": "outage_select_capability_0001", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    path = f"{profile_path}/services/security/configuration"
    create_headers = {"Idempotency-Key": "outage_configuration_00001", "If-None-Match": "*"}
    calls = []

    async def failed_link(*args, **kwargs):
        calls.append("product-conditional-patch")
        raise HTTPException(503, detail={"code": "source_unavailable"})

    with monkeypatch.context() as outage:
        outage.setattr(l5_integration, "link_l5_configuration", failed_link)
        first = await test_client.put(path, json={"capabilities": ["threat_model"]}, headers=create_headers)
    assert first.status_code == 201, first.text
    assert first.json()["handoff_state"] == "unavailable"
    assert calls == ["product-conditional-patch"]
    checked = (await test_client.post(f"{path}/check")).json()
    assert checked["ready"] is False and checked["provider_contacted"] is False
    assert "configuration_not_linked" in checked["gaps"]
    db_session.expire_all()
    stored = await db_session.scalar(select(Configuration))
    initial_version = await db_session.get(ConfigurationVersion, (l5_owner.org_id, first.json()["configuration_id"], 1))
    assert stored.handoff_state == "unavailable" and stored.current_revision == 1
    assert initial_version.settings == first.json()["settings"]

    async def missing_profile(*args, **kwargs):
        raise HTTPException(503, detail={"code": "private-peer-error"})

    operations = (
        (test_client.get, path, {}),
        (test_client.get, f"{path}/versions/1", {}),
        (test_client.post, f"{path}/check", {}),
        (test_client.put, path, {"json": {"capabilities": ["threat_model"]}, "headers": create_headers}),
        (
            test_client.put,
            path,
            {
                "json": {"exclusions": ["uncommitted/"]},
                "headers": {
                    "Idempotency-Key": "outage_config_edit_0001",
                    "If-Match": first.headers["etag"],
                },
            },
        ),
        (
            test_client.post,
            f"{path}/refresh",
            {
                "json": {"profile_ref": first.json()["settings"]["profile_ref"]},
                "headers": {"Idempotency-Key": "outage_config_refresh_0001", "If-Match": first.headers["etag"]},
            },
        ),
    )
    with monkeypatch.context() as outage:
        outage.setattr(l5_integration.product_routes, "get_profile", missing_profile)
        for method, url, kwargs in operations:
            response = await method(url, **kwargs)
            assert response.status_code == 503, response.text
            assert response.json()["detail"] == {"code": "dependency_unavailable", "retryable": True}
            assert "private-peer-error" not in response.text

    async def disconnected(*args, **kwargs):
        raise httpx.ConnectError("private-transport-error")

    with monkeypatch.context() as outage:
        outage.setattr(l5_integration.product_routes, "get_profile", disconnected)
        response = await test_client.get(path)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "dependency_unavailable"
        assert "private-transport-error" not in response.text

    with monkeypatch.context() as outage:
        outage.setattr(l5_integration.product_routes, "get_revision", missing_profile)
        response = await test_client.put(
            path,
            json={"exclusions": ["uncommitted/"]},
            headers={
                "Idempotency-Key": "outage_config_edit_0002",
                "If-Match": first.headers["etag"],
            },
        )
        assert response.status_code == 503, response.text

    with monkeypatch.context() as outage:
        outage.setattr(l5_integration, "permitted_source_refs", disconnected)
        response = await test_client.get(path)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "dependency_unavailable"

    db_session.expire_all()
    unchanged = await db_session.get(Configuration, (l5_owner.org_id, first.json()["configuration_id"]))
    assert unchanged.current_revision == 1 and unchanged.handoff_state == "unavailable"
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 1
    assert calls == ["product-conditional-patch"]

    outsider = l5_owner.model_copy(update={"org_id": "outage-other-tenant", "user_id": "outage-other-owner"})
    db_session.add_all(
        [
            Organization(id=outsider.org_id, name="Other outage workspace"),
            User(id=outsider.user_id, org_id=outsider.org_id, team_id=outsider.team_id, email="outage-other@example.test", user_kind="human"),
        ]
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: outsider
    denied = await test_client.get(path)
    assert denied.status_code == 404 and first.json()["configuration_id"] not in denied.text
    app.dependency_overrides[get_current_user] = lambda: l5_owner

    retry = await test_client.post(
        f"{path}/handoff",
        headers={
            "Idempotency-Key": "recover_handoff_0000001",
            "If-Match": first.headers["etag"],
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["handoff_state"] == "linked"
    assert retry.json()["revision"] == 1 and retry.json()["settings"] == first.json()["settings"]
    assert (await test_client.get(path)).json() == retry.json()
    db_session.expire_all()
    assert (await db_session.get(Configuration, (l5_owner.org_id, first.json()["configuration_id"]))).current_revision == 1
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1


@pytest.mark.asyncio
async def test_mounted_run_reads_and_queued_cancel_are_scoped(
    app,
    test_client,
    test_engine,
    db_session,
    l5_owner,
    monkeypatch,
):
    from l5_services.findings import normalize_result
    from l5_services.models import AssessmentResult

    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post(
        "/v1/products",
        json=REQUEST,
        headers={"Idempotency-Key": "create_l5_run_scope_00001"},
    )
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    relationship = await db_session.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == l5_owner.org_id,
            ProductProjectBinding.product_id == product_id,
            ProductProjectBinding.project_id == project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    assert relationship is not None
    config_id, run_id, work_id = (str(uuid4()) for _ in range(3))
    db_session.add(
        Configuration(
            tenant_id=l5_owner.org_id,
            configuration_id=config_id,
            product_id=product_id,
            project_id=project_id,
            service_id="security",
            relationship_revision=str(relationship.relationship_revision),
            owner_id=l5_owner.user_id,
            current_revision=1,
            handoff_state="linked",
        )
    )
    db_session.add(
        ConfigurationVersion(
            tenant_id=l5_owner.org_id,
            configuration_id=config_id,
            revision=1,
            settings={},
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="a" * 64,
        )
    )
    await db_session.flush()
    source = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "source-connection",
        "record_type": "document",
        "record_id": "source-document",
        "record_revision": "document-revision",
    }
    db_session.add(
        Run(
            tenant_id=l5_owner.org_id,
            run_id=run_id,
            product_id=product_id,
            project_id=project_id,
            service_id="security",
            configuration_id=config_id,
            configuration_revision=1,
            binding_id="verified-binding",
            trigger_id="manual-test",
            binding_generation=1,
            input={"sources": [source]},
            admission={
                "scope": {
                    "tenant_id": l5_owner.org_id,
                    "product_id": product_id,
                    "project_id": project_id,
                    "relationship_revision": str(relationship.relationship_revision),
                    "owner_id": l5_owner.user_id,
                    "visibility": "only_me",
                }
            },
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    db_session.add(
        Work(
            tenant_id=l5_owner.org_id,
            work_id=work_id,
            run_id=run_id,
            kind="execute",
            generation=1,
            step=0,
            due_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    rights = {("source-connection", "source-document")}

    async def permitted_sources(db, actor, selected_project_id):
        assert selected_project_id == project_id
        return rights.copy()

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted_sources)
    path = f"/v1/products/{product_id}/projects/{project_id}/services/security/runs"
    read = await test_client.get(f"{path}/{run_id}")
    assert read.status_code == 200, read.text
    assert read.json()["run_id"] == run_id
    assert read.headers["cache-control"] == "no-store"
    run = await db_session.get(Run, (l5_owner.org_id, run_id))
    native = {**source, "source_connection_id": "security-provider", "record_type": "threat_job", "record_id": "native-job"}
    payload = {
        "native_job_ref": native,
        "coverage": "complete",
        "findings": [
            {
                "native_id": "native-finding",
                "kind": "candidate_threat",
                "claim": "private host finding",
                "provider_severity": "medium",
                "source_refs": [source],
            }
        ],
    }
    result_id = str(uuid4())
    detail, findings = normalize_result(run, result_id, 1, payload, datetime.now(UTC))
    db_session.add(AssessmentResult(tenant_id=l5_owner.org_id, result_id=result_id, run_id=run_id, version=1, payload=payload))
    db_session.add(detail)
    db_session.add_all(findings)
    await db_session.commit()
    results_path = path.removesuffix("/runs") + "/results"
    visible = await test_client.get(results_path, params={"limit": 1})
    assert visible.status_code == 200, visible.text
    assert [item["result_id"] for item in visible.json()["items"]] == [result_id]
    artifact_id = visible.json()["items"][0]["findings_artifact"]["artifact_id"]
    metadata_path = path.removesuffix("/runs") + f"/artifacts/{artifact_id}"
    metadata = await test_client.get(metadata_path)
    assert metadata.status_code == 200, metadata.text
    content_path = metadata.json()["download_path"]
    exported = await test_client.get(content_path)
    assert exported.status_code == 200 and exported.json()["items"][0]["claim"] == "private host finding"
    listed = await test_client.get(path)
    assert listed.status_code == 200 and [item["run_id"] for item in listed.json()["items"]] == [run_id]
    db_session.expire_all()
    assert (await db_session.get(Run, (l5_owner.org_id, run_id))).revision == 1
    assert (await db_session.get(Work, (l5_owner.org_id, work_id))).completed_at is None

    rights.clear()
    assert (await test_client.get(results_path)).json() == {"items": []}
    for location in (f"{results_path}/{result_id}", f"{results_path}/{result_id}/findings", metadata_path, content_path):
        denied = await test_client.get(location)
        assert denied.status_code == 404 and "private host finding" not in denied.text
    assert (await test_client.get(f"{path}/{run_id}")).status_code == 404
    assert (await test_client.get(path)).json()["items"] == []
    for operation in ("cancel", "reconcile", "retry"):
        assert (
            await test_client.post(
                f"{path}/{run_id}/{operation}", json={"reason": "no source rights"}, headers={"Idempotency-Key": "source_rights_mutation_01"}
            )
        ).status_code == 404
    rights.add(("source-connection", "source-document"))
    app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(
        update={
            "org_id": "foreign-workspace",
            "user_id": "foreign-actor",
        }
    )
    assert (await test_client.get(f"{path}/{run_id}")).status_code == 404
    assert (
        await test_client.post(
            f"{path}/{run_id}/cancel", json={"reason": "test cancellation"}, headers={"Idempotency-Key": "foreign_cancel_attempt_001"}
        )
    ).status_code == 404
    for operation in ("reconcile", "retry"):
        assert (
            await test_client.post(
                f"{path}/{run_id}/{operation}", json={"reason": "foreign request"}, headers={"Idempotency-Key": "foreign_mutation_attempt_01"}
            )
        ).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: l5_owner
    assert (await test_client.post(f"{path}/{run_id}/cancel", json={"reason": "test cancellation"})).status_code == 428
    cancelled = await test_client.post(
        f"{path}/{run_id}/cancel",
        json={"reason": "test cancellation"},
        headers={"Idempotency-Key": "owner_cancel_queued_run_0001"},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    db_session.expire_all()
    assert (await db_session.get(Run, (l5_owner.org_id, run_id))).cancel_reason == "test cancellation"
    work = await db_session.get(Work, (l5_owner.org_id, work_id))
    assert work.completed_at is not None and work.fence == 1
    replay = await test_client.post(
        f"{path}/{run_id}/cancel",
        json={"reason": "test cancellation"},
        headers={"Idempotency-Key": "owner_cancel_queued_run_0001"},
    )
    assert replay.json() == cancelled.json()
    assert (
        await test_client.post(
            f"{path}/{run_id}/cancel",
            json={"reason": "changed"},
            headers={"Idempotency-Key": "owner_cancel_queued_run_0001"},
        )
    ).status_code == 409
    assert (
        await test_client.post(
            f"{path}/{run_id}/retry",
            json={"reason": "no definitive failed-no-effect proof"},
            headers={"Idempotency-Key": "owner_retry_refused_0001"},
        )
    ).status_code == 409
    reconciled = await test_client.post(
        f"{path}/{run_id}/reconcile",
        json={"reason": "confirm native state"},
        headers={"Idempotency-Key": "owner_reconcile_run_0001"},
    )
    assert reconciled.status_code == 202, reconciled.text
    assert reconciled.json()["admission"] == cancelled.json()["admission"]
    db_session.expire_all()
    reconciliation = (await db_session.scalars(select(Work).where(Work.kind == "reconcile"))).one()
    receipt = (await db_session.scalars(select(RunReceipt).where(RunReceipt.operation == "reconcile"))).one()
    assert reconciliation.run_id == run_id and reconciliation.completed_at is None
    assert receipt.response == reconciled.json()
    rights.clear()
    assert (
        await test_client.post(
            f"{path}/{run_id}/reconcile",
            json={"reason": "confirm native state"},
            headers={"Idempotency-Key": "owner_reconcile_run_0001"},
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_native_browser_launch_without_configured_binding_fails_closed(test_client, test_engine, l5_owner, app, monkeypatch):
    limiter = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_l5_launch_fixture_0001"})
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/services/devops/bindings/unconfigured/browser-launch"
    rejected = await test_client.post(path, headers={"Authorization": "Bearer offline-placeholder"}, json={"configuration_revision": 1})
    assert rejected.status_code == 404
    assert rejected.json() == {"detail": {"code": "not_found"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 401),
        ("service", 401),
        ("iam", 401),
        ("expired", 401),
        ("throttled", 429),
        ("limiter_unavailable", 503),
        ("foreign_origin", 403),
    ],
)
async def test_browser_launch_denials_precede_product_and_provider_work(
    test_client,
    app,
    l5_owner,
    monkeypatch,
    case,
    status,
):
    from src import l5_integration

    selected = l5_owner
    if case == "service":
        selected = selected.model_copy(update={"account_type": "service"})
    elif case == "iam":
        selected = selected.model_copy(update={"auth_source": "iam"})
    elif case == "expired":
        selected = selected.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    app.dependency_overrides[get_current_user] = lambda: selected
    scope = AsyncMock()
    monkeypatch.setattr(l5_integration.product_routes, "scoped_product", scope)
    guard = Mock()
    provider = AsyncMock()
    monkeypatch.setattr(l5_integration, "authorize_browser_launch", guard)
    monkeypatch.setattr(federation, "mint_browser_launch", provider)
    result = SimpleNamespace(allowed=case != "throttled", retry_after_seconds=12)
    limiter = AsyncMock(return_value=result)
    if case == "limiter_unavailable":
        limiter.side_effect = RuntimeError("offline limiter unavailable")
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    headers = {} if case == "missing" else {"Authorization": "Bearer offline-placeholder"}
    if case == "foreign_origin":
        headers["Origin"] = "https://different.example.test"
    response = await test_client.post(
        "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch",
        json={"configuration_revision": 1},
        headers=headers,
    )
    assert response.status_code == status
    assert response.headers.get("Retry-After") == ("12" if case == "throttled" else None)
    scope.assert_not_awaited()
    guard.assert_not_called()
    provider.assert_not_awaited()
    if case in {"missing", "service", "iam", "expired"}:
        limiter.assert_not_awaited()
    else:
        limiter.assert_awaited_once()


@pytest.mark.asyncio
async def test_foreign_product_bookmark_does_not_reach_binding_or_federation(
    test_client,
    test_engine,
    l5_owner,
    app,
    monkeypatch,
):
    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_foreign_bookmark_0001"})
    assert created.status_code == 201
    limiter = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    guard = Mock()
    monkeypatch.setattr(l5_integration, "authorize_browser_launch", guard)
    result = await test_client.post(
        f"/v1/products/{created.json()['product_id']}/projects/another-project/services/devops/bindings/binding/browser-launch",
        headers={"Authorization": "Bearer offline-placeholder"},
        json={"configuration_revision": 1},
    )
    assert result.status_code == 404
    guard.assert_not_called()
    limiter.assert_awaited_once()


@pytest.mark.asyncio
async def test_removed_member_with_unexpired_token_cannot_retry_native_launch(
    test_client,
    test_engine,
    db_session,
    l5_owner,
    app,
    monkeypatch,
):
    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_native_removal_0001"})
    assert created.status_code == 201
    await revoke_membership(db_session, org_id=l5_owner.org_id, user_id=l5_owner.user_id)
    limiter = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    guard = Mock()
    provider = AsyncMock()
    current_binding = AsyncMock()
    monkeypatch.setattr(l5_integration, "authorize_browser_launch", guard)
    monkeypatch.setattr(federation, "mint_browser_launch", provider)
    monkeypatch.setattr(l5_integration.ConfigurationStore, "current", current_binding)

    result = await test_client.post(
        f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/services/devops/bindings/binding/browser-launch",
        headers={"Authorization": "Bearer offline-placeholder"},
        json={"configuration_revision": 1},
    )
    assert result.status_code == 404
    # The shared L5 identity dependency now rejects removed members before
    # building a backend or entering the native-launch handler.
    limiter.assert_not_awaited()
    current_binding.assert_not_awaited()
    guard.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["product", "project"])
async def test_archived_product_scope_cannot_retry_native_launch(
    test_client,
    test_engine,
    db_session,
    l5_owner,
    app,
    monkeypatch,
    target,
):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_native_archived_0001"})
    assert created.status_code == 201
    model, identity = (Product, created.json()["product_id"]) if target == "product" else (Project, created.json()["project_id"])
    archived = await db_session.get(model, (l5_owner.org_id, identity))
    archived.archived_at = datetime.now(UTC)
    await db_session.commit()
    limiter = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    result = await test_client.post(
        f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/services/devops/bindings/binding/browser-launch",
        headers={"Authorization": "Bearer offline-placeholder"},
        json={"configuration_revision": 1},
    )
    assert result.status_code == 404
    limiter.assert_awaited_once()


@pytest.mark.asyncio
async def test_logged_out_native_launch_is_denied_before_rate_or_provider(
    test_client,
    app,
    l5_owner,
    monkeypatch,
):
    from src import l5_integration

    limiter = AsyncMock()
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    session = AsyncMock(side_effect=HTTPException(401, detail={"code": "session_required"}))
    monkeypatch.setattr(l5_integration, "require_native_launch_session", session)
    scope = AsyncMock()
    monkeypatch.setattr(l5_integration.product_routes, "scoped_product", scope)
    result = await test_client.post(
        "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch",
        headers={"Authorization": "Bearer offline-placeholder"},
        json={"configuration_revision": 1},
    )
    assert result.status_code == 401
    session.assert_awaited_once_with("Bearer offline-placeholder")
    limiter.assert_not_awaited()
    scope.assert_not_awaited()


@pytest.fixture
async def durable_devops_scope(test_client, test_engine, db_session, l5_owner):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_native_browser_scope_0001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    product_path = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.patch(
        product_path + "/configuration",
        json={
            "configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "integrated"}]},
        },
        headers={"Idempotency-Key": "select_native_browser_capability_0001", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    relationship = await db_session.scalar(
        select(ProductProjectBinding).where(ProductProjectBinding.product_id == product_id, ProductProjectBinding.project_id == project_id)
    )
    selection = SpaceSelection(l5_owner.org_id, product_id, project_id, "browser-binding", "123456789012", "us-east-1")
    role = "arn:aws:iam::123456789012:role/hosting"
    launcher = "arn:aws:iam::123456789012:role/launch"
    operator = "arn:aws:iam::123456789012:role/operator"
    credential = UserCredential(
        id=str(uuid4()),
        org_id=l5_owner.org_id,
        user_id=l5_owner.user_id,
        service="aws",
        credential_type="aws_role",
        label="selected",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
        aws_external_id="offline-external-id",
        aws_verified_at=datetime.now(UTC),
        aws_verification_attempt=str(uuid4()),
        aws_verified_version_id="version-one",
        scopes={"account_id": selection.account_id, "role_arn": role, "status": "verified"},
    )
    reference = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": "version-one",
    }
    settings = {
        "visibility": "only_me",
        "devops": {
            "hosting_connection_ref": reference,
            "region": selection.region,
            "authentication_mode": "scoped_iam_federation",
            "browser_launch_policy": "scoped_session",
            "permitted_native_actions": ["launchNativeBrowser"],
            "launcher_role_arn": launcher,
            "operator_role_arn": operator,
        },
    }
    config_id = str(uuid4())
    configuration = Configuration(
        tenant_id=l5_owner.org_id,
        configuration_id=config_id,
        product_id=product_id,
        project_id=project_id,
        service_id="devops",
        relationship_revision=str(relationship.relationship_revision),
        owner_id=l5_owner.user_id,
        current_revision=1,
        handoff_state="linked",
    )
    scope = {
        "tenant_id": l5_owner.org_id,
        "product_id": product_id,
        "project_id": project_id,
        "relationship_revision": configuration.relationship_revision,
        "owner_id": l5_owner.user_id,
        "visibility": "only_me",
    }
    observed_at = datetime.now(UTC)
    db_session.add_all(
        [
            credential,
            configuration,
            ProductSourceBinding(
                tenant_id=l5_owner.org_id,
                project_id=project_id,
                kind="environment",
                provider="aws",
                credential_id=credential.id,
                namespace=selection.account_id,
                native_id=selection.region,
                desired={
                    "kind": "environment",
                    "provider": "aws",
                    "namespace": selection.account_id,
                    "native_id": selection.region,
                    "connection_ref": {"kind": "credential", "id": credential.id},
                },
                observed={
                    "namespace": selection.account_id,
                    "source_revision": "version-one",
                    "observed_at": observed_at.isoformat(),
                    "stale_after": (observed_at + timedelta(hours=1)).isoformat(),
                },
                status="healthy",
            ),
            ConfigurationVersion(
                tenant_id=l5_owner.org_id,
                configuration_id=config_id,
                revision=1,
                settings=settings,
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="fixture-hash",
            ),
            DevopsSpaceIntent(
                tenant_id=l5_owner.org_id,
                operation_id=str(uuid4()),
                selection=vars(selection),
                configuration_revision=1,
                intent_hash="fixture-hash",
                state="ready",
                native_space_id="native-space",
                ownership="adopted",
                scope_fingerprint=selection.fingerprint(),
            ),
            Binding(
                tenant_id=l5_owner.org_id,
                binding_id=selection.binding_id,
                configuration_id=config_id,
                product_id=product_id,
                project_id=project_id,
                service_id="devops",
                scope=scope,
                revision=1,
                generation=1,
                state="ready",
                ownership="adopted",
                connection_ref=reference,
                connection_key="connection-key",
                native_resource_ref={"record_type": "agent_space", "source_connection_id": credential.id, "record_id": "native-space"},
                native_key="native-key",
                provider_identity={"account_id": selection.account_id, "region": selection.region, "role_arn": role},
                readiness=[],
                gaps=[],
            ),
            CredentialServiceDelegation(
                tenant_id=l5_owner.org_id,
                credential_id=credential.id,
                service_id="l5-devops-worker",
                product_id=product_id,
                project_id=project_id,
                created_by=l5_owner.user_id,
                operations=["credential:session"],
                capabilities=["native_ui"],
                sources=[{**reference, "record_type": "credential"}],
                visibility="only_me",
                binding_generation=1,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    await db_session.commit()
    return SimpleNamespace(
        product_id=product_id,
        project_id=project_id,
        relationship=relationship,
        selection=selection,
        role=role,
        launcher=launcher,
        operator=operator,
        credential=credential,
        reference=reference,
        settings=settings,
        config_id=config_id,
        scope=scope,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("setup_history", ["original", "checked", "checked_stale_delegation", "adopted", "adopted_legacy"])
async def test_normal_mounted_devops_launch_uses_durable_product_scope(
    test_client,
    test_engine,
    l5_owner,
    app,
    durable_devops_scope,
    monkeypatch,
    setup_history,
):
    from src import l5_integration

    scope = durable_devops_scope
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        from l5_services.api.bindings import BindingStore
        from l5_services.devops.setup import record_setup

        binding = await session.get(Binding, (l5_owner.org_id, scope.selection.binding_id))
        grant = await session.scalar(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.credential_id == scope.credential.id,
            )
        )
        if setup_history in {"checked", "checked_stale_delegation"}:
            store = BindingStore(session, l5_owner.org_id, scope.product_id, scope.project_id, "devops")
            native_reference = {**scope.reference, "record_type": "agent_space", "record_id": "native-space", "record_revision": "native-space"}
            verified = {
                "configuration_id": scope.config_id,
                "configuration_revision": 1,
                "binding_id": binding.binding_id,
                "scope": scope.scope,
                "connection_ref": scope.reference,
                **binding.provider_identity,
                "authorization": {"actor_id": l5_owner.user_id, "delegation_id": grant.id, "delegation_revision": grant.revision},
            }
            native = AsyncMock(
                read=AsyncMock(
                    return_value={
                        "native_resource_ref": native_reference,
                        "provider_identity": binding.provider_identity,
                        "state": "ready",
                        "readiness": [],
                        "gaps": [],
                        "prevention_observation": {
                            "scope_fingerprint": scope.selection.fingerprint(),
                            "configuration_revision": 1,
                            "native_space_id": binding.native_resource_ref["record_id"],
                        },
                    }
                )
            )
            # The browser-only legacy fixture has no original setup operation.
            # CHECK needs a real retained link, including its successful intent;
            # build it through the store with the historical authorization shape.
            receipt = await session.scalar(
                select(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == l5_owner.org_id,
                    DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
                )
            )
            await session.delete(receipt)
            await session.delete(binding)
            await session.flush()
            original = await store.start(
                {"action": "link", "configuration_revision": 1, "native_resource_ref": native_reference},
                "link_before_browser_check",
                verified,
            )
            original = await store.finish_link(original.operation_id, verified, native)
            assert original.state == "succeeded"
            binding = await store.binding(scope.selection.binding_id)
            assert binding.ownership == "customer_linked" and binding.creation_receipt is None
            operation = await store.control(
                {"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id},
                "check_before_browser_launch",
                '"1"',
                verified,
            )
            await store.finish_control(operation.operation_id, verified, native)
            await session.refresh(binding)
            assert binding.revision == 3
            from l5_services.devops.setup import original_setup

            retained = await original_setup(session, binding, require_setup_consent=False)
            assert retained.operation_id == original.operation_id
            with pytest.raises(ValueError, match="setup_provenance_unavailable"):
                await original_setup(session, binding)
            if setup_history == "checked":
                grant.binding_generation = binding.generation
        elif setup_history in {"adopted", "adopted_legacy"}:
            receipt = await session.scalar(
                select(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == l5_owner.org_id,
                    DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
                )
            )
            if setup_history == "adopted":
                await session.delete(receipt)
            else:
                receipt.scope_fingerprint = None
            await session.flush()
            intent = SimpleNamespace(
                tenant_id=l5_owner.org_id,
                product_id=scope.product_id,
                project_id=scope.project_id,
                operation_id=str(uuid4()),
                request={"configuration_revision": 1},
                intent_hash="adoption-hash",
            )
            await record_setup(
                session,
                intent,
                binding,
                {
                    "prevention_observation": {
                        "scope_fingerprint": scope.selection.fingerprint(),
                        "configuration_revision": 1,
                        "native_space_id": binding.native_resource_ref["record_id"],
                    }
                },
            )
            assert binding.ownership == "adopted" and binding.creation_receipt is None
            store = BindingStore(session, l5_owner.org_id, scope.product_id, scope.project_id, "devops")
            with pytest.raises(HTTPException) as denied:
                await store.control(
                    {"action": "cleanup", "configuration_revision": 1, "binding_id": binding.binding_id, "owned_cleanup_confirmed": True},
                    "adopted_cleanup_denied",
                    '"1"',
                    {
                        "configuration_id": scope.config_id,
                        "configuration_revision": 1,
                        "scope": scope.scope,
                        "connection_ref": scope.reference,
                        **binding.provider_identity,
                        "authorization": {"actor_id": l5_owner.user_id},
                    },
                )
            assert denied.value.status_code == 403
            assert denied.value.detail == {"code": "ownership_required"}
        await session.commit()
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(l5_integration, "get_settings", lambda: SimpleNamespace(gateway_base_url="https://gateway.example.test"))
    connection_session = AsyncMock(return_value=Mock())
    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "_devops_connection_session", connection_session)
    provider = AsyncMock(
        return_value={
            "binding_id": scope.selection.binding_id,
            "launch_url": "https://signin.aws.amazon.com/federation?ticket=fixture",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )
    monkeypatch.setattr(federation, "mint_browser_launch", provider)
    path = f"/v1/products/{scope.product_id}/projects/{scope.project_id}"
    profile = await test_client.get(path)
    options = await test_client.get(path + "/services/devops/configuration/options")
    assert options.status_code == 200, options.text
    assert options.json()["connections"] == [scope.reference]
    configured = await test_client.get(path + "/services/devops/configuration")
    assert configured.status_code == 200, configured.text
    assert configured.json()["settings"] == scope.settings
    bindings = await test_client.get(path + "/services/devops/bindings")
    assert bindings.status_code == 200, bindings.text
    assert [binding["binding_id"] for binding in bindings.json()["items"]] == [scope.selection.binding_id]
    launched = await test_client.post(
        path + f"/services/devops/bindings/{scope.selection.binding_id}/browser-launch",
        headers={"Authorization": "Bearer offline-placeholder"},
        json={"configuration_revision": 1},
    )
    if setup_history == "checked_stale_delegation":
        assert launched.status_code == 403, launched.text
        assert launched.json() == {"detail": {"code": "delegation_denied"}}
        connection_session.assert_not_awaited()
        provider.assert_not_awaited()
        return
    assert profile.status_code == 200, profile.text
    assert {"capability": "devops", "ownership_mode": "integrated"} in profile.json()["configuration"]["selected_capabilities"]
    assert launched.status_code == 200, launched.text
    assert launched.json() == provider.return_value
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        product = await independent.get(Product, (l5_owner.org_id, scope.product_id))
        project = await independent.get(Project, (l5_owner.org_id, scope.project_id))
        version = await independent.get(ConfigurationVersion, (l5_owner.org_id, scope.config_id, 1))
        stored_binding = await independent.get(Binding, (l5_owner.org_id, scope.selection.binding_id))
        source = await independent.scalar(
            select(ProductSourceBinding).where(
                ProductSourceBinding.project_id == scope.project_id, ProductSourceBinding.credential_id == scope.credential.id
            )
        )
        delegation = await independent.scalar(
            select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == scope.credential.id)
        )
        assert product is not None and project is not None
        assert version.state == stored_binding.state == "ready"
        assert stored_binding.connection_ref == scope.reference
        assert source.status == "healthy" and source.observed["source_revision"] == scope.reference["record_revision"]
        assert delegation.binding_generation == stored_binding.generation == (2 if setup_history == "checked" else 1)
    connection_session.assert_awaited_once()
    provider.assert_awaited_once()
    assert provider.await_args.args == (scope.selection,)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unverified", "expired", "changed_authority", "new_version", "foreign_project", "removed_membership"])
async def test_mounted_devops_configuration_rechecks_connection_authority(
    test_client,
    db_session,
    l5_owner,
    durable_devops_scope,
    case,
):
    scope = durable_devops_scope
    path = f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops"
    configured = await test_client.get(path + "/configuration")
    assert configured.status_code == 200, configured.text
    if case == "unverified":
        scope.credential.aws_verified_version_id = None
    elif case == "expired":
        scope.credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif case == "changed_authority":
        scope.credential.scopes = {**scope.credential.scopes, "role_arn": "arn:aws:iam::123456789012:role/other"}
    elif case == "new_version":
        scope.credential.aws_verified_version_id = "version-two"
    elif case == "foreign_project":
        version = await db_session.get(ConfigurationVersion, (l5_owner.org_id, scope.config_id, 1))
        version.settings = {
            **scope.settings,
            "devops": {**scope.settings["devops"], "hosting_connection_ref": {**scope.reference, "project_id": "foreign-project"}},
        }
    else:
        await revoke_membership(db_session, org_id=l5_owner.org_id, user_id=l5_owner.user_id)
    await db_session.commit()
    denied = await test_client.get(path + "/configuration")
    assert denied.status_code == 404, denied.text
    bindings = await test_client.get(path + "/bindings")
    assert bindings.status_code == 404, bindings.text
    options = await test_client.get(path + "/configuration/options")
    if case == "removed_membership":
        assert options.status_code == 404, options.text
    else:
        assert options.status_code == 200, options.text
        references = options.json()["connections"]
        if case == "new_version":
            assert references == [{**scope.reference, "record_revision": "version-two"}]
        elif case == "foreign_project":
            assert references == [scope.reference]
        else:
            assert references == []


@pytest.fixture
async def durable_devops_boundary(test_engine, durable_devops_scope, app, monkeypatch):
    from src import l5_integration

    scope = durable_devops_scope
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    connection_session = AsyncMock(return_value=Mock())
    browser = AsyncMock(
        return_value={
            "binding_id": scope.selection.binding_id,
            "launch_url": "https://signin.aws.amazon.com/federation?ticket=fixture",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )
    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "_devops_connection_session", connection_session)
    monkeypatch.setattr(l5_integration, "get_settings", lambda: SimpleNamespace(gateway_base_url="https://gateway.example.test"))
    monkeypatch.setattr(federation, "mint_browser_launch", browser)

    class NativeSpaces:
        def __init__(self):
            self.spaces = {}
            self.creates = 0
            self.lose_readback = False

        def list_agent_spaces(self, **kwargs):
            return {"agentSpaces": [item["agentSpace"] for item in self.spaces.values()]}

        def create_agent_space(self, *, name, tags, **request):
            assert set(request) == {"clientToken"}
            assert request["clientToken"]
            self.creates += 1
            space_id = "recovered-space"
            self.spaces[space_id] = {"agentSpace": {"agentSpaceId": space_id, "name": name}, "tags": tags}
            return {"agentSpace": {"agentSpaceId": space_id}}

        def get_agent_space(self, **request):
            assert set(request) == {"agentSpaceId"}
            if self.lose_readback:
                raise ConnectionError("native response lost")
            return self.spaces[request["agentSpaceId"]]

    native = NativeSpaces()
    selection = replace(scope.selection, binding_id="recovery-binding")
    lifecycle = SpaceLifecycle(
        async_sessionmaker(test_engine, expire_on_commit=False), SpaceAdapter(native, account_id=selection.account_id, region=selection.region)
    )
    operation_id = "durable-recovery-operation"
    await lifecycle.prepare_create(selection, operation_id, 1)
    return SimpleNamespace(
        scope=scope,
        native=native,
        selection=selection,
        lifecycle=lifecycle,
        operation_id=operation_id,
        browser=browser,
        connection_session=connection_session,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked_delegation", "stale_binding", "foreign_member"])
async def test_mounted_devops_scope_denial_preserves_recovered_space(
    test_client,
    test_engine,
    db_session,
    l5_owner,
    app,
    durable_devops_boundary,
    denial,
):
    boundary = durable_devops_boundary
    selection, operation_id = boundary.selection, boundary.operation_id
    with pytest.raises(ValueError, match="changed creation intent"):
        await boundary.lifecycle.prepare_create(replace(selection, product_id="foreign-product"), operation_id, 1)
    assert boundary.native.creates == 0
    boundary.native.lose_readback = True
    with pytest.raises(ConnectionError, match="native response lost"):
        await boundary.lifecycle.execute_create(selection.tenant_id, operation_id)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        uncertain = await independent.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
        assert uncertain.state == "outcome_unknown" and uncertain.native_space_id is None
    boundary.native.lose_readback = False
    await boundary.lifecycle.prepare_create(selection, operation_id, 1)
    observed = await boundary.lifecycle.execute_create(selection.tenant_id, operation_id)
    replay = await boundary.lifecycle.execute_create(selection.tenant_id, operation_id)
    assert replay == observed and observed.agent_space_id == "recovered-space"
    assert boundary.native.creates == 1

    scope = boundary.scope
    if denial == "revoked_delegation":
        grant = await db_session.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == scope.credential.id))
        grant.revoked_at = datetime.now(UTC)
    elif denial == "stale_binding":
        binding = await db_session.get(Binding, (l5_owner.org_id, scope.selection.binding_id))
        binding.generation += 1
    else:
        outsider = l5_owner.model_copy(update={"org_id": "foreign-workspace", "user_id": "foreign-member"})
        app.dependency_overrides[get_current_user] = lambda: outsider
    await db_session.commit()
    path = f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops/bindings/{scope.selection.binding_id}/browser-launch"
    denied = await test_client.post(path, headers={"Authorization": "Bearer offline-placeholder"}, json={"configuration_revision": 1})
    assert denied.status_code == (404 if denial == "foreign_member" else 403), denied.text
    boundary.browser.assert_not_awaited()
    boundary.connection_session.assert_not_awaited()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        receipt = await independent.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
        version = await independent.get(ConfigurationVersion, (l5_owner.org_id, scope.config_id, 1))
        assert receipt.state == "succeeded" and receipt.native_space_id == observed.agent_space_id
        assert receipt.selection == vars(selection) and version.settings == scope.settings
    assert boundary.native.creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "real_connection,case",
    [
        (False, "valid"),
        (True, "valid"),
        (False, "first_party_origin"),
        (False, "foreign_connection_scope"),
        (False, "stale_generation"),
        (False, "revoked_delegation"),
        (False, "expired_delegation"),
        (False, "stale_receipt"),
        (False, "removed_membership"),
        (False, "db_stale_configuration"),
        (False, "db_stale_relationship"),
        (False, "db_expired_credential"),
        (False, "db_archived_product"),
        (False, "db_stale_receipt"),
        (False, "db_unlinked_relationship"),
    ],
)
async def test_selected_binding_mints_browser_handoff(
    test_client,
    db_session,
    l5_owner,
    app,
    monkeypatch,
    durable_devops_scope,
    real_connection,
    case,
):
    from src import l5_integration

    fixture = durable_devops_scope
    product_id, project_id = fixture.product_id, fixture.project_id
    relationship, selection = fixture.relationship, fixture.selection
    role, launcher, operator = fixture.role, fixture.launcher, fixture.operator
    credential, reference, settings = fixture.credential, fixture.reference, fixture.settings
    config_id = fixture.config_id
    if case not in {"valid", "first_party_origin"}:
        binding = await db_session.get(Binding, (l5_owner.org_id, selection.binding_id))
        grant = await db_session.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == credential.id))
        if case == "foreign_connection_scope":
            wrong_ref = {**reference, "tenant_id": "another-tenant"}
            version = await db_session.get(ConfigurationVersion, (l5_owner.org_id, config_id, 1))
            version.settings = {**settings, "devops": {**settings["devops"], "hosting_connection_ref": wrong_ref}}
            binding.connection_ref = wrong_ref
            grant.sources = [{**wrong_ref, "record_type": "credential"}]
        elif case == "stale_generation":
            binding.generation += 1
        elif case == "revoked_delegation":
            grant.revoked_at = datetime.now(UTC)
        elif case == "expired_delegation":
            grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif case == "stale_receipt":
            receipt = await db_session.scalar(select(DevopsSpaceIntent).where(DevopsSpaceIntent.tenant_id == l5_owner.org_id))
            receipt.selection = {**receipt.selection, "binding_id": "different-binding"}
        elif case == "removed_membership":
            await revoke_membership(db_session, org_id=l5_owner.org_id, user_id=l5_owner.user_id)
        elif case == "db_stale_configuration":
            await db_session.get(ConfigurationVersion, (l5_owner.org_id, config_id, 1))
            await db_session.execute(
                update(ConfigurationVersion).where(ConfigurationVersion.configuration_id == config_id).values(state="disabled"),
                execution_options={"synchronize_session": False},
            )
        elif case == "db_stale_relationship":
            await db_session.execute(
                update(ProductProjectBinding)
                .where(ProductProjectBinding.product_id == product_id)
                .values(relationship_revision=relationship.relationship_revision + 1),
                execution_options={"synchronize_session": False},
            )
        elif case == "db_expired_credential":
            await db_session.execute(
                update(UserCredential).where(UserCredential.id == credential.id).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
                execution_options={"synchronize_session": False},
            )
        elif case == "db_archived_product":
            await db_session.get(Product, (l5_owner.org_id, product_id))
            await db_session.execute(
                update(Product).where(Product.id == product_id).values(archived_at=datetime.now(UTC)),
                execution_options={"synchronize_session": False},
            )
        elif case == "db_stale_receipt":
            await db_session.scalar(select(DevopsSpaceIntent).where(DevopsSpaceIntent.tenant_id == l5_owner.org_id))
            await db_session.execute(
                update(DevopsSpaceIntent).where(DevopsSpaceIntent.tenant_id == l5_owner.org_id).values(state="outcome_unknown"),
                execution_options={"synchronize_session": False},
            )
        elif case == "db_unlinked_relationship":
            await db_session.execute(
                update(ProductProjectBinding).where(ProductProjectBinding.product_id == product_id).values(valid_to=datetime.now(UTC)),
                execution_options={"synchronize_session": False},
            )
        await db_session.commit()
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    provider = AsyncMock(
        return_value={
            "binding_id": selection.binding_id,
            "launch_url": "https://signin.aws.amazon.com/federation?ticket=fixture",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )
    monkeypatch.setattr(
        l5_integration, "get_settings", lambda: SimpleNamespace(gateway_base_url="https://gateway.example.test", aws_region="us-east-1")
    )
    if real_connection:
        secret = {"role_arn": role, "account_id": selection.account_id, "external_id": credential.aws_external_id}
        monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
        helper = Mock(
            current_version_id=Mock(return_value="version-one"), get_secret_at_version=Mock(return_value=(json.dumps(secret), "version-one"))
        )
        monkeypatch.setattr(l5_integration, "SecretsManagerHelper", Mock(return_value=helper))
        hosting_assume = Mock(
            return_value=SimpleNamespace(
                access_key_id="offline-key-id",
                secret_access_key="offline-secret",
                session_token="offline-session",
                assumed_role_arn="arn:aws:sts::123456789012:assumed-role/hosting/fixture",
            )
        )
        monkeypatch.setattr(l5_integration, "assume_role", hosting_assume)
        launcher_assume = Mock(
            side_effect=lambda **kwargs: {
                "AssumedRoleUser": {"Arn": f"arn:aws:sts::123456789012:assumed-role/launch/{kwargs['RoleSessionName']}"},
                "Credentials": {
                    "AccessKeyId": "temporary-id",
                    "SecretAccessKey": "temporary-secret",
                    "SessionToken": "temporary-token",
                    "Expiration": datetime.now(UTC) + timedelta(minutes=15),
                },
            }
        )
        aws_session = Mock()
        aws_session.client.return_value = Mock(
            assume_role=launcher_assume,
            get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": "native-space"}}),
            get_operator_app=Mock(
                return_value={"iam": {"operatorAppRoleArn": operator}, "operatorAppUrl": "https://native-space.aidevops.global.app.aws/"}
            ),
        )
        monkeypatch.setattr(l5_integration.boto3, "Session", Mock(return_value=aws_session))
        seen = []

        def federation_response(request):
            seen.append(request)
            return httpx.Response(200, json={"SigninToken": "offline-ticket"})

        async_client = httpx.AsyncClient
        monkeypatch.setattr(l5_integration.httpx, "AsyncClient", lambda: async_client(transport=httpx.MockTransport(federation_response)))
    else:
        connection_session = AsyncMock(return_value=Mock())
        monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "_devops_connection_session", connection_session)
        monkeypatch.setattr(federation, "mint_browser_launch", provider)
    path = f"/v1/products/{product_id}/projects/{project_id}/services/devops/bindings/{selection.binding_id}/browser-launch"
    headers = {"Authorization": "Bearer offline-placeholder"}
    if case == "first_party_origin":
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://console.example.test, https://secondary.example.test")
        headers["Origin"] = "https://secondary.example.test"
    response = await test_client.post(path, headers=headers, json={"configuration_revision": 1})
    if case not in {"valid", "first_party_origin"}:
        expected = 403 if case in {"stale_generation", "revoked_delegation", "expired_delegation"} else 404
        assert response.status_code == (503 if case == "db_expired_credential" else expected), response.text
        provider.assert_not_awaited()
        connection_session.assert_not_awaited()
        return
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    if real_connection:
        assert response.json()["binding_id"] == selection.binding_id
        assert "offline-ticket" in response.json()["launch_url"]
        assert "temporary-secret" not in response.json()["launch_url"]
        assert helper.current_version_id.call_count == 2
        assert hosting_assume.call_args.kwargs["role_arn"] == role
        assert hosting_assume.call_args.kwargs["external_id"] == credential.aws_external_id
        assert hosting_assume.call_args.kwargs["agent_id"] == "l5-browser-launch"
        assert launcher_assume.call_args.kwargs["RoleArn"] == launcher
        assert len(seen) == 1 and seen[0].url.host == "signin.aws.amazon.com"
    else:
        assert response.json() == provider.return_value
        connection_session.assert_awaited_once()
        provider.assert_awaited_once()
        assert provider.await_args.args == (selection,)
        assert {
            key: provider.await_args.kwargs[key] for key in ("native_space_id", "launcher_role_arn", "operator_role_arn", "user_id", "issuer")
        } == {
            "native_space_id": "native-space",
            "launcher_role_arn": launcher,
            "operator_role_arn": operator,
            "user_id": l5_owner.user_id,
            "issuer": "https://gateway.example.test",
        }


@pytest.mark.asyncio
async def test_well_architected_configuration_handoff_is_conditional(
    test_client,
    test_engine,
    db_session,
    l5_owner,
    monkeypatch,
):
    from src import l5_integration

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_wa02_host_00001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    document = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-connection",
        "record_type": "document",
        "record_id": "architecture-document",
        "record_revision": "revision-1",
    }
    repository = {**document, "record_type": "repository", "record_id": "architecture-repository", "record_revision": "a" * 40}
    sources = [{**repository, "record_type": "repository_commit"}, document]
    allowed = {("fixture-connection", source["record_id"]) for source in sources}
    monkeypatch.setattr(l5_integration, "permitted_source_refs", AsyncMock(return_value=allowed))
    from src.products import source_routes

    monkeypatch.setattr(source_routes, "permitted_source_refs", AsyncMock(return_value=allowed))
    monkeypatch.setattr(
        l5_integration,
        "authorized_connections",
        AsyncMock(return_value=[({"connection_ref": {"kind": "credential", "id": "fixture-connection"}}, None)]),
    )
    patched = await test_client.patch(
        f"{profile_path}/configuration",
        json={
            "configuration": {
                "basic": {"repositories": [repository], "documents": [document]},
                "selected_capabilities": [{"capability": "well_architected", "ownership_mode": "integrated"}],
            }
        },
        headers={"Idempotency-Key": "wa02_select_cap_00001", "If-Profile-Revision": "1"},
    )
    assert patched.status_code == 200, patched.text
    assert (await test_client.get(f"{profile_path}/services/well_architected/configuration/options")).json()["sources"] == sources

    actual_link = l5_integration.link_l5_configuration
    calls = []

    async def conflicting_link(*args):
        calls.append("attempt")
        raise HTTPException(412, detail={"code": "stale_revision"})

    monkeypatch.setattr(l5_integration, "link_l5_configuration", conflicting_link)
    path = f"{profile_path}/services/well_architected/configuration"
    saved = await test_client.put(
        path,
        json={
            "service_id": "well_architected",
            "capabilities": ["architecture_review"],
            "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
        },
        headers={"Idempotency-Key": "wa02_create_config_00001", "If-None-Match": "*"},
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["handoff_state"] == "conflict"
    assert saved.json()["settings"]["source_refs"] == sources
    from l5_services.run_input import WellArchitectedRunInput

    run_input = WellArchitectedRunInput.model_validate(
        {
            "service_id": "well_architected",
            "configuration_revision": 1,
            "binding_id": "selected-binding",
            "capability": "architecture_review",
            "sources": saved.json()["settings"]["source_refs"],
        }
    )
    assert run_input.model_dump()["sources"] == sources
    grant = SimpleNamespace(tenant_id=l5_owner.org_id, project_id=project_id, sources=sources, execution_scope={})
    source_binding = SimpleNamespace(
        tenant_id=l5_owner.org_id,
        project_id=project_id,
        kind="repository",
        installation_id="fixture-connection",
        credential_id=None,
        native_id=repository["record_id"],
        desired={},
        observed={"source_revision": "a" * 40},
    )
    assert source_routes.allowed_source_ref(grant, source_binding) == sources[0]
    assert (await test_client.get(path)).json()["hash"] == saved.json()["hash"]
    monkeypatch.setattr(l5_integration, "link_l5_configuration", actual_link)
    retry = await test_client.post(f"{path}/handoff", headers={"Idempotency-Key": "wa02_retry_link_00001", "If-Match": saved.headers["ETag"]})
    assert retry.status_code == 200, retry.text
    assert retry.json()["handoff_state"] == "linked"
    assert len(calls) == 1
    assert (await test_client.get(path)).status_code == 200
    current_profile = await test_client.get(profile_path)
    assert current_profile.status_code == 200, current_profile.text
    assert current_profile.json()["configuration"]["basic"]["repositories"] == [repository]
    new_ref = {**saved.json()["settings"]["profile_ref"], "record_revision": str(current_profile.json()["revision"])}
    stale = await test_client.post(
        f"{path}/refresh", json={"profile_ref": new_ref}, headers={"Idempotency-Key": "wa02_stale_edit_0001", "If-Match": '"old-hash"'}
    )
    assert stale.status_code == 412, stale.text
    assert stale.json()["detail"]["code"] == "stale_revision"
    stale_edit = await test_client.put(path, json={"enabled": True}, headers={"Idempotency-Key": "wa02_stale_put_0001", "If-Match": '"old-hash"'})
    assert stale_edit.status_code == 412, stale_edit.text
    assert (await test_client.get(path)).json()["hash"] == saved.json()["hash"]
    assert (await test_client.post(f"{path}/check")).json()["provider_contacted"] is False
    refused = await test_client.post(
        f"{profile_path}/services/well_architected/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "wa02_unqualified_00001"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "connection_not_selected"
    from l5_services.models.setup import SetupIntent

    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 0
    db_session.expire_all()
    project = await db_session.get(Project, (l5_owner.org_id, project_id))
    selection = project.configuration["selected_capabilities"][0]
    assert selection["capability"] == "well_architected"
    assert selection["configuration_ref"]["record_id"] == saved.json()["configuration_id"]


@pytest.fixture
async def retained_devops_access_policy(durable_devops_scope, db_session, monkeypatch):
    from dataclasses import asdict

    from l5_services.devops.resource_scope import ResourceSelection, policy_digest

    from src import l5_devops_access_policy

    scope = durable_devops_scope
    resource = ResourceSelection(
        scope.selection.account_id,
        scope.selection.region,
        "arn:aws:iam::123456789012:role/resources",
        scope.role,
        {**scope.reference, "record_type": "credential"},
        "lambda",
        resource_arn="arn:aws:lambda:us-east-1:123456789012:function:fixture",
        telemetry_arn="arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fixture:*",
    )
    snapshot = {
        "version": 1,
        "role_arn": resource.role_arn,
        "selected_resources": {"lambda:GetFunction": [resource.resource_arn], "logs:FilterLogEvents": [resource.telemetry_arn]},
    }
    receipt = await db_session.scalar(
        select(DevopsSpaceIntent).where(DevopsSpaceIntent.selection["binding_id"].as_string() == scope.selection.binding_id)
    )
    receipt.resource_observation = {
        "status": "verifying",
        "account_id": resource.account_id,
        "region": resource.region,
        "role_arn": resource.role_arn,
        "resource_selection": asdict(resource),
        "native_space_id": "native-space",
        "space_fingerprint": scope.selection.fingerprint(),
        "scope_fingerprint": resource.fingerprint(),
        "association_id": "association-fixture",
        "association_status": "valid",
        "effective_scope": {"trust": "selected_space", "policy_snapshot": snapshot, "policy_sha256": policy_digest(snapshot)},
        "checked_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    await db_session.commit()
    monkeypatch.setattr(l5_devops_access_policy.SecretsManagerHelper, "current_version_id", lambda *_: "version-one")
    return scope, receipt.operation_id


@pytest.mark.asyncio
async def test_devops_policy_read_and_select_content_addressed_observation(
    test_client,
    db_session,
    l5_owner,
    retained_devops_access_policy,
):
    scope, operation_id = retained_devops_access_policy
    base = f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops"
    policy = await test_client.get(base + f"/bindings/{scope.selection.binding_id}/access-policy")
    assert policy.status_code == 200, policy.text
    assert policy.headers["cache-control"] == "no-store"
    result = policy.json()
    assert result["native_readiness"] == "verifying"
    assert result["policy_ref"]["record_id"] == operation_id
    assert result["policy_ref"]["record_type"] == "policy"
    assert len(result["policy_ref"]["record_revision"]) == 64
    # Selecting the returned policy creates a later configuration revision. It must
    # not invalidate the underlying binding's earlier verified observation.
    configuration = await db_session.get(Configuration, (l5_owner.org_id, scope.config_id))
    configuration.current_revision = 2
    db_session.add(
        ConfigurationVersion(
            tenant_id=l5_owner.org_id,
            configuration_id=scope.config_id,
            revision=2,
            settings={**scope.settings, "devops": {**scope.settings["devops"], "access_policy_ref": result["policy_ref"]}},
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="policy-selected",
        )
    )
    await db_session.commit()
    read = await test_client.get(base + "/configuration")
    assert read.status_code == 200, read.text
    assert read.json()["settings"]["devops"]["access_policy_ref"] == result["policy_ref"]
    assert (await test_client.get(base + f"/bindings/{scope.selection.binding_id}/access-policy")).json() == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("expired", "access_policy_expired"),
        ("partial", "access_policy_unverified"),
        ("tampered", "access_policy_unverified"),
        ("revoked", "not_found"),
        ("selection", "access_policy_selection_changed"),
        ("rotated", "selected_connection_changed"),
    ],
)
async def test_devops_policy_rejects_invalid_current_authority(
    test_client,
    db_session,
    l5_owner,
    retained_devops_access_policy,
    monkeypatch,
    mutation,
    code,
):
    from src import l5_devops_access_policy

    scope, operation_id = retained_devops_access_policy
    receipt = await db_session.get(DevopsSpaceIntent, (l5_owner.org_id, operation_id))
    observation = {**receipt.resource_observation}
    if mutation == "expired":
        observation["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif mutation == "partial":
        observation["reason"] = "rechecking_current_access"
    elif mutation == "tampered":
        observation["effective_scope"] = {**observation["effective_scope"], "policy_sha256": "bad-hash"}
    elif mutation == "revoked":
        credential = await db_session.get(UserCredential, scope.credential.id)
        credential.user_id = "different-owner"
    elif mutation == "selection":
        configuration = await db_session.get(Configuration, (l5_owner.org_id, scope.config_id))
        configuration.current_revision = 2
        db_session.add(
            ConfigurationVersion(
                tenant_id=l5_owner.org_id,
                configuration_id=scope.config_id,
                revision=2,
                settings={**scope.settings, "devops": {**scope.settings["devops"], "accounts": [{"changed": True}]}},
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="changed-selection",
            )
        )
    else:
        monkeypatch.setattr(l5_devops_access_policy.SecretsManagerHelper, "current_version_id", lambda *_: "rotated-version")
    receipt.resource_observation = observation
    await db_session.commit()
    response = await test_client.get(
        f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops/bindings/{scope.selection.binding_id}/access-policy"
    )
    assert response.status_code in {404, 409}, response.text
    assert response.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_devops_policy_rechecks_product_after_external_connection_read(
    test_client,
    test_engine,
    l5_owner,
    retained_devops_access_policy,
    monkeypatch,
):
    from src import l5_devops_access_policy

    scope, _ = retained_devops_access_policy
    calls = 0

    async def version_read(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with async_sessionmaker(test_engine)() as db:
                await db.execute(update(Product).where(Product.id == scope.product_id).values(archived_at=datetime.now(UTC)))
                await db.commit()
        return "version-one"

    monkeypatch.setattr(l5_devops_access_policy, "run_in_threadpool", version_read)
    result = await test_client.get(
        f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops/bindings/{scope.selection.binding_id}/access-policy"
    )
    assert result.status_code == 404, result.text
    assert calls == 2


@pytest.mark.asyncio
async def test_devops_policy_changed_reference_remains_readable_but_cannot_authorize_new_work(
    db_session,
    l5_owner,
    retained_devops_access_policy,
):
    from src.l5_integration import GatewayConfigurationBackend

    scope, operation_id = retained_devops_access_policy
    backend = GatewayConfigurationBackend(db_session, l5_owner)
    response = await backend.devops_access_policy(scope.product_id, scope.project_id, scope.selection.binding_id)
    version = await db_session.get(ConfigurationVersion, (l5_owner.org_id, scope.config_id, 1))
    version.settings = {**version.settings, "devops": {**version.settings["devops"], "access_policy_ref": response["policy_ref"]}}
    receipt = await db_session.get(DevopsSpaceIntent, (l5_owner.org_id, operation_id))
    receipt.resource_observation = {**receipt.resource_observation, "association_id": "different-association"}
    await db_session.commit()
    retained, saved = await backend.current(scope.product_id, scope.project_id, "devops")
    assert retained.configuration_id == scope.config_id
    assert saved.settings["devops"]["access_policy_ref"] == response["policy_ref"]
    with pytest.raises(HTTPException) as denied:
        await backend.authorize_settings(saved.settings, scope.project_id, "devops")
    assert denied.value.detail["code"] == "access_policy_changed"
    repaired, _ = await backend.current(scope.product_id, scope.project_id, "devops", repair_access_policy=True)
    assert repaired.configuration_id == scope.config_id
    # The repair path still enforces ordinary Product/source authorization.
    await db_session.delete(await db_session.get(UserCredential, scope.credential.id))
    await db_session.commit()
    with pytest.raises(HTTPException) as read_revoked:
        await backend.current(scope.product_id, scope.project_id, "devops")
    assert read_revoked.value.status_code == 404
    with pytest.raises(HTTPException) as revoked:
        await backend.current(scope.product_id, scope.project_id, "devops", repair_access_policy=True)
    assert revoked.value.status_code == 404
