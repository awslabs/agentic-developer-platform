"""Profile, session, revision and replay must recheck source access at read time."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.products.models import ProductChange, ProductSourceBinding, Project
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i08_api import mutation, setup_source


async def saved_source(test_client, db_session, source_member, *, verified=True):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_downstream_12345"))
    assert created.status_code == 201, created.text
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    project = await db_session.get(Project, (source_member.org_id, project_id))
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    if verified:
        binding.status = "healthy"
        binding.observed = {
            "observed_at": observed_at.isoformat(),
            "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
            "source_revision": "source-revision-1",
            "native_id": selection["native_id"],
        }
    reference = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": selection["connection_ref"]["id"],
        "record_type": "repository",
        "record_id": selection["native_id"],
        "record_revision": "private-revision",
    }
    project.configuration = {"basic": {"repositories": [reference]}}
    project.provenance = [
        {
            "state": "accepted",
            "method": "connector",
            "paths": ["/configuration/basic/repositories"],
            "source_refs": [reference],
            "observed_at": observed_at.isoformat(),
            "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
        }
    ]
    change = await db_session.scalar(
        select(ProductChange).where(
            ProductChange.tenant_id == source_member.org_id,
            ProductChange.product_id == product_id,
            ProductChange.revision == 2,
        )
    )
    snapshot = deepcopy(change.snapshot)
    snapshot.update(configuration=project.configuration, provenance=project.provenance)
    if "scope_snapshots" in snapshot:
        snapshot["scope_snapshots"][project_id].update(configuration=project.configuration, provenance=project.provenance)
    change.snapshot = snapshot
    await db_session.commit()
    root = path.removesuffix("/bindings")
    return root, path, created, binding, observed_at


@pytest.mark.asyncio
async def test_expired_binding_degrades_profile_session_and_export(test_client, db_session, source_member, monkeypatch):
    root, path, created, binding, observed_at = await saved_source(test_client, db_session, source_member)
    clock = observed_at + timedelta(minutes=14)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    session_root = root + "/onboarding-sessions"
    started = await test_client.post(session_root, json={}, headers=mutation(2, "start_source_session_123"))
    assert started.status_code == 201, started.text
    session_path = session_root + "/" + started.json()["id"]
    current = (await test_client.get(root)).json()
    assert next(item for item in current["progress"]["checks"] if item["id"] == "repository")["status"] == "complete"
    assert started.json()["progress"] == current["progress"]
    revision = await test_client.get(root + "/revisions/2")
    assert revision.status_code == 200, revision.text
    assert revision.json()["progress"] == current["progress"]

    clock = observed_at + timedelta(minutes=15)
    for response in (
        await test_client.get(root),
        await test_client.get(session_path),
        await test_client.get(root + "/revisions/2"),
    ):
        assert response.status_code == 200, response.text
        check = next(item for item in response.json()["progress"]["checks"] if item["id"] == "repository")
        assert check["status"] == "stale" and response.json()["progress"]["maturity"] == "saved"
    listed = (await test_client.get(path)).json()["items"][0]
    assert listed["status"] == "degraded" and listed["observed_at"] == observed_at.isoformat()
    assert binding.status == "healthy" and binding.observed["observed_at"] == observed_at.isoformat()
    assert (await test_client.get(root)).headers["Profile-Revision"] == "2"


@pytest.mark.asyncio
async def test_revoked_binding_redacts_profile_session_export_and_receipt(test_client, db_session, source_member, monkeypatch):
    root, path, created, binding, observed_at = await saved_source(test_client, db_session, source_member)
    clock = observed_at + timedelta(minutes=1)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    session_root = root + "/onboarding-sessions"
    started = await test_client.post(session_root, json={}, headers=mutation(2, "start_redacted_session_123"))
    assert started.status_code == 201, started.text
    mapping = await db_session.get(ChannelTenantMap, binding.installation_id)
    mapping.ownership_disputed = True
    await db_session.commit()
    for response in (
        await test_client.get(root),
        await test_client.get(session_root + "/" + started.json()["id"]),
        await test_client.get(root + "/revisions/2"),
        await test_client.post(path, json=created.json()["selection"], headers=mutation(1, "create_downstream_12345")),
    ):
        assert response.status_code == 200, response.text
        assert "0035" not in response.text and "private-revision" not in response.text
        if "progress" in response.json():
            check = next(item for item in response.json()["progress"]["checks"] if item["id"] == "repository")
            assert check["status"] != "complete"
        if "configuration" in response.json():
            assert response.json()["configuration"]["basic"]["repositories"] == []
            assert response.json()["provenance"] == []
    assert (await test_client.get(path)).json()["items"][0]["status"] == "restricted"


@pytest.mark.asyncio
async def test_cached_profile_receipt_rechecks_current_source_access(test_client, db_session, source_member, monkeypatch):
    root, _, _, binding, observed_at = await saved_source(test_client, db_session, source_member)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at + timedelta(minutes=1))
    headers = mutation(2, "update_cached_source_123")
    edit = await test_client.patch(root + "/configuration", json={"name": "Updated project"}, headers=headers)
    assert edit.status_code == 200, edit.text
    assert "0035" in edit.text and edit.json()["revision"] == 3
    mapping = await db_session.get(ChannelTenantMap, binding.installation_id)
    mapping.ownership_disputed = True
    await db_session.commit()
    replay = await test_client.patch(root + "/configuration", json={"name": "Updated project"}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["revision"] == 3
    assert replay.json()["configuration"]["basic"]["repositories"] == []
    assert "0035" not in replay.text and "private-revision" not in replay.text
    assert next(check for check in replay.json()["progress"]["checks"] if check["id"] == "repository")["status"] != "complete"
