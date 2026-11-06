"""Observe only recorded, owned infrastructure and accrue attributable intervals."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from src.shared.models.base import new_uuid
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxPolicy, DevBoxResource

from controller.cost import record_interval
from controller.state import owned_instance
from controller.storage import observed_volume

logger = logging.getLogger(__name__)


def tags_for(box, generation):
    return {
        "adp:component": "dev-box", "adp:deployment-id": box.deployment_id,
        "adp:owner-id": box.user_id, "adp:box-id": box.id, "adp:generation": str(generation),
    }


def snapshot(ec2, resource, tags):
    try:
        response = ec2.describe_snapshots(SnapshotIds=[resource.provider_resource_id])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "InvalidSnapshot.NotFound":
            return None
        raise
    snapshots = response.get("Snapshots", [])
    if not snapshots:
        return None
    if len(snapshots) != 1 or snapshots[0].get("SnapshotId") != resource.provider_resource_id:
        raise ValueError("Snapshot provider inventory is not unique")
    recorded = snapshots[0]
    actual = {tag["Key"]: tag["Value"] for tag in recorded.get("Tags", [])}
    if any(actual.get(key) != value for key, value in tags.items()) or recorded.get("VolumeSize") != resource.size_gb:
        raise ValueError("Snapshot ownership or size is unverified")
    return recorded


def instance_rate(policy, instance, launch_template):
    """Hourly rate of the size that actually launched (#8423), else the policy's preferred-size rate.

    A capacity fallback may launch a different admitted size than the template's; the release
    manifest prices every size in the plan, and the instance row records which one it got.
    """
    rates = getattr(launch_template, "instance_hour_rates_usd", None) or {}
    size = getattr(instance, "instance_size", None)
    if size and size in rates:
        try:
            return Decimal(str(rates[size]))
        except (InvalidOperation, ValueError):
            return None
    return policy.instance_hour_rate_usd


async def observe_resource(session_factory, ec2, resource_id, launch_template, *, now=None):
    now = now or datetime.now(UTC)
    async with session_factory() as db:
        resource = await db.get(DevBoxResource, resource_id)
        if resource is None or resource.deletion_state == "verified_deleted" or resource.provider_resource_id.startswith("intent:"):
            return False
        box = await db.get(DevBox, resource.box_id)
        policy = await db.scalar(select(DevBoxPolicy).where(
            DevBoxPolicy.org_id == resource.org_id, DevBoxPolicy.deployment_id == resource.deployment_id,
        ))
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == resource.box_id, DevBoxInstance.machine_generation == resource.machine_generation,
        ))
        if (box is None or policy is None or (box.user_id, box.org_id, box.deployment_id)
                != (resource.user_id, resource.org_id, resource.deployment_id)):
            return False
        provider_id = resource.provider_resource_id
        kind = resource.resource_kind
        rate = {
            "instance": instance_rate(policy, instance, launch_template),
            "workspace_disk": policy.disk_gb_hour_rate_usd,
            "snapshot": policy.snapshot_gb_hour_rate_usd,
        }.get(kind)
        if rate is None or rate <= 0:
            return False
        tags = tags_for(box, resource.machine_generation)
        if kind == "instance" and (not instance or instance.provider_instance_id != provider_id):
            return False
        if kind == "workspace_disk" and (not instance or not instance.availability_zone
                                              or not launch_template or not launch_template.kms_key_arn):
            return False
    observed = None
    try:
        if kind == "instance":
            observed = await asyncio.to_thread(owned_instance, ec2, provider_id, tags)
            state = observed["State"]["Name"]
        elif kind == "workspace_disk":
            observed = await asyncio.to_thread(observed_volume, ec2, box, resource, instance.availability_zone, launch_template.kms_key_arn)
            state = "present" if observed is not None else "unknown"
        elif kind == "snapshot":
            observed = await asyncio.to_thread(snapshot, ec2, resource, tags)
            state = "present" if observed is not None else "missing"
        else:
            return False
    except (ValueError, KeyError, AttributeError, OSError, BotoCoreError, ClientError):
        state = "unknown"
    async with session_factory() as db:
        resource = await db.scalar(select(DevBoxResource).where(DevBoxResource.id == resource_id).with_for_update())
        if resource is None or resource.provider_resource_id != provider_id or resource.deletion_state == "verified_deleted":
            await db.rollback()
            return False
        if resource.last_observed_at and now <= resource.last_observed_at.replace(tzinfo=UTC):
            await db.rollback()
            return False
        previous = resource.last_observed_state
        if (state in ("unknown", "missing") or (resource.last_rate_usd is not None and resource.last_rate_usd != rate)) and resource.coverage_gap_at is None:
            resource.coverage_gap_at = resource.last_observed_at or now
        if (resource.last_observed_at and resource.last_rate_usd == rate
                and now > resource.last_observed_at.replace(tzinfo=UTC)
                and previous in ("present", "running") and state in (previous, "stopped", "terminated")):
            await record_interval(db, resource.id, resource.last_observed_at, now, rate, f"policy:{policy.id}:{rate}")
        resource.last_observed_state = state
        resource.last_observed_at = now
        resource.last_rate_usd = rate
        if kind == "instance" and state == "terminated":
            resource.deletion_state = "verified_deleted"
        if kind == "snapshot" and state == "missing" and previous == "missing":
            resource.deletion_state = "verified_deleted"
            resource.retention_state = "deleted"
        await db.commit()
    return state != "unknown"


def listed_snapshots(ec2, deployment_id, account_id):
    filters = [
        {"Name": "tag:adp:component", "Values": ["dev-box"]},
        {"Name": "tag:adp:deployment-id", "Values": [deployment_id]},
        {"Name": "tag:adp:workspace", "Values": ["persistent"]},
    ]
    token = None
    while True:
        request = {"OwnerIds": [account_id], "Filters": filters, "MaxResults": 1000}
        if token:
            request["NextToken"] = token
        response = ec2.describe_snapshots(**request)
        yield from response.get("Snapshots", [])
        token = response.get("NextToken")
        if not token:
            break


async def inventory_snapshots(session_factory, ec2, deployment_id, launch_template):
    if not launch_template or not launch_template.account_id or not launch_template.kms_key_arn:
        return
    snapshots = await asyncio.to_thread(lambda: list(listed_snapshots(ec2, deployment_id, launch_template.account_id)))
    async with session_factory() as db:
        disks = list((await db.scalars(select(DevBoxResource).where(
            DevBoxResource.deployment_id == deployment_id,
            DevBoxResource.resource_kind == "workspace_disk",
        ))).all())
        boxes = {box.id: box for box in (await db.scalars(select(DevBox).where(
            DevBox.deployment_id == deployment_id,
        ).order_by(DevBox.id).with_for_update())).all()}
        existing = {row.provider_resource_id: row for row in (await db.scalars(select(DevBoxResource).where(
            DevBoxResource.resource_kind == "snapshot",
        ))).all()}
        disks_by_volume = {disk.provider_resource_id: disk for disk in disks if disk.provider_resource_id.startswith("vol-") and disk.encrypted is True}
        for observed in snapshots:
            tags = {tag["Key"]: tag["Value"] for tag in observed.get("Tags", [])}
            source = disks_by_volume.get(observed.get("VolumeId"))
            box = boxes.get(source.box_id) if source else None
            snapshot_id = observed.get("SnapshotId", "")
            if (not snapshot_id.startswith("snap-")
                    or not box or tags.get("adp:workspace") != "persistent"
                    or any(tags.get(key) != value for key, value in tags_for(box, source.machine_generation).items())
                    or (source.user_id, source.org_id, source.deployment_id) != (box.user_id, box.org_id, box.deployment_id)
                    or observed.get("OwnerId") != launch_template.account_id
                    or observed.get("VolumeSize") != source.size_gb
                    or observed.get("Encrypted") is not True
                    or observed.get("KmsKeyId") != launch_template.kms_key_arn):
                continue
            start_time = observed.get("StartTime")
            created_at = start_time if isinstance(start_time, datetime) and start_time.tzinfo else None
            recorded = existing.get(snapshot_id)
            if recorded:
                if ((recorded.box_id, recorded.user_id, recorded.org_id, recorded.deployment_id, recorded.machine_generation)
                        == (box.id, box.user_id, box.org_id, box.deployment_id, source.machine_generation)
                        and recorded.snapshot_created_at is None):
                    recorded.snapshot_created_at = created_at
                continue
            resource = DevBoxResource(
                id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id,
                deployment_id=deployment_id, machine_generation=source.machine_generation,
                resource_kind="snapshot", provider_resource_id=snapshot_id,
                size_gb=source.size_gb, encrypted=True, retention_state="retained", deletion_state="present",
                snapshot_created_at=created_at,
            )
            db.add(resource)
            existing[snapshot_id] = resource
        await db.commit()


async def reconcile_usage(session_factory, ec2, deployment_id, launch_template):
    try:
        await inventory_snapshots(session_factory, ec2, deployment_id, launch_template)
    except ClientError as error:
        logger.warning("Snapshot inventory unavailable: %s", error.response.get("Error", {}).get("Code", "unknown"))
    except (BotoCoreError, OSError):
        logger.warning("Snapshot inventory unavailable")
    async with session_factory() as db:
        instances = list((await db.scalars(select(DevBoxInstance).where(
            DevBoxInstance.deployment_id == deployment_id, DevBoxInstance.provider_instance_id.is_not(None),
        ))).all())
        for instance in instances:
            resource = await db.scalar(select(DevBoxResource.id).where(
                DevBoxResource.box_id == instance.box_id, DevBoxResource.resource_kind == "instance",
                DevBoxResource.machine_generation == instance.machine_generation,
            ))
            if resource is None:
                db.add(DevBoxResource(
                    id=new_uuid(), box_id=instance.box_id, user_id=instance.user_id, org_id=instance.org_id,
                    deployment_id=instance.deployment_id, machine_generation=instance.machine_generation,
                    resource_kind="instance", provider_resource_id=instance.provider_instance_id,
                    retention_state="temporary", deletion_state="present",
                ))
        await db.commit()
    async with session_factory() as db:
        resource_ids = list((await db.scalars(select(DevBoxResource.id).where(
            DevBoxResource.deployment_id == deployment_id, DevBoxResource.resource_kind.in_(("instance", "workspace_disk", "snapshot")),
            DevBoxResource.deletion_state != "verified_deleted",
        ))).all())
    for resource_id in resource_ids:
        await observe_resource(session_factory, ec2, resource_id, launch_template)
