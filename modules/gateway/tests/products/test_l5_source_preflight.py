"""Mounted preflight enforces current Product/source access without persistence."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from l5_services.models.base import Base as L5Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy import func, select, update

from src import l5_source_preflight
from src.products.models import ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture_metadata import REPOSITORY_MAX_BYTES as MAX_CONTENT
from src.products.source_capture_metadata import REPOSITORY_MAX_FILES as MAX_FILES
from src.products.source_capture_metadata import repository_capture_preflight
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products.test_i05 import REQUEST
from tests.products.test_l5_configuration_host import l5_owner  # noqa: F401


@pytest.fixture
async def selected(test_client, test_engine, db_session, l5_owner, monkeypatch):  # noqa: F811
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "preflight-product-001"})
    assert created.status_code == 201, created.text
    product, project = created.json()["product_id"], created.json()["project_id"]
    relationship = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == product))
    mapping = ChannelTenantMap(
        id=str(uuid4()), org_id=l5_owner.org_id, provider="github", provider_scope_id="789", installation_id="456", ownership_disputed=False
    )
    db_session.add(mapping)
    await db_session.flush()
    source = {
        "tenant_id": l5_owner.org_id,
        "project_id": project,
        "source_connection_id": mapping.id,
        "record_type": "repository",
        "record_id": "123",
        "record_revision": "a" * 40,
    }
    binding = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=project,
        kind="repository",
        provider="github",
        installation_id=mapping.id,
        namespace="789",
        native_id="123",
        status="healthy",
        desired={"source_revision": "a" * 40},
        observed={"source_revision": "a" * 40},
    )
    identifier = str(uuid4())
    configuration = Configuration(
        tenant_id=l5_owner.org_id,
        configuration_id=identifier,
        product_id=product,
        project_id=project,
        service_id="security",
        relationship_revision=str(relationship.relationship_revision),
        owner_id=l5_owner.user_id,
        current_revision=1,
        handoff_state="linked",
    )
    settings = {"visibility": "only_me", "source_refs": [source], "exclusions": [], "enabled": False, "capabilities": ["code_review"]}
    version = ConfigurationVersion(
        tenant_id=l5_owner.org_id,
        configuration_id=identifier,
        revision=1,
        settings=settings,
        inherited_fields=[],
        gaps=["limits"],
        state="disabled",
        hash="b" * 64,
    )
    db_session.add_all([binding, configuration, version])
    await db_session.commit()
    monkeypatch.setattr("src.products.source_routes.assert_installation_owned_by", AsyncMock())
    metadata = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [{"path": "private-large.bin", "size": 7 * 1024 * 1024, "type": "blob", "mode": "100644", "sha": "d" * 40}],
    }
    read = AsyncMock(return_value=repository_capture_preflight(metadata, "c" * 40, [], max_bytes=MAX_CONTENT, max_files=MAX_FILES))
    monkeypatch.setattr(l5_source_preflight, "read_repository_preflight", read)
    return SimpleNamespace(
        path=f"/v1/products/{product}/projects/{project}/services/security/configuration/source-preflight",
        configuration_id=identifier,
        source=source,
        settings=settings,
        binding_id=binding.id,
        mapping_id=mapping.id,
        tenant=l5_owner.org_id,
        read=read,
    )


@pytest.mark.asyncio
async def test_mounted_source_preflight_is_saved_scoped_and_read_only(test_client, db_session, selected):
    response = await test_client.get(selected.path, params={"configuration_revision": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert result["configuration_id"] == selected.configuration_id and result["configuration_revision"] == 1
    assert result["status"] == "within_limits" and result["advisory_only"] and not result["content_captured"]
    assert result["sources"][0]["source_ref"] == selected.source
    assert result["sources"][0]["oversized_files"] == []
    assert result["limits"] == {"max_bytes": MAX_CONTENT, "max_files": MAX_FILES}
    version = await db_session.get(ConfigurationVersion, (selected.tenant, selected.configuration_id, 1), populate_existing=True)
    assert version.settings == selected.settings and version.state == "disabled" and version.gaps == ["limits"]
    assert await db_session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProductSourceSnapshot)) == 0
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 0
    selected.read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale_revision", "foreign_product", "foreign_project", "other_service", "changed_pin"])
async def test_preflight_denies_invalid_scope_before_metadata(test_client, db_session, selected, change):
    path, revision = selected.path, 1
    if change == "stale_revision":
        revision = 2
    elif change == "foreign_product":
        path = path.replace(path.split("/")[3], str(uuid4()))
    elif change == "foreign_project":
        path = path.replace(path.split("/")[5], str(uuid4()))
    elif change == "other_service":
        path = path.replace("/security/", "/devops/")
    else:
        await db_session.execute(
            update(ProductSourceBinding).where(ProductSourceBinding.id == selected.binding_id).values(observed={"source_revision": "f" * 40})
        )
        await db_session.commit()
    response = await test_client.get(path, params={"configuration_revision": revision})
    assert response.status_code == (412 if change == "stale_revision" else 404), response.text
    assert "private-large.bin" not in response.text
    selected.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revocation", "configuration_changed"])
async def test_preflight_rechecks_authority_before_releasing_paths(test_client, db_session, selected, change):
    answer = selected.read.return_value

    async def read(*_):
        if change == "revocation":
            await db_session.execute(update(ChannelTenantMap).where(ChannelTenantMap.id == selected.mapping_id).values(ownership_disputed=True))
        else:
            await db_session.execute(
                update(ConfigurationVersion).where(ConfigurationVersion.configuration_id == selected.configuration_id).values(hash="e" * 64)
            )
        await db_session.commit()
        return answer

    selected.read.side_effect = read
    response = await test_client.get(selected.path, params={"configuration_revision": 1})
    assert response.status_code == (404 if change == "revocation" else 409), response.text
    assert "private-large.bin" not in response.text


@pytest.mark.asyncio
async def test_preflight_unknown_does_not_report_partial_counts(test_client, selected):
    selected.read.side_effect = ValueError("source_metadata_incomplete")
    response = await test_client.get(selected.path, params={"configuration_revision": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "unknown"
    assert result["sources"][0]["reasons"] == ["source_metadata_incomplete"]
    assert "selected_files" not in result["sources"][0] and "oversized_files" not in result["sources"][0]
    assert "private-large.bin" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["files", "content", "archive_headers_only"])
async def test_combined_repositories_enforce_actual_cumulative_bounds(test_client, db_session, selected, case):
    second = {**selected.source, "record_id": "124"}
    db_session.add(
        ProductSourceBinding(
            tenant_id=selected.tenant,
            project_id=second["project_id"],
            kind="repository",
            provider="github",
            installation_id=selected.mapping_id,
            namespace="789",
            native_id="124",
            status="healthy",
            desired={"source_revision": "a" * 40},
            observed={"source_revision": "a" * 40},
        )
    )
    await db_session.execute(
        update(ConfigurationVersion)
        .where(
            ConfigurationVersion.configuration_id == selected.configuration_id,
        )
        .values(settings={**selected.settings, "source_refs": [selected.source, second]})
    )
    await db_session.commit()
    count = 60000 if case == "files" else 1
    size = 1 if case == "files" else 600 * 1024 * 1024 if case == "content" else MAX_CONTENT // 2 - 50
    metadata = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [{"path": f"file-{index}", "size": size, "type": "blob", "mode": "100644", "sha": "d" * 40} for index in range(count)],
    }
    each = repository_capture_preflight(metadata, "c" * 40, [], max_bytes=MAX_CONTENT, max_files=MAX_FILES)
    assert each["status"] == "within_limits"
    selected.read.return_value = each
    response = await test_client.get(selected.path, params={"configuration_revision": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    combined = result["combined_repositories"]
    assert combined["known_selected_files"] == count * 2
    assert combined["known_selected_bytes"] == size * count * 2
    assert combined["native_combined_archive_bytes"] is None
    if case == "archive_headers_only":
        assert combined["snapshot_archive_bytes"] > MAX_CONTENT
        assert combined["known_selected_bytes"] < MAX_CONTENT
        assert result["status"] == "within_limits" and combined["reasons"] == []
    else:
        assert result["status"] == "exceeds_limits"
        assert combined["reasons"] == ["combined_source_file_count_exceeded" if case == "files" else "combined_source_content_bytes_exceeded"]
