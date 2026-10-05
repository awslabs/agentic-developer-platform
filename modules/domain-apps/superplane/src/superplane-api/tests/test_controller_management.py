"""Management stays useful at zero targets without activating legacy mutation."""

import json
import uuid
from pathlib import Path
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
    monkeypatch.setattr(
        settings,
        "observation_submitters",
        json.dumps(
            [
                {
                    "submitter_id": "management",
                    "credential": token,
                    "signing_key": "unused-for-read",
                    "workspaces": [],
                    "lease_scopes": [f"controller_management/{org}"],
                }
            ]
        ),
    )
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
        cluster = Cluster(
            id=uuid.uuid4(), org_id=org, name="workspace", status="Pending"
        )
        db.add(cluster)
        await db.flush()
        workspace = Workspace(
            id=uuid.uuid4(),
            org_id=org,
            name="target",
            cluster_id=cluster.id,
            isolation_mode="dedicated",
            namespace_name="tenant-a",
            status="pending",
        )
        db.add(workspace)
        await db.commit()
    second = await client.post(path, headers=headers, json=body)
    assert second.status_code == 200
    assert second.json()["fence_token"] > first.json()["fence_token"]
    assert [row["workspace_id"] for row in second.json()["targets"]] == [
        str(workspace.id)
    ]
    assert "credential" not in json.dumps(second.json())
    # A second process cannot own this registration loop while the first holds
    # its lease. The lease survives a new database session/process connection.
    other = dict(body, instance_id=str(uuid.uuid4()))
    assert (await client.post(path, headers=headers, json=other)).status_code == 409
    assert (
        await client.post(
            path, headers=headers, json=dict(body, org_id=str(uuid.uuid4()))
        )
    ).status_code == 403
    async with async_session_test() as db:
        lease = await db.get(ObservationLease, f"controller_management/{org}")
        assert lease.fence_token == second.json()["fence_token"]
    monkeypatch.setattr(settings, "observation_submitters", "[]")
    assert (await client.post(path, headers=headers, json=body)).status_code == 401


async def test_management_reads_remain_available_without_execution_facade(
    client, monkeypatch, internal_token_header
):
    from app.services import provisioning

    org, _, admin = await seed(monkeypatch)
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    monkeypatch.setattr(provisioning, "_facade", None)
    assert (await client.get("/workspaces", headers=admin)).json() == {
        "workspaces": [],
        "total": 0,
    }
    assert (await client.get("/orgs/current", headers=admin)).status_code == 200
    assert (
        await client.patch(
            "/orgs/current", headers=admin, json={"billing_email": "admin@example.com"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/workspaces",
            headers=admin,
            json={"name": "needs-admission", "isolation_mode": "dedicated"},
        )
    ).status_code == 503
    assert (
        await client.post(
            "/internal/vault-sync/trigger", headers=internal_token_header, json={}
        )
    ).status_code == 503
    assert (await client.get("/readyz")).status_code == 200


async def test_management_startup_never_starts_legacy_reconcilers(monkeypatch):
    from app import installation, main
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    monkeypatch.setattr(app.state, "domain_policy", object())
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    current_head = ScriptDirectory.from_config(config).get_current_head()
    monkeypatch.setattr(
        installation,
        "database_check",
        AsyncMock(return_value={"revision": current_head}),
    )
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


async def test_assignment_metadata_is_bound_to_current_replica(client, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from app.models.controller_execution import ControllerExecution

    org, _, _ = await seed(monkeypatch)
    headers = grant(monkeypatch, org)
    instance = str(uuid.uuid4())
    workspace = uuid.uuid4()
    async with async_session_test() as db:
        cluster = Cluster(id=uuid.uuid4(), org_id=org, name="workspace", status="Ready")
        db.add(cluster)
        await db.flush()
        db.add(
            Workspace(
                id=workspace,
                org_id=org,
                name="target",
                cluster_id=cluster.id,
                isolation_mode="dedicated",
                namespace_name="tenant-a",
                status="active",
            )
        )
        await db.flush()
        for suffix, holder in (
            ("ours", "management:" + instance),
            ("other", "management:" + str(uuid.uuid4())),
        ):
            db.add(
                ControllerExecution(
                    operation_id=suffix,
                    org_id=str(org),
                    workspace_id=str(workspace),
                    controller_holder=holder,
                    assignment={
                        "operation_id": suffix,
                        "org_id": str(org),
                        "workspace_id": str(workspace),
                        "credential_name": "token-reference-only",
                    },
                    expires_at=datetime.now(UTC) + timedelta(seconds=30),
                )
            )
        await db.commit()
    response = await client.post(
        "/internal/controller/reconcile",
        headers=headers,
        json={"org_id": str(org), "instance_id": instance},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["governed_provisioning"] is True
    target = result["targets"][0]
    assert target["execution_org_id"] == str(org)
    assert [entry["operation_id"] for entry in target["execution_assignments"]] == [
        "ours"
    ]
    assert "token" not in target["execution_assignments"][0]
