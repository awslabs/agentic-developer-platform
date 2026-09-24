"""Management stays useful at zero targets without activating legacy mutation."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.main import app
from app.models.cluster import Cluster
from app.models.observation import ObservationLease
from app.models.workspace import Workspace
from tests.conftest import async_session_test
from tests.test_organization_grants import seed


def grant(monkeypatch, org):
    token = "controller-registry-test-credential-" + "x" * 32
    monkeypatch.setattr(settings, "observation_submitters", json.dumps([{
        "submitter_id": "management", "credential": token,
        "signing_key": "unused-for-read", "workspaces": [],
        "lease_scopes": [f"controller_management/{org}"],
    }]))
    return {"Authorization": token}


async def test_zero_targets_then_registration_and_revocation(client, monkeypatch):
    org, _, _ = await seed(monkeypatch)
    headers = grant(monkeypatch, org)
    body = {"org_id": str(org), "instance_id": str(uuid.uuid4())}
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    path = "/internal/controller/reconcile"
    assert (await client.post(path, json=body)).status_code == 401
    first = await client.post(path, headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["targets"] == []
    assert first.json()["governed_provisioning"] is False
    async with async_session_test() as db:
        cluster = Cluster(id=uuid.uuid4(), org_id=org, name="workspace", status="Pending")
        db.add(cluster)
        await db.flush()
        workspace = Workspace(id=uuid.uuid4(), org_id=org, name="target", cluster_id=cluster.id, isolation_mode="dedicated", namespace_name="tenant-a", status="pending")
        db.add(workspace)
        await db.commit()
    second = await client.post(path, headers=headers, json=body)
    assert second.status_code == 200
    assert second.json()["fence_token"] > first.json()["fence_token"]
    assert [row["workspace_id"] for row in second.json()["targets"]] == [str(workspace.id)]
    assert "credential" not in json.dumps(second.json())
    # A second process cannot own this registration loop while the first holds
    # its lease. The lease survives a new database session/process connection.
    other = dict(body, instance_id=str(uuid.uuid4()))
    assert (await client.post(path, headers=headers, json=other)).status_code == 409
    assert (await client.post(path, headers=headers, json=dict(body, org_id=str(uuid.uuid4())))).status_code == 403
    async with async_session_test() as db:
        lease = await db.get(ObservationLease, f"controller_management/{org}")
        assert lease.fence_token == second.json()["fence_token"]
    monkeypatch.setattr(settings, "observation_submitters", "[]")
    assert (await client.post(path, headers=headers, json=body)).status_code == 401


async def test_management_admin_can_read_but_cannot_trigger_workspace_execution(client, monkeypatch, internal_token_header):
    org, _, admin = await seed(monkeypatch)
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    assert (await client.get("/workspaces", headers=admin)).json() == {"workspaces": [], "total": 0}
    assert (await client.get("/orgs/current", headers=admin)).status_code == 200
    assert (await client.patch("/orgs/current", headers=admin, json={"billing_email": "admin@example.com"})).status_code == 200
    assert (await client.post("/workspaces", headers=admin, json={})).status_code == 503
    assert (await client.post("/internal/vault-sync/trigger", headers=internal_token_header, json={})).status_code == 503
    assert (await client.get("/readyz")).status_code == 200


def image_head() -> str:
    """The migration head this image actually ships.

    Derived, not spelled. This test is about which background tasks management mode
    starts, and it has to get past the lifespan's schema check to observe that — so
    the revision here is a fixture value, not an assertion. A literal made every
    migration break this test with "Management database schema does not match the
    image", which says nothing about reconcilers; #5535's 018 is the second time
    that happened. The check itself is asserted against a literal where it IS the
    subject, in `tests/test_installation_postgres.py`.
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app import main

    root = Path(main.__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"the migration chain has {len(heads)} heads: {heads}"
    return heads[0]


async def test_management_startup_never_starts_legacy_reconcilers(monkeypatch):
    from app import installation, main
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    monkeypatch.setattr(app.state, "domain_policy", object())
    monkeypatch.setattr(installation, "database_check", AsyncMock(return_value={"revision": image_head()}))
    workspace_start, vault_start = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main.workspace_reconciler, "start", workspace_start)
    monkeypatch.setattr(main.vault_sync_reconciler, "start", vault_start)
    async with main.lifespan(app):
        workspace_start.assert_not_called()
        vault_start.assert_not_called()
    monkeypatch.setattr(app.state, "domain_policy", None)
    with pytest.raises(RuntimeError, match="strict domain"):
        async with main.lifespan(app):
            pass


async def test_legacy_full_installation_gate_still_refuses(monkeypatch):
    from app import main
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "false")
    monkeypatch.setenv("SUPERPLANE_INSTALLATION_REQUIRED", "true")
    with pytest.raises(RuntimeError, match="trust adapters"):
        async with main.lifespan(app):
            pass
