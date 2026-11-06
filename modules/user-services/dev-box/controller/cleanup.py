"""Reconcile an explicit release without inventing resource ownership or deletion."""

import asyncio
import logging
import uuid
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.dev_box import (
    DevBox,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxResource,
)

from controller.fencing import (
    claim,
    current,
    finish,
    maintained_lease,
    require_access_fenced,
)
from controller.launch import inventory, ownership_tags, private_host_address
from controller.storage import observed_volume
from controller.state import retire_host_executors

logger = logging.getLogger(__name__)


def provider_instance(ec2, instance, tags):
    try:
        response = ec2.describe_instances(InstanceIds=[instance.provider_instance_id])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
            return None
        raise
    instances = [item for group in response.get("Reservations", []) for item in group.get("Instances", [])]
    if not instances:
        return None
    if len(instances) != 1 or instances[0].get("InstanceId") != instance.provider_instance_id:
        raise ValueError("Recorded machine identity is ambiguous")
    actual = {tag["Key"]: tag["Value"] for tag in instances[0].get("Tags", [])}
    if any(actual.get(key) != value for key, value in tags.items()):
        raise ValueError("Recorded machine is not owned by the releasing box")
    return instances[0]


async def emit(db, operation, event_type, resource_id):
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dev-box:{operation.id}:{event_type}:{resource_id}"))
    if await db.get(DevBoxEvent, identity) is None:
        db.add(DevBoxEvent(
            id=identity, box_id=operation.box_id, user_id=operation.user_id, org_id=operation.org_id,
            deployment_id=operation.deployment_id, machine_generation=operation.machine_generation,
            operation_id=operation.id, event_type=event_type,
        ))


async def report(session_factory, operation_id, worker_id, lease_generation, error):
    async with session_factory() as db:
        operation, _, stale = await current(db, operation_id, worker_id, lease_generation)
        if operation is None:
            await db.rollback()
            return False
        finish(operation, stale or error)
        await db.commit()
    return True


async def release_instance(session_factory, ec2, operation_id, worker_id, lease_generation, instance_id):
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        instance = await db.get(DevBoxInstance, instance_id)
        if instance.box_id != box.id or instance.machine_generation > box.machine_generation:
            await db.rollback()
            return "machine_inventory_unresolved"
        if not instance.provider_instance_id:
            # #8037: resolve_launch proved by inventory that this generation's launch never produced a machine.
            absent = instance.observed_state == "absent"
            await db.rollback()
            return None if absent else "machine_inventory_unresolved"
        tags = {"adp:component": "dev-box", "adp:owner-id": box.user_id,
                "adp:deployment-id": box.deployment_id, "adp:box-id": box.id,
                "adp:generation": str(instance.machine_generation)}
        already_requested = instance.observed_state == "termination_requested"
        await db.commit()
    try:
        observed = await asyncio.to_thread(provider_instance, ec2, instance, tags)
        if observed and observed["State"]["Name"] not in ("terminated", "shutting-down"):
            if already_requested:
                return "machine_deletion_pending"
            async with session_factory() as db:
                _, _, error = await current(db, operation_id, worker_id, lease_generation)
                if error:
                    await db.rollback()
                    return error
                recorded = await db.get(DevBoxInstance, instance_id)
                recorded.observed_state = "termination_requested"
                await db.commit()
            async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                await asyncio.to_thread(ec2.terminate_instances, InstanceIds=[instance.provider_instance_id])
            observed = await asyncio.to_thread(provider_instance, ec2, instance, tags)
        if observed and observed["State"]["Name"] != "terminated":
            return "machine_deletion_pending"
    except ValueError:
        return "machine_ownership_mismatch"
    except (OSError, BotoCoreError, ClientError):
        return "machine_provider_unknown"
    async with session_factory() as db:
        operation, _, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        recorded = await db.get(DevBoxInstance, instance_id)
        recorded.observed_state = "terminated"
        await emit(db, operation, "instance_terminated", recorded.id)
        await db.commit()
    return None


async def release_disk(session_factory, ec2, operation_id, worker_id, lease_generation, resource_id, launch_template):
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        resource = await db.get(DevBoxResource, resource_id)
        if resource.box_id == box.id and resource.resource_kind == "snapshot":
            if resource.deletion_state != "verified_deleted":
                resource.retention_state = "retained"
                await emit(db, operation, "resource_retained", resource.id)
            await db.commit()
            return None
        if resource.box_id != box.id or resource.resource_kind != "workspace_disk" or not resource.provider_resource_id.startswith("vol-"):
            await db.rollback()
            return "unsupported_resource"
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == resource.machine_generation,
        ))
        if not instance or not instance.availability_zone or not launch_template.kms_key_arn:
            await db.rollback()
            return "workspace_inventory_unresolved"
        zone = instance.availability_zone
        deleting = resource.deletion_state == "deleting"
        await db.commit()
    try:
        volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn)
        if operation.retention_choice == "retain":
            if volume is None:
                return "retained_workspace_missing"
            if volume.get("Attachments") or volume.get("State") != "available":
                return "retained_workspace_attached"
        elif operation.retention_choice == "delete":
            if volume and volume.get("Attachments"):
                return "workspace_deletion_pending"
            if volume and not deleting:
                async with session_factory() as db:
                    _, _, error = await current(db, operation_id, worker_id, lease_generation)
                    if error:
                        await db.rollback()
                        return error
                    recorded = await db.get(DevBoxResource, resource_id)
                    recorded.deletion_state = "deleting"
                    await db.commit()
                async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                    await asyncio.to_thread(ec2.delete_volume, VolumeId=resource.provider_resource_id)
                volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn)
            if volume is not None:
                return "workspace_deletion_pending"
        else:
            return "retention_choice_missing"
    except ValueError:
        return "workspace_ownership_mismatch"
    except ClientError as error:
        if operation.retention_choice != "delete" or error.response.get("Error", {}).get("Code") != "InvalidVolume.NotFound":
            return "workspace_provider_unknown"
    except (OSError, BotoCoreError):
        return "workspace_provider_unknown"
    async with session_factory() as db:
        operation, _, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        recorded = await db.get(DevBoxResource, resource_id)
        if operation.retention_choice == "delete":
            recorded.retention_state = "deleted"
            recorded.deletion_state = "verified_deleted"
            await emit(db, operation, "resource_deleted", recorded.id)
        else:
            recorded.retention_state = "retained"
            recorded.deletion_state = "present"
            await emit(db, operation, "resource_retained", recorded.id)
        await db.commit()
    return None


async def resolve_launch(session_factory, ec2, operation_id, worker_id, lease_generation, launch_id, launch_template):
    """Settle a failed launch before release: prove it absent by inventory or adopt the machine it did produce.

    #8037: a refused or unconfirmed launch keeps `provider_request_ref = launch:<token>` so it is never mistaken
    for a resolved machine. Release may proceed only after an inventory read with the launch's own ClientToken and
    ownership tags; a launch that is still pending/unknown, or an inventory read that errors, stays unresolved.
    """
    async with session_factory() as db:
        _, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        launch = await db.get(DevBoxOperation, launch_id)
        if launch is None or launch.status != "failed":
            await db.rollback()
            return "launch_inventory_unresolved"
        # Plain values only: the inventory read runs in a thread after this session is closed.
        intent = SimpleNamespace(id=launch.id, box_id=launch.box_id, provider_token=launch.provider_token,
                                 machine_generation=launch.machine_generation)
        tags = ownership_tags(box, intent)
        await db.rollback()
    try:
        observed = await asyncio.to_thread(inventory, ec2, intent, tags)
    except (ValueError, OSError, BotoCoreError, ClientError):
        return "launch_inventory_unresolved"
    async with session_factory() as db:
        _, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        recorded = await db.get(DevBoxOperation, intent.id)
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == recorded.machine_generation,
        ))
        if observed is None:
            recorded.provider_request_ref = f"launch-absent:{recorded.provider_token}"
            if instance is not None and not instance.provider_instance_id:
                instance.observed_state = "absent"
                instance.readiness_class = None
                instance.error_class = "launch_absent"
            await emit(db, recorded, "launch_absent", recorded.id)
            outcome = "absent"
        else:
            if instance is None:
                instance = DevBoxInstance(
                    id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id,
                    machine_generation=recorded.machine_generation, template_revision=box.template_revision,
                    region=launch_template.region, observed_state="unknown",
                )
                db.add(instance)
            instance.provider_instance_id = observed["InstanceId"]
            instance.provider_account_ref = launch_template.account_id
            instance.availability_zone = observed.get("Placement", {}).get("AvailabilityZone")
            instance.private_address = private_host_address(observed)
            instance.observed_state = observed["State"]["Name"]
            recorded.provider_request_ref = observed["InstanceId"]
            await emit(db, recorded, "launch_adopted", recorded.id)
            outcome = f"adopted:{observed['InstanceId']}"
        await db.commit()
    logger.info("dev-box release resolved failed launch operation_id=%s launch_operation_id=%s box_id=%s outcome=%s",
                operation_id, intent.id, intent.box_id, outcome)
    return None


async def reconcile_release(session_factory, ec2, operation_id, worker_id, launch_template):
    lease_generation = await claim(session_factory, operation_id, worker_id, ("release",))
    if lease_generation is None:
        return False
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if operation is None:
            await db.rollback()
            return False
        if error or operation.retention_choice not in ("retain", "delete"):
            finish(operation, error or "retention_choice_missing")
            await db.commit()
            return True
        fence_error = await require_access_fenced(box, "release")
        if fence_error:
            finish(operation, fence_error)
            await db.commit()
            return True
        unresolved = list(await db.scalars(select(DevBoxOperation.id).where(
            DevBoxOperation.box_id == box.id, DevBoxOperation.action.in_(("setup", "replace")),
            DevBoxOperation.provider_request_ref.like("launch:%"),
        ).order_by(DevBoxOperation.created_at, DevBoxOperation.id)))
        await db.commit()
    for launch_id in unresolved:
        error = await resolve_launch(session_factory, ec2, operation_id, worker_id, lease_generation, launch_id, launch_template)
        if error:
            return await report(session_factory, operation_id, worker_id, lease_generation, error)
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if operation is None:
            await db.rollback()
            return False
        if error:
            finish(operation, error)
            await db.commit()
            return True
        instances = list(await db.scalars(select(DevBoxInstance.id).where(DevBoxInstance.box_id == box.id).order_by(DevBoxInstance.machine_generation)))
        resources = list(await db.scalars(select(DevBoxResource.id).where(
            DevBoxResource.box_id == box.id, DevBoxResource.resource_kind.in_(("workspace_disk", "snapshot")),
        )))
        await db.commit()
    for instance_id in instances:
        error = await release_instance(session_factory, ec2, operation_id, worker_id, lease_generation, instance_id)
        if error:
            return await report(session_factory, operation_id, worker_id, lease_generation, error)
    for resource_id in resources:
        error = await release_disk(session_factory, ec2, operation_id, worker_id, lease_generation, resource_id, launch_template)
        if error:
            return await report(session_factory, operation_id, worker_id, lease_generation, error)
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return False
        box.status = "released"
        box.observed_state = "released"
        box.released_at = utcnow()
        # Every machine and disk of this box is gone, so no lease can be live: retire the
        # owner's executor rows for it, or the next box can never bind one (#8223 cause 23).
        if await retire_host_executors(db, box):
            await emit(db, operation, "host_executor_retired", box.id)
        operation.status = "succeeded"
        operation.progress = "release_verified"
        operation.error_class = None
        await emit(db, operation, "release_verified", box.id)
        finish(operation)
        await db.commit()
    return True


# #8223 cause 53: a Replace detaches the workspace disk from the previous machine and leaves that machine
# `stopped` forever; nothing retired it. Six such machines of one box held six addresses of a /24 that EKS
# had otherwise exhausted, and the next Replace could not get an address. Once the new generation is
# verified ready, the superseded machine is terminated with the release path's checks: the recorded
# provider id must still carry this box's ownership tags for its generation, the request is recorded
# (`termination_requested`) before it is sent, and the row only reads `terminated` after EC2 says so.
# Usage accounting (controller/usage.py) marks the instance resource `verified_deleted` on that state.
# The workspace disk is never touched here; the current generation and other boxes are never selected.
RETIRE_BATCH = 8


async def retire_superseded_instances(session_factory, ec2, deployment_id):
    """Terminate stopped machines of generations a verified-ready box has replaced. Returns the count acted on."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(DevBoxInstance.id, DevBoxInstance.box_id)
                .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                .where(
                    DevBox.deployment_id == deployment_id,
                    DevBox.status == "running",
                    DevBox.observed_state == "running",
                    DevBox.auth_readiness == "ready",
                    DevBoxInstance.machine_generation < DevBox.machine_generation,
                    DevBoxInstance.provider_instance_id.is_not(None),
                    DevBoxInstance.observed_state.in_(("stopped", "termination_requested")),
                )
                .order_by(DevBoxInstance.machine_generation)
                .limit(RETIRE_BATCH)
            )
        ).all()
    acted = 0
    for instance_id, box_id in rows:
        acted += 1 if await retire_superseded_instance(session_factory, ec2, instance_id, box_id) else 0
    return acted


async def retire_superseded_instance(session_factory, ec2, instance_id, box_id):
    async with session_factory() as db:
        instance = await db.get(DevBoxInstance, instance_id)
        box = await db.get(DevBox, box_id)
        if (instance is None or box is None or instance.box_id != box.id or not instance.provider_instance_id
                or instance.machine_generation >= box.machine_generation
                or box.status != "running" or box.auth_readiness != "ready"
                or instance.observed_state not in ("stopped", "termination_requested")):
            await db.rollback()
            return False
        tags = {"adp:component": "dev-box", "adp:owner-id": box.user_id,
                "adp:deployment-id": box.deployment_id, "adp:box-id": box.id,
                "adp:generation": str(instance.machine_generation)}
        already_requested = instance.observed_state == "termination_requested"
        provider_id = instance.provider_instance_id
        generation = instance.machine_generation
        current_generation = box.machine_generation
        await db.rollback()
    recorded_machine = SimpleNamespace(provider_instance_id=provider_id)
    try:
        observed = await asyncio.to_thread(provider_instance, ec2, recorded_machine, tags)
        if observed and observed["State"]["Name"] not in ("terminated", "shutting-down"):
            if not already_requested:
                async with session_factory() as db:
                    recorded = await db.get(DevBoxInstance, instance_id)
                    if recorded is None or recorded.observed_state != "stopped":
                        await db.rollback()
                        return False
                    recorded.observed_state = "termination_requested"
                    await db.commit()
            await asyncio.to_thread(ec2.terminate_instances, InstanceIds=[provider_id])
            observed = await asyncio.to_thread(provider_instance, ec2, recorded_machine, tags)
    except ValueError:
        logger.warning("dev-box superseded machine not retired: box=%s generation=%s instance=%s reason=ownership_mismatch",
                       box_id, generation, provider_id)
        return False
    except (OSError, BotoCoreError, ClientError) as error:
        logger.warning("dev-box superseded machine not retired: box=%s generation=%s instance=%s reason=%s",
                       box_id, generation, provider_id, type(error).__name__)
        return False
    if observed and observed["State"]["Name"] != "terminated":
        # Requested; a later tick observes the terminated state and settles the row.
        return True
    async with session_factory() as db:
        recorded = await db.get(DevBoxInstance, instance_id)
        if recorded is None:
            await db.rollback()
            return False
        recorded.observed_state = "terminated"
        # The event hangs off the operation that produced the current generation (events need an operation).
        operation = await db.scalar(
            select(DevBoxOperation)
            .where(DevBoxOperation.box_id == box_id, DevBoxOperation.machine_generation == current_generation,
                   DevBoxOperation.action.in_(("replace", "setup")), DevBoxOperation.status == "succeeded")
            .order_by(DevBoxOperation.created_at.desc())
            .limit(1)
        )
        if operation is not None:
            await emit(db, operation, "superseded_machine_retired", recorded.id)
        await db.commit()
    logger.info("dev-box superseded machine retired: box=%s generation=%s instance=%s", box_id, generation, provider_id)
    return True
