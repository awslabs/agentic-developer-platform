"""Verify DLM snapshot inventory against recorded workspace ownership."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.dev_box import DevBox, DevBoxDevice, DevBoxInstance, DevBoxOperation, DevBoxPolicy, DevBoxResource, DevBoxTemplate, DevBoxUsage
from src.shared.models.organization import Organization, User

from controller import usage, worker


ACCOUNT = "000000000000"
KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/00000000-0000-0000-0000-000000000000"
DEPLOYMENT = "example-deployment"
TEMPLATE = SimpleNamespace(account_id=ACCOUNT, kms_key_arn=KEY)


class SnapshotEC2:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def describe_snapshots(self, **request):
        self.calls.append(request)
        assert request["OwnerIds"] == [ACCOUNT]
        assert {item["Name"]: item["Values"] for item in request["Filters"]} == {
            "tag:adp:component": ["dev-box"],
            "tag:adp:deployment-id": [DEPLOYMENT],
            "tag:adp:workspace": ["persistent"],
        }
        page = 1 if request.get("NextToken") else 0
        result = {"Snapshots": self.pages[page]}
        if page == 0 and len(self.pages) > 1:
            result["NextToken"] = "next-page"
        return result


def snapshot(snapshot_id, **changes):
    row = {
        "SnapshotId": snapshot_id, "VolumeId": "vol-owned", "OwnerId": ACCOUNT,
        "VolumeSize": 40, "Encrypted": True, "KmsKeyId": KEY,
        "StartTime": datetime(2026, 10, 22, 12, tzinfo=UTC),
        "Tags": [{"Key": key, "Value": value} for key, value in {
            "adp:component": "dev-box", "adp:deployment-id": DEPLOYMENT,
            "adp:owner-id": "owner", "adp:box-id": "box", "adp:generation": "1",
            "adp:workspace": "persistent",
        }.items()],
    }
    row.update(changes)
    return row


@pytest_asyncio.fixture
async def snapshot_database():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [Organization.__table__, User.__table__, DevBoxTemplate.__table__, DevBox.__table__, DevBoxInstance.__table__,
              DevBoxPolicy.__table__, DevBoxResource.__table__, DevBoxUsage.__table__, DevBoxOperation.__table__, DevBoxDevice.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Organization(id="org", name="Example Org"))
        db.add(User(id="owner", org_id="org", team_id="team", email="owner@example.test", cognito_sub="owner"))
        db.add(DevBoxTemplate(id="template", name="assistant", revision="r1", profile="crew", instance_size="t3.small", disk_gb=40, approved=True))
        await db.flush()
        db.add(DevBox(id="box", user_id="owner", org_id="org", deployment_id=DEPLOYMENT,
                      template_id="template", template_revision="r1", profile="crew", status="running"))
        await db.flush()
        db.add(DevBoxResource(id="disk", box_id="box", user_id="owner", org_id="org", deployment_id=DEPLOYMENT,
                              machine_generation=1, resource_kind="workspace_disk", provider_resource_id="vol-owned",
                              size_gb=40, encrypted=True, deletion_state="present"))
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_inventory_records_verified_snapshots_once_across_pages(snapshot_database, monkeypatch):
    ec2 = SnapshotEC2([[snapshot("snap-first")], [snapshot("snap-second")]])
    observed = []

    async def track_observation(_factory, _ec2, resource_id, _template):
        observed.append(resource_id)

    monkeypatch.setattr(usage, "observe_resource", track_observation)
    for _ in range(2):
        await usage.reconcile_usage(snapshot_database, ec2, DEPLOYMENT, TEMPLATE)
    async with snapshot_database() as db:
        rows = list((await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "snapshot"))).all())
    assert {row.provider_resource_id for row in rows} == {"snap-first", "snap-second"}
    assert all((row.box_id, row.machine_generation, row.size_gb, row.encrypted, row.deletion_state)
               == ("box", 1, 40, True, "present") for row in rows)
    assert all(row.snapshot_created_at == datetime(2026, 10, 22, 12) for row in rows)
    assert all(observed.count(row.id) == 2 for row in rows)
    assert [len(ec2.calls), ec2.calls[1]["NextToken"]] == [4, "next-page"]


@pytest.mark.asyncio
async def test_inventory_backfills_creation_time_only_for_verified_owner(snapshot_database):
    async with snapshot_database() as db:
        db.add(DevBoxResource(id="old-snapshot", box_id="box", user_id="owner", org_id="org", deployment_id=DEPLOYMENT,
                              machine_generation=1, resource_kind="snapshot", provider_resource_id="snap-old",
                              size_gb=40, encrypted=True, deletion_state="present"))
        await db.commit()
    created_at = datetime(2026, 10, 21, 12, tzinfo=UTC)
    ec2 = SnapshotEC2([[snapshot("snap-old", StartTime=created_at),
                       snapshot("snap-other", StartTime=created_at, OwnerId="unauthorized")]])
    await usage.inventory_snapshots(snapshot_database, ec2, DEPLOYMENT, TEMPLATE)
    async with snapshot_database() as db:
        old = await db.get(DevBoxResource, "old-snapshot")
        assert old.snapshot_created_at == created_at.replace(tzinfo=None)
        assert len(list((await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "snapshot"))).all())) == 1
    ec2.pages = [[snapshot("snap-old", StartTime=created_at + timedelta(hours=1))]]
    await usage.inventory_snapshots(snapshot_database, ec2, DEPLOYMENT, TEMPLATE)
    async with snapshot_database() as db:
        assert (await db.get(DevBoxResource, "old-snapshot")).snapshot_created_at == created_at.replace(tzinfo=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"VolumeId": "vol-foreign"}, {"OwnerId": "111111111111"}, {"VolumeSize": 41},
    {"Encrypted": False}, {"KmsKeyId": "wrong-key"},
    {"Tags": snapshot("snap-ignored")["Tags"][:-1]},
    {"Tags": [{**tag, "Value": "another-owner"} if tag["Key"] == "adp:owner-id" else tag
              for tag in snapshot("snap-ignored")["Tags"]]},
])
async def test_inventory_rejects_unverified_provider_results(snapshot_database, change):
    ec2 = SnapshotEC2([[snapshot("snap-unverified", **change)]])
    await usage.inventory_snapshots(snapshot_database, ec2, DEPLOYMENT, TEMPLATE)
    async with snapshot_database() as db:
        assert list((await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "snapshot"))).all()) == []

class MissingSnapshotEC2:
    def __init__(self, error_code="InvalidSnapshot.NotFound"):
        self.error_code = error_code
        self.calls = 0

    def describe_snapshots(self, **request):
        assert request == {"SnapshotIds": ["snap-owned"]}
        self.calls += 1
        raise ClientError({"Error": {"Code": self.error_code, "Message": "provider inventory unavailable"}}, "DescribeSnapshots")


async def seed_released_snapshot(factory, now):
    async with factory() as db:
        box = await db.get(DevBox, "box")
        box.status = "released"
        disk = await db.get(DevBoxResource, "disk")
        disk.deletion_state = "verified_deleted"
        db.add(DevBoxPolicy(id="policy", org_id="org", deployment_id=DEPLOYMENT, enabled=True,
                            max_live_boxes=1, max_running_boxes=1, allowed_sizes=["t3.small"], max_disk_gb=40,
                            snapshot_gb_hour_rate_usd=Decimal("0.00007")))
        db.add(DevBoxResource(id="owned-snapshot", box_id="box", user_id="owner", org_id="org",
                              deployment_id=DEPLOYMENT, machine_generation=1, resource_kind="snapshot",
                              provider_resource_id="snap-owned", size_gb=40, encrypted=True,
                              retention_state="retained", deletion_state="present", last_observed_state="present",
                              last_observed_at=now, last_rate_usd=Decimal("0.00007")))
        await db.commit()


@pytest.mark.asyncio
async def test_released_snapshot_is_verified_deleted_only_after_two_missing_observations(snapshot_database):
    now = datetime(2026, 10, 23, tzinfo=UTC)
    await seed_released_snapshot(snapshot_database, now)
    ec2 = MissingSnapshotEC2()
    assert await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(hours=1))
    async with snapshot_database() as db:
        row = await db.get(DevBoxResource, "owned-snapshot")
        assert row.deletion_state == "present" and row.retention_state == "retained"
        assert row.last_observed_state == "missing" and row.coverage_gap_at == now.replace(tzinfo=None)
    assert await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(hours=2))
    async with snapshot_database() as db:
        row = await db.get(DevBoxResource, "owned-snapshot")
        disk = await db.get(DevBoxResource, "disk")
        assert row.deletion_state == "verified_deleted" and row.retention_state == "deleted"
        assert disk.deletion_state == "verified_deleted"
    assert not await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(hours=3))
    assert ec2.calls == 2


@pytest.mark.asyncio
async def test_snapshot_provider_denial_never_claims_verified_deletion(snapshot_database):
    now = datetime(2026, 10, 23, tzinfo=UTC)
    await seed_released_snapshot(snapshot_database, now)
    ec2 = MissingSnapshotEC2("UnauthorizedOperation")
    assert not await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(hours=1))
    assert not await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(hours=2))
    async with snapshot_database() as db:
        row = await db.get(DevBoxResource, "owned-snapshot")
        assert row.deletion_state == "present" and row.retention_state == "retained"
        assert row.last_observed_state == "unknown"

@pytest.mark.asyncio
async def test_deleted_workspace_does_not_imply_snapshot_expired(snapshot_database):
    now = datetime(2026, 10, 23, tzinfo=UTC)
    await seed_released_snapshot(snapshot_database, now)

    class PresentSnapshotEC2:
        def describe_snapshots(self, **request):
            assert request == {"SnapshotIds": ["snap-owned"]}
            return {"Snapshots": [snapshot("snap-owned")]}

    ec2 = PresentSnapshotEC2()
    for days in (1, 8, 14):
        assert await usage.observe_resource(snapshot_database, ec2, "owned-snapshot", TEMPLATE, now=now + timedelta(days=days))
    async with snapshot_database() as db:
        resource = await db.get(DevBoxResource, "owned-snapshot")
        intervals = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == resource.id))).all())
        assert resource.deletion_state == "present" and resource.retention_state == "retained"
        assert resource.last_observed_state == "present" and len(intervals) == 3
        assert intervals[0].units == Decimal("960.000000")
        assert intervals[0].rate_usd == Decimal("0.000070") and intervals[0].cost_usd == Decimal("0.067200")
        assert all(interval.unit_name == "gb_hour" and interval.cost_usd > 0 for interval in intervals)


class MotoSnapshotEC2:
    def __init__(self, client, source_volume_id, key_arn):
        self.client = client
        self.source_volume_id = source_volume_id
        self.key_arn = key_arn
        self.deny_list = False

    def describe_snapshots(self, **request):
        if self.deny_list and "Filters" in request:
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}}, "DescribeSnapshots")
        response = self.client.describe_snapshots(**request)
        for row in response["Snapshots"]:
            if row["VolumeId"] == self.source_volume_id and row["Encrypted"]:
                row["KmsKeyId"] = self.key_arn
        return response


@pytest.mark.asyncio
async def test_moto_snapshot_tick_discovers_prices_and_verifies_expiry_after_source_disk_deletion(snapshot_database, monkeypatch, caplog):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        key_arn = boto3.client("kms", region_name="us-east-1").create_key()["KeyMetadata"]["Arn"]
        volume_id = ec2.create_volume(Size=40, AvailabilityZone="us-east-1a", Encrypted=True, KmsKeyId=key_arn)["VolumeId"]
        assert ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]["KmsKeyId"] == key_arn
        tags = [{"Key": key, "Value": value} for key, value in {
            **usage.tags_for(SimpleNamespace(id="box", user_id="owner", deployment_id=DEPLOYMENT), 1),
            "adp:workspace": "persistent",
        }.items()]
        ec2.create_tags(Resources=[volume_id], Tags=tags)
        snapshot_id = ec2.create_snapshot(VolumeId=volume_id, TagSpecifications=[{
            "ResourceType": "snapshot", "Tags": tags,
        }])["SnapshotId"]
        assert ec2.describe_snapshots(SnapshotIds=[snapshot_id])["Snapshots"][0]["Encrypted"] is True
        provider = MotoSnapshotEC2(ec2, volume_id, key_arn)
        template = SimpleNamespace(account_id="123456789012", kms_key_arn=key_arn)
        async with snapshot_database() as db:
            disk = await db.get(DevBoxResource, "disk")
            disk.provider_resource_id = volume_id
            db.add(DevBoxPolicy(id="policy", org_id="org", deployment_id=DEPLOYMENT, enabled=True,
                                max_live_boxes=1, max_running_boxes=1, allowed_sizes=["t3.small"], max_disk_gb=40,
                                snapshot_gb_hour_rate_usd=Decimal("0.000070")))
            await db.commit()

        clock = SimpleNamespace(now=datetime(2026, 10, 23, tzinfo=UTC))
        observe_resource = usage.observe_resource

        async def observe_at_clock(factory, provider_client, resource_id, manifest):
            return await observe_resource(factory, provider_client, resource_id, manifest, now=clock.now)

        monkeypatch.setattr(usage, "observe_resource", observe_at_clock)

        async def tick():
            assert await worker.scan_once(snapshot_database, provider, None, DEPLOYMENT, template, "test-worker") == 0

        await tick()
        async with snapshot_database() as db:
            rows = list((await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "snapshot"))).all())
            assert len(rows) == 1 and rows[0].provider_resource_id == snapshot_id
            assert rows[0].deletion_state == "present" and rows[0].last_observed_state == "present"
            resource_id = rows[0].id

        clock.now += timedelta(hours=24)
        provider.deny_list = True
        await tick()
        assert "Snapshot inventory unavailable: UnauthorizedOperation" in caplog.text
        async with snapshot_database() as db:
            resource = await db.get(DevBoxResource, resource_id)
            intervals = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == resource_id))).all())
            assert resource.last_observed_state == "present" and resource.deletion_state == "present"
            assert len(intervals) == 1 and intervals[0].units == Decimal("960.000000")
            assert intervals[0].cost_usd == Decimal("0.067200")

        provider.deny_list = False
        async with snapshot_database() as db:
            box = await db.get(DevBox, "box")
            box.status = "released"
            box.desired_state = "released"
            disk = await db.get(DevBoxResource, "disk")
            disk.deletion_state = "verified_deleted"
            await db.commit()
        ec2.delete_volume(VolumeId=volume_id)
        clock.now += timedelta(hours=1)
        await tick()
        async with snapshot_database() as db:
            assert (await db.get(DevBoxResource, resource_id)).deletion_state == "present"

        ec2.delete_snapshot(SnapshotId=snapshot_id)
        clock.now += timedelta(hours=1)
        await tick()
        async with snapshot_database() as db:
            assert (await db.get(DevBoxResource, resource_id)).deletion_state == "present"
        clock.now += timedelta(hours=1)
        await tick()
        async with snapshot_database() as db:
            assert (await db.get(DevBoxResource, resource_id)).deletion_state == "verified_deleted"
