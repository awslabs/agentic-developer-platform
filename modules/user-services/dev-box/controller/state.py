"""Reconcile fenced instance start and stop; uncertainty is observed, never replayed."""

import asyncio
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, update
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBoxInstance, DevBoxOperation

from controller.checkpoint import capture_before_stop
from controller.fencing import (
    claim,
    current,
    finish,
    maintained_lease,
    require_access_fenced,
)
from controller.launch import ownership_tags, private_host_address
from controller.storage import workspace


def owned_instance(ec2, instance_id, tags):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [item for reservation in response.get("Reservations", []) for item in reservation.get("Instances", [])]
    if len(instances) != 1 or instances[0].get("InstanceId") != instance_id:
        raise ValueError("Recorded instance is not uniquely observable")
    actual = {tag["Key"]: tag["Value"] for tag in instances[0].get("Tags", [])}
    if any(actual.get(key) != value for key, value in tags.items()):
        raise ValueError("Recorded instance ownership differs from operation")
    return instances[0]


async def reconcile_state(session_factory, ec2, operation_id, worker_id, launch_template=None, *, fence=None, ssm=None):
    lease_generation = await claim(session_factory, operation_id, worker_id, ("start", "stop"))
    if lease_generation is None:
        return False
    async with session_factory() as db:
        operation, box, error = await current(db, operation_id, worker_id, lease_generation)
        if operation is None:
            await db.rollback()
            return False
        instance = None if error else await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == box.machine_generation,
        ))
        if not error and (instance is None or not instance.provider_instance_id):
            error = "instance_unobserved"
        if not error and operation.action == "stop":
            error = await require_access_fenced(box, "stop", fence, db=db)
        first_attempt = operation.progress != "start_intent" if operation.action == "start" else operation.progress == "admitted"
        if error:
            finish(operation, error)
            await db.commit()
            return True
        instance_id = instance.provider_instance_id
        tags = ownership_tags(box, operation)
        if operation.action == "stop":
            operation.progress = "stop_intent"
        await db.commit()

    if operation.action == "stop" and first_attempt:
        # Host authority is already fenced (API or scheduler admission); the machine is still up.
        # Save the Crew home once more before it stops — bounded, recorded, never blocking the
        # stop (controller/checkpoint.py, #8379). Only the first attempt captures: a retried
        # operation after a controller restart goes straight to the provider.
        async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
            await capture_before_stop(session_factory, ssm, box=box, instance=instance, operation=operation)

    try:
        observed = await asyncio.to_thread(owned_instance, ec2, instance_id, tags)
    except ValueError:
        observed = None
        error = "ownership_unknown"
    except (OSError, BotoCoreError, ClientError):
        observed = None
        error = "provider_unknown"
    target = "stopped" if operation.action == "stop" else "running"
    if observed and operation.action == "start" and not error:
        error = await workspace(session_factory, ec2, operation_id, worker_id, lease_generation, observed, launch_template, allow_create=False)
    if observed and observed["State"]["Name"] != target and first_attempt and not error:
        async with session_factory() as db:
            _, _, stale = await current(db, operation_id, worker_id, lease_generation)
            await db.rollback()
        if stale:
            observed = None
            error = stale
        else:
            try:
                if operation.action == "start":
                    async with session_factory() as db:
                        _, _, stale = await current(db, operation_id, worker_id, lease_generation)
                        if stale:
                            await db.rollback()
                            raise ValueError("Start admission changed before provider call")
                        pending = await db.get(DevBoxOperation, operation_id)
                        pending.progress = "start_intent"
                        await db.commit()
                action = ec2.stop_instances if operation.action == "stop" else ec2.start_instances
                async with maintained_lease(session_factory, operation_id, worker_id, lease_generation):
                    await asyncio.to_thread(action, InstanceIds=[instance_id])
                observed = await asyncio.to_thread(owned_instance, ec2, instance_id, tags)
            except ValueError:
                observed = None
                error = "ownership_unknown"
            except (OSError, BotoCoreError, ClientError):
                observed = None
                error = "provider_unknown"
    async with session_factory() as db:
        current_operation, box, stale = await current(db, operation_id, worker_id, lease_generation)
        if current_operation is None:
            await db.rollback()
            return False
        if stale:
            observed = None
            error = stale
        if observed and observed["State"]["Name"] == target and not error:
            instance = await db.scalar(select(DevBoxInstance).where(
                DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == box.machine_generation,
            ))
            instance.observed_state = target
            instance.private_address = private_host_address(observed) if target == "running" else None
            instance.readiness_class = "machine_running" if target == "running" else None
            instance.enrollment_public_key = None
            # /run/adp is tmpfs: a stop/start wipes the host enrollment key AND the host
            # certificate the first boot generated (write_host_certificate.py). The key was
            # already forgotten here; the certificate pin was not, so reconcile_host_fingerprint
            # never re-ran and every later Crew bootstrap failed "certificate unavailable"
            # (dev 2026-10-19, gen 13 after its third start; #8223). Forget the pin too, so the
            # next tick writes a fresh certificate and store_host_fingerprint re-pins it.
            instance.certificate_fingerprint = None
            if target == "stopped":
                await park_host_executor(db, box)
            box.observed_state = target
            box.status = target
            current_operation.status = "succeeded"
            current_operation.progress = "instance_observed"
            current_operation.error_class = None
        else:
            error = error or "state_not_observed"
        finish(current_operation, error)
        await db.commit()
    return True


async def retire_host_executors(db, box):
    """Retire every live Crew executor bound to ``box`` once its release is verified.

    A released box leaves no machine to resume, but its executor row stayed ``stopped``/``ready``
    and the owner's partial index admits one live executor per (tenant, user). The owner's NEXT
    box (a different ``dev_box_id``) then hit ``host executor conflicts with canonical box`` on
    every enrollment (dev 2026-10-20 15:20 UTC, #8223 cause 23). ``latest_verified_checkpoint_id``
    is kept on the deleted row so the successor inherits it (``CrewExecutorInventory.bind_host``
    reads the newest row, deleted or not) and restores the released box's last checkpoint (#8379).
    Returns the number of executors retired.
    """
    result = await db.execute(
        update(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == box.org_id, CrewExecutor.user_id == box.user_id,
            CrewExecutor.target_kind == "ec2-instance", CrewExecutor.dev_box_id == box.id,
            CrewExecutor.state != "deleted",
        )
        .values(state="deleted", target_ref=None, lease_generation=None, lease_expires_at=None)
    )
    return result.rowcount or 0


async def park_host_executor(db, box):
    """Mark the generation's Crew executor ``stopped`` once the machine is observed stopped.

    The gateway's lease activation leaves the executor ``ready``; a later same-generation
    start must be re-verified (readiness only runs for ``provisioning``), so the enrollment
    that follows the start moves ``stopped`` back to ``provisioning`` (identity.py,
    ``resume_host_executor``). Without this leg a stopped-and-started box stayed
    ``lease_fenced`` forever (#8223 cause 18).
    """
    await db.execute(
        update(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == box.org_id, CrewExecutor.user_id == box.user_id,
            CrewExecutor.target_kind == "ec2-instance", CrewExecutor.dev_box_id == box.id,
            CrewExecutor.generation == box.machine_generation, CrewExecutor.state == "ready",
        )
        .values(state="stopped", lease_generation=None, lease_expires_at=None, last_stopped_at=datetime.now(UTC))
    )
