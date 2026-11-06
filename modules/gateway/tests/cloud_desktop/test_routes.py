"""Mounted Dev Box API exercises real admission rows, not a mocked controller."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.middleware import get_current_user_context
from src.cloud_desktop.routes import router
from src.crew.inventory import CrewExecutorInventory
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
    DevBoxTemplate,
    DevBoxUsage,
)
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext


def context(subject, org="one"):
    return TokenContext(
        user_id=subject, org_id=org, team_id="team", department_id="dept", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


@pytest.fixture
async def database(monkeypatch):
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "test-deployment")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES", "2")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES", "2")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB", "200")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS", "200")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "100")
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fks(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        Organization.__table__,
        User.__table__,
        TenantMembership.__table__,
        UserIdentity.__table__,
        DevBoxPolicy.__table__,
        DevBoxTemplate.__table__,
        CrewExecutor.__table__,
        CrewOwnerGeneration.__table__,
        DevBox.__table__,
        DevBoxInstance.__table__,
        DevBoxDevice.__table__,
        DevBoxAccessSession.__table__,
        DevBoxResource.__table__,
        DevBoxUsage.__table__,
        DevBoxOperation.__table__,
        DevBoxEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        ec2_policy = {"crew": {"allowed_targets": ["ec2-instance"]}}
        session.add_all(
            [
                Organization(id="one", name="One", settings=ec2_policy),
                Organization(id="two", name="Two", settings=ec2_policy),
            ]
        )
        session.add_all(
            [
                User(id="alice", org_id="one", team_id="team", email="alice@example.test", cognito_sub="alice-sub"),
                User(id="bob", org_id="one", team_id="team", email="bob@example.test", cognito_sub="bob-sub"),
                User(id="other", org_id="two", team_id="team", email="other@example.test", cognito_sub="other-sub"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantMembership(user_id=identity, tenant_id=org, revoked_at=None)
                for identity, org in (("alice", "one"), ("bob", "one"), ("other", "two"))
            ]
        )
        session.add_all(
            [
                DevBoxPolicy(
                    id="p1",
                    org_id="one",
                    deployment_id="test-deployment",
                    enabled=True,
                    max_live_boxes=1,
                    max_running_boxes=1,
                    allowed_sizes=["small"],
                    max_disk_gb=50,
                    max_storage_gb=100,
                    max_running_hours=100,
                    instance_hour_rate_usd=Decimal("0.10"),
                    disk_gb_hour_rate_usd=Decimal("0.0001"),
                    snapshot_gb_hour_rate_usd=Decimal("0.0001"),
                    budget_usd=Decimal("50"),
                    max_lease_minutes=60,
                    idle_stop_minutes=10,
                ),
                DevBoxPolicy(
                    id="p2",
                    org_id="two",
                    deployment_id="test-deployment",
                    enabled=True,
                    max_live_boxes=1,
                    max_running_boxes=1,
                    allowed_sizes=["small"],
                    max_disk_gb=50,
                    max_storage_gb=100,
                    max_running_hours=100,
                    instance_hour_rate_usd=Decimal("0.10"),
                    disk_gb_hour_rate_usd=Decimal("0.0001"),
                    snapshot_gb_hour_rate_usd=Decimal("0.0001"),
                    budget_usd=Decimal("50"),
                    max_lease_minutes=60,
                    idle_stop_minutes=10,
                ),
                DevBoxTemplate(id="t1", name="assistant", revision="r1", profile="crew", instance_size="small", disk_gb=30, approved=True),
            ]
        )
        await session.commit()
    yield factory
    await engine.dispose()


def client(factory, caller, *, raise_server_exceptions=False):
    app = FastAPI()
    app.include_router(router)

    async def get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = get_session
    app.dependency_overrides[get_current_user_context] = lambda: caller
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def create(http, key="setup-1", profile="crew"):
    return http.post("/cloud-desktops", headers={"Idempotency-Key": key}, json={"template": "assistant", "profile": profile})


@pytest.mark.asyncio
async def test_pod_and_box_share_generation_through_provision_and_retire(database):
    inventory = CrewExecutorInventory(database)
    pod = await inventory.ensure(tenant_id="one", user_id="alice")
    assert pod.generation == 1
    http = client(database, context("alice-sub"))
    admitted = create(http, key="after-pod")
    assert admitted.status_code == 202, admitted.text
    async with database() as db:
        box = await db.get(DevBox, admitted.json()["desktop_id"])
        assert box.machine_generation == 2
        assert (await db.get(CrewOwnerGeneration, ("one", "alice"))).generation == 2
        assert (await db.get(CrewExecutor, pod.id)).generation == 1
        box.status = box.observed_state = "running"
        operation = await db.get(DevBoxOperation, admitted.json()["operation_id"])
        operation.status = "succeeded"
        await db.commit()
    retired = http.post(f"/cloud-desktops/{box.id}/release", headers={"Idempotency-Key": "retire"}, json={"workspace_retention": "retain"})
    assert retired.status_code == 202, retired.text
    async with database() as db:
        assert (await db.get(CrewOwnerGeneration, ("one", "alice"))).generation == 3
        assert (await db.get(DevBox, box.id)).machine_generation == 2
        (await db.get(CrewExecutor, pod.id)).state = "deleted"
        await db.commit()
    next_pod = await inventory.ensure(tenant_id="one", user_id="alice")
    assert next_pod.generation == 4
    assert (await inventory.ensure(tenant_id="one", user_id="alice")).id == next_pod.id
    assert (await inventory.ensure(tenant_id="two", user_id="other")).generation == 1


@pytest.mark.asyncio
async def test_duplicate_request_and_changed_digest_have_one_reservation(database):
    http = client(database, context("alice-sub"))
    first = create(http)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "pending"
    assert create(http).json() == first.json()
    assert create(http, profile="different").status_code == 409
    assert create(http, key="second").status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(DevBoxOperation.id))) == 1
        assert await db.scalar(select(func.count(DevBoxEvent.id))) == 1


@pytest.mark.asyncio
async def test_running_reservation_includes_pending_and_uncertain_stop(database):
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.max_live_boxes = 2
        await db.commit()
    setup = create(alice).json()
    assert create(bob).status_code == 403
    assert create(alice).json() == setup
    async with database() as db:
        operation = await db.get(DevBoxOperation, setup["operation_id"])
        operation.status = "succeeded"
        box = await db.get(DevBox, setup["desktop_id"])
        box.status = "running"
        box.observed_state = "running"
        await db.commit()
    stop = alice.post(f"/cloud-desktops/{setup['desktop_id']}/stop", headers={"Idempotency-Key": "stop"}).json()
    assert create(bob).status_code == 403
    async with database() as db:
        operation = await db.get(DevBoxOperation, stop["operation_id"])
        operation.status = "succeeded"
        box = await db.get(DevBox, setup["desktop_id"])
        box.status = "stopped"
        box.observed_state = "stopped"
        await db.commit()
    admitted = create(bob)
    assert admitted.status_code == 202, admitted.text
    assert alice.post(f"/cloud-desktops/{setup['desktop_id']}/start", headers={"Idempotency-Key": "start"}).status_code == 403


@pytest.mark.asyncio
async def test_deployment_capacity_counts_other_tenants_and_requires_configuration(database, monkeypatch):
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.max_live_boxes = 2
        policy.max_running_boxes = 2
        await db.commit()
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    other = client(database, context("other-sub", "two"))
    monkeypatch.delenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES")
    assert create(alice).status_code == 403
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES", "2")
    assert create(alice).status_code == 202
    assert create(other).status_code == 202
    assert create(bob).status_code == 403
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 2


@pytest.mark.parametrize(
    "field,value",
    (
        ("max_storage_gb", 20),
        ("max_running_hours", 0),
        ("budget_usd", Decimal("0.10")),
        ("disk_gb_hour_rate_usd", None),
    ),
)
@pytest.mark.asyncio
async def test_admission_requires_capacity_prices_and_conservative_budget(database, field, value):
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        setattr(policy, field, value)
        await db.commit()
    http = client(database, context("alice-sub"))
    denied = create(http)
    assert denied.status_code == 403, denied.text
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 0


@pytest.fixture
async def concurrent_database(tmp_path, monkeypatch):
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "test-deployment")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB", "200")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS", "200")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "100")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission.sqlite'}", connect_args={"timeout": 10})

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fks(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine.sync_engine, "begin")
    def serialize_sqlite_transactions(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    tables = [
        Organization.__table__,
        User.__table__,
        TenantMembership.__table__,
        UserIdentity.__table__,
        DevBoxPolicy.__table__,
        DevBoxTemplate.__table__,
        CrewExecutor.__table__,
        CrewOwnerGeneration.__table__,
        DevBox.__table__,
        DevBoxInstance.__table__,
        DevBoxDevice.__table__,
        DevBoxAccessSession.__table__,
        DevBoxResource.__table__,
        DevBoxOperation.__table__,
        DevBoxEvent.__table__,
        DevBoxUsage.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Organization(id="one", name="One", settings={"crew": {"allowed_targets": ["ec2-instance"]}}))
        session.add_all(
            [
                User(id=identity, org_id="one", team_id="team", email=f"{identity}@example.test", cognito_sub=f"{identity}-sub")
                for identity in ("alice", "bob")
            ]
        )
        await session.flush()
        session.add_all([TenantMembership(user_id=identity, tenant_id="one", revoked_at=None) for identity in ("alice", "bob")])
        session.add(
            DevBoxPolicy(
                id="p1",
                org_id="one",
                deployment_id="test-deployment",
                enabled=True,
                max_live_boxes=1,
                max_running_boxes=1,
                allowed_sizes=["small"],
                max_disk_gb=50,
                max_lease_minutes=60,
                idle_stop_minutes=10,
                max_storage_gb=100,
                max_running_hours=100,
                instance_hour_rate_usd=Decimal("0.10"),
                disk_gb_hour_rate_usd=Decimal("0.0001"),
                snapshot_gb_hour_rate_usd=Decimal("0.0001"),
                budget_usd=Decimal("50"),
            )
        )
        session.add(DevBoxTemplate(id="t1", name="assistant", revision="r1", profile="crew", instance_size="small", disk_gb=30, approved=True))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_simultaneous_admission_and_replay_share_one_transactional_reservation(concurrent_database):
    alice = client(concurrent_database, context("alice-sub"))
    bob = client(concurrent_database, context("bob-sub"))
    responses = await asyncio.gather(asyncio.to_thread(create, alice), asyncio.to_thread(create, bob))
    assert sorted(response.status_code for response in responses) == [202, 403]
    winner = alice if responses[0].status_code == 202 else bob
    assert create(winner).json() == responses[0 if winner is alice else 1].json()
    async with concurrent_database() as session:
        assert await session.scalar(select(func.count(DevBox.id))) == 1
        assert await session.scalar(select(func.count(DevBoxOperation.id))) == 1


@pytest.mark.asyncio
async def test_owner_scope_revocation_and_quota(database, monkeypatch):
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    other = client(database, context("other-sub", "two"))
    forged_org = client(database, context("other-sub", "one"), raise_server_exceptions=True)
    admitted = create(alice).json()
    identifier = admitted["desktop_id"]
    assert create(bob).status_code == 403
    assert bob.get(f"/cloud-desktops/{identifier}").status_code == 404
    assert bob.get(f"/cloud-desktops/{identifier}/operations/{admitted['operation_id']}").status_code == 404
    assert (
        bob.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "x"}, json={"workspace_retention": "retain"}).status_code == 404
    )
    assert other.get(f"/cloud-desktops/{identifier}").status_code == 404
    assert other.get("/cloud-desktops").json()["items"] == []
    assert create(forged_org, key="forged-org").status_code == 403
    assert create(other).status_code == 202
    assert alice.get(f"/cloud-desktops/{identifier}").json()["observed_state"] == "unknown"
    assert len(alice.get("/cloud-desktops").json()["items"]) == 1
    assert alice.get("/cloud-desktops").json()["items"][0]["name"] == "assistant"
    async with database() as db:
        member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
        member.revoked_at = datetime.now(UTC)
        await db.commit()
    assert alice.get("/cloud-desktops").status_code == 403
    assert alice.get(f"/cloud-desktops/{identifier}").status_code == 403
    assert create(alice, key="new").status_code == 403
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "other-deployment")
    assert alice.get(f"/cloud-desktops/{identifier}").status_code == 403


@pytest.mark.asyncio
async def test_policy_and_template_default_deny_without_reservation(database, monkeypatch):
    http = client(database, context("alice-sub"))
    assert create(http, profile="unapproved").status_code == 403
    assert (
        http.post(
            "/cloud-desktops", json={"template": "assistant", "profile": "crew", "user_id": "bob"}, headers={"Idempotency-Key": "forged"}
        ).status_code
        == 422
    )
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.enabled = False
        await db.commit()
    assert create(http).status_code == 403
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.enabled = True
        policy.max_running_boxes = 0
        await db.commit()
    assert create(http).status_code == 403
    monkeypatch.delenv("BG_DEV_BOX_DEPLOYMENT_ID")
    assert create(http).status_code == 403
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 0


@pytest.mark.asyncio
async def test_conflicts_and_unknown_operation_are_pollable(database):
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    url = f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}"
    assert http.get(url).json()["status"] == "pending"
    assert http.get(f"/cloud-desktops/{identifier}/operations/not-an-operation").status_code == 404
    assert http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-1"}).status_code == 409
    async with database() as db:
        operation = await db.get(DevBoxOperation, setup["operation_id"])
        operation.status = "unknown"
        await db.commit()
    assert http.get(url).json()["status"] == "unknown"
    assert (
        http.post(
            f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release-1"}, json={"workspace_retention": "retain"}
        ).status_code
        == 409
    )
    async with database() as db:
        operation = await db.get(DevBoxOperation, setup["operation_id"])
        operation.status = "succeeded"
        await db.commit()
    stop = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-1"})
    assert stop.status_code == 202 and stop.json()["status"] == "pending"
    assert http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-1"}).json() == stop.json()
    assert (
        http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "stop-1"}, json={"workspace_retention": "retain"}).status_code
        == 409
    )
    async with database() as db:
        operation = await db.get(DevBoxOperation, stop.json()["operation_id"])
        operation.status = "succeeded"
        await db.commit()
    release = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release-1"}, json={"workspace_retention": "retain"})
    assert release.status_code == 202 and release.json()["status"] == "pending"
    assert create(http, key="new-setup").status_code == 409
    assert http.get(f"/cloud-desktops/{identifier}").json()["desired_state"] == "released"
    assert http.get(f"/cloud-desktops/{identifier}").json()["observed_state"] == "unknown"


@pytest.mark.asyncio
async def test_disabled_admission_still_allows_owner_cleanup(database):
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    async with database() as db:
        operation = await db.get(DevBoxOperation, setup["operation_id"])
        operation.status = "succeeded"
        policy = await db.get(DevBoxPolicy, "p1")
        policy.enabled = False
        await db.commit()
    assert create(http, key="new").status_code == 403
    assert http.post(f"/cloud-desktops/{identifier}/start", headers={"Idempotency-Key": "start"}).status_code == 403
    stop = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop"})
    assert stop.status_code == 202
    async with database() as db:
        operation = await db.get(DevBoxOperation, stop.json()["operation_id"])
        operation.status = "succeeded"
        await db.commit()
    release = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release"}, json={"workspace_retention": "retain"})
    assert release.status_code == 202 and release.json()["status"] == "pending"
    assert http.get(f"/cloud-desktops/{identifier}").json()["observed_state"] == "unknown"


class FakeEC2:
    def __init__(self):
        self.launches = []
        self.visible = False
        self.instance = None
        self.instances = {}
        self.lose_response = True
        self.actions = []

    def run_instances(self, **kwargs):
        self.launches.append(kwargs)
        # Like EC2, a repeated RunInstances with the same ClientToken returns the existing instance.
        existing = next((item for item in self.instances.values() if item["ClientToken"] == kwargs["ClientToken"]), None)
        self.instance = existing or {
            "InstanceId": "i-example-owned" if not self.instances else "i-example-replacement",
            "ClientToken": kwargs["ClientToken"],
            "Tags": kwargs["TagSpecifications"][0]["Tags"],
            "State": {"Name": "running"},
            "Placement": {"AvailabilityZone": "us-east-1a"},
        }
        self.instances[self.instance["InstanceId"]] = self.instance
        if self.lose_response:
            raise ConnectionError("provider accepted the launch but response was lost")
        return {"Instances": [self.instance]}

    def describe_instances(self, **kwargs):
        if "InstanceIds" in kwargs:
            instances = [self.instances[identity] for identity in kwargs["InstanceIds"] if identity in self.instances]
        else:
            filters = kwargs["Filters"]
            token = next(item["Values"][0] for item in filters if item["Name"] == "client-token")
            instances = [item for item in self.instances.values() if item["ClientToken"] == token] if self.visible else []
        return {"Reservations": [{"Instances": instances}]} if instances else {"Reservations": []}

    def create_volume(self, **kwargs):
        self.volume_calls = getattr(self, "volume_calls", [])
        self.volume_calls.append(kwargs)
        if not hasattr(self, "volume"):
            self.volume = {
                "VolumeId": "vol-owned-example",
                "Size": kwargs["Size"],
                "Encrypted": kwargs["Encrypted"],
                "KmsKeyId": kwargs["KmsKeyId"],
                "AvailabilityZone": kwargs["AvailabilityZone"],
                "Tags": kwargs["TagSpecifications"][0]["Tags"],
                "State": "available",
                "Attachments": [],
            }
        return {"VolumeId": self.volume["VolumeId"]}

    def describe_volumes(self, **kwargs):
        if "VolumeIds" in kwargs and hasattr(self, "volume"):
            if kwargs["VolumeIds"] != [self.volume["VolumeId"]]:
                return {"Volumes": []}
        return {"Volumes": [self.volume]} if hasattr(self, "volume") else {"Volumes": []}

    def attach_volume(self, **kwargs):
        self.volume["State"] = "in-use"
        self.volume["Attachments"] = [{"InstanceId": kwargs["InstanceId"], "State": "attached"}]
        self.actions.append(("attach", kwargs["InstanceId"]))
        if getattr(self, "lose_attachment", False):
            raise ConnectionError("attach response was lost")

    def detach_volume(self, **kwargs):
        self.actions.append(("detach", kwargs["InstanceId"]))
        self.volume["State"] = "available"
        self.volume["Attachments"] = []
        if getattr(self, "lose_detachment", False):
            raise ConnectionError("detach response was lost")

    def stop_instances(self, **kwargs):
        self.actions.append(("stop", kwargs["InstanceIds"][0]))
        self.instances[kwargs["InstanceIds"][0]]["State"] = {"Name": "stopped"}

    def start_instances(self, **kwargs):
        self.actions.append(("start", kwargs["InstanceIds"][0]))
        self.instances[kwargs["InstanceIds"][0]]["State"] = {"Name": "running"}

    def terminate_instances(self, **kwargs):
        instance_id = kwargs["InstanceIds"][0]
        self.actions.append(("terminate", instance_id))
        self.instances[instance_id]["State"] = {"Name": "terminated"}
        if hasattr(self, "volume") and self.volume["Attachments"] and self.volume["Attachments"][0]["InstanceId"] == instance_id:
            self.volume["Attachments"] = []
            self.volume["State"] = "available"
        if getattr(self, "lose_termination", False):
            self.lose_termination = False
            raise ConnectionError("termination response was lost")

    def delete_volume(self, **kwargs):
        assert kwargs["VolumeId"] == self.volume["VolumeId"]
        self.actions.append(("delete_volume", kwargs["VolumeId"]))
        if not getattr(self, "delay_deletion", False):
            del self.volume
        if getattr(self, "lose_deletion", False):
            raise ConnectionError("volume deletion response was lost")


class FakeSSM:
    def __init__(self):
        self.calls = []

    def describe_instance_information(self, **kwargs):
        filters = kwargs["Filters"]
        self.calls.append(filters)
        return {"InstanceInformationList": [{"InstanceId": filters[0]["Values"][0], "PingStatus": "Online"}]}


@pytest.mark.asyncio
async def test_mounted_setup_worker_restart_and_operation_poll(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    async with database() as db:
        template = await db.get(DevBoxTemplate, "t1")
        template.image_ref = "ami-example"
        await db.commit()

    http = client(database, context("alice-sub"))
    accepted = create(http)
    assert accepted.status_code == 202
    operation = accepted.json()
    assert operation["progress"] == "admitted"
    operation_url = f"/cloud-desktops/{operation['desktop_id']}/operations/{operation['operation_id']}"
    ec2 = FakeEC2()
    ssm = FakeSSM()
    launch_template = LaunchTemplate("lt-example", "1", "us-east-1", "ami-example", "r1", "small", 30, "kms-example")
    assert await scan_once(database, ec2, ssm, "test-deployment", launch_template, "first-worker") == 1
    pending = http.get(operation_url).json()
    assert pending["status"] == "unknown"
    assert pending["error_class"] == "provider_unknown"
    assert pending["progress"] == "launch_intent"
    assert ssm.calls == []
    assert len(ec2.launches) == 1
    assert await scan_once(database, ec2, ssm, "test-deployment", launch_template, "restarted-worker") == 1
    assert http.get(operation_url).json()["status"] == "unknown"
    # #8037: the unconfirmed launch is re-sent with the same ClientToken, so EC2 returns the same instance.
    assert len(ec2.launches) == 2 and ec2.launches[1]["ClientToken"] == ec2.launches[0]["ClientToken"]
    ec2.visible = True
    assert await scan_once(database, ec2, ssm, "test-deployment", launch_template, "restarted-worker") == 1
    assert len(ec2.launches) == 2
    assert http.get(operation_url).json()["status"] == "succeeded", http.get(operation_url).json()
    assert await scan_once(database, ec2, ssm, "test-deployment", launch_template, "restarted-worker") == 0
    result = http.get(operation_url).json()
    assert result["status"] == "succeeded"
    assert result["progress"] == "instance_observed"
    assert http.get(f"/cloud-desktops/{operation['desktop_id']}").json()["observed_state"] == "running"
    async with database() as db:
        instances = list(await db.scalars(select(DevBoxInstance)))
        assert len(instances) == 1
        assert instances[0].provider_instance_id == "i-example-owned"
        assert instances[0].readiness_class == "machine_ssm_online"
    assert ssm.calls == [[{"Key": "InstanceIds", "Values": ["i-example-owned"]}]]


async def test_refused_launch_fails_operation_and_owner_can_release(database, monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    class RefusingEC2(FakeEC2):
        def run_instances(self, **kwargs):
            self.launches.append(kwargs)
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "not authorized"}}, "RunInstances")

    async with database() as db:
        template = await db.get(DevBoxTemplate, "t1")
        template.image_ref = "ami-example"
        await db.commit()
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    operation_url = f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}"
    ec2 = RefusingEC2()
    launch_template = LaunchTemplate("lt-example", "1", "us-east-1", "ami-example", "r1", "small", 30, "kms-example")
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one") == 1
    failed = http.get(operation_url).json()
    assert failed["status"] == "failed" and failed["error_class"] == "provider_refused:UnauthorizedOperation"
    box = http.get(f"/cloud-desktops/{identifier}").json()
    assert box["status"] == "requested" and box["observed_state"] == "unknown" and box["desired_state"] == "running"
    # The failed launch is not re-claimed and does not block the owner any more.
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two") == 0
    assert len(ec2.launches) == 1
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release-refused"}, json={"workspace_retention": "retain"}
    )
    assert release.status_code == 202, release.text
    assert http.get(f"/cloud-desktops/{identifier}").json()["desired_state"] == "released"
    async with database() as db:
        events = list(await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.box_id == identifier).order_by(DevBoxEvent.created_at)))
        assert events == ["setup_admitted", "launch_refused", "release_admitted"]


@pytest.mark.asyncio
async def test_ensure_retries_a_refused_setup_launch_on_a_new_generation(database, monkeypatch):
    """#8423: a launch EC2 refused leaves nothing behind, so the next ensure is a fresh setup, not a 409."""
    from botocore.exceptions import ClientError

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    class RefusingEC2(FakeEC2):
        def run_instances(self, **kwargs):
            self.launches.append(kwargs)
            raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity", "Message": "no capacity"}}, "RunInstances")

    async with database() as db:
        template = await db.get(DevBoxTemplate, "t1")
        template.image_ref = "ami-example"
        await db.commit()
    http = client(database, context("alice-sub"))

    def ensure(key):
        return http.post("/cloud-desktops/ensure", headers={"Idempotency-Key": key}, json={"template": "assistant", "profile": "crew"})

    first = ensure("ensure-first")
    assert first.status_code == 202, first.text
    identifier = first.json()["desktop_id"]
    launch_template = LaunchTemplate("lt-example", "1", "us-east-1", "ami-example", "r1", "small", 30, "kms-example")
    assert await scan_once(database, RefusingEC2(), FakeSSM(), "test-deployment", launch_template, "worker-one") == 1
    failed = http.get(f"/cloud-desktops/{identifier}/operations/{first.json()['operation_id']}").json()
    assert failed["status"] == "failed" and failed["error_class"] == "provider_refused:InsufficientInstanceCapacity"
    before = http.get(f"/cloud-desktops/{identifier}").json()
    assert before["status"] == "requested" and before["machine_generation"] == 1

    retry = ensure("ensure-retry")
    assert retry.status_code == 202, retry.text
    assert retry.json()["desktop_id"] == identifier and retry.json()["operation_id"] != first.json()["operation_id"]
    assert retry.json()["status"] == "pending"
    after = http.get(f"/cloud-desktops/{identifier}").json()
    assert after["status"] == "requested" and after["desired_state"] == "running" and after["machine_generation"] == 2
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        operations = list(await db.scalars(select(DevBoxOperation).where(DevBoxOperation.box_id == identifier).order_by(DevBoxOperation.created_at)))
        assert [(item.action, item.status, item.machine_generation) for item in operations] == [("setup", "failed", 1), ("setup", "pending", 2)]
        events = list(await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.box_id == identifier).order_by(DevBoxEvent.created_at)))
        assert events == ["setup_admitted", "launch_refused", "setup_admitted", "setup_retry_admitted"]
    # The retried launch runs like any first launch, on the new generation.
    ec2 = FakeEC2()
    ec2.lose_response = False
    ec2.visible = True
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two") == 1
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] == "running"
    assert ensure("ensure-after-running").status_code == 200
    # A failed setup that did launch a machine EC2 then ended is not retried here: the owner releases it.
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.status, box.observed_state = "requested", "unknown"
        operation = await db.scalar(select(DevBoxOperation).where(DevBoxOperation.machine_generation == 2))
        operation.status, operation.error_class = "failed", "provider_refused:Client.InvalidKMSKey.InvalidState"
        await db.commit()
    assert ensure("ensure-after-terminated").status_code == 409


@pytest.mark.asyncio
async def test_abandoned_unknown_launch_admits_release_but_live_or_fresh_unknown_does_not(database):
    from src.cloud_desktop.routes import DEV_BOX_LAUNCH_RETRY_BOUND

    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    release = {"Idempotency-Key": "release-abandoned"}

    async def park(**values):
        async with database() as db:
            operation = await db.get(DevBoxOperation, setup["operation_id"])
            for field, value in values.items():
                setattr(operation, field, value)
            await db.commit()

    await park(status="unknown", error_class="launch_not_observed", progress="launch_intent", retries=1, lease_owner=None, lease_expires_at=None)
    assert http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"}).status_code == 409
    await park(retries=DEV_BOX_LAUNCH_RETRY_BOUND + 1, lease_owner="worker", lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
    assert http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"}).status_code == 409
    await park(lease_owner=None, lease_expires_at=None)
    # Only release and replace may supersede the abandoned launch. The replace passes the guard and is then
    # rejected by its own later checks (capacity, verified stopped workspace), which rolls the supersession back.
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-abandoned"})
    assert stopped.status_code == 409 and stopped.json()["detail"] == "A desktop operation is already active"
    replaced = http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-abandoned"})
    assert replaced.status_code in (403, 409) and replaced.json()["detail"] != "A desktop operation is already active"
    assert http.get(f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}").json()["status"] == "unknown"
    accepted = http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"})
    assert accepted.status_code == 202 and accepted.json()["status"] == "pending"
    superseded = http.get(f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}").json()
    assert superseded["status"] == "failed" and superseded["error_class"] == "launch_not_observed"
    assert http.get(f"/cloud-desktops/{identifier}").json()["desired_state"] == "released"
    async with database() as db:
        events = list(await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.box_id == identifier).order_by(DevBoxEvent.created_at)))
        assert events == ["setup_admitted", "launch_abandoned", "release_admitted"]
        active = list(
            await db.scalars(select(DevBoxOperation).where(DevBoxOperation.box_id == identifier, DevBoxOperation.status.in_(["pending", "unknown"])))
        )
        assert [operation.action for operation in active] == ["release"]


@pytest.mark.asyncio
async def test_unknown_launch_older_than_the_token_window_admits_release(database):
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    async with database() as db:
        operation = await db.get(DevBoxOperation, setup["operation_id"])
        operation.status = "unknown"
        operation.progress = "launch_intent"
        operation.created_at = datetime.now(UTC) - timedelta(hours=23, minutes=1)
        await db.commit()
    assert http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-old"}).status_code == 409
    accepted = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release-old"}, json={"workspace_retention": "delete"})
    assert accepted.status_code == 202, accepted.text
    superseded = http.get(f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}").json()
    assert superseded["status"] == "failed" and superseded["error_class"] == "launch_abandoned"


def test_worker_refuses_mismatched_release_and_controller_account(monkeypatch):
    import hashlib
    import json

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.worker import controller_clients, validate_release

    manifest = {
        "component": "dev-box",
        "deployment_id": "test-deployment",
        "account_id": "000000000000",
        "region": "us-east-1",
        "image_id": "ami-example",
        "template_revision": "r1",
        "instance_size": "small",
        "workspace_disk_gb": 30,
        "source_revision": "f" * 40,
        "kms_key_arn": "kms-example",
    }
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    outputs = {
        key: {"value": value}
        for key, value in (
            ("controller_role_arn", "arn:aws:iam::999999999999:role/foreign"),
            ("image_id", "ami-example"),
            ("template_revision", "r1"),
            ("source_revision", "f" * 40),
            ("launch_template_id", "lt-example"),
            ("launch_template_version", 1),
        )
    }
    assert validate_release(manifest, outputs, "test-deployment").template_id == "lt-example"
    with pytest.raises(ValueError, match="account"):
        controller_clients(None, manifest, outputs)
    outputs["image_id"]["value"] = "ami-other"
    with pytest.raises(ValueError, match="image"):
        validate_release(manifest, outputs, "test-deployment")
    manifest["region"] = "us-west-2"
    with pytest.raises(ValueError, match="digest"):
        validate_release(manifest, outputs, "test-deployment")


class StateEC2:
    def __init__(self, instance):
        self.instance = instance
        self.actions = []

    def describe_instances(self, **kwargs):
        if kwargs["InstanceIds"] != [self.instance["InstanceId"]]:
            return {"Reservations": []}
        return {"Reservations": [{"Instances": [self.instance]}]}

    def stop_instances(self, **kwargs):
        self.actions.append(("stop", kwargs["InstanceIds"]))
        self.instance["State"] = {"Name": "stopped"}

    def start_instances(self, **kwargs):
        self.actions.append(("start", kwargs["InstanceIds"]))
        self.instance["State"] = {"Name": "running"}


async def admitted_running_instance(database, http):
    accepted = create(http).json()
    async with database() as db:
        operation = await db.get(DevBoxOperation, accepted["operation_id"])
        box = await db.get(DevBox, accepted["desktop_id"])
        operation.status = "succeeded"
        box.status = "running"
        box.observed_state = "running"
        tags = {
            "adp:component": "dev-box",
            "adp:owner-id": box.user_id,
            "adp:deployment-id": box.deployment_id,
            "adp:box-id": box.id,
            "adp:generation": "1",
        }
        db.add(
            DevBoxInstance(
                id="instance-one",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=1,
                template_revision="r1",
                provider_instance_id="i-original",
                observed_state="running",
            )
        )
        await db.commit()
    return accepted, StateEC2(
        {
            "InstanceId": "i-original",
            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            "State": {"Name": "running"},
        }
    )


@pytest.mark.asyncio
async def test_idle_stop_is_durable_and_worker_finishes_after_browser_exits(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.created_at = now - timedelta(minutes=30)
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now - timedelta(minutes=20)
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 1
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    async with database() as db:
        pending = await db.scalar(select(DevBoxOperation).where(DevBoxOperation.action == "stop"))
        assert pending.status == "pending" and pending.idempotency_key.startswith("auto-stop:idle:")
    assert await scan_once(database, ec2, None, "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "restarted-worker") == 1
    assert ec2.actions == [("stop", ["i-original"])]
    async with database() as db:
        assert (await db.get(DevBoxOperation, pending.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_host_activity_and_assistant_busy_timeout_are_independent(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.worker import schedule_stops

    http = client(database, context("alice-sub"))
    setup, _ = await admitted_running_instance(database, http)
    identifier = setup["desktop_id"]
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.created_at = now - timedelta(minutes=30)
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now - timedelta(minutes=20)
        await db.commit()
    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "host"}).status_code == 202
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "assistant", "busy": True}).status_code == 202
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now - timedelta(minutes=20)
        box.assistant_busy_until_at = now + timedelta(minutes=1)
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    assert await schedule_stops(database, "test-deployment", now=now + timedelta(minutes=2)) == 1
    async with database() as db:
        events = list((await db.scalars(select(DevBoxEvent.event_type))).all())
        assert "assistant_busy_timeout" in events


@pytest.mark.asyncio
async def test_owner_heartbeat_preserves_developer_activity_and_explicit_consolidation(database):
    http = client(database, context("alice-sub"))
    setup, _ = await admitted_running_instance(database, http)
    path = f"/cloud-desktops/{setup['desktop_id']}/activity"
    assert http.post(path, json={"kind": "host", "consolidation_complete": True}).status_code == 422
    assert http.post(path, json={"kind": "assistant", "busy": True, "consolidation_complete": True}).status_code == 422
    assert client(database, context("other-sub", "two")).post(path, json={"kind": "host"}).status_code == 404
    assert http.post(path, json={"kind": "host"}).status_code == 202
    async with database() as db:
        developer_at = (await db.get(DevBox, setup["desktop_id"])).last_activity_at
    assert http.post(path, json={"kind": "assistant", "busy": True, "consolidation_complete": False}).status_code == 202
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        assert box.last_activity_at == developer_at
        assert box.assistant_consolidated_at is None and box.assistant_busy_until_at is not None
    assert http.post(path, json={"kind": "assistant", "busy": False}).status_code == 202
    async with database() as db:
        assert (await db.get(DevBox, setup["desktop_id"])).assistant_consolidated_at is None
    assert http.post(path, json={"kind": "assistant", "busy": False, "consolidation_complete": True}).status_code == 202
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        assert box.last_activity_at == developer_at
        assert box.assistant_consolidated_at is not None and box.assistant_busy_until_at is None


@pytest.mark.asyncio
async def test_idle_stop_waits_for_both_authenticated_activity_times(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.worker import schedule_stops

    http = client(database, context("alice-sub"))
    setup, _ = await admitted_running_instance(database, http)
    path = f"/cloud-desktops/{setup['desktop_id']}/activity"
    assert http.post(path, json={"kind": "assistant", "busy": False, "consolidation_complete": True}).status_code == 202
    assert http.post(path, json={"kind": "host"}).status_code == 202
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.created_at = now - timedelta(minutes=30)
        box.assistant_activity_at = now - timedelta(minutes=20)
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.assistant_activity_at = now - timedelta(minutes=20)
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 1
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        assert box.desired_state == "stopped"
        operation = await db.scalar(select(DevBoxOperation).where(DevBoxOperation.box_id == box.id, DevBoxOperation.action == "stop"))
        assert operation and operation.status == "pending"


@pytest.mark.asyncio
async def test_assistant_completion_cannot_stop_a_developer_active_box(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    identifier = setup["desktop_id"]
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.created_at = now - timedelta(minutes=30)
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now - timedelta(minutes=20)
        await db.commit()

    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "host"}).status_code == 202
    async with database() as db:
        developer_activity = (await db.get(DevBox, identifier)).last_activity_at
    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "assistant", "busy": True}).status_code == 202
    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "assistant", "busy": False}).status_code == 202
    async with database() as db:
        box = await db.get(DevBox, identifier)
        assert box.last_activity_at == developer_activity
        assert box.assistant_busy_until_at is None
        assert box.desired_state == "running" and box.observed_state == "running"
    assert await schedule_stops(database, "test-deployment", now=now + timedelta(minutes=1)) == 0
    assert await scan_once(database, ec2, None, "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "worker") == 0
    assert ec2.actions == []
    async with database() as db:
        assert await db.scalar(select(DevBoxOperation).where(DevBoxOperation.box_id == identifier, DevBoxOperation.action == "stop")) is None


@pytest.mark.asyncio
async def test_fresh_activity_cancels_queued_idle_stop_without_mutating_provider(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    identifier = setup["desktop_id"]
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.created_at = datetime.now(UTC) - timedelta(minutes=30)
        box.last_activity_at = datetime.now(UTC) - timedelta(minutes=20)
        await db.commit()
    assert await schedule_stops(database, "test-deployment") == 1
    assert http.post(f"/cloud-desktops/{identifier}/activity", json={"kind": "host"}).status_code == 202
    assert await scan_once(database, ec2, None, "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "worker") == 0
    assert ec2.actions == []
    async with database() as db:
        box = await db.get(DevBox, identifier)
        stop = await db.scalar(select(DevBoxOperation).where(DevBoxOperation.action == "stop"))
        assert box.desired_state == "running" and stop.error_class == "superseded_by_activity"


@pytest.mark.asyncio
async def test_lease_expiry_stops_even_recently_active_host(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.lease_expires_at = now - timedelta(seconds=1)
        box.last_activity_at = now
        box.assistant_busy_until_at = now + timedelta(minutes=10)
        await db.commit()
    assert http.post(f"/cloud-desktops/{setup['desktop_id']}/activity", json={"kind": "host"}).status_code == 409
    assert await schedule_stops(database, "test-deployment", now=now) == 1
    async with database() as db:
        assert "assistant_busy_interrupted" in list((await db.scalars(select(DevBoxEvent.event_type))).all())
    assert await scan_once(database, ec2, None, "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "worker") == 1
    assert ec2.actions == [("stop", ["i-original"])]


@pytest.mark.asyncio
async def test_activity_is_rejected_after_provider_stop_intent(database, monkeypatch):
    import threading

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    started = threading.Event()
    resume = threading.Event()

    def delayed_stop(**kwargs):
        started.set()
        if not resume.wait(timeout=5):
            raise TimeoutError("Timed out waiting for concurrent activity")
        return StateEC2.stop_instances(ec2, **kwargs)

    monkeypatch.setattr(ec2, "stop_instances", delayed_stop)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.created_at = datetime.now(UTC) - timedelta(minutes=30)
        box.last_activity_at = datetime.now(UTC) - timedelta(minutes=20)
        await db.commit()
    assert await schedule_stops(database, "test-deployment") == 1
    worker = asyncio.create_task(scan_once(database, ec2, None, "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "worker"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        result = await asyncio.to_thread(http.post, f"/cloud-desktops/{setup['desktop_id']}/activity", json={"kind": "host"})
        assert result.status_code == 409
    finally:
        resume.set()
        await worker
    assert ec2.actions == [("stop", ["i-original"])]


@pytest.mark.asyncio
async def test_pending_work_defers_stop_and_guest_without_fence_stays_unknown(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once, schedule_stops

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    now = datetime.now(UTC)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.created_at = now - timedelta(minutes=30)
        box.last_activity_at = now - timedelta(minutes=20)
        box.auth_readiness = "machine_registered"
        db.add(
            DevBoxOperation(
                id="outstanding",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                action="start",
                status="pending",
                actor_user_id=box.user_id,
                idempotency_key="outstanding",
                request_digest="e" * 64,
                provider_token="f" * 64,
            )
        )
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 0
    async with database() as db:
        (await db.get(DevBoxOperation, "outstanding")).status = "succeeded"
        await db.commit()
    assert await schedule_stops(database, "test-deployment", now=now) == 1
    launch = LaunchTemplate("lt-example", "1", "us-east-1")
    assert await scan_once(database, ec2, None, "test-deployment", launch, "worker") == 1
    assert ec2.actions == []
    calls = []

    async def fence(db, box, action):
        # The fence runs inside the reconcile transaction: it receives the caller's session
        # and the already-locked box, never identifiers to re-fetch and re-lock.
        assert db is not None and box.id == setup["desktop_id"]
        calls.append((box.id, box.machine_generation, action))
        return True

    assert await scan_once(database, ec2, None, "test-deployment", launch, "worker", fence=fence) == 1
    assert calls == [(setup["desktop_id"], 1, "stop")]
    assert ec2.actions == [("stop", ["i-original"])]


@pytest.mark.asyncio
async def test_controller_races_stop_and_start_without_flapping(concurrent_database, monkeypatch):
    import asyncio

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    http = client(concurrent_database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(concurrent_database, http)
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-once"})
    assert stopped.status_code == 202
    assert http.post(f"/cloud-desktops/{identifier}/start", headers={"Idempotency-Key": "start-racing"}).status_code == 409
    await asyncio.gather(
        *(
            scan_once(concurrent_database, ec2, FakeSSM(), "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), worker)
            for worker in ("worker-one", "worker-two")
        )
    )
    assert ec2.actions == [("stop", ["i-original"])]
    assert http.get(f"/cloud-desktops/{identifier}/operations/{stopped.json()['operation_id']}").json()["status"] == "succeeded"
    assert http.get(f"/cloud-desktops/{identifier}").json()["observed_state"] == "stopped"


@pytest.mark.asyncio
async def test_stale_generation_and_revoked_owner_cannot_mutate_replacement(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.fencing import claim
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    stopped = http.post(f"/cloud-desktops/{setup['desktop_id']}/stop", headers={"Idempotency-Key": "stop-stale"}).json()
    lease = await claim(database, stopped["operation_id"], "old-worker", ("stop",))
    assert lease is not None
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        box.machine_generation = 2
        box.desired_state = "stopped"
        operation = await db.get(DevBoxOperation, stopped["operation_id"])
        operation.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "new-worker") == 1
    assert ec2.actions == []
    assert http.get(f"/cloud-desktops/{setup['desktop_id']}/operations/{stopped['operation_id']}").json()["error_class"] == "stale_generation"
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        assert box.machine_generation == 2
        assert box.observed_state == "running"
        member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
        member.revoked_at = datetime.now(UTC)
        await db.commit()
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "old-worker") == 1
    assert ec2.actions == []


@pytest.mark.asyncio
async def test_delayed_stop_rechecks_membership_before_provider_call(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    stopped = http.post(f"/cloud-desktops/{setup['desktop_id']}/stop", headers={"Idempotency-Key": "stop-revoked"}).json()
    async with database() as db:
        member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
        member.revoked_at = datetime.now(UTC)
        await db.commit()
    await scan_once(database, ec2, FakeSSM(), "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "worker")
    assert ec2.actions == []
    operation_url = f"/cloud-desktops/{setup['desktop_id']}/operations/{stopped['operation_id']}"
    assert http.get(operation_url).status_code == 403
    async with database() as db:
        operation = await db.get(DevBoxOperation, stopped["operation_id"])
        box = await db.get(DevBox, setup["desktop_id"])
        assert operation.error_class == "owner_membership_revoked"
        assert box.observed_state == "running"


@pytest.mark.asyncio
async def test_inflight_old_worker_cannot_commit_after_replacement(database, monkeypatch):
    import asyncio

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    http = client(database, context("alice-sub"))
    setup, ec2 = await admitted_running_instance(database, http)
    stopped = http.post(f"/cloud-desktops/{setup['desktop_id']}/stop", headers={"Idempotency-Key": "stop-inflight"}).json()
    loop = asyncio.get_running_loop()

    async def retire():
        async with database() as db:
            box = await db.get(DevBox, setup["desktop_id"])
            box.machine_generation = 2
            db.add(
                DevBoxInstance(
                    id="replacement",
                    box_id=box.id,
                    user_id=box.user_id,
                    org_id=box.org_id,
                    deployment_id=box.deployment_id,
                    machine_generation=2,
                    template_revision="r1",
                    provider_instance_id="i-replacement",
                    observed_state="running",
                )
            )
            await db.commit()

    original_stop = ec2.stop_instances

    def replace_during_stop(**kwargs):
        original_stop(**kwargs)
        asyncio.run_coroutine_threadsafe(retire(), loop).result(timeout=5)

    ec2.stop_instances = replace_during_stop
    await scan_once(database, ec2, FakeSSM(), "test-deployment", LaunchTemplate("lt-example", "1", "us-east-1"), "old-worker")
    assert ec2.actions == [("stop", ["i-original"])]
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        operation = await db.get(DevBoxOperation, stopped["operation_id"])
        replacement = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == 2))
        assert box.machine_generation == 2
        assert box.observed_state == "running"
        assert operation.error_class == "stale_generation"
        assert replacement.observed_state == "running"


async def boot_workspace(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    async with database() as db:
        template = await db.get(DevBoxTemplate, "t1")
        template.image_ref = "ami-example"
        await db.commit()
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    ec2 = FakeEC2()
    launch_template = LaunchTemplate("lt-example", "1", "us-east-1", "ami-example", "r1", "small", 30, "kms-example")
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    ec2.visible = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    assert http.get(f"/cloud-desktops/{setup['desktop_id']}/operations/{setup['operation_id']}").json()["status"] == "succeeded"
    return http, setup, ec2, launch_template, scan_once


@pytest.mark.asyncio
async def test_idle_stop_then_explicit_retain_release_keeps_owned_workspace_and_cost(database, monkeypatch):
    from controller.usage import observe_resource
    from controller.worker import schedule_stops

    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    ec2.volume["Files"] = {"memory.db": "acknowledged owner work"}
    now = datetime.now(UTC)

    # This assertion checks that release preserves already-metered cost. Keep
    # metering at one instant; wall time between scans legitimately adds storage
    # cost, which made exact equality depend on how busy the CI runner was.
    class MeteringClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("controller.usage.datetime", MeteringClock)
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.created_at = now - timedelta(minutes=30)
        box.last_activity_at = now - timedelta(minutes=20)
        box.assistant_activity_at = now - timedelta(minutes=20)
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        disk.last_observed_at = now - timedelta(hours=1)
        disk.last_observed_state = "present"
        disk.last_rate_usd = Decimal("0.0001")
        await db.commit()

    assert await observe_resource(database, ec2, disk.id, launch_template, now=now)
    assert await schedule_stops(database, "test-deployment", now=now) == 1
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one") == 1
    assert ("stop", "i-example-owned") in ec2.actions
    assert ec2.volume["Files"] == {"memory.db": "acknowledged owner work"}
    before = http.get(f"/cloud-desktops/{identifier}").json()["infrastructure_cost"]
    assert Decimal(before["by_kind"]["workspace_disk"]["known_cost_usd"]) > 0
    assert any(resource["kind"] == "workspace_disk" for resource in before["retained_resources"])

    assert http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "no-choice"}).status_code == 422
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "retain-after-idle"}, json={"workspace_retention": "retain"}
    )
    assert release.status_code == 202
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two") == 1
    assert http.get(f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}").json()["status"] == "succeeded"
    after = http.get(f"/cloud-desktops/{identifier}").json()
    assert after["status"] == "released"
    assert after["infrastructure_cost"]["by_kind"]["workspace_disk"]["known_cost_usd"] == before["by_kind"]["workspace_disk"]["known_cost_usd"]
    assert any(resource["kind"] == "workspace_disk" for resource in after["infrastructure_cost"]["retained_resources"])
    assert ec2.volume["Files"] == {"memory.db": "acknowledged owner work"}
    assert ("delete_volume", "vol-owned-example") not in ec2.actions
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1


@pytest.mark.asyncio
async def test_cost_summary_aggregates_usage_in_sql_without_loading_rows(database, monkeypatch):
    """#9548: a two-week-old box on dev owned 939 232 usage rows (one per resource every ~7 s) and every
    GET /cloud-desktops read loaded them all, taking ~52 s. The summary must come from grouped sums."""
    from sqlalchemy import event

    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    start = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        resources = list((await db.scalars(select(DevBoxResource))).all())
        machine = next(item for item in resources if item.resource_kind == "instance")
        disk = next(item for item in resources if item.resource_kind == "workspace_disk")
        for resource in (machine, disk):
            resource.last_observed_at = start
            resource.last_observed_state = "running" if resource is machine else "present"
        for index in range(1_200):
            resource = machine if index % 3 else disk
            interval_start = start + timedelta(seconds=7 * index)
            db.add(
                DevBoxUsage(
                    id=f"usage-{index}",
                    box_id=box.id,
                    resource_id=resource.id,
                    user_id=box.user_id,
                    org_id=box.org_id,
                    deployment_id=box.deployment_id,
                    machine_generation=1,
                    interval_start=interval_start,
                    interval_end=interval_start + timedelta(seconds=7),
                    units=Decimal("0.001944"),
                    unit_name="hour" if resource is machine else "gb_hour",
                    rate_usd=Decimal("0.1"),
                    cost_usd=Decimal("0.000194") if index % 2 else Decimal("0.000388"),
                    source_revision="policy:p1:test",
                    reconciled=index % 5 == 0,
                    currency="USD",
                )
            )
        await db.commit()
        expected = {
            (reconciled, kind): total
            for reconciled, kind, total in (
                await db.execute(
                    select(DevBoxUsage.reconciled, DevBoxResource.resource_kind, func.sum(DevBoxUsage.cost_usd))
                    .join(DevBoxResource, DevBoxResource.id == DevBoxUsage.resource_id)
                    .group_by(DevBoxUsage.reconciled, DevBoxResource.resource_kind)
                )
            ).all()
        }

    loaded = []

    def note_load(target, context):
        loaded.append(target.id)

    event.listen(DevBoxUsage, "load", note_load)
    try:
        cost = http.get(f"/cloud-desktops/{box.id}").json()["infrastructure_cost"]
    finally:
        event.remove(DevBoxUsage, "load", note_load)
    assert loaded == [], "the summary must not materialise usage rows"
    money = lambda value: str(Decimal(str(value or 0)).quantize(Decimal("0.000001")))  # noqa: E731
    estimated = sum(total for (reconciled, _), total in expected.items() if not reconciled)
    reconciled_total = sum(total for (reconciled, _), total in expected.items() if reconciled)
    assert cost["estimated_cost_usd"] == money(estimated)
    assert cost["reconciled_cost_usd"] == money(reconciled_total)
    assert cost["known_cost_usd"] == money(estimated + reconciled_total)
    for kind in ("instance", "workspace_disk"):
        assert cost["by_kind"][kind]["known_cost_usd"] == money(sum(total for (_, k), total in expected.items() if k == kind))
        assert cost["by_kind"][kind]["estimated_cost_usd"] == money(
            sum(total for (reconciled, k), total in expected.items() if k == kind and not reconciled)
        )
    assert cost["by_kind"]["instance"]["covered_until"] == (start + timedelta(seconds=7 * 1_200)).replace(tzinfo=None).isoformat()
    assert cost["estimated_through"] is not None and cost["reconciled_through"] is not None


@pytest.mark.asyncio
async def test_provider_usage_keeps_retained_storage_after_verified_compute_stop(database, monkeypatch):
    from controller.usage import observe_resource

    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    start = datetime.now(UTC) - timedelta(minutes=1)
    async with database() as db:
        box = await db.get(DevBox, setup["desktop_id"])
        policy = await db.get(DevBoxPolicy, "p1")
        resources = list((await db.scalars(select(DevBoxResource))).all())
        machine = next(item for item in resources if item.resource_kind == "instance")
        disk = next(item for item in resources if item.resource_kind == "workspace_disk")
        snapshot = DevBoxResource(
            id="snapshot-usage",
            box_id=box.id,
            user_id=box.user_id,
            org_id=box.org_id,
            deployment_id=box.deployment_id,
            machine_generation=1,
            resource_kind="snapshot",
            provider_resource_id="snap-owned-example",
            size_gb=8,
            retention_state="retained",
            deletion_state="present",
        )
        db.add(snapshot)
        for resource, state, rate, hours in (
            (machine, "running", policy.instance_hour_rate_usd, 2),
            (disk, "present", policy.disk_gb_hour_rate_usd, 2),
            (snapshot, "present", policy.snapshot_gb_hour_rate_usd, 3),
        ):
            resource.last_observed_at = start - timedelta(hours=hours)
            resource.last_observed_state = state
            resource.last_rate_usd = rate
        await db.commit()

    def snapshots(**kwargs):
        assert kwargs["SnapshotIds"] == ["snap-owned-example"]
        return {
            "Snapshots": [
                {
                    "SnapshotId": "snap-owned-example",
                    "VolumeSize": 8,
                    "Tags": [
                        {"Key": key, "Value": value}
                        for key, value in {
                            "adp:component": "dev-box",
                            "adp:deployment-id": box.deployment_id,
                            "adp:owner-id": box.user_id,
                            "adp:box-id": box.id,
                            "adp:generation": "1",
                        }.items()
                    ],
                }
            ]
        }

    monkeypatch.setattr(ec2, "describe_snapshots", snapshots, raising=False)
    for resource_id in (machine.id, disk.id, snapshot.id):
        assert await observe_resource(database, ec2, resource_id, launch_template, now=start)
    async with database() as db:
        totals = {
            kind: await db.scalar(
                select(func.sum(DevBoxUsage.cost_usd))
                .join(DevBoxResource, DevBoxResource.id == DevBoxUsage.resource_id)
                .where(DevBoxResource.resource_kind == kind)
            )
            for kind in ("instance", "workspace_disk", "snapshot")
        }
        assert totals == {"instance": Decimal("0.200000"), "workspace_disk": Decimal("0.006000"), "snapshot": Decimal("0.002400")}
        snapshot_usage = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == snapshot.id))).all())
        assert len(snapshot_usage) == 1
        assert snapshot_usage[0].units == Decimal("24.000000") and snapshot_usage[0].unit_name == "gb_hour"
        assert snapshot_usage[0].rate_usd == policy.snapshot_gb_hour_rate_usd
    owner_cost = http.get(f"/cloud-desktops/{box.id}").json()["infrastructure_cost"]
    assert owner_cost["by_kind"]["snapshot"] == {
        "known_cost_usd": "0.002400",
        "estimated_cost_usd": "0.002400",
        "covered_until": (start.replace(tzinfo=None)).isoformat(),
        "storage_gb": 8,
        "rate_usd_per_gb_hour": "0.000100",
    }
    assert owner_cost["total_cost_usd"] is not None
    async with database() as db:
        resource = await db.get(DevBoxResource, snapshot.id)
        resource.size_gb = None
        await db.commit()
    unknown_cost = http.get(f"/cloud-desktops/{box.id}").json()["infrastructure_cost"]
    assert unknown_cost["by_kind"]["snapshot"]["storage_gb"] is None
    assert unknown_cost["coverage"] == "partial_unknown" and unknown_cost["total_cost_usd"] is None
    async with database() as db:
        resource = await db.get(DevBoxResource, snapshot.id)
        policy = await db.get(DevBoxPolicy, "p1")
        resource.size_gb = 8
        policy.snapshot_gb_hour_rate_usd = None
        await db.commit()
    unknown_rate = http.get(f"/cloud-desktops/{box.id}").json()["infrastructure_cost"]
    assert unknown_rate["by_kind"]["snapshot"]["rate_usd_per_gb_hour"] is None
    assert unknown_rate["coverage"] == "partial_unknown" and unknown_rate["total_cost_usd"] is None
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.snapshot_gb_hour_rate_usd = Decimal("0.0001")
        await db.commit()
    observation_times = {"running": start + timedelta(minutes=1), "stopped": start + timedelta(minutes=2)}

    class UsageClock:
        @staticmethod
        def now(timezone):
            return observation_times[ec2.instance["State"]["Name"]].astimezone(timezone)

    monkeypatch.setattr("controller.usage.datetime", UsageClock)
    stopped = http.post(f"/cloud-desktops/{box.id}/stop", headers={"Idempotency-Key": "stop-cost"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker")
    async with database() as db:
        assert (await db.get(DevBoxResource, machine.id)).last_observed_state == "stopped"
    assert await observe_resource(database, ec2, disk.id, launch_template, now=start + timedelta(hours=1))
    assert await observe_resource(database, ec2, snapshot.id, launch_template, now=start + timedelta(hours=1))
    async with database() as db:
        compute_rows = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == machine.id))).all())
        disk_total = await db.scalar(select(func.sum(DevBoxUsage.cost_usd)).where(DevBoxUsage.resource_id == disk.id))
        assert max(row.interval_end for row in compute_rows) == observation_times["stopped"].replace(tzinfo=None)
        assert disk_total == Decimal("0.009000")
    observation_times.update({"stopped": start + timedelta(hours=1, minutes=1), "terminated": start + timedelta(hours=1, minutes=2)})
    release = http.post(f"/cloud-desktops/{box.id}/release", headers={"Idempotency-Key": "retain-cost"}, json={"workspace_retention": "retain"})
    assert release.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker")
    async with database() as db:
        assert (await db.get(DevBox, box.id)).status == "released"
        assert (await db.get(DevBoxResource, disk.id)).deletion_state == "present"
    assert await observe_resource(database, ec2, disk.id, launch_template, now=start + timedelta(hours=2))


@pytest.mark.asyncio
async def test_owner_status_shows_known_cost_retained_storage_and_policy_reason_without_provider_secrets(database, monkeypatch):
    from controller.usage import observe_resource

    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    now = datetime.now(UTC)
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        disk.last_observed_at = now - timedelta(hours=2)
        disk.last_observed_state = "present"
        disk.last_rate_usd = Decimal("0.0001")
        await db.commit()
    assert await observe_resource(database, ec2, disk.id, launch_template, now=now)
    first = http.get(f"/cloud-desktops/{setup['desktop_id']}")
    assert first.status_code == 200
    cost = first.json()["infrastructure_cost"]
    assert Decimal(cost["known_cost_usd"]) >= Decimal("0.006000")
    assert cost["reconciled_cost_usd"] is None
    assert cost["retained_resources"][0]["kind"] == "workspace_disk"
    assert "vol-owned-example" not in first.text and "kms-example" not in first.text
    released = http.post(
        f"/cloud-desktops/{setup['desktop_id']}/release", headers={"Idempotency-Key": "retain-status"}, json={"workspace_retention": "retain"}
    )
    assert released.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker")
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.enabled = False
        await db.commit()
    final = http.get(f"/cloud-desktops/{setup['desktop_id']}")
    assert final.json()["infrastructure_cost"]["policy_denial_reason"] == "admission_disabled"
    assert final.json()["infrastructure_cost"]["retained_resources"][0]["deletion_state"] == "present"
    assert Decimal(final.json()["infrastructure_cost"]["known_cost_usd"]) > Decimal("0")


@pytest.mark.asyncio
async def test_policy_status_reports_denial_without_exposing_another_owner(database, monkeypatch):
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    other = client(database, context("other-sub", "two"))
    assert alice.get("/cloud-desktops/policy").json() == {
        "contract_version": 1,
        "admission_allowed": True,
        "reason": None,
    }
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.budget_usd = Decimal("0.10")
        await db.commit()
    denied = alice.get("/cloud-desktops/policy")
    assert denied.status_code == 200
    assert denied.json()["reason"] == "Dev Box budget allowance exhausted"
    assert create(alice).status_code == 403
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "invalid")
    assert alice.get("/cloud-desktops/policy").json()["reason"] == "Dev Box deployment budget is not configured"
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "100")
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.budget_usd = Decimal("50")
        await db.commit()
    setup = create(alice).json()
    assert bob.get("/cloud-desktops/policy").json()["reason"] == "Dev Box live capacity reached"
    assert other.get("/cloud-desktops/policy").json()["admission_allowed"] is True
    assert bob.get(f"/cloud-desktops/{setup['desktop_id']}").status_code == 404
    assert bob.get("/cloud-desktops").json()["items"] == []
    async with database() as db:
        membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
        membership.revoked_at = datetime.now(UTC)
        await db.commit()
    assert alice.get("/cloud-desktops/policy").status_code == 403
    assert alice.get(f"/cloud-desktops/{setup['desktop_id']}").status_code == 403


@pytest.mark.asyncio
async def test_missing_provider_observation_leaves_gap_unknown_not_free_or_reconciled(database, monkeypatch):
    from controller.usage import observe_resource

    http, setup, ec2, launch_template, _ = await boot_workspace(database, monkeypatch)
    start = datetime.now(UTC) - timedelta(hours=4)
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        disk.last_observed_at = start
        disk.last_observed_state = "present"
        disk.last_rate_usd = Decimal("0.0001")
        await db.commit()
    normal_observation = ec2.describe_volumes

    def provider_unavailable(**kwargs):
        raise ConnectionError("Billing-period provider inventory is unavailable")

    monkeypatch.setattr(ec2, "describe_volumes", provider_unavailable)
    assert not await observe_resource(database, ec2, disk.id, launch_template, now=start + timedelta(hours=1))
    monkeypatch.setattr(ec2, "describe_volumes", normal_observation)
    assert await observe_resource(database, ec2, disk.id, launch_template, now=start + timedelta(hours=2))
    async with database() as db:
        assert (await db.get(DevBoxResource, disk.id)).last_observed_state == "present"
        assert await db.scalar(select(func.count(DevBoxUsage.id)).where(DevBoxUsage.resource_id == disk.id)) == 0
    assert await observe_resource(database, ec2, disk.id, launch_template, now=start + timedelta(hours=3))
    async with database() as db:
        rows = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == disk.id))).all())
        assert len(rows) == 1
        assert rows[0].interval_start == (start + timedelta(hours=2)).replace(tzinfo=None)
        assert rows[0].cost_usd == Decimal("0.003000")
        assert rows[0].reconciled is False
    status = http.get(f"/cloud-desktops/{setup['desktop_id']}").json()["infrastructure_cost"]
    assert status["coverage"] == "partial_unknown"
    assert status["total_cost_usd"] is None
    assert Decimal(status["known_cost_usd"]) >= Decimal("0.003000")


@pytest.mark.asyncio
async def test_partial_cleanup_retains_attribution_until_provider_confirms_disk_deletion(database, monkeypatch):
    from controller.usage import observe_resource

    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    start = datetime.now(UTC) - timedelta(hours=1)
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        disk.last_observed_at = start
        disk.last_observed_state = "present"
        disk.last_rate_usd = Decimal("0.0001")
        await db.commit()
    release = http.post(
        f"/cloud-desktops/{setup['desktop_id']}/release", headers={"Idempotency-Key": "delete-with-cost"}, json={"workspace_retention": "delete"}
    )
    assert release.status_code == 202
    ec2.lose_termination = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "first-worker")
    async with database() as db:
        pending = await db.get(DevBoxOperation, release.json()["operation_id"])
        recorded = await db.get(DevBoxResource, disk.id)
        total = await db.scalar(select(func.sum(DevBoxUsage.cost_usd)).where(DevBoxUsage.resource_id == disk.id))
        assert pending.status == "unknown"
        assert recorded.deletion_state == "present" and recorded.last_observed_state == "present"
        assert total >= Decimal("0.003000")
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "second-worker")
    async with database() as db:
        assert (await db.get(DevBoxOperation, release.json()["operation_id"])).status == "succeeded"
        assert (await db.get(DevBoxResource, disk.id)).deletion_state == "verified_deleted"
    assert not await observe_resource(database, ec2, disk.id, launch_template, now=datetime.now(UTC) + timedelta(days=1))


@pytest.mark.asyncio
async def test_owned_encrypted_workspace_survives_stop_start_and_replacement(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    ec2.volume["Files"] = {"draft.txt": "unchanged"}
    identifier = setup["desktop_id"]
    async with database() as db:
        resources = list(await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk")))
        assert len(resources) == 1
        assert resources[0].provider_resource_id == "vol-owned-example"
        assert resources[0].encrypted is True
        assert resources[0].retention_state == "retained"
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-disk"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    assert http.get(f"/cloud-desktops/{identifier}").json()["observed_state"] == "stopped"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-example-owned"
    started = http.post(f"/cloud-desktops/{identifier}/start", headers={"Idempotency-Key": "start-disk"})
    assert started.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    assert http.get(f"/cloud-desktops/{identifier}/operations/{started.json()['operation_id']}").json()["status"] == "succeeded"
    assert ec2.volume["Files"] == {"draft.txt": "unchanged"}
    stopped_again = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-replace"})
    assert stopped_again.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    replaced = http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-disk"})
    assert replaced.status_code == 202
    assert http.get(f"/cloud-desktops/{identifier}").json()["machine_generation"] == 2
    async with database() as db:
        assert (await db.get(CrewOwnerGeneration, ("one", "alice"))).generation == 2
    ec2.lose_response = False
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    assert http.get(f"/cloud-desktops/{identifier}/operations/{replaced.json()['operation_id']}").json()["status"] == "succeeded"
    assert ec2.volume["Attachments"] == [{"InstanceId": "i-example-replacement", "State": "attached"}]
    assert ec2.volume["Files"] == {"draft.txt": "unchanged"}
    assert ec2.actions.count(("detach", "i-example-owned")) == 1
    async with database() as db:
        instances = list(await db.scalars(select(DevBoxInstance).order_by(DevBoxInstance.machine_generation)))
        resources = list(await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk")))
        assert [(item.machine_generation, item.provider_instance_id) for item in instances] == [
            (1, "i-example-owned"),
            (2, "i-example-replacement"),
        ]
        assert len(resources) == 1 and resources[0].machine_generation == 1
        assert resources[0].retention_state == "retained"


@pytest.mark.parametrize("corruption", ("foreign_owner", "wrong_key", "foreign_attachment", "duplicate_record"))
@pytest.mark.asyncio
async def test_start_refuses_foreign_or_incompatible_workspace(database, monkeypatch, corruption):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-before-denial"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    if corruption == "foreign_owner":
        next(tag for tag in ec2.volume["Tags"] if tag["Key"] == "adp:owner-id")["Value"] = "foreign-owner"
    elif corruption == "wrong_key":
        ec2.volume["KmsKeyId"] = "kms-foreign"
    elif corruption == "foreign_attachment":
        ec2.volume["Attachments"][0]["InstanceId"] = "i-foreign"
    else:
        async with database() as db:
            disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
            db.add(
                DevBoxResource(
                    id="another-disk",
                    box_id=disk.box_id,
                    user_id=disk.user_id,
                    org_id=disk.org_id,
                    deployment_id=disk.deployment_id,
                    machine_generation=disk.machine_generation,
                    resource_kind="workspace_disk",
                    provider_resource_id="vol-another-disk",
                    size_gb=30,
                    encrypted=True,
                    retention_state="retained",
                    deletion_state="present",
                )
            )
            await db.commit()
    started = http.post(f"/cloud-desktops/{identifier}/start", headers={"Idempotency-Key": "start-denied"})
    assert started.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    assert ("start", "i-example-owned") not in ec2.actions
    assert http.get(f"/cloud-desktops/{identifier}/operations/{started.json()['operation_id']}").json()["status"] == "unknown"
    assert ec2.volume.get("Files", {}) == {}


@pytest.mark.asyncio
async def test_lost_attachment_response_reconciles_without_second_attach(database, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    async with database() as db:
        template = await db.get(DevBoxTemplate, "t1")
        template.image_ref = "ami-example"
        await db.commit()
    http = client(database, context("alice-sub"))
    setup = create(http).json()
    ec2 = FakeEC2()
    ec2.lose_attachment = True
    launch_template = LaunchTemplate("lt-example", "1", "us-east-1", "ami-example", "r1", "small", 30, "kms-example")
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    ec2.visible = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    operation_url = f"/cloud-desktops/{setup['desktop_id']}/operations/{setup['operation_id']}"
    assert http.get(operation_url).json()["status"] == "unknown"
    assert ec2.actions == [("attach", "i-example-owned")]
    ec2.lose_attachment = False
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-three")
    assert http.get(operation_url).json()["status"] == "succeeded"
    assert ec2.actions == [("attach", "i-example-owned")]


@pytest.mark.asyncio
async def test_replacement_recovers_lost_detach_response_without_relaunch(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    ec2.volume["Files"] = {"work.txt": "retained"}
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-for-retry"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    replaced = http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-after-retry"}).json()
    ec2.lose_detachment = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    operation_url = f"/cloud-desktops/{identifier}/operations/{replaced['operation_id']}"
    assert http.get(operation_url).json()["status"] == "unknown"
    assert len(ec2.launches) == 1
    assert ec2.actions.count(("detach", "i-example-owned")) == 1
    ec2.lose_detachment = False
    ec2.lose_response = False
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-three")
    assert http.get(operation_url).json()["status"] == "succeeded"
    assert len(ec2.launches) == 2
    assert ec2.actions.count(("detach", "i-example-owned")) == 1
    assert ec2.volume["Files"] == {"work.txt": "retained"}


@pytest.mark.asyncio
async def test_guest_requires_identity_fence_before_stop(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-guest"}).json()
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.auth_readiness = "ready"
        await db.commit()
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    assert ("stop", "i-example-owned") not in ec2.actions
    assert http.get(f"/cloud-desktops/{identifier}/operations/{stopped['operation_id']}").json()["error_class"] == "identity_fence_required"
    assert http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-guest"}).status_code == 409


@pytest.mark.asyncio
async def test_release_retries_partial_cleanup_without_touching_foreign_inventory(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-for-release"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    replaced = http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-for-release"})
    assert replaced.status_code == 202
    ec2.lose_response = False
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    ec2.instances["i-foreign"] = {"InstanceId": "i-foreign", "State": {"Name": "running"}, "Tags": []}
    ec2.foreign_disk = {"VolumeId": "vol-foreign", "State": "available"}
    assert http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "missing-choice"}).status_code == 422
    release = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "delete-all"}, json={"workspace_retention": "delete"})
    assert release.status_code == 202
    operation_url = f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}"
    async with database() as db:
        operation = await db.get(DevBoxOperation, release.json()["operation_id"])
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        assert operation.retention_choice == "delete" and disk.retention_state == "delete_requested"
    ec2.lose_termination = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-three")
    assert http.get(operation_url).json()["status"] == "unknown"
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] != "released"
    assert ec2.actions.count(("terminate", "i-example-owned")) == 1
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-four")
    assert http.get(operation_url).json()["status"] == "succeeded"
    assert ec2.actions.count(("terminate", "i-example-owned")) == 1
    assert ec2.actions.count(("terminate", "i-example-replacement")) == 1
    assert ec2.actions.count(("delete_volume", "vol-owned-example")) == 1
    assert ec2.instances["i-foreign"]["State"]["Name"] == "running"
    assert ec2.foreign_disk["State"] == "available"
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        events = list(
            await db.scalars(
                select(DevBoxEvent).where(
                    DevBoxEvent.event_type.in_(
                        (
                            "instance_terminated",
                            "resource_deleted",
                            "release_verified",
                        )
                    )
                )
            )
        )
        assert disk.provider_resource_id == "vol-owned-example"
        assert disk.deletion_state == "verified_deleted" and disk.retention_state == "deleted"
        assert len(events) == 4
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] == "released"


@pytest.mark.asyncio
async def test_release_retain_keeps_owned_disk_attributed_and_reserved(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    ec2.volume["Files"] = {"workspace.txt": "keep"}
    release = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "retain-disk"}, json={"workspace_retention": "retain"})
    assert release.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    operation_url = f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}"
    assert http.get(operation_url).json()["status"] == "succeeded"
    assert ec2.volume["Files"] == {"workspace.txt": "keep"}
    assert ec2.volume["State"] == "available"
    visible = http.get(f"/cloud-desktops/{identifier}/resources")
    assert visible.status_code == 200
    assert next(item for item in visible.json()["items"] if item["resource_kind"] == "workspace_disk")["retention_state"] == "retained"
    foreign_owner = client(database, context("bob-sub"))
    assert foreign_owner.get(f"/cloud-desktops/{identifier}/resources").status_code == 404
    assert ("delete_volume", "vol-owned-example") not in ec2.actions
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] == "released"
    assert create(http, key="new-box-with-retained-disk").status_code == 403
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        assert disk.retention_state == "retained" and disk.deletion_state == "present"
        assert disk.provider_resource_id == "vol-owned-example"


@pytest.mark.parametrize("failure", ("foreign_owner", "lost_response", "still_present"))
@pytest.mark.asyncio
async def test_release_never_claims_deleted_until_owned_disk_disappears(database, monkeypatch, failure):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "delete-pending"}, json={"workspace_retention": "delete"}
    ).json()
    if failure == "foreign_owner":
        next(tag for tag in ec2.volume["Tags"] if tag["Key"] == "adp:owner-id")["Value"] = "foreign-owner"
    elif failure == "lost_response":
        ec2.lose_deletion = True
    else:
        ec2.delay_deletion = True
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    operation_url = f"/cloud-desktops/{identifier}/operations/{release['operation_id']}"
    assert http.get(operation_url).json()["status"] == "unknown"
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] != "released"
    async with database() as db:
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk"))
        assert disk.deletion_state != "verified_deleted"
    if failure == "foreign_owner":
        assert ("delete_volume", "vol-owned-example") not in ec2.actions
    elif failure == "lost_response":
        await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
        assert http.get(operation_url).json()["status"] == "succeeded"
        assert ec2.actions.count(("delete_volume", "vol-owned-example")) == 1
    else:
        ec2.delay_deletion = False
        del ec2.volume
        await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
        assert http.get(operation_url).json()["status"] == "succeeded"
        assert ec2.actions.count(("delete_volume", "vol-owned-example")) == 1


@pytest.mark.asyncio
async def test_retained_snapshot_stays_attributed_without_provider_deletion(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    async with database() as db:
        box = await db.get(DevBox, identifier)
        db.add(
            DevBoxResource(
                id="snapshot-inventory",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                resource_kind="snapshot",
                provider_resource_id="snap-example",
                retention_state="retained",
                deletion_state="present",
            )
        )
        await db.commit()
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "retain-snapshot"}, json={"workspace_retention": "retain"}
    )
    assert release.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    assert http.get(f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}").json()["status"] == "succeeded"
    async with database() as db:
        snapshot = await db.get(DevBoxResource, "snapshot-inventory")
        assert snapshot.provider_resource_id == "snap-example"
        assert snapshot.retention_state == "retained" and snapshot.deletion_state == "present"
    assert all(action[0] != "delete_snapshot" for action in ec2.actions)


@pytest.mark.asyncio
async def test_release_does_not_terminate_foreign_recorded_instance(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "foreign-machine"}, json={"workspace_retention": "delete"}
    )
    assert release.status_code == 202
    next(tag for tag in ec2.instance["Tags"] if tag["Key"] == "adp:owner-id")["Value"] = "foreign-owner"
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    result = http.get(f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}").json()
    assert result["status"] == "unknown" and result["error_class"] == "machine_ownership_mismatch"
    assert ("terminate", "i-example-owned") not in ec2.actions
    assert ("delete_volume", "vol-owned-example") not in ec2.actions
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] != "released"


@pytest.mark.asyncio
async def test_release_choice_is_required_and_idempotent(database, monkeypatch):
    http, setup, _, _, _ = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    url = f"/cloud-desktops/{identifier}/release"
    assert http.post(url, headers={"Idempotency-Key": "same-key"}).status_code == 422
    assert http.post(url, headers={"Idempotency-Key": "same-key"}, json={"workspace_retention": "invalid"}).status_code == 422
    accepted = http.post(url, headers={"Idempotency-Key": "same-key"}, json={"workspace_retention": "retain"})
    assert accepted.status_code == 202
    assert http.post(url, headers={"Idempotency-Key": "same-key"}, json={"workspace_retention": "retain"}).json() == accepted.json()
    assert http.post(url, headers={"Idempotency-Key": "same-key"}, json={"workspace_retention": "delete"}).status_code == 409
    async with database() as db:
        operation = await db.get(DevBoxOperation, accepted.json()["operation_id"])
        assert operation.retention_choice == "retain"


@pytest.mark.asyncio
async def test_release_remains_available_after_admissions_are_disabled(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    async with database() as db:
        policy = await db.get(DevBoxPolicy, "p1")
        policy.enabled = False
        await db.commit()
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "disabled-release"}, json={"workspace_retention": "retain"}
    )
    assert release.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    assert http.get(f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}").json()["status"] == "succeeded"
    assert ec2.volume["State"] == "available"
    assert create(http, key="disabled-new-setup").status_code == 403


@pytest.mark.asyncio
async def test_release_blocks_guest_before_provider_cleanup(database, monkeypatch):
    http, setup, ec2, _, _ = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    async with database() as db:
        box = await db.get(DevBox, identifier)
        box.auth_readiness = "ready"
        await db.commit()
    release = http.post(f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "guest-release"}, json={"workspace_retention": "delete"})
    assert release.status_code == 503
    async with database() as db:
        assert await db.scalar(select(func.count(DevBoxOperation.id))) == 1
    assert ("terminate", "i-example-owned") not in ec2.actions
    assert ("delete_volume", "vol-owned-example") not in ec2.actions
    assert http.get(f"/cloud-desktops/{identifier}").json()["status"] != "released"


@pytest.mark.asyncio
async def test_current_host_resolves_native_and_github_owner_without_browser_session(database, monkeypatch):
    from src.cloud_desktop.host_identity import current_host
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal

    async with database() as db:
        db.add_all(
            [
                User(id="broker-login", org_id="one", team_id="team", email="broker@example.test", cognito_sub="broker-sub"),
                User(id="github-owner", org_id="two", team_id="team", email="github@example.test"),
            ]
        )
        await db.flush()
        db.add(TenantMembership(user_id="github-owner", tenant_id="two", revoked_at=None))
        db.add(
            UserIdentity(user_id="github-owner", org_id="two", team_id="team", provider="github", provider_user_id="123", verification_method="oauth")
        )
        for box_id, user_id, org_id, provider_id in (("native-box", "alice", "one", "i-native"), ("github-box", "github-owner", "two", "i-github")):
            db.add(
                DevBox(
                    id=box_id,
                    user_id=user_id,
                    org_id=org_id,
                    deployment_id="test-deployment",
                    template_id="t1",
                    template_revision="r1",
                    profile="crew",
                    machine_generation=2,
                    desired_state="running",
                    observed_state="running",
                    status="running",
                    auth_readiness="ready",
                )
            )
            await db.flush()
            db.add(
                DevBoxInstance(
                    id=f"instance-{box_id}",
                    box_id=box_id,
                    user_id=user_id,
                    org_id=org_id,
                    deployment_id="test-deployment",
                    machine_generation=2,
                    provider_instance_id=provider_id,
                    template_revision="r1",
                    observed_state="running",
                )
            )
            await db.flush()
            db.add(
                DevBoxDevice(
                    id=f"device-{box_id}",
                    box_id=box_id,
                    instance_id=f"instance-{box_id}",
                    user_id=user_id,
                    org_id=org_id,
                    deployment_id="test-deployment",
                    machine_generation=2,
                    public_key_digest="a" * 64,
                    enrollment_consumed_at=datetime.now(UTC),
                )
            )
            await db.flush()
            db.add(
                DevBoxAccessSession(
                    box_id=box_id,
                    device_id=f"device-{box_id}",
                    user_id=user_id,
                    org_id=org_id,
                    deployment_id="test-deployment",
                    machine_generation=2,
                    provider_session_ref="run-example",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
        await db.commit()

    row = {
        "event_id": "run-example",
        "run_id": "run-example",
        "kind": "crew",
        "status": "active",
        "is_human_rooted": True,
        "root_human_id": "alice",
        "user_id": "alice",
        "tenant_id": "one",
        "deployment_id": "test-deployment",
        "session_id": "native-box",
        "executor_scope": "session",
        "lease_generation": 2,
        "lease_expires_at": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "target_kind": "ec2-instance",
        "target_ref": "i-native",
        "host_role_arn": "arn:aws:iam::000000000000:role/guest",
        "expires_at": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
    }

    async def run_row(_run_id):
        return row

    monkeypatch.setattr("src.proxy.bedrock_principal.read_routing_run", run_row)
    async with database() as db:
        for signed, box_id, user_id, org_id, provider_id in (
            (context("alice-sub"), "native-box", "alice", "one", "i-native"),
            (context("broker-sub", "two").model_copy(update={"cognito_username": "GitHub_123"}), "github-box", "github-owner", "two", "i-github"),
        ):
            assert (
                await current_host(
                    db,
                    box_id=box_id,
                    generation=2,
                    provider_instance_id=provider_id,
                    user_id=user_id,
                    org_id=org_id,
                    deployment_id="test-deployment",
                    owner=signed,
                )
                is not None
            )
            assert client(database, signed).get(f"/cloud-desktops/{box_id}").status_code == 200
            row.update(root_human_id=user_id, user_id=user_id, tenant_id=org_id, session_id=box_id, target_ref=provider_id)
            service = TokenContext(
                user_id="gateway-service",
                org_id=org_id,
                team_id="",
                department_id="",
                account_type="service",
                auth_source="iam",
                scope="internal",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            service._dev_box_instance_id = provider_id
            service._dev_box_role_arn = "arn:aws:iam::000000000000:role/guest"
            routed, root = await resolve_routing_principal(db, service, agent_run_id="run-example")
            assert routed.user_id == root == user_id
            assert routed.org_id == org_id
            assert routed._crew_root_human_id == user_id
            assert routed._crew_root_run_id == routed._dev_box_host_run_id == "run-example"
        row["executor_scope"] = "user"
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_principal(db, service, agent_run_id="run-example")
        row["executor_scope"] = "session"
        row["lease_generation"] = 1
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_principal(db, service, agent_run_id="run-example")


@pytest.mark.asyncio
async def test_owner_can_approve_only_current_host_enrollment(database):
    async with database() as db:
        db.add(
            DevBox(
                id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=2,
                desired_state="running",
                observed_state="running",
                status="running",
            )
        )
        await db.flush()
        db.add(
            DevBoxInstance(
                box_id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=2,
                provider_instance_id="i-example",
                template_revision="r1",
                observed_state="running",
            )
        )
        await db.commit()
    url = "/cloud-desktops/host-box/host-enrollment"
    body = {"public_key_digest": "a" * 64}
    assert client(database, context("bob-sub")).post(url, json=body).status_code == 404
    allowed = client(database, context("alice-sub")).post(url, json=body)
    assert allowed.status_code == 201, allowed.text
    assert allowed.headers["Cache-Control"] == "no-store"
    assert len(allowed.json()["challenge"]) >= 32
    assert client(database, context("alice-sub")).post(url, json=body).status_code == 409
    async with database() as db:
        device = await db.scalar(select(DevBoxDevice))
        assert device.enrollment_digest != allowed.json()["challenge"]
        assert device.user_id == "alice" and device.machine_generation == 2
        box = await db.get(DevBox, "host-box")
        box.machine_generation = 3
        await db.commit()
    assert client(database, context("alice-sub")).post(url, json=body).status_code == 409


@pytest.mark.asyncio
async def test_github_broker_owner_can_approve_own_host(database):
    async with database() as db:
        db.add(User(id="github-owner", org_id="two", team_id="team", email="github@example.test"))
        await db.flush()
        db.add(TenantMembership(user_id="github-owner", tenant_id="two", revoked_at=None))
        db.add(User(id="github-login", org_id="one", team_id="team", email="broker@example.test", cognito_sub="broker-sub"))
        db.add(
            UserIdentity(user_id="github-owner", org_id="two", team_id="team", provider="github", provider_user_id="123", verification_method="oauth")
        )
        db.add(
            DevBox(
                id="github-box",
                user_id="github-owner",
                org_id="two",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=1,
                desired_state="running",
                observed_state="running",
                status="running",
            )
        )
        await db.flush()
        db.add(
            DevBoxInstance(
                box_id="github-box",
                user_id="github-owner",
                org_id="two",
                deployment_id="test-deployment",
                machine_generation=1,
                provider_instance_id="i-github",
                template_revision="r1",
                observed_state="running",
            )
        )
        await db.commit()
    signed = context("broker-sub", "two").model_copy(update={"cognito_username": "GitHub_123"})
    http = client(database, signed)
    reply = http.post("/cloud-desktops/github-box/host-enrollment", json={"public_key_digest": "b" * 64})
    assert reply.status_code == 201, reply.text
    unproven = client(database, context("broker-sub", "two"))
    assert unproven.post("/cloud-desktops/github-box/host-enrollment", json={"public_key_digest": "b" * 64}).status_code == 403


@pytest.mark.asyncio
async def test_controller_attests_approved_host_and_gateway_resolves_its_run(database, monkeypatch):
    import importlib.util
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import boto3
    from moto import mock_aws

    from src.agentauth.chat_crew_run import CrewRunStore
    from src.crew.model_token import mint_model_token
    from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget
    from src.proxy import service as proxy_service
    from src.proxy.bedrock_principal import resolve_routing_principal
    from src.shared.models.usage import UsageLog

    controller_root = Path(__file__).resolve().parents[3] / "user-services" / "dev-box"
    sys.path.insert(0, str(controller_root))
    spec = importlib.util.spec_from_file_location("dev_box_controller_identity", controller_root / "controller" / "identity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class EC2:
        def describe_instances(self, **arguments):
            assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-0123456789abcdef0",
                                "State": {"Name": "running"},
                                "Tags": [
                                    {"Key": key, "Value": value}
                                    for key, value in {
                                        "adp:component": "dev-box",
                                        "adp:deployment-id": "test-deployment",
                                        "adp:owner-id": "alice",
                                        "adp:box-id": "approved-box",
                                        "adp:generation": "1",
                                    }.items()
                                ],
                            }
                        ]
                    }
                ]
            }

    class SSM:
        def send_command(self, **arguments):
            assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
            assert "challenge" not in str(arguments)
            return {"Command": {"CommandId": "ssm-command"}}

        def get_command_invocation(self, **arguments):
            assert arguments == {"CommandId": "ssm-command", "InstanceId": "i-0123456789abcdef0"}
            return {
                "Status": "Success",
                "StandardOutputContent": (f"{digest}  /run/adp/dev-box-enrollment\n{'a' * 64}  /run/adp/dev-box-public-key\n"),
            }

    async with database() as db:
        db.add(
            DevBox(
                id="approved-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=1,
                desired_state="running",
                observed_state="running",
                status="running",
            )
        )
        await db.flush()
        db.add(
            DevBoxInstance(
                box_id="approved-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=1,
                provider_instance_id="i-0123456789abcdef0",
                template_revision="r1",
                observed_state="running",
            )
        )
        await db.commit()
    reply = client(database, context("alice-sub")).post("/cloud-desktops/approved-box/host-enrollment", json={"public_key_digest": "a" * 64})
    assert reply.status_code == 201, reply.text
    digest = __import__("hashlib").sha256(reply.json()["challenge"].encode()).hexdigest()
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="host-runs",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
        )
        monkeypatch.setattr(
            "src.proxy.bedrock_principal.get_settings", lambda: SimpleNamespace(webhook_events_table=table.name, aws_region="us-east-1")
        )
        monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: None)

        class MismatchedSSM(SSM):
            def get_command_invocation(self, **arguments):
                return {"Status": "Success", "StandardOutputContent": "unverified\n"}

        guest_role = "arn:aws:iam::000000000000:role/guest"
        guest_store = CrewRunStore(table, host_role_arn=guest_role)
        assert await module.reconcile_enrollment(database, EC2(), MismatchedSSM(), guest_store, reply.json()["device_id"]) is None
        assert table.scan()["Count"] == 0
        from controller.worker import scan_once

        assert await scan_once(database, EC2(), SSM(), "test-deployment", None, "worker", run_store=guest_store) == 1
        async with database() as db:
            run_id = (await db.scalar(select(DevBoxAccessSession))).provider_session_ref
        assert run_id
        row = guest_store.read_run(run_id)
        owner_scope = CrewOwner("one", "alice")
        endpoint = "https://10.4.0.3:8443"
        target = ResolvedCrewTarget(
            endpoint,
            CrewLease(
                tenant_id="one",
                user_id="alice",
                target_kind="ec2-instance",
                target_ref="approved-box",
                generation=1,
                expires_at=int(row["lease_expires_at"]),
                endpoint=endpoint,
            ),
            "ready",
            host_run_id=run_id,
            host_instance_ref="i-0123456789abcdef0",
            host_fingerprint="a" * 64,
        )

        async def resolve(_owner, *, now):
            assert _owner == owner_scope
            return target

        monkeypatch.setattr("src.crew.proxy.get_target_resolver", lambda: SimpleNamespace(resolve=resolve))
        monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: guest_store)
        monkeypatch.setattr("src.crew.handoff.current_crew_store", lambda: guest_store)
        monkeypatch.setattr("src.crew.model_token._secret", lambda: b"fictional-test-signing-key")
        model_token = await mint_model_token(owner_scope, target, now=int(datetime.now(UTC).timestamp()))
        async with database() as db:
            saved = await db.scalar(select(DevBoxAccessSession))
            assert saved.provider_session_ref == run_id
            service = TokenContext(
                user_id="gateway-service",
                org_id="one",
                team_id="",
                department_id="",
                account_type="service",
                auth_source="iam",
                scope="internal",
                requires_host_identity=True,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            service._dev_box_instance_id = "i-0123456789abcdef0"
            service._dev_box_role_arn = guest_role
            routed, root = await resolve_routing_principal(db, service, agent_run_id=run_id)
            assert routed.user_id == root == "alice"
        from src.proxy.routes import get_proxy_service, get_token_context
        from src.proxy.routes import router as model_router

        async with database.kw["bind"].begin() as connection:
            await connection.run_sync(UsageLog.__table__.create)
        monkeypatch.setattr(proxy_service, "get_session_factory", lambda: database)
        monkeypatch.setattr(proxy_service, "resolve_shadow_target", AsyncMock(return_value=None))
        monkeypatch.setattr(proxy_service, "reconcile_budget_reservation", AsyncMock())

        class ProviderSubstitute:
            async def invoke_model(self, _model_id, _body, context, *, stream, pricing_capture, agent_run_id, **_kwargs):
                async with database() as db:
                    routed, owner_id = await resolve_routing_principal(db, context, agent_run_id=agent_run_id)
                    assert routed.org_id == "one" and routed._dev_box_host_run_id == agent_run_id
                recorded = await proxy_service.ProxyService.__new__(proxy_service.ProxyService)._log_usage(
                    context=context,
                    model=_model_id,
                    input_tokens=4,
                    output_tokens=2,
                    cost_usd=Decimal("0.01"),
                    latency_ms=1,
                    status_code=200,
                    request_id="host-turn",
                )
                assert recorded
                return {"owner_id": owner_id, "usage": {"input_tokens": 4, "output_tokens": 2}}

        app = FastAPI()
        app.include_router(model_router)

        async def authenticated(request: Request):
            request.state.token_context = service
            return service

        app.dependency_overrides[get_token_context] = authenticated
        monkeypatch.setattr(
            "src.auth.caller_provenance.verified_caller_identity",
            lambda _request: "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0",
        )
        app.dependency_overrides[get_proxy_service] = lambda: ProviderSubstitute()
        monkeypatch.setattr("src.proxy.routes.get_chat_logging_service", lambda: SimpleNamespace(log_chat_async=lambda **_kwargs: None))
        gateway = TestClient(app)
        headers = {"x-agent-runid": run_id, "x-adp-crew-model-token": model_token}
        denied = gateway.post("/bedrock/invoke", json={"model": "example-model"}, headers=headers)
        assert denied.status_code == 403
        model_reply = gateway.post("/model/example-model/invoke", json={"messages": []}, headers=headers)
        assert model_reply.status_code == 200, model_reply.text
        assert model_reply.json()["owner_id"] == "alice"
        async with database() as db:
            usage = (await db.scalars(select(UsageLog))).one()
            assert (usage.org_id, usage.root_human_id, usage.agent_run_id) == ("one", "alice", run_id)
            assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (4, 2, Decimal("0.01"))
        row = guest_store.read_run(run_id)
        table.update_item(
            Key={"event_id": run_id, "arrived_at": row["arrived_at"]},
            UpdateExpression="SET executor_scope = :scope",
            ExpressionAttributeValues={":scope": "user"},
        )
        refused = gateway.post("/model/example-model/invoke", json={"messages": []}, headers=headers)
        assert refused.status_code == 403
        table.update_item(
            Key={"event_id": run_id, "arrived_at": row["arrived_at"]},
            UpdateExpression="SET executor_scope = :scope",
            ExpressionAttributeValues={":scope": "session"},
        )
        async with database() as db:
            await db.execute(update(DevBox).where(DevBox.id == "approved-box").values(machine_generation=2))
            await db.commit()
        stale = gateway.post("/model/example-model/invoke", json={"messages": []}, headers=headers)
        assert stale.status_code == 403
        async with database() as db:
            await db.execute(update(DevBox).where(DevBox.id == "approved-box").values(machine_generation=1))
            await db.commit()
        assert guest_store.revoke_for_lease(
            tenant_id="one",
            session_id="approved-box",
            lease_generation=1,
            expected_run_id=run_id,
        )
        revoked = gateway.post("/model/example-model/invoke", json={"messages": []}, headers=headers)
        assert revoked.status_code == 403
        async with database() as db:
            assert (await db.scalars(select(UsageLog))).one().agent_run_id == run_id


@pytest.mark.asyncio
async def test_ensure_reuses_pending_setup_and_never_reserves_a_second_owner_box(database):
    http = client(database, context("alice-sub"))

    def ensure(key, template="assistant"):
        return http.post("/cloud-desktops/ensure", headers={"Idempotency-Key": key}, json={"template": template, "profile": "crew"})

    first = ensure("ensure-new")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "pending"
    assert ensure("ensure-new").json() == first.json()
    assert ensure("ensure-other").json() == first.json()
    assert ensure("ensure-new", "other").status_code == 409
    assert ensure("ensure-other", "other").status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(DevBoxOperation.id))) == 1
    foreign = client(database, context("other-sub", "two"))
    assert foreign.get(f"/cloud-desktops/{first.json()['desktop_id']}").status_code == 404
    async with database() as db:
        operation = await db.get(DevBoxOperation, first.json()["operation_id"])
        operation.status = "succeeded"
        await db.commit()
    assert ensure("unverified-state").status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(DevBoxOperation.id))) == 1


@pytest.mark.asyncio
async def test_ensure_wakes_only_existing_machine_after_verified_stop(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    desktop_id = setup["desktop_id"]

    def ensure(key):
        return http.post("/cloud-desktops/ensure", headers={"Idempotency-Key": key}, json={"template": "assistant", "profile": "crew"})

    running = ensure("observe-running")
    assert running.status_code == 200, running.text
    assert running.json() == {
        "contract_version": 1,
        "desktop_id": desktop_id,
        "operation_id": None,
        "status": "succeeded",
        "progress": "last_observed_running",
        "error_class": None,
    }
    assert len(ec2.launches) == 1
    assert ensure("setup-1").status_code == 200
    stop = http.post(f"/cloud-desktops/{desktop_id}/stop", headers={"Idempotency-Key": "stop-for-ensure"}).json()
    during_stop = ensure("wait-for-stop")
    assert during_stop.status_code == 202 and during_stop.json()["operation_id"] == stop["operation_id"]
    assert ensure("setup-1").status_code == 409
    async with database() as db:
        box = await db.get(DevBox, desktop_id)
        assert box.desired_state == "stopped"
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    wake = ensure("wake-current")
    assert wake.status_code == 202, wake.text
    assert wake.json()["status"] == "pending" and wake.json()["desktop_id"] == desktop_id
    assert ensure("wake-current").json() == wake.json()
    assert ensure("wake-duplicate").json() == wake.json()
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    assert http.get(f"/cloud-desktops/{desktop_id}/operations/{wake.json()['operation_id']}").json()["status"] == "succeeded"
    assert ec2.actions.count(("start", "i-example-owned")) == 1
    assert len(ec2.launches) == 1
    async with database() as db:
        box = await db.get(DevBox, desktop_id)
        assert box.machine_generation == 1
        assert await db.scalar(select(func.count(DevBox.id))) == 1


@pytest.mark.asyncio
async def test_owner_crew_template_and_status_support_ui_ensure_without_guessing(database):
    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    other = client(database, context("other-sub", "two"))
    assert alice.get("/cloud-desktops/crew-template").json() == {"contract_version": 1, "template": "assistant"}
    assert other.get("/cloud-desktops/crew-template").json()["template"] == "assistant"
    setup = create(alice).json()
    box = alice.get(f"/cloud-desktops/{setup['desktop_id']}").json()
    assert box["template"] == "assistant"
    assert box["infrastructure_cost"]["total_cost_usd"] is None
    assert alice.get("/cloud-desktops/crew-template").status_code == 409
    assert bob.get(f"/cloud-desktops/{setup['desktop_id']}").status_code == 404
    async with database() as db:
        org = await db.get(Organization, "one")
        org.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    assert bob.get("/cloud-desktops/crew-template").status_code == 403
    denied = bob.post("/cloud-desktops/ensure", json={"profile": "crew", "template": "assistant"}, headers={"Idempotency-Key": "policy-removed"})
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_crew_template_does_not_offer_a_stale_permitted_revision(database):
    alice = client(database, context("alice-sub"))
    async with database() as db:
        db.add(DevBoxTemplate(id="new-r2", name="assistant", revision="r2", profile="crew", instance_size="large", disk_gb=60, approved=True))
        await db.commit()
    assert alice.get("/cloud-desktops/crew-template").status_code == 403
    assert (
        alice.post(
            "/cloud-desktops/ensure", json={"profile": "crew", "template": "assistant"}, headers={"Idempotency-Key": "new-revision"}
        ).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_ssh_access_is_fenced_audited_and_scoped_to_the_running_owner_instance(database, monkeypatch):
    import json
    from unittest.mock import Mock

    from src.cloud_desktop import ssh_access

    monkeypatch.setenv("BG_DEV_BOX_SSH_GRANT_ROLE_ARN", "arn:aws:iam::000000000000:role/dev-box-ssh")
    sts = Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "test-access",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-token",
            "Expiration": datetime.now(UTC) + timedelta(minutes=14),
        }
    }
    monkeypatch.setattr(ssh_access.boto3, "client", lambda *args, **kwargs: sts)
    alice = client(database, context("alice-sub"))
    admitted = create(alice).json()
    async with database() as db:
        box = await db.get(DevBox, admitted["desktop_id"])
        box.status = box.observed_state = "running"
        db.add(
            DevBoxInstance(
                id="ssh-instance",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                template_revision=box.template_revision,
                provider_instance_id="i-0123456789abcdef0",
                provider_account_ref="000000000000",
                region="us-east-1",
                observed_state="running",
            )
        )
        await db.commit()

    uri = f"/cloud-desktops/{admitted['desktop_id']}/ssh-access"
    assert client(database, context("bob-sub")).post(uri).status_code == 404
    assert client(database, context("other-sub", "two")).post(uri).status_code == 404
    sts.assume_role.assert_not_called()

    granted = alice.post(uri)
    assert granted.status_code == 200, granted.text
    assert granted.headers["cache-control"] == "no-store"
    body = granted.json()
    assert body["instance_id"] == "i-0123456789abcdef0"
    assert body["credentials"]["session_token"] == "test-token"
    params = sts.assume_role.call_args.kwargs
    assert params["DurationSeconds"] == 900
    assert json.loads(params["Policy"])["Statement"] == [
        {
            "Effect": "Allow",
            "Action": "ssm:StartSession",
            "Resource": [
                "arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0",
                "arn:aws:ssm:us-east-1::document/AWS-StartSSHSession",
            ],
        },
        {
            "Effect": "Allow",
            "Action": "ec2-instance-connect:SendSSHPublicKey",
            "Resource": "arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0",
            "Condition": {"StringEquals": {"ec2:osuser": "ec2-user"}},
        },
    ]
    async with database() as db:
        operation = await db.get(DevBoxOperation, body["operation_id"])
        assert operation.action == "ssh_access" and operation.status == "succeeded"
        assert operation.actor_user_id == "alice"
        assert "test-token" not in str(operation.__dict__)
        events = await db.scalars(select(DevBoxEvent).where(DevBoxEvent.operation_id == operation.id))
        assert [event.event_type for event in events] == ["ssh_access_issued"]

    # Policy revocation must also fence host access without a run_id.
    async with database() as db:
        org = await db.get(Organization, "one")
        org.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    assert alice.post(uri).status_code == 409
    assert sts.assume_role.call_count == 1
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DevBoxOperation).where(DevBoxOperation.action == "ssh_access")) == 1


@pytest.mark.asyncio
async def test_ssh_access_refuses_foreign_instance_and_wrong_account_without_sts(database, monkeypatch):
    import json
    from unittest.mock import Mock

    from src.cloud_desktop import ssh_access

    monkeypatch.setenv("BG_DEV_BOX_SSH_GRANT_ROLE_ARN", "arn:aws:iam::000000000000:role/dev-box-ssh")
    sts = Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "test-access",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-token",
            "Expiration": datetime.now(UTC) + timedelta(minutes=14),
        }
    }
    monkeypatch.setattr(ssh_access.boto3, "client", lambda *args, **kwargs: sts)
    async with database() as db:
        db.add(
            DevBox(
                id="foreign-box",
                user_id="bob",
                org_id="one",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=1,
                status="running",
                desired_state="running",
                observed_state="running",
            )
        )
        await db.flush()
        db.add(
            DevBoxInstance(
                id="foreign-instance",
                box_id="foreign-box",
                user_id="bob",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=1,
                template_revision="r1",
                provider_instance_id="i-11111111111111111",
                provider_account_ref="000000000000",
                region="us-east-1",
                observed_state="running",
            )
        )
        await db.commit()

    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    foreign_url = "/cloud-desktops/foreign-box/ssh-access"
    assert alice.post(foreign_url).status_code == 404
    assert alice.post("/cloud-desktops/i-11111111111111111/ssh-access").status_code == 404
    sts.assume_role.assert_not_called()
    authorized = bob.post(foreign_url)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["instance_id"] == "i-11111111111111111"
    policy = json.loads(sts.assume_role.call_args.kwargs["Policy"])
    assert "i-11111111111111111" in policy["Statement"][0]["Resource"][0]
    assert "i-0123456789abcdef0" not in json.dumps(policy)

    async with database() as db:
        instance = await db.get(DevBoxInstance, "foreign-instance")
        instance.provider_account_ref = "111111111111"
        await db.commit()
    assert bob.post(foreign_url).status_code == 503
    assert sts.assume_role.call_count == 1

    async with database() as db:
        instance = await db.get(DevBoxInstance, "foreign-instance")
        instance.provider_account_ref = "000000000000"
        policy_row = await db.get(DevBoxPolicy, "p1")
        policy_row.enabled = False
        await db.commit()
    assert bob.post(foreign_url).status_code == 409
    assert sts.assume_role.call_count == 1
    async with database() as db:
        operations = await db.scalars(select(DevBoxOperation).where(DevBoxOperation.box_id == "foreign-box", DevBoxOperation.action == "ssh_access"))
        assert len(list(operations)) == 1


@pytest.mark.asyncio
async def test_unresolved_setup_or_replace_tells_the_owner_when_release_may_supersede_it(database):
    """#8100: a setup/replace parked on an unresolved observation reports the countdown to the retry bound, and once
    the bound has passed (without a live lease) release or replace supersede it."""
    from src.cloud_desktop.routes import DEV_BOX_LAUNCH_RETRY_BOUND, DEV_BOX_RECONCILE_INTERVAL_SECONDS

    http = client(database, context("alice-sub"))
    setup = create(http).json()
    identifier = setup["desktop_id"]
    release = {"Idempotency-Key": "release-unresolved"}

    async def park(**values):
        async with database() as db:
            operation = await db.get(DevBoxOperation, setup["operation_id"])
            for field, value in values.items():
                setattr(operation, field, value)
            await db.commit()

    await park(
        status="unknown",
        error_class="workspace_attached_elsewhere",
        progress="attachment_intent",
        retries=DEV_BOX_LAUNCH_RETRY_BOUND - 2,
        lease_owner=None,
        lease_expires_at=None,
    )
    refused = http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        f"A desktop operation is already active (setup unknown: workspace_attached_elsewhere, reconcile {DEV_BOX_LAUNCH_RETRY_BOUND - 2} "
        f"of {DEV_BOX_LAUNCH_RETRY_BOUND}); if it stays unresolved the controller fails it and release or replace may supersede it "
        f"in about {3 * DEV_BOX_RECONCILE_INTERVAL_SECONDS} s"
    )
    # Other lifecycle actions keep the plain refusal: they never supersede an operation.
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-unresolved"})
    assert stopped.status_code == 409 and stopped.json()["detail"] == "A desktop operation is already active"
    # At the bound itself the operation is still the controller's; one claim past it the owner may supersede it.
    await park(retries=DEV_BOX_LAUNCH_RETRY_BOUND)
    refused = http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"})
    assert refused.status_code == 409 and f"in about {DEV_BOX_RECONCILE_INTERVAL_SECONDS} s" in refused.json()["detail"]
    await park(retries=DEV_BOX_LAUNCH_RETRY_BOUND + 1)
    accepted = http.post(f"/cloud-desktops/{identifier}/release", headers=release, json={"workspace_retention": "retain"})
    assert accepted.status_code == 202, accepted.text
    superseded = http.get(f"/cloud-desktops/{identifier}/operations/{setup['operation_id']}").json()
    assert superseded["status"] == "failed" and superseded["error_class"] == "workspace_attached_elsewhere"
    async with database() as db:
        events = list(await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.box_id == identifier).order_by(DevBoxEvent.created_at)))
        assert events == ["setup_admitted", "launch_abandoned", "release_admitted"]


@pytest.mark.parametrize("target_recorded", [True, False], ids=["generation-row-has-provider-id", "provider-id-only-in-inventory"])
@pytest.mark.asyncio
async def test_replace_recovers_after_a_restart_with_the_disk_already_on_the_new_instance(database, monkeypatch, target_recorded):
    """#8100 live shape: the replace launched the new generation and attached the workspace disk to it, then the
    controller restarted; the detach-from-previous re-check must not report the disk as attached elsewhere."""
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    ec2.volume["Files"] = {"work.txt": "retained"}
    identifier = setup["desktop_id"]
    assert http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-for-recovery"}).status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    replaced = http.post(f"/cloud-desktops/{identifier}/replace", headers={"Idempotency-Key": "replace-recovery"}).json()
    operation_url = f"/cloud-desktops/{identifier}/operations/{replaced['operation_id']}"
    ec2.lose_response = False
    ec2.lose_attachment = True  # the attach reached EC2; the controller died before it saw the answer
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-two")
    parked = http.get(operation_url).json()
    assert parked["status"] == "unknown" and parked["error_class"] == "workspace_provider_unknown" and parked["progress"] == "attachment_requested"
    assert ec2.volume["Attachments"] == [{"InstanceId": "i-example-replacement", "State": "attached"}]
    if not target_recorded:
        async with database() as db:
            replacement = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.box_id == identifier, DevBoxInstance.machine_generation == 2))
            replacement.provider_instance_id = None
            await db.commit()
    ec2.lose_attachment = False
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "restarted-worker")
    recovered = http.get(operation_url).json()
    assert recovered["status"] == "succeeded" and recovered["error_class"] is None, recovered
    box = http.get(f"/cloud-desktops/{identifier}").json()
    assert box["status"] == "running" and box["observed_state"] == "running" and box["machine_generation"] == 2
    assert len(ec2.launches) == 2 and ec2.actions.count(("attach", "i-example-replacement")) == 1
    assert ec2.actions.count(("detach", "i-example-owned")) == 1
    assert ec2.volume["Files"] == {"work.txt": "retained"}
    assert await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "restarted-worker") == 0


@pytest.mark.asyncio
async def test_desktop_read_names_the_hosts_readiness_refusal_until_crew_is_ready(database):
    """#8223: a fenced lease says why — the controller's host_refused:<Class> travels on the instance row."""
    alice = client(database, context("alice-sub"))
    accepted = create(alice).json()
    assert alice.get(f"/cloud-desktops/{accepted['desktop_id']}").json()["host_readiness_refusal"] is None
    # No device approved for this generation yet: the browser enrolls once (#8223 simplification).
    assert alice.get(f"/cloud-desktops/{accepted['desktop_id']}").json()["host_binding"] == "none"
    async with database() as db:
        box = await db.get(DevBox, accepted["desktop_id"])
        box.status = "running"
        box.observed_state = "running"
        db.add(
            DevBoxInstance(
                id="instance-refused",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                template_revision="r1",
                provider_instance_id="i-refused",
                observed_state="running",
                readiness_class="machine_running",
                error_class="host_refused:InvalidCheckpoint",
            )
        )
        await db.commit()
    assert alice.get(f"/cloud-desktops/{accepted['desktop_id']}").json()["host_readiness_refusal"] == "host_refused:InvalidCheckpoint"
    assert alice.get("/cloud-desktops").json()["items"][0]["host_readiness_refusal"] == "host_refused:InvalidCheckpoint"
    async with database() as db:
        instance = await db.get(DevBoxInstance, "instance-refused")
        # Other error classes (launch outcomes) are not readiness refusals; readiness clears the field.
        instance.error_class = "provider_refused:Unsupported"
        await db.commit()
    assert alice.get(f"/cloud-desktops/{accepted['desktop_id']}").json()["host_readiness_refusal"] is None
    async with database() as db:
        instance = await db.get(DevBoxInstance, "instance-refused")
        instance.error_class = "host_refused:InvalidCheckpoint"
        instance.readiness_class = "crew_ready"
        await db.commit()
    assert alice.get(f"/cloud-desktops/{accepted['desktop_id']}").json()["host_readiness_refusal"] is None


@pytest.mark.asyncio
async def test_snapshot_list_is_read_only_and_scoped_to_the_box_owner(database):
    observed = datetime(2026, 10, 23, tzinfo=UTC)
    created = datetime(2026, 10, 22, tzinfo=UTC)
    async with database() as db:
        db.add_all(
            [
                DevBox(
                    id=identifier,
                    user_id=owner,
                    org_id=org,
                    deployment_id="test-deployment",
                    template_id="t1",
                    template_revision="r1",
                    profile="crew",
                    status="released",
                )
                for identifier, owner, org in (("alice-box", "alice", "one"), ("bob-box", "bob", "one"), ("other-box", "other", "two"))
            ]
        )
        await db.flush()
        db.add_all(
            [
                DevBoxResource(
                    id=f"resource-{identifier}",
                    box_id=box_id,
                    user_id=owner,
                    org_id=org,
                    deployment_id="test-deployment",
                    machine_generation=1,
                    resource_kind="snapshot",
                    provider_resource_id=identifier,
                    size_gb=40,
                    encrypted=encrypted,
                    deletion_state=deletion_state,
                    last_observed_state="present",
                    last_observed_at=observed,
                    snapshot_created_at=created if identifier == "snap-alice" else None,
                )
                for identifier, box_id, owner, org, encrypted, deletion_state in (
                    ("snap-alice", "alice-box", "alice", "one", True, "present"),
                    ("snap-alice-old", "alice-box", "alice", "one", True, "present"),
                    ("snap-bob", "bob-box", "bob", "one", True, "present"),
                    ("snap-other", "other-box", "other", "two", True, "present"),
                    ("snap-deleted", "alice-box", "alice", "one", True, "verified_deleted"),
                    ("snap-unsafe", "alice-box", "alice", "one", False, "present"),
                )
            ]
        )
        await db.commit()

    alice = client(database, context("alice-sub"))
    bob = client(database, context("bob-sub"))
    foreign_org = client(database, context("other-sub", org="two"))
    response = alice.get("/cloud-desktops/alice-box/snapshots")
    assert response.status_code == 200
    assert response.json() == {
        "contract_version": 1,
        "items": [
            {
                "snapshot_id": "snap-alice",
                "machine_generation": 1,
                "size_gb": 40,
                "state": "present",
                "created_at": created.isoformat(),
                "observed_at": observed.isoformat().replace("+00:00", ""),
            },
            {
                "snapshot_id": "snap-alice-old",
                "machine_generation": 1,
                "size_gb": 40,
                "state": "present",
                "created_at": None,
                "observed_at": observed.isoformat().replace("+00:00", ""),
            },
        ],
    }
    assert bob.get("/cloud-desktops/alice-box/snapshots").status_code == 404
    assert foreign_org.get("/cloud-desktops/alice-box/snapshots").status_code == 404
    guest_context = context("alice-sub").model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )
    guest = client(database, guest_context)
    assert guest.get("/cloud-desktops/alice-box/snapshots").status_code == 403
    assert guest.get("/cloud-desktops/bob-box/snapshots").status_code == 403
    assert (
        guest.post(
            "/cloud-desktops/alice-box/replace", headers={"Idempotency-Key": "guest-replace"}, json={"snapshot_id": "snap-0123456789abcdef0"}
        ).status_code
        == 403
    )
    assert bob.get("/cloud-desktops/bob-box/snapshots").json()["items"][0]["snapshot_id"] == "snap-bob"
    assert foreign_org.get("/cloud-desktops/other-box/snapshots").json()["items"][0]["snapshot_id"] == "snap-other"
    assert alice.post("/cloud-desktops/alice-box/snapshots").status_code == 422


@pytest.mark.asyncio
async def test_replace_admits_only_verified_owner_snapshot_and_persists_retry_choice(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    stopped = http.post(f"/cloud-desktops/{identifier}/stop", headers={"Idempotency-Key": "stop-before-snapshot"})
    assert stopped.status_code == 202
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    snapshot_id = "snap-0123456789abcdef0"
    async with database() as db:
        box = await db.get(DevBox, identifier)
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.box_id == identifier, DevBoxResource.resource_kind == "workspace_disk"))
        db.add(
            DevBoxResource(
                id="snapshot-restore-choice",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=disk.machine_generation,
                resource_kind="snapshot",
                provider_resource_id=snapshot_id,
                size_gb=disk.size_gb,
                encrypted=True,
                retention_state="retained",
                deletion_state="present",
                last_observed_state="present",
            )
        )
        await db.commit()

    endpoint = f"/cloud-desktops/{identifier}/replace"
    assert http.post(endpoint, headers={"Idempotency-Key": "foreign-snapshot"}, json={"snapshot_id": "snap-fffffffffffffffff"}).status_code == 404
    assert (
        client(database, context("bob-sub")).post(endpoint, headers={"Idempotency-Key": "other-owner"}, json={"snapshot_id": snapshot_id}).status_code
        == 404
    )
    assert http.post(endpoint, headers={"Idempotency-Key": "invalid-snapshot"}, json={"snapshot_id": "not-a-snapshot"}).status_code == 422
    result = http.post(endpoint, headers={"Idempotency-Key": "restore-choice"}, json={"snapshot_id": snapshot_id})
    assert result.status_code == 202
    assert (
        http.post(endpoint, headers={"Idempotency-Key": "restore-choice"}, json={"snapshot_id": snapshot_id}).json()["operation_id"]
        == result.json()["operation_id"]
    )
    assert http.post(endpoint, headers={"Idempotency-Key": "restore-choice"}, json={"snapshot_id": "snap-fffffffffffffffff"}).status_code == 409
    async with database() as db:
        events = list((await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.operation_id == result.json()["operation_id"]))).all())
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.box_id == identifier, DevBoxResource.resource_kind == "workspace_disk"))
    assert events == ["replace_admitted", f"restore:{snapshot_id}"]
    assert disk.provider_resource_id == "vol-owned-example" and ec2.volume["Attachments"][0]["InstanceId"] == "i-example-owned"


@pytest.mark.asyncio
async def test_release_delete_preserves_snapshots_until_provider_expiry(database, monkeypatch):
    http, setup, ec2, launch_template, scan_once = await boot_workspace(database, monkeypatch)
    identifier = setup["desktop_id"]
    async with database() as db:
        box = await db.get(DevBox, identifier)
        db.add(
            DevBoxResource(
                id="snapshot-after-delete",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                resource_kind="snapshot",
                provider_resource_id="snap-example",
                size_gb=30,
                encrypted=True,
                retention_state="retained",
                deletion_state="present",
            )
        )
        await db.commit()
    release = http.post(
        f"/cloud-desktops/{identifier}/release", headers={"Idempotency-Key": "release-with-snapshot"}, json={"workspace_retention": "delete"}
    )
    assert release.status_code == 202
    async with database() as db:
        snapshot_row = await db.get(DevBoxResource, "snapshot-after-delete")
        assert snapshot_row.retention_state == "retained"
    await scan_once(database, ec2, FakeSSM(), "test-deployment", launch_template, "worker-one")
    assert http.get(f"/cloud-desktops/{identifier}/operations/{release.json()['operation_id']}").json()["status"] == "succeeded"
    async with database() as db:
        snapshot_row = await db.get(DevBoxResource, "snapshot-after-delete")
        disk = await db.scalar(select(DevBoxResource).where(DevBoxResource.box_id == identifier, DevBoxResource.resource_kind == "workspace_disk"))
        assert snapshot_row.retention_state == "retained" and snapshot_row.deletion_state == "present"
        assert disk.deletion_state == "verified_deleted"
    assert all(action[0] != "delete_snapshot" for action in ec2.actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_direct_setup_checks_crew_policy_before_creating_or_replaying(database, existing):
    http = client(database, context("alice-sub"))
    if existing:
        assert create(http).status_code == 202
    async with database() as db:
        org = await db.get(Organization, "one")
        org.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    assert create(http).status_code == 403
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DevBox)) == int(existing)
        assert await db.scalar(select(func.count()).select_from(DevBoxOperation)) == int(existing)
