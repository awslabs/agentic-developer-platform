"""Rollback pauses new owner admissions but retains stopped inventory and disks."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.cloud_desktop import rollback
from src.cloud_desktop.rollback import run, suspend_admissions
from src.shared.models.base import Base
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxOperation, DevBoxPolicy, DevBoxResource
from src.shared.models.onboarding import TenantMembership
from tests.cloud_desktop.test_routes import client, context, create

pytest_plugins = ("tests.cloud_desktop.test_routes",)


@pytest.mark.asyncio
async def test_rollback_preserves_stopped_box_and_retained_workspace_disk(database):
    async with database.kw["bind"].begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[DevBoxInstance.__table__, DevBoxResource.__table__]))
    owner = client(database, context("alice-sub"))
    setup = create(owner).json()
    async with database.begin() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.status = "stopped"
        box.desired_state = "stopped"
        box.observed_state = "stopped"
        (await db.get(DevBoxOperation, setup["operation_id"])).status = "succeeded"
        common = dict(
            box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id, machine_generation=box.machine_generation
        )
        db.add(
            DevBoxInstance(
                id="instance-a",
                **common,
                provider_instance_id="i-0123456789abcdef0",
                template_revision=box.template_revision,
                observed_state="stopped",
            )
        )
        db.add(
            DevBoxResource(
                id="disk-a",
                **common,
                resource_kind="workspace-disk",
                provider_resource_id="vol-0123456789abcdef0",
                size_gb=30,
                encrypted=True,
                retention_state="retained",
                deletion_state="none",
            )
        )
    async with database.begin() as db:
        assert await suspend_admissions(db, org_id="one", deployment_id="test-deployment", policy_id="p1") is True
    async with database() as db:
        assert (await db.get(DevBoxPolicy, "p1")).enabled is False
        box = await db.get(DevBox, setup["desktop_id"])
        disk = await db.get(DevBoxResource, "disk-a")
        instance = await db.get(DevBoxInstance, "instance-a")
        assert (box.status, box.desired_state, box.observed_state) == ("stopped", "stopped", "stopped")
        assert (instance.provider_instance_id, instance.observed_state) == ("i-0123456789abcdef0", "stopped")
        assert (disk.provider_resource_id, disk.retention_state, disk.deletion_state) == ("vol-0123456789abcdef0", "retained", "none")
        assert (await db.get(DevBoxOperation, setup["operation_id"])).status == "succeeded"
        assert await db.scalar(select(DevBoxResource.id).where(DevBoxResource.box_id == box.id)) == "disk-a"
    assert owner.get(f"/cloud-desktops/{setup['desktop_id']}").json()["status"] == "stopped"
    assert create(owner, key="new-setup").status_code == 403
    assert (
        owner.post(
            f"/cloud-desktops/{setup['desktop_id']}/release", headers={"Idempotency-Key": "release"}, json={"workspace_retention": "retain"}
        ).status_code
        == 202
    )
    async with database() as db:
        assert (await db.get(DevBoxResource, "disk-a")).retention_state == "retained"


@pytest.mark.asyncio
async def test_operator_rollback_requires_exact_configured_deployment(database, monkeypatch):
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "test-deployment")
    monkeypatch.setattr(rollback, "get_session_factory", lambda: database)
    with pytest.raises(ValueError, match="not configured"):
        await run("one", "another-deployment", "p1")
    assert await run("one", "test-deployment", "p1") is True
    assert await run("one", "test-deployment", "p1") is False
    async with database() as db:
        assert (await db.get(DevBoxPolicy, "p2")).enabled is True


@pytest.mark.asyncio
async def test_rollback_denies_new_setup_and_start_but_allows_cleanup_and_membership_revocation(database):
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    other = client(database, context("other-sub", "two"))
    setup = create(alice).json()
    desktop_id = setup["desktop_id"]
    async with database.begin() as db:
        (await db.get(DevBoxOperation, setup["operation_id"])).status = "succeeded"
        assert await suspend_admissions(db, org_id="one", deployment_id="test-deployment", policy_id="p1")
    assert create(alice).status_code == 403
    assert create(bob, key="different-owner").status_code == 403
    assert alice.post(f"/cloud-desktops/{desktop_id}/start", headers={"Idempotency-Key": "start"}).status_code == 403
    assert create(other).status_code == 202
    stop = alice.post(f"/cloud-desktops/{desktop_id}/stop", headers={"Idempotency-Key": "stop"})
    assert stop.status_code == 202 and stop.json()["status"] == "pending"
    async with database.begin() as db:
        (await db.get(DevBoxOperation, stop.json()["operation_id"])).status = "succeeded"
    release = alice.post(f"/cloud-desktops/{desktop_id}/release", headers={"Idempotency-Key": "release"}, json={"workspace_retention": "retain"})
    assert release.status_code == 202 and release.json()["status"] == "pending"
    assert alice.get(f"/cloud-desktops/{desktop_id}").json()["desired_state"] == "released"
    async with database.begin() as db:
        member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice", TenantMembership.tenant_id == "one"))
        member.revoked_at = datetime.now(UTC)
    assert alice.get(f"/cloud-desktops/{desktop_id}").status_code == 403
    assert (
        alice.post(f"/cloud-desktops/{desktop_id}/release", headers={"Idempotency-Key": "again"}, json={"workspace_retention": "retain"}).status_code
        == 403
    )
    async with database() as db:
        box = await db.get(DevBox, desktop_id)
        assert box.desired_state == "released" and box.observed_state == "unknown"
        assert (await db.get(DevBoxOperation, release.json()["operation_id"])).status == "pending"
        assert (await db.get(DevBoxPolicy, "p2")).enabled is True


@pytest.mark.asyncio
async def test_rollback_rejects_wrong_policy_identity_without_changes(database):
    async with database.begin() as db:
        with pytest.raises(ValueError, match="identity mismatch"):
            await suspend_admissions(db, org_id="two", deployment_id="test-deployment", policy_id="p1")
        with pytest.raises(ValueError, match="identity mismatch"):
            await suspend_admissions(db, org_id="one", deployment_id="test-deployment", policy_id="missing")
    async with database() as db:
        assert (await db.get(DevBoxPolicy, "p1")).enabled is True
        assert (await db.get(DevBoxPolicy, "p2")).enabled is True
