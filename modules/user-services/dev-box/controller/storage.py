"""Create and reattach one attributed workspace disk across machine generations."""

import asyncio
import hashlib

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from src.shared.models.base import new_uuid
from src.shared.models.dev_box import DevBoxInstance, DevBoxOperation, DevBoxResource

from controller.fencing import current, maintained_lease, require_access_fenced

# Progress markers of the attach step, in order. `attachment_requested` is persisted immediately before the
# AttachVolume call (#8100) so a controller that restarts mid-operation knows an attach may already have
# reached EC2 and judges the observed attachment instead of treating its own new instance as foreign.
ATTACH_PROGRESS = ("volume_intent", "attachment_intent", "attachment_requested")
# Progress values at which the replace's detach-from-previous step has already completed: the disk was
# observed free before the launch, so the step must not reset progress (that would drop the #8037 same-token
# launch retry) and must expect the disk to be on the operation's own instance from now on.
PAST_DETACH_PROGRESS = ("launch_intent", "instance_observed", *ATTACH_PROGRESS)


class ObservationError(str):
    """An operation error class that also carries a StateReason-style detail for the controller log (#8100)."""

    def __new__(cls, code, detail=None):
        value = super().__new__(cls, code)
        value.detail = detail
        return value


def disk_token(box_id):
    return hashlib.sha256(f"dev-box-workspace:{box_id}".encode()).hexdigest()


def volume_tags(box, resource):
    return {
        "adp:component": "dev-box", "adp:deployment-id": box.deployment_id,
        "adp:owner-id": box.user_id, "adp:box-id": box.id,
        "adp:generation": str(resource.machine_generation),
        "adp:workspace": "persistent",
    }


# Retained disks are gp3: a 100 GB gp2 disk has a 300 IOPS baseline, gp3 has 3000 IOPS and 125 MB/s at
# any size and costs less per GB (dev 2026-10-26: the owner's disk was gp2).
WORKSPACE_VOLUME_TYPE = "gp3"
# EBS reports the new size seconds after ModifyVolume is accepted; the attach waits this long for it.
GROW_OBSERVATIONS = 15
GROW_POLL_SECONDS = 2
# ModifyVolume refusals that mean "not now" (one modification per volume per six hours; a
# modification still optimizing): the box starts on its current size and the next operation retries.
GROW_DEFERRED_CODES = frozenset({"VolumeModificationRateExceeded", "IncorrectModificationState"})


def observed_volume(ec2, box, resource, zone, kms_key, grow_to=None):
    """The owned disk, or None. `grow_to`: a size the disk may already report after an accepted ModifyVolume
    whose record was lost (#8835 disk growth); the recorded size is the other admitted value."""
    tags = volume_tags(box, resource)
    filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in tags.items()]
    if resource.provider_resource_id.startswith("vol-"):
        response = ec2.describe_volumes(VolumeIds=[resource.provider_resource_id])
    else:
        response = ec2.describe_volumes(Filters=filters)
    volumes = response.get("Volumes", [])
    if len(volumes) > 1:
        raise ValueError("Multiple workspace volumes match one logical box")
    if not volumes:
        return None
    volume = volumes[0]
    actual = {tag["Key"]: tag["Value"] for tag in volume.get("Tags", [])}
    if (any(actual.get(key) != value for key, value in tags.items())
            or not volume.get("Encrypted") or volume.get("KmsKeyId") != kms_key
            or volume.get("Size") not in ({resource.size_gb, grow_to} if grow_to else {resource.size_gb}) or volume.get("AvailabilityZone") != zone
            or (resource.provider_resource_id.startswith("vol-") and volume.get("VolumeId") != resource.provider_resource_id)):
        raise ValueError("Workspace disk ownership or encryption does not match intent")
    return volume


def restore_volume_from_snapshot(ec2, box, snapshot_resource, source_disk, target_disk, zone, kms_key, account_id):
    scope = (box.id, box.user_id, box.org_id, box.deployment_id)
    if (snapshot_resource.resource_kind != "snapshot" or source_disk.resource_kind != "workspace_disk"
            or target_disk.resource_kind != "workspace_disk" or snapshot_resource.deletion_state == "verified_deleted"
            or not snapshot_resource.provider_resource_id.startswith("snap-")
            or not source_disk.provider_resource_id.startswith("vol-")
            or any((row.box_id, row.user_id, row.org_id, row.deployment_id) != scope
                   for row in (snapshot_resource, source_disk, target_disk))
            or source_disk.machine_generation != snapshot_resource.machine_generation
            or target_disk.machine_generation <= source_disk.machine_generation
            or any(row.encrypted is not True for row in (snapshot_resource, source_disk, target_disk))
            or source_disk.size_gb != snapshot_resource.size_gb
            # The template may have grown since the snapshot: a larger target is filled by the guest's
            # xfs_growfs; a smaller one cannot hold the snapshot.
            or target_disk.size_gb < snapshot_resource.size_gb
            or not zone or not kms_key or not account_id):
        raise ValueError("Workspace snapshot restore ownership is unverified")
    response = ec2.describe_snapshots(SnapshotIds=[snapshot_resource.provider_resource_id])
    snapshots = response.get("Snapshots", [])
    if len(snapshots) != 1:
        raise ValueError("Workspace snapshot is not available")
    observed = snapshots[0]
    actual = {tag["Key"]: tag["Value"] for tag in observed.get("Tags", [])}
    expected = volume_tags(box, source_disk)
    if (observed.get("SnapshotId") != snapshot_resource.provider_resource_id
            or observed.get("VolumeId") != source_disk.provider_resource_id
            or observed.get("OwnerId") != account_id
            or observed.get("VolumeSize") != source_disk.size_gb
            or observed.get("Encrypted") is not True or observed.get("KmsKeyId") != kms_key
            or observed.get("State") != "completed"
            or any(actual.get(key) != value for key, value in expected.items())):
        raise ValueError("Workspace snapshot source, encryption or ownership is unverified")
    volume = observed_volume(ec2, box, target_disk, zone, kms_key)
    if volume is None:
        tag_list = [{"Key": key, "Value": value} for key, value in volume_tags(box, target_disk).items()]
        token = hashlib.sha256(f"dev-box-restore:{box.id}:{target_disk.machine_generation}:{snapshot_resource.provider_resource_id}".encode()).hexdigest()
        ec2.create_volume(
            AvailabilityZone=zone, SnapshotId=snapshot_resource.provider_resource_id,
            Size=target_disk.size_gb, VolumeType=WORKSPACE_VOLUME_TYPE, Encrypted=True, KmsKeyId=kms_key, ClientToken=token,
            TagSpecifications=[{"ResourceType": "volume", "Tags": tag_list}],
        )
        volume = observed_volume(ec2, box, target_disk, zone, kms_key)
    if volume is not None and volume.get("SnapshotId") != snapshot_resource.provider_resource_id:
        raise ValueError("Workspace volume was not created from the selected snapshot")
    return volume


def attachment(volume, target_id, previous_id):
    """Locate the disk: ("none", volume state), or ("target" | "previous" | "foreign", attachment state).

    #8100: the operation's own instance (`target_id`, the generation it launched) and the instance it replaces
    (`previous_id`) are both expected holders during a replace; only a third instance is "foreign".
    """
    attachments = volume.get("Attachments", [])
    if not attachments:
        return "none", volume.get("State")
    if len(attachments) != 1:
        return "foreign", None
    instance_id, state = attachments[0].get("InstanceId"), attachments[0].get("State")
    if target_id and instance_id == target_id:
        return "target", state
    if previous_id and instance_id == previous_id:
        return "previous", state
    return "foreign", state


def attached_elsewhere(volume, target_id, previous_id):
    holders = ", ".join(f"{item.get('InstanceId')} ({item.get('State')})" for item in volume.get("Attachments", []))
    return ObservationError(
        "workspace_attached_elsewhere",
        f"workspace volume {volume.get('VolumeId')} is attached to {holders}, which is neither the previous instance "
        f"{previous_id or 'none'} nor this operation's instance {target_id or 'not yet launched'}",
    )


def not_yet(code, volume, where, state):
    return ObservationError(code, f"workspace volume {volume.get('VolumeId')} is {state} on the {where} instance")


def grow_volume(ec2, volume_id, size_gb):
    """Ask EBS to grow (and move to gp3) the owned disk; True when accepted, False when deferred.

    Growth is online: EBS enlarges an attached volume in place and the guest's mount command runs
    `xfs_growfs` on every boot (identity.py), so the owner's files are never copied or re-created.
    A refusal with a GROW_DEFERRED_CODES code is not an error of this operation: the box starts on
    its current size and a later operation grows it.
    """
    try:
        ec2.modify_volume(VolumeId=volume_id, Size=size_gb, VolumeType=WORKSPACE_VOLUME_TYPE)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in GROW_DEFERRED_CODES:
            return False
        raise
    return True


async def workspace(session_factory, ec2, operation_id, worker_id, lease_generation, instance, launch_template, *, allow_create=True, restore_snapshot_id=None):
    if launch_template.disk_gb is None:
        return None if allow_create else "workspace_policy_missing"
    if not launch_template.kms_key_arn or not instance.get("Placement", {}).get("AvailabilityZone"):
        return "workspace_policy_missing"
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error
        resources = list(await db.scalars(select(DevBoxResource).where(
            DevBoxResource.box_id == box.id, DevBoxResource.resource_kind == "workspace_disk",
            DevBoxResource.deletion_state != "verified_deleted",
            DevBoxResource.machine_generation == box.machine_generation if restore_snapshot_id else DevBoxResource.machine_generation <= box.machine_generation,
        ).order_by(DevBoxResource.machine_generation.desc()).limit(2).with_for_update()))
        if len(resources) > 1 and resources[0].machine_generation == resources[1].machine_generation:
            await db.rollback()
            return "workspace_inventory_ambiguous"
        resource = resources[0] if resources else None
        snapshot_resource = None
        source_disk = None
        if restore_snapshot_id:
            snapshot_resource = await db.scalar(select(DevBoxResource).where(
                DevBoxResource.box_id == box.id, DevBoxResource.user_id == box.user_id,
                DevBoxResource.org_id == box.org_id, DevBoxResource.deployment_id == box.deployment_id,
                DevBoxResource.resource_kind == "snapshot", DevBoxResource.provider_resource_id == restore_snapshot_id,
                DevBoxResource.deletion_state != "verified_deleted",
            ))
            if snapshot_resource is None:
                await db.rollback()
                return "snapshot_ownership_mismatch"
            source_disk = await db.scalar(select(DevBoxResource).where(
                DevBoxResource.box_id == box.id, DevBoxResource.resource_kind == "workspace_disk",
                DevBoxResource.machine_generation == snapshot_resource.machine_generation,
            ))
            if source_disk is None:
                await db.rollback()
                return "snapshot_ownership_mismatch"
        if resource is None:
            if not allow_create:
                await db.rollback()
                return "workspace_missing"
            resource = DevBoxResource(
                id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id,
                deployment_id=box.deployment_id, machine_generation=box.machine_generation,
                resource_kind="workspace_disk", provider_resource_id=f"intent:{disk_token(box.id)}:{box.machine_generation}" if restore_snapshot_id else f"intent:{disk_token(box.id)}",
                size_gb=launch_template.disk_gb, encrypted=True, retention_state="retained", deletion_state="unknown",
            )
            db.add(resource)
        # #8835 disk growth: a template larger than the recorded disk grows the disk in place (below);
        # a template smaller than the disk is a policy mismatch, EBS never shrinks a volume.
        grow_to = None
        if resource.size_gb < launch_template.disk_gb:
            if resource.provider_resource_id.startswith("vol-"):
                grow_to = launch_template.disk_gb
            else:
                resource.size_gb = launch_template.disk_gb  # nothing exists yet: the intent simply follows the template
        if resource.size_gb > launch_template.disk_gb or resource.encrypted is not True or resource.retention_state != "retained":
            await db.rollback()
            return "workspace_policy_mismatch"
        previous_id = await db.scalar(select(DevBoxInstance.provider_instance_id).where(
            *previous_machine_filter(box.id, operation.machine_generation)
        ).order_by(previous_machine_order()).limit(1))
        if operation.progress not in ATTACH_PROGRESS:
            operation.progress = "volume_intent"
        await db.commit()

    zone = instance["Placement"]["AvailabilityZone"]
    target_id = instance["InstanceId"]
    try:
        volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn, grow_to)
        if restore_snapshot_id and allow_create:
            async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                volume = await asyncio.to_thread(
                    restore_volume_from_snapshot, ec2, box, snapshot_resource, source_disk, resource,
                    zone, launch_template.kms_key_arn, launch_template.account_id,
                )
        elif volume is None and allow_create:
            tag_list = [{"Key": key, "Value": value} for key, value in volume_tags(box, resource).items()]
            async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                await asyncio.to_thread(
                    ec2.create_volume, AvailabilityZone=zone, Size=resource.size_gb, VolumeType=WORKSPACE_VOLUME_TYPE, Encrypted=True,
                    KmsKeyId=launch_template.kms_key_arn, ClientToken=disk_token(box.id),
                    TagSpecifications=[{"ResourceType": "volume", "Tags": tag_list}],
                )
            volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn)
        if volume is None:
            return "workspace_not_observed"
        if grow_to is not None and volume.get("Size") != grow_to:
            async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                accepted = await asyncio.to_thread(grow_volume, ec2, volume["VolumeId"], grow_to)
                for _ in range(GROW_OBSERVATIONS if accepted else 0):
                    described = await asyncio.to_thread(ec2.describe_volumes, VolumeIds=[volume["VolumeId"]])
                    if described.get("Volumes", [{}])[0].get("Size") == grow_to:
                        break
                    await asyncio.sleep(GROW_POLL_SECONDS)
                else:
                    if accepted:
                        return ObservationError("workspace_growing", f"workspace volume {volume['VolumeId']} still reports {volume.get('Size')} GB while growing to {grow_to} GB")
            if not accepted:
                grow_to = None  # deferred by EBS: start on the current size, a later operation grows it
        if grow_to is not None:
            # Record the new size before any further observation compares against it.
            async with session_factory() as db:
                _, _, error = await current(db, operation_id, worker_id, lease_generation)
                if error:
                    await db.rollback()
                    return error
                recorded = await db.get(DevBoxResource, resource.id)
                recorded.size_gb = grow_to
                await db.commit()
            resource.size_gb = grow_to
            volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn)
            if volume is None:
                return "workspace_not_observed"
        async with session_factory() as db:
            _, _, error = await current(db, operation_id, worker_id, lease_generation)
            if error:
                await db.rollback()
                return error
            recorded = await db.get(DevBoxResource, resource.id)
            recorded.provider_resource_id = volume["VolumeId"]
            recorded.deletion_state = "present"
            operation = await db.get(DevBoxOperation, operation_id)
            if operation.progress not in ("attachment_intent", "attachment_requested"):
                operation.progress = "attachment_intent"
            await db.commit()
        where, state = attachment(volume, target_id, previous_id)
        if where == "target":
            return None if state == "attached" else not_yet("attachment_not_observed", volume, "target", state)
        if where == "previous":
            return not_yet("detach_not_observed", volume, "previous", state)
        if where == "foreign":
            return attached_elsewhere(volume, target_id, previous_id)
        if state != "available":
            return ObservationError("workspace_not_observed", f"workspace volume {volume['VolumeId']} is {state}, not available")
        # A disk that reads `available` has no attach in flight (EC2 moves it to `attaching` synchronously), so
        # re-sending AttachVolume after a lost response or a restart cannot create a second attachment; the
        # marker below is what tells the recovering controller that the request may already have reached EC2.
        async with session_factory() as db:
            _, _, error = await current(db, operation_id, worker_id, lease_generation)
            if error:
                await db.rollback()
                return error
            operation = await db.get(DevBoxOperation, operation_id)
            operation.progress = "attachment_requested"
            await db.commit()
        async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
            await asyncio.to_thread(ec2.attach_volume, VolumeId=volume["VolumeId"], InstanceId=target_id, Device="/dev/xvdf")
        volume = await asyncio.to_thread(observed_volume, ec2, box, resource, zone, launch_template.kms_key_arn)
        if volume is None:
            return "attachment_not_observed"
        where, state = attachment(volume, target_id, previous_id)
        if where == "target" and state == "attached":
            return None
        return not_yet("attachment_not_observed", volume, where, state)
    except ValueError:
        return "snapshot_ownership_mismatch" if restore_snapshot_id else "workspace_ownership_mismatch"
    except (OSError, BotoCoreError, ClientError):
        return "workspace_provider_unknown"


def previous_machine_filter(box_id, below_generation: int):
    """Where-clause pieces selecting the machine a generation ``below_generation`` replaces.

    Not ``machine_generation == below_generation - 1``: a Replace whose launch never produced a
    machine (dev 2026-10-21, gen 29: the controller restarted between the detach and
    RunInstances, #8223 cause 49) still consumed an owner generation, so the next Replace
    (gen 30) found no row at 29 and refused forever with
    ``replacement_not_stopped_or_missing_workspace``. The machine being replaced is the latest
    recorded one below the new generation.
    """
    return (DevBoxInstance.box_id == box_id, DevBoxInstance.machine_generation < below_generation,
            DevBoxInstance.provider_instance_id.is_not(None), DevBoxInstance.observed_state != "terminated")


def previous_machine_order():
    return DevBoxInstance.machine_generation.desc()


async def prepare_replacement(session_factory, ec2, operation_id, worker_id, lease_generation, launch_template, *, fence=None,
                              target_instance_id=None, restore_snapshot_id=None):
    """Observe the workspace disk free of the previous machine before a replace launches, detaching it once if needed.

    `target_instance_id` is the instance this operation launched (recorded generation row or inventory by the
    launch token), so a recovering controller recognises the disk already attached there as the attach step's
    result rather than a foreign attachment (#8100).
    """
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if error:
            await db.rollback()
            return error

        previous = await db.scalar(select(DevBoxInstance).where(
            *previous_machine_filter(box.id, box.machine_generation)
        ).order_by(previous_machine_order()).limit(1))
        target_id = target_instance_id or await db.scalar(select(DevBoxInstance.provider_instance_id).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == box.machine_generation,
        ))
        disks = list(await db.scalars(select(DevBoxResource).where(
            DevBoxResource.box_id == box.id, DevBoxResource.resource_kind == "workspace_disk",
            DevBoxResource.deletion_state != "verified_deleted",
            DevBoxResource.machine_generation < box.machine_generation,
        ).order_by(DevBoxResource.machine_generation.desc()).limit(2)))
        if len(disks) > 1 and disks[0].machine_generation == disks[1].machine_generation:
            await db.rollback()
            return "workspace_inventory_ambiguous"
        disk = disks[0] if disks else None
        if (not previous or not previous.provider_instance_id or previous.observed_state != "stopped"
                or disk is None or disk.encrypted is not True or disk.retention_state != "retained"):
            await db.rollback()
            return "replacement_not_stopped_or_missing_workspace"
        error = await require_access_fenced(box, "replace", fence, db=db)
        if error:
            await db.rollback()
            return error
        first_detach = operation.progress != "detach_intent"
        await db.commit()
    if launch_template.disk_gb is None or not launch_template.kms_key_arn or not previous.availability_zone:
        return "workspace_policy_missing"
    # #8835 disk growth: EBS may already report the template's larger size after an accepted ModifyVolume
    # whose record was lost; the attach step records it (workspace()).
    grow_to = launch_template.disk_gb if launch_template.disk_gb > disk.size_gb else None
    try:
        response = await asyncio.to_thread(ec2.describe_instances, InstanceIds=[previous.provider_instance_id])
        instances = [item for group in response.get("Reservations", []) for item in group.get("Instances", [])]
        old_tags = {"adp:component": "dev-box", "adp:deployment-id": box.deployment_id,
                    "adp:owner-id": box.user_id, "adp:box-id": box.id,
                    "adp:generation": str(previous.machine_generation)}
        if len(instances) != 1 or instances[0].get("InstanceId") != previous.provider_instance_id or instances[0]["State"]["Name"] != "stopped":
            return "replacement_old_instance_unverified"
        actual_tags = {item["Key"]: item["Value"] for item in instances[0].get("Tags", [])}
        if any(actual_tags.get(key) != value for key, value in old_tags.items()):
            return "replacement_old_instance_unverified"
        volume = await asyncio.to_thread(observed_volume, ec2, box, disk, previous.availability_zone, launch_template.kms_key_arn, grow_to)
        if restore_snapshot_id:
            if volume is None:
                return "workspace_not_observed"
            where, state = attachment(volume, target_id, previous.provider_instance_id)
            if (where == "previous" and state == "attached") or (where == "none" and state == "available"):
                return None
            return attached_elsewhere(volume, target_id, previous.provider_instance_id)
        if volume is None:
            return "workspace_not_observed"
        where, state = attachment(volume, target_id, previous.provider_instance_id)
        if where == "none" and state == "available":
            if not first_detach:
                async with session_factory() as db:
                    operation, _, error = await current(db, operation_id, worker_id, lease_generation)
                    if error:
                        await db.rollback()
                        return error
                    operation.progress = "admitted"
                    await db.commit()
            return None
        if where == "target":
            # Recovery after the launch and attach already happened: the attach step owns this observation.
            return None if state == "attached" else not_yet("attachment_not_observed", volume, "target", state)
        if where == "foreign":
            return attached_elsewhere(volume, target_id, previous.provider_instance_id)
        if where == "none":
            return ObservationError("workspace_not_observed", f"workspace volume {volume['VolumeId']} is {state}, not available")
        if not first_detach or state != "attached":
            return not_yet("detach_not_observed", volume, "previous", state)
        async with session_factory() as db:
            operation, _, error = await current(db, operation_id, worker_id, lease_generation)
            if error:
                await db.rollback()
                return error
            operation.progress = "detach_intent"
            await db.commit()
        async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
            await asyncio.to_thread(ec2.detach_volume, VolumeId=volume["VolumeId"], InstanceId=previous.provider_instance_id)
        return await prepare_replacement(session_factory, ec2, operation_id, worker_id, lease_generation, launch_template, fence=fence,
                                         target_instance_id=target_id)
    except ValueError:
        return "workspace_ownership_mismatch"
    except (OSError, BotoCoreError, ClientError):
        return "workspace_provider_unknown"
