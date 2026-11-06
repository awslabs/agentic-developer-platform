"""Offline Product environment and DevOps handoff regressions."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.configuration import ProjectConfiguration, normalize_environments
from src.products.models import Product, ProductSourceBinding, Project
from tests.products.membership import add_team_member

ENVIRONMENT = {
    "tenant_id": "fixture-tenant",
    "project_id": "fixture-project",
    "source_connection_id": "fixture-connection",
    "record_type": "environment",
    "record_id": "fixture-environment",
    "record_revision": "source-1",
}


@pytest.mark.asyncio
async def test_environment_normalization(test_client, test_engine, db_session, app, source_member, monkeypatch, record_property):
    from tests.products.test_i01 import REQUEST
    from tests.products.test_i08_api import mutation
    from tests.products.test_i10 import setup_aws

    legacy = {"basic": {"environments": [ENVIRONMENT], "repositories": []}}
    normalized = normalize_environments(legacy)
    assert normalized["advanced"]["environments"] == [ENVIRONMENT]
    assert normalized["basic"] == {"repositories": []}
    assert legacy["basic"]["environments"] == [ENVIRONMENT]
    assert normalize_environments(normalized) == normalized
    assert ProjectConfiguration.model_validate(legacy).model_dump(exclude_none=True) == normalized

    matching = {"basic": {"environments": [ENVIRONMENT]}, "advanced": {"environments": [ENVIRONMENT]}}
    assert normalize_environments(matching) == {"advanced": {"environments": [ENVIRONMENT]}}

    newer = {**ENVIRONMENT, "record_revision": "source-2"}
    conflicting = {"basic": {"environments": [ENVIRONMENT]}, "advanced": {"environments": [newer]}}
    expected = {
        **conflicting,
        "advanced": {
            "environments": [newer],
            "environment_conflicts": [{"legacy_ref": ENVIRONMENT, "advanced_ref": newer}],
        },
    }
    assert normalize_environments(conflicting) == expected
    assert normalize_environments(expected) == expected
    assert ProjectConfiguration.model_validate(conflicting).model_dump(exclude_none=True) == expected
    assert normalize_environments({"advanced": {"environments": [newer]}}) == {"advanced": {"environments": [newer]}}

    fixture_clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixture_clock)
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: fixture_clock)
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=fixture_clock)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("normalization must not contact a provider"))
    root = path.removesuffix("/bindings")
    bound = await test_client.post(path, json=selection, headers=mutation(1, "scn01_source_binding_12345"))
    assert bound.status_code == 201, bound.text
    project_id, product_id = root.split("/")[5], root.split("/")[3]
    source_ref = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "environment",
        "record_id": selection["native_id"],
        "record_revision": secret.version,
    }
    sibling = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "scn01_other_product_12345"})
    assert sibling.status_code == 201, sibling.text
    sibling_id = sibling.json()["project_id"]
    sibling_ref = {
        **source_ref,
        "project_id": sibling_id,
        "source_connection_id": "fixture-sibling-source",
        "record_id": "sibling-environment",
        "record_revision": "sibling-1",
    }
    legacy_ref = {**source_ref, "record_revision": "legacy-1"}
    project = await db_session.get(Project, (source_member.org_id, project_id))
    assert project.configuration == {"advanced": {"environments": [source_ref]}}
    provenance = [
        {
            "state": "accepted",
            "method": "connector",
            "paths": ["/configuration/basic/environments"],
            "source_refs": [legacy_ref],
            "observed_at": fixture_clock.isoformat(),
            "stale_after": (fixture_clock + timedelta(minutes=15)).isoformat(),
        }
    ]
    project.provenance = provenance
    project.configuration = {
        "basic": {"environments": [legacy_ref]},
        "advanced": {"environments": [source_ref]},
    }
    other_project = await db_session.get(Project, (source_member.org_id, sibling_id))
    other_project.configuration = {"basic": {"environments": [sibling_ref]}}
    await db_session.commit()

    path_to_migration = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "141_normalize_profile_environments.py"
    spec = importlib.util.spec_from_file_location("normalize_profile_environments_scenario", path_to_migration)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection):
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        migration.upgrade()

    async with test_engine.begin() as connection:
        await connection.run_sync(migrate)

    conflict = {
        "basic": {"environments": [legacy_ref]},
        "advanced": {
            "environments": [source_ref],
            "environment_conflicts": [{"legacy_ref": legacy_ref, "advanced_ref": source_ref}],
        },
    }
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        persisted = await independent.get(Project, (source_member.org_id, project_id))
        other = await independent.get(Project, (source_member.org_id, sibling_id))
        binding = await independent.scalar(
            sa.select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == source_member.org_id,
                ProductSourceBinding.project_id == project_id,
            )
        )
        assert persisted.configuration == conflict and persisted.provenance == provenance
        assert other.configuration == {"advanced": {"environments": [sibling_ref]}}
        assert binding.observed["source_revision"] == secret.version
        assert (await independent.get(Product, (source_member.org_id, product_id))).revision == 2
        assert (await independent.get(Product, (source_member.org_id, sibling.json()["product_id"]))).revision == 1

    profile = (await test_client.get(root)).json()
    assert profile["configuration"] == conflict
    assert profile["provenance"] == provenance
    sibling_root = f"/v1/products/{sibling.json()['product_id']}/projects/{sibling_id}"
    assert (await test_client.get(sibling_root)).json()["configuration"] == {"advanced": {"environments": [sibling_ref]}}
    headers = mutation(2, "scn01_resolve_conflict_12345")
    patch = {"configuration": {"advanced": {"environments": [source_ref]}}}
    saved = await test_client.patch(root + "/configuration", json=patch, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configuration"] == {"advanced": {"environments": [source_ref]}}
    replay = await test_client.patch(root + "/configuration", json=patch, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    readback = (await test_client.get(root)).json()
    assert readback["configuration"] == saved.json()["configuration"]
    assert readback["provenance"] == provenance
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        persisted = await independent.get(Project, (source_member.org_id, project_id))
        assert persisted.configuration == {"advanced": {"environments": [source_ref]}}
        assert persisted.provenance == provenance
        assert (await independent.get(Product, (source_member.org_id, product_id))).revision == 3
    record_property("scenario_id", "SCN-01")
    record_property("fixture_clock", fixture_clock.isoformat())
    record_property("source_revision", secret.version)
    record_property("profile_revision", "3")


def test_environment_backfill_preserves_revision_and_unrelated_projects(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "141_normalize_profile_environments.py"
    spec = importlib.util.spec_from_file_location("normalize_profile_environments", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    projects = sa.Table(
        "projects",
        metadata,
        sa.Column("tenant_id", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("configuration", sa.JSON, nullable=False),
        sa.Column("provenance", sa.JSON, nullable=False),
    )
    metadata.create_all(engine)
    legacy = {"basic": {"environments": [ENVIRONMENT]}}
    other = {"advanced": {"environments": [{**ENVIRONMENT, "project_id": "other"}]}}
    with engine.begin() as connection:
        connection.execute(
            projects.insert(),
            [
                {"tenant_id": "fixture-tenant", "id": "fixture-project", "configuration": legacy, "provenance": ["source-1"]},
                {"tenant_id": "other-tenant", "id": "other", "configuration": other, "provenance": ["other-source"]},
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        migration.upgrade()
        rows = {row.id: row for row in connection.execute(sa.select(projects)).mappings()}
    assert rows["fixture-project"].configuration == {"advanced": {"environments": [ENVIRONMENT]}}
    assert rows["fixture-project"].provenance == ["source-1"]
    assert rows["other"].configuration == other
    assert rows["other"].provenance == ["other-source"]


@pytest.mark.asyncio
async def test_source_and_profile_environment_readback(test_client, db_session, app, source_member, monkeypatch):
    from tests.products.test_i08_api import mutation
    from tests.products.test_i10 import setup_aws

    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    root = path.removesuffix("/bindings")
    created = await test_client.post(path, json=selection, headers=mutation(1, "normalized_source_save_12345"))
    assert created.status_code == 201, created.text
    reference = {
        "tenant_id": source_member.org_id,
        "project_id": root.split("/")[5],
        "source_connection_id": credential.id,
        "record_type": "environment",
        "record_id": selection["native_id"],
        "record_revision": secret.version,
    }
    project = await db_session.get(Project, (source_member.org_id, reference["project_id"]))
    await db_session.refresh(project)
    assert project.configuration == {"advanced": {"environments": [reference]}}
    initial = await test_client.get(root)
    assert initial.status_code == 200 and initial.json()["configuration"] == project.configuration

    extra = {**reference, "record_id": "fixture-extra-environment"}
    headers = mutation(2, "normalized_profile_edit_12345")
    patch = {"configuration": {"advanced": {"environments": [reference, extra]}}}
    saved = await test_client.patch(root + "/configuration", json=patch, headers=headers)
    assert saved.status_code == 200, saved.text
    replay = await test_client.patch(root + "/configuration", json=patch, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    await db_session.refresh(project)
    assert project.configuration["advanced"]["environments"] == [reference, extra]
    assert (await test_client.get(root)).json()["configuration"]["advanced"]["environments"] == [reference, extra]


@pytest.mark.asyncio
async def test_environment_conflict_readback_and_human_resolution(test_client, db_session, source_member):
    from tests.products.test_i01 import HEADERS, REQUEST

    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    project = await db_session.get(Project, (source_member.org_id, project_id))
    legacy = {**ENVIRONMENT, "tenant_id": source_member.org_id, "project_id": project_id}
    newer = {**legacy, "record_revision": "source-2"}
    project.configuration = {"basic": {"environments": [legacy]}, "advanced": {"environments": [newer]}}
    await db_session.commit()

    readback = await test_client.get(root)
    assert readback.status_code == 200, readback.text
    conflict = readback.json()["configuration"]
    assert conflict["basic"]["environments"] == [legacy]
    assert conflict["advanced"]["environments"] == [newer]
    assert conflict["advanced"]["environment_conflicts"] == [{"legacy_ref": legacy, "advanced_ref": newer}]
    assert readback.headers["Profile-Revision"] == "1"

    edited = {"configuration": {"advanced": {**conflict["advanced"], "environments": [legacy]}}}
    headers = {"Idempotency-Key": "resolve_environment_12345", "If-Profile-Revision": "1"}
    saved = await test_client.patch(root + "/configuration", json=edited, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configuration"] == {"advanced": {"environments": [legacy]}}
    replay = await test_client.patch(root + "/configuration", json=edited, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    await db_session.refresh(project)
    assert project.configuration == {"advanced": {"environments": [legacy]}}
    assert (await test_client.get(root)).json()["configuration"] == project.configuration


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["environment", "devops"])
@pytest.mark.parametrize("foreign_scope", ["tenant", "project", "product"])
async def test_foreign_profile_selection_is_denied_without_disclosure(test_client, db_session, app, source_member, location, foreign_scope):
    from src.products.models import Product
    from tests.products.test_i01 import HEADERS, REQUEST

    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    sibling = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "foreign_selection_sibling_12345"})
    assert sibling.status_code == 201, sibling.text
    reference = {
        "tenant_id": "foreign-tenant" if foreign_scope == "tenant" else source_member.org_id,
        "project_id": project_id if foreign_scope == "product" else sibling.json()["project_id"],
        "source_connection_id": "adp.profile" if foreign_scope == "product" else "fixture-connection",
        "record_type": "product" if foreign_scope == "product" else ("environment" if location == "environment" else "l5_configuration"),
        "record_id": sibling.json()["product_id"] if foreign_scope == "product" else "private-foreign-selection",
        "record_revision": "source-1",
    }
    selection = (
        {"advanced": {"environments": [reference]}}
        if location == "environment"
        else {"selected_capabilities": [{"capability": "devops", "ownership_mode": "managed", "configuration_ref": reference}]}
    )
    headers = {"Idempotency-Key": f"foreign_{location}_{foreign_scope}_12345", "If-Profile-Revision": "1"}
    private_id = reference["record_id"]
    rejected = await test_client.patch(root + "/configuration", json={"configuration": selection}, headers=headers)
    assert rejected.status_code == 404 and rejected.json() == {"detail": {"code": "not_found"}}
    assert private_id not in rejected.text
    assert (await test_client.get(root)).json()["configuration"] == {}
    assert (await db_session.get(Product, (source_member.org_id, product_id))).revision == 1

    project = await db_session.get(Project, (source_member.org_id, project_id))
    project.configuration = selection
    await db_session.commit()
    readback = await test_client.get(root)
    assert readback.status_code == 200
    assert private_id not in readback.text
    if location == "devops":
        assert readback.json()["configuration"] == {
            "selected_capabilities": [{"capability": "devops", "ownership_mode": "managed", "configuration_ref": None}]
        }
    else:
        assert readback.json()["configuration"] == {"advanced": {"environments": []}}
    if foreign_scope == "tenant":
        from src.auth.dependencies import get_current_user

        foreign_actor = source_member.model_copy(update={"org_id": "foreign-tenant", "user_id": "foreign-user"})
        app.dependency_overrides[get_current_user] = lambda: foreign_actor
        denied_read = await test_client.get(root)
        denied_edit = await test_client.patch(root + "/configuration", json={"configuration": selection}, headers=headers)
        for denied in (denied_read, denied_edit):
            assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
            assert private_id not in denied.text


@pytest.mark.asyncio
async def test_devops_link_retains_scoped_reference_on_replay(test_client, db_session, source_member):
    from fastapi import HTTPException

    from src.product_onboarding.handoffs import link_l5_configuration
    from src.products.models import ProductOperation, ProductProjectBinding
    from tests.products.test_i01 import HEADERS, REQUEST

    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.patch(
        root + "/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "managed"}]}},
        headers={"Idempotency-Key": "select_devops_link_12345", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    binding = await db_session.scalar(
        sa.select(ProductProjectBinding).where(ProductProjectBinding.product_id == product_id, ProductProjectBinding.project_id == project_id)
    )
    reference = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": binding.source_connection_id,
        "record_type": "l5_configuration",
        "record_id": "fixture-devops-configuration",
        "record_revision": "1",
    }
    key = "link_devops_config_12345"
    receipt = await link_l5_configuration(db_session, source_member, product_id, project_id, reference, ["devops"], 2, key)
    assert receipt == {"revision": 3}
    assert await link_l5_configuration(db_session, source_member, product_id, project_id, reference, ["devops"], 2, key) == receipt
    project = await db_session.get(Project, (source_member.org_id, project_id))
    assert project.configuration["selected_capabilities"] == [{"capability": "devops", "ownership_mode": "managed", "configuration_ref": reference}]
    assert (await test_client.get(root)).json()["configuration"] == project.configuration
    assert (
        await db_session.scalar(sa.select(sa.func.count()).select_from(ProductOperation).where(ProductOperation.operation == "linkL5Configuration"))
    ) == 1

    with pytest.raises(HTTPException) as invalid:
        await link_l5_configuration(
            db_session,
            source_member,
            product_id,
            project_id,
            {**reference, "record_revision": "latest"},
            ["devops"],
            3,
            "invalid_devops_revision_12345",
        )
    assert invalid.value.status_code == 422 and invalid.value.detail == {"code": "invalid_configuration_ref"}
    with pytest.raises(HTTPException) as conflict:
        await link_l5_configuration(
            db_session,
            source_member,
            product_id,
            project_id,
            {**reference, "record_id": "foreign-configuration"},
            ["devops"],
            3,
            "relink_devops_other_12345",
        )
    assert conflict.value.status_code == 409 and conflict.value.detail == {"code": "configuration_ref_conflict"}
    await db_session.refresh(project)
    assert project.configuration["selected_capabilities"][0]["configuration_ref"] == reference


@pytest.fixture
def devops_draft():
    return {
        "service_id": "devops",
        "capabilities": ["investigation"],
        "enabled": False,
        "triggers": [],
        "devops": {"space_mode": "create", "accounts": [], "integrations": []},
    }


@pytest.mark.asyncio
async def test_handoff_replay(test_client, test_engine, db_session, app, source_member, monkeypatch, record_property, devops_draft):
    pytest.importorskip("l5_services")
    from l5_services.models.base import Base as L5Base
    from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion

    from src.products.models import ProductOperation, ProductProjectBinding
    from tests.products.test_i08_api import mutation
    from tests.products.test_i10 import setup_aws

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    fixture_clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixture_clock)
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: fixture_clock)
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=fixture_clock)
    bound = await test_client.post(path, json=selection, headers=mutation(1, "devops_environment_12345"))
    assert bound.status_code == 201, bound.text
    root = path.removesuffix("/bindings")
    chosen = await test_client.patch(
        root + "/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "managed"}]}},
        headers=mutation(2, "devops_selected_12345"),
    )
    assert chosen.status_code == 200, chosen.text
    configuration_path = root + "/services/devops/configuration"
    options = await test_client.get(configuration_path + "/options")
    assert options.status_code == 200, options.text
    environment_ref = {
        "tenant_id": source_member.org_id,
        "project_id": root.split("/")[5],
        "source_connection_id": credential.id,
        "record_type": "environment",
        "record_id": selection["native_id"],
        "record_revision": secret.version,
    }
    assert options.json()["capabilities"] == ["investigation", "release_readiness", "release_verification", "prevention"]
    assert options.json()["sources"] == [environment_ref]

    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("configuration save must not contact a provider"))
    rejected = await test_client.put(
        configuration_path,
        json={**devops_draft, "enabled": True},
        headers={"Idempotency-Key": "devops_enabled_12345", "If-None-Match": "*"},
    )
    assert rejected.status_code == 422, rejected.text
    assert (await db_session.scalar(sa.select(sa.func.count()).select_from(Configuration))) == 0

    headers = {"Idempotency-Key": "create_devops_config_12345", "If-None-Match": "*"}
    saved = await test_client.put(configuration_path, json=devops_draft, headers=headers)
    assert saved.status_code == 201, saved.text
    assert saved.json()["state"] == "disabled" and saved.json()["handoff_state"] == "linked"
    assert saved.json()["gaps"] == [
        "investigation.needs_space",
        "investigation.needs_account",
        "investigation.needs_resources",
        "investigation.needs_telemetry",
    ]
    assert saved.json()["settings"]["source_refs"] == [environment_ref]
    assert (await test_client.put(configuration_path, json=devops_draft, headers=headers)).json() == saved.json()
    replay = await test_client.post(
        configuration_path + "/handoff",
        headers={"Idempotency-Key": "scn02_replay_handoff_12345", "If-Match": saved.headers["etag"]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == saved.json()
    assert (await test_client.put(configuration_path, json=devops_draft, headers=headers)).json() == saved.json()
    assert (await test_client.get(configuration_path)).json() == saved.json()
    profile = (await test_client.get(root)).json()
    linked = profile["configuration"]["selected_capabilities"]
    assert len(linked) == 1 and linked[0]["ownership_mode"] == "managed"
    assert linked[0]["configuration_ref"]["record_id"] == saved.json()["configuration_id"]
    assert linked[0]["configuration_ref"]["record_revision"] == "1"
    product_id, project_id = root.split("/")[3], root.split("/")[5]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        product = await independent.get(Product, (source_member.org_id, product_id))
        project = await independent.get(Project, (source_member.org_id, project_id))
        configuration = await independent.get(Configuration, (source_member.org_id, saved.json()["configuration_id"]))
        binding = await independent.scalar(
            sa.select(ProductProjectBinding).where(
                ProductProjectBinding.tenant_id == source_member.org_id,
                ProductProjectBinding.product_id == product_id,
                ProductProjectBinding.project_id == project_id,
                ProductProjectBinding.valid_to.is_(None),
            )
        )
        reference = {
            "tenant_id": source_member.org_id,
            "project_id": project_id,
            "source_connection_id": binding.source_connection_id,
            "record_type": "l5_configuration",
            "record_id": saved.json()["configuration_id"],
            "record_revision": "1",
        }
        assert (configuration.tenant_id, configuration.product_id, configuration.project_id, configuration.service_id) == (
            source_member.org_id,
            product_id,
            project_id,
            "devops",
        )
        assert (configuration.relationship_revision, configuration.current_revision, configuration.handoff_state) == (
            str(binding.relationship_revision),
            1,
            "linked",
        )
        assert project.configuration["selected_capabilities"] == [
            {"capability": "devops", "ownership_mode": "managed", "configuration_ref": reference}
        ]
        assert linked[0]["configuration_ref"] == reference
        assert product.revision == 4 and profile["revision"] == 4
        assert (await independent.scalar(sa.select(sa.func.count()).select_from(Configuration))) == 1
        assert (await independent.scalar(sa.select(sa.func.count()).select_from(ConfigurationVersion))) == 1
        assert (await independent.scalar(sa.select(sa.func.count()).select_from(ConfigurationReceipt))) == 1
        assert (
            await independent.scalar(
                sa.select(sa.func.count()).select_from(ProductOperation).where(ProductOperation.operation == "linkL5Configuration")
            )
        ) == 1
    record_property("scenario_id", "SCN-02")
    record_property("fixture_clock", fixture_clock.isoformat())
    record_property("source_revision", secret.version)
    record_property("profile_revision", "4")


@pytest.mark.asyncio
async def test_handoff_recovery(test_client, test_engine, db_session, source_member, monkeypatch, record_property, devops_draft):
    pytest.importorskip("l5_services")
    from fastapi import HTTPException
    from httpx import ASGITransport, AsyncClient
    from l5_services.models.base import Base as L5Base
    from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src import l5_integration
    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.products.models import Product, ProductOperation
    from src.shared.database import get_db
    from src.shared.models.organization import Organization, User
    from tests.products.test_i01 import HEADERS, REQUEST

    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    fixture_clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixture_clock)
    monkeypatch.setattr("src.product_onboarding.handoffs.utcnow", lambda: fixture_clock)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    selected = await test_client.patch(
        root + "/configuration",
        json={"configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "integrated"}]}},
        headers={"Idempotency-Key": "select_devops_retry_12345", "If-Profile-Revision": "1"},
    )
    assert selected.status_code == 200, selected.text
    path = root + "/services/devops/configuration"
    create_headers = {"Idempotency-Key": "create_devops_retry_12345", "If-None-Match": "*"}

    async def failed_link(*args, **kwargs):
        raise HTTPException(503, detail={"code": "fixture_handoff_unavailable"})

    with monkeypatch.context() as outage:
        outage.setattr(l5_integration, "link_l5_configuration", failed_link)
        failed = await test_client.put(path, json=devops_draft, headers=create_headers)
        assert failed.status_code == 201, failed.text
        for attempt in range(2):
            unavailable = await test_client.post(
                path + "/handoff",
                headers={"Idempotency-Key": f"retry_unavailable_{attempt}_12345", "If-Match": failed.headers["etag"]},
            )
            assert unavailable.status_code == 200, unavailable.text
            assert unavailable.json() == failed.json()
    assert failed.json()["handoff_state"] == "unavailable"
    db_session.expire_all()
    saved = await db_session.get(Project, (source_member.org_id, project_id))
    assert saved.configuration["selected_capabilities"] == [{"capability": "devops", "ownership_mode": "integrated"}]
    assert (await db_session.get(Product, (source_member.org_id, product_id))).revision == 2
    assert (await db_session.scalar(sa.select(sa.func.count()).select_from(Configuration))) == 1

    foreign_actor = source_member.model_copy(
        update={"org_id": "fixture-other-tenant", "user_id": "fixture-other-user", "team_id": "fixture-other-team"}
    )
    db_session.add(Organization(id=foreign_actor.org_id, name="Independent fixture workspace"))
    db_session.add(
        User(
            id=foreign_actor.user_id,
            org_id=foreign_actor.org_id,
            team_id=foreign_actor.team_id,
            email="independent-fixture@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, foreign_actor)
    await db_session.commit()

    restarted = create_app()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def new_session():
        async with factory() as session:
            yield session

    restarted.dependency_overrides[get_db] = new_session
    restarted.dependency_overrides[get_current_user] = lambda: source_member
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("handoff retry must not contact a provider"))
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as fresh:
        recovered = await fresh.put(path, json=devops_draft, headers=create_headers)
        assert recovered.status_code == 201, recovered.text
        assert recovered.json()["handoff_state"] == "linked"
        assert recovered.json()["configuration_id"] == failed.json()["configuration_id"]
        assert (await fresh.put(path, json=devops_draft, headers=create_headers)).json() == recovered.json()
        for attempt in range(3):
            retry = await fresh.post(
                path + "/handoff",
                headers={"Idempotency-Key": f"retry_linked_{attempt}_12345", "If-Match": recovered.headers["etag"]},
            )
            assert retry.status_code == 200, retry.text
            assert retry.json() == recovered.json()
        assert (await fresh.get(path)).json() == recovered.json()
        readback = (await fresh.get(root)).json()
        assert readback["configuration"]["selected_capabilities"][0]["configuration_ref"]["record_id"] == recovered.json()["configuration_id"]
        assert readback["revision"] == 3
        restarted.dependency_overrides[get_current_user] = lambda: foreign_actor
        foreign_product = await fresh.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "scn03_foreign_product_12345"})
        assert foreign_product.status_code == 201, foreign_product.text
        foreign_root = f"/v1/products/{foreign_product.json()['product_id']}/projects/{foreign_product.json()['project_id']}"
        reference = readback["configuration"]["selected_capabilities"][0]["configuration_ref"]
        foreign_selection = await fresh.patch(
            foreign_root + "/configuration",
            json={
                "configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "managed", "configuration_ref": reference}]}
            },
            headers={"Idempotency-Key": "scn03_foreign_selection_12345", "If-Profile-Revision": "1"},
        )
        foreign_read = await fresh.get(root)
        foreign_retry = await fresh.put(path, json=devops_draft, headers=create_headers)
        for denied in (foreign_selection, foreign_read, foreign_retry):
            assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
            assert recovered.json()["configuration_id"] not in denied.text
        assert (await fresh.get(foreign_root)).json()["configuration"] == {}
        restarted.dependency_overrides[get_current_user] = lambda: source_member
        assert (await fresh.get(root)).json() == readback
    db_session.expire_all()
    assert (await db_session.scalar(sa.select(sa.func.count()).select_from(Configuration))) == 1
    assert (await db_session.scalar(sa.select(sa.func.count()).select_from(ConfigurationVersion))) == 1
    assert (await db_session.scalar(sa.select(sa.func.count()).select_from(ConfigurationReceipt))) == 1
    assert (
        await db_session.scalar(sa.select(sa.func.count()).select_from(ProductOperation).where(ProductOperation.operation == "linkL5Configuration"))
    ) == 1
    assert (await db_session.get(Product, (source_member.org_id, product_id))).revision == 3
    assert (await db_session.get(Product, (foreign_actor.org_id, foreign_product.json()["product_id"]))).revision == 1
    record_property("scenario_id", "SCN-03")
    record_property("fixture_clock", fixture_clock.isoformat())
    record_property("profile_revision", "3")
