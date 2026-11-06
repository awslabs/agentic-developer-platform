"""Periodic controller entrypoint for operations admitted by the mounted gateway API."""

import argparse
import asyncio
import hashlib
import functools
import json
import logging
import os
import re
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from sqlalchemy import and_, or_, select, update
from src.agentauth.chat_crew_run import CrewRunStore
from src.shared.database import get_session_factory
from src.shared.models.base import new_uuid
from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
)
from src.shared.models.onboarding import TenantMembership

from controller.cleanup import reconcile_release, retire_superseded_instances
from controller.identity import (
    reconcile_enrollment,
    reconcile_expired_enrollment,
    reconcile_expired_host_key,
    reconcile_host_fingerprint,
    reconcile_host_key,
    rebind_host_run,
    reconcile_host_readiness,
    reconcile_revoked_host_ingress,
)
from controller.launch import LaunchTemplate, reconcile_setup
from controller.state import reconcile_state
from controller.usage import reconcile_usage

logger = logging.getLogger(__name__)


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


async def schedule_stops(session_factory, deployment_id, *, now=None, run_store=None):
    now = now or datetime.now(UTC)
    async with session_factory() as db:
        box_ids = list(
            (
                await db.scalars(
                    select(DevBox.id)
                    .where(
                        DevBox.deployment_id == deployment_id,
                        DevBox.desired_state == "running",
                        DevBox.status == "running",
                    )
                    .order_by(DevBox.id)
                )
            ).all()
        )
    scheduled = 0
    for box_id in box_ids:
        async with session_factory() as db:
            box = await db.scalar(
                select(DevBox).where(DevBox.id == box_id).with_for_update()
            )
            if box is None or box.desired_state != "running" or box.status != "running":
                await db.rollback()
                continue
            policy = await db.scalar(
                select(DevBoxPolicy).where(
                    DevBoxPolicy.org_id == box.org_id,
                    DevBoxPolicy.deployment_id == deployment_id,
                )
            )
            lease_end = aware(box.lease_expires_at)
            if (
                lease_end is None
                and policy
                and policy.max_lease_minutes
                and policy.max_lease_minutes > 0
            ):
                lease_end = aware(box.created_at) + timedelta(
                    minutes=policy.max_lease_minutes
                )
            lease_expired = lease_end is not None and lease_end <= now
            developer_at = aware(box.last_activity_at or box.created_at)
            assistant_at = aware(box.assistant_activity_at or box.created_at)
            latest = max(developer_at, assistant_at)
            idle = (
                policy is not None
                and policy.idle_stop_minutes is not None
                and policy.idle_stop_minutes > 0
                and developer_at + timedelta(minutes=policy.idle_stop_minutes) <= now
                and assistant_at + timedelta(minutes=policy.idle_stop_minutes) <= now
                and (
                    box.assistant_busy_until_at is None
                    or aware(box.assistant_busy_until_at) <= now
                )
            )
            if not lease_expired and not idle:
                await db.rollback()
                continue
            active = await db.scalar(
                select(DevBoxOperation.id).where(
                    DevBoxOperation.box_id == box.id,
                    DevBoxOperation.status.in_(("pending", "unknown")),
                )
            )
            if active:
                await db.rollback()
                continue
            cause = "max_lease" if lease_expired else "idle"
            operation_id = new_uuid()
            operation = DevBoxOperation(
                id=operation_id,
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                action="stop",
                status="pending",
                progress="admitted",
                actor_user_id=box.user_id,
                idempotency_key=f"auto-stop:{cause}:{box.machine_generation}:{int((lease_end if lease_expired else latest).timestamp())}",
                request_digest=hashlib.sha256(
                    f"{box.id}:{cause}:{latest}".encode()
                ).hexdigest(),
                provider_token=hashlib.sha256(operation_id.encode()).hexdigest(),
                created_at=now,
            )
            from src.cloud_desktop.host_fencing import fence_host

            # A scheduled stop is a stop: the approved device stays so the next start re-binds
            # without the owner's browser (#8223 simplification).
            await fence_host(db, box, run_store, keep_devices=True)
            box.desired_state = "stopped"
            db.add(operation)
            await db.flush()
            db.add(
                DevBoxEvent(
                    id=new_uuid(),
                    box_id=box.id,
                    user_id=box.user_id,
                    org_id=box.org_id,
                    deployment_id=box.deployment_id,
                    machine_generation=box.machine_generation,
                    operation_id=operation_id,
                    event_type=f"{cause}_stop_admitted",
                    created_at=now,
                )
            )
            if box.assistant_busy_until_at:
                db.add(
                    DevBoxEvent(
                        id=new_uuid(),
                        box_id=box.id,
                        user_id=box.user_id,
                        org_id=box.org_id,
                        deployment_id=box.deployment_id,
                        machine_generation=box.machine_generation,
                        operation_id=operation_id,
                        event_type="assistant_busy_timeout"
                        if aware(box.assistant_busy_until_at) <= now
                        else "assistant_busy_interrupted",
                        created_at=now,
                    )
                )
            await db.commit()
            scheduled += 1
    return scheduled


def controller_clients(sts, manifest, outputs):
    account = manifest["account_id"]
    role = outputs["controller_role_arn"]["value"]
    if not role.startswith(f"arn:aws:iam::{account}:role/"):
        raise ValueError("Controller role is outside the approved account")
    identity = sts.assume_role(RoleArn=role, RoleSessionName="dev-box-reconciler")
    assumed = identity["AssumedRoleUser"]["Arn"]
    expected = f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[1]}/"
    if not assumed.startswith(expected):
        raise ValueError("Controller role identity mismatch")
    credentials = identity["Credentials"]
    session = boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=manifest["region"],
    )
    run_table_arn = manifest.get("run_table_arn", "")
    if run_table_arn:
        if outputs.get("run_table_arn", {}).get(
            "value"
        ) != run_table_arn or not run_table_arn.startswith(
            f"arn:aws:dynamodb:{manifest['region']}:{account}:table/"
        ):
            raise ValueError("Controller run registry differs from approved release")
        guest_role_arn = outputs["guest_role_arn"]["value"]
        if not guest_role_arn.startswith(
            f"arn:aws:iam::{account}:role/adp-dev-box-guest-"
        ):
            raise ValueError("Controller guest role differs from approved release")
        store = CrewRunStore(
            session.resource("dynamodb").Table(run_table_arn.split(":table/", 1)[1]),
            host_role_arn=guest_role_arn
            if manifest.get("gateway_execute_arn")
            else None,
        )
        return session.client("ec2"), session.client("ssm"), store
    return session.client("ec2"), session.client("ssm")


def validate_release(manifest, outputs, deployment_id):
    release = dict(manifest)
    declared_digest = release.pop("manifest_sha256", None)
    actual_digest = hashlib.sha256(
        json.dumps(release, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if declared_digest != actual_digest:
        raise ValueError("Controller release digest mismatch")
    if manifest.get("image_id") != outputs["image_id"]["value"]:
        raise ValueError("Controller image differs from deployed infrastructure")
    if manifest.get("source_revision") != outputs["source_revision"]["value"]:
        raise ValueError("Controller source differs from deployed infrastructure")
    if (
        manifest.get("component") != "dev-box"
        or manifest.get("deployment_id") != deployment_id
    ):
        raise ValueError("Controller release does not match gateway deployment")
    if manifest.get("template_revision") != outputs["template_revision"]["value"]:
        raise ValueError("Controller template differs from deployed infrastructure")
    if manifest.get("dashboard_enabled") and not manifest.get("crew_profile_revision"):
        raise ValueError("Controller Crew profile is not pinned")
    if manifest.get("gateway_execute_arn") and (
        not manifest.get("crew_profile_revision") or not manifest.get("crew_model_id")
    ):
        raise ValueError("Controller Crew gateway bootstrap is not pinned")
    # Crew bootstrap mode (#8055): the manifest and the applied foundation must agree,
    # and a user-data release must pin the bundle digest and its source revision so the
    # on-box receipt check (identity.crew_profile_command) has something exact to compare.
    crew_bootstrap = manifest.get("crew_bootstrap", "image")
    if crew_bootstrap not in ("image", "user-data"):
        raise ValueError("Controller Crew bootstrap mode is unknown")
    if crew_bootstrap != outputs.get("crew_bootstrap", {}).get("value", "image"):
        raise ValueError(
            "Controller Crew bootstrap differs from deployed infrastructure"
        )
    if crew_bootstrap == "user-data" and (
        not manifest.get("crew_profile_revision")
        or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("crew_bundle_sha256") or "")
        or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("crew_source_revision") or "")
    ):
        raise ValueError("Controller Crew user-data bootstrap is not pinned")
    # Capacity fallback (#8423): the manifest's ordered plan, defaulting to the template's
    # own size and subnet. The plan must start with the template's values, and every size
    # must carry a rate, or the release is refused before any launch.
    fallback_sizes = tuple(manifest.get("fallback_sizes") or [manifest["instance_size"]])
    subnets = tuple(
        (item["subnet_id"], item.get("availability_zone"))
        for item in (manifest.get("private_subnets") or [{"subnet_id": manifest.get("private_subnet_id")}])
    )
    rates = dict(manifest.get("instance_hour_rates_usd") or {})
    allowed_sizes = manifest.get("allowed_sizes") or [manifest["instance_size"]]
    if (fallback_sizes[0] != manifest["instance_size"] or len(set(fallback_sizes)) != len(fallback_sizes)
            or subnets[0][0] != manifest.get("private_subnet_id") or len({subnet for subnet, _ in subnets}) != len(subnets)
            or any(size not in allowed_sizes for size in fallback_sizes)
            or (rates and any(size not in rates for size in fallback_sizes))):
        raise ValueError("Controller launch plan differs from approved release")
    guest_security_group_id = outputs.get("guest_security_group_id", {}).get("value")
    if len(subnets) > 1 and not str(guest_security_group_id or "").startswith("sg-"):
        raise ValueError("Controller subnet fallback requires the guest security group")
    return LaunchTemplate(
        template_id=outputs["launch_template_id"]["value"],
        version=str(outputs["launch_template_version"]["value"]),
        region=manifest["region"],
        account_id=manifest["account_id"],
        image_id=manifest["image_id"],
        revision=manifest["template_revision"],
        instance_size=manifest["instance_size"],
        fallback_sizes=fallback_sizes,
        subnets=subnets,
        security_group_id=guest_security_group_id,
        instance_hour_rates_usd=rates,
        disk_gb=manifest["workspace_disk_gb"],
        kms_key_arn=manifest["kms_key_arn"],
        profile_revision=manifest.get("crew_profile_revision"),
        source_revision=manifest["source_revision"],
        gateway_execute_arn=manifest.get("gateway_execute_arn"),
        crew_model_id=manifest.get("crew_model_id"),
        crew_bootstrap=crew_bootstrap,
        crew_bundle_sha256=manifest.get("crew_bundle_sha256")
        if crew_bootstrap == "user-data"
        else None,
        crew_source_revision=manifest.get("crew_source_revision")
        if crew_bootstrap == "user-data"
        else None,
    )


async def scan_once(
    session_factory,
    ec2,
    ssm,
    deployment_id,
    launch_template,
    worker_id,
    *,
    run_store=None,
    fence=None,
):
    await schedule_stops(session_factory, deployment_id, run_store=run_store)
    await reconcile_usage(session_factory, ec2, deployment_id, launch_template)
    async with session_factory() as db:
        operations = list(
            (
                await db.execute(
                    select(DevBoxOperation.id, DevBoxOperation.action)
                    .where(
                        DevBoxOperation.deployment_id == deployment_id,
                        DevBoxOperation.action.in_(
                            ("setup", "start", "stop", "replace", "release")
                        ),
                        DevBoxOperation.status.in_(("pending", "unknown")),
                    )
                    .order_by(DevBoxOperation.created_at, DevBoxOperation.id)
                    .limit(32)
                )
            ).all()
        )
    for operation_id, action in operations:
        if action in ("setup", "replace"):
            await reconcile_setup(
                session_factory, ec2, operation_id, worker_id, launch_template, ssm=ssm
            )
        elif action == "release":
            await reconcile_release(
                session_factory, ec2, operation_id, worker_id, launch_template
            )
        else:
            await reconcile_state(
                session_factory,
                ec2,
                operation_id,
                worker_id,
                launch_template,
                fence=fence,
                ssm=ssm,
            )
    await reconcile_usage(session_factory, ec2, deployment_id, launch_template)
    async with session_factory() as db:
        await db.execute(
            update(DevBoxDevice)
            .where(
                DevBoxDevice.deployment_id == deployment_id,
                DevBoxDevice.sealed_enrollment.is_not(None),
                or_(
                    DevBoxDevice.enrollment_expires_at.is_(None),
                    DevBoxDevice.enrollment_expires_at <= datetime.now(UTC),
                    DevBoxDevice.enrollment_consumed_at.is_not(None),
                    DevBoxDevice.revoked_at.is_not(None),
                ),
            )
            .values(sealed_enrollment=None)
        )
        await db.commit()
    if run_store is None:
        return len(operations)
    # A started box whose generation the owner already approved gets its host run back from
    # the controller alone: the stop kept the consumed device, the start brought the same
    # machine up, so nothing is left for a browser to do (#8223 simplification).
    rebound = []
    if run_store.host_role_arn:
        async with session_factory() as db:
            rebound = list(
                (
                    await db.scalars(
                        select(DevBox.id)
                        .join(
                            DevBoxInstance,
                            and_(
                                DevBoxInstance.box_id == DevBox.id,
                                DevBoxInstance.machine_generation
                                == DevBox.machine_generation,
                            ),
                        )
                        .join(
                            DevBoxDevice,
                            and_(
                                DevBoxDevice.box_id == DevBox.id,
                                DevBoxDevice.machine_generation
                                == DevBox.machine_generation,
                                DevBoxDevice.instance_id == DevBoxInstance.id,
                                DevBoxDevice.enrollment_consumed_at.is_not(None),
                                DevBoxDevice.revoked_at.is_(None),
                            ),
                        )
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.observed_state == "running",
                            DevBox.auth_readiness.in_(("fenced", "unknown")),
                            DevBoxInstance.observed_state == "running",
                        )
                        .distinct()
                        .limit(8)
                    )
                ).all()
            )
        for box_id in rebound:
            await rebind_host_run(
                session_factory,
                ec2,
                ssm,
                run_store,
                box_id,
                launch_template=launch_template,
            )
    from src.cloud_desktop.host_fencing import fence_host
    from src.cloud_desktop.host_identity import ec2_crew_allowed

    async with session_factory() as db:
        revoked_boxes = (
            await db.scalars(
                select(DevBox.id)
                .outerjoin(
                    TenantMembership,
                    (TenantMembership.user_id == DevBox.user_id)
                    & (TenantMembership.tenant_id == DevBox.org_id)
                    & TenantMembership.revoked_at.is_not(None),
                )
                .outerjoin(
                    DevBoxPolicy,
                    (DevBoxPolicy.org_id == DevBox.org_id)
                    & (DevBoxPolicy.deployment_id == DevBox.deployment_id),
                )
                .where(
                    DevBox.deployment_id == deployment_id,
                    DevBox.auth_readiness == "ready",
                    or_(
                        TenantMembership.id.is_not(None),
                        DevBoxPolicy.enabled.is_not(True),
                    ),
                )
                .limit(32)
            )
        ).all()
        crew_boxes = (
            await db.execute(
                select(DevBox.id, DevBox.org_id).where(
                    DevBox.deployment_id == deployment_id,
                    DevBox.profile == "crew",
                    DevBox.auth_readiness == "ready",
                )
            )
        ).all()
        revoked_crew_boxes = [
            box_id
            for box_id, org_id in crew_boxes
            if not await ec2_crew_allowed(db, org_id)
        ]
    revoked_box_ids = list(dict.fromkeys([*revoked_boxes, *revoked_crew_boxes]))
    for box_id in revoked_box_ids:
        async with session_factory() as db:
            box = await db.scalar(
                select(DevBox).where(DevBox.id == box_id).with_for_update()
            )
            if box is None or box.auth_readiness != "ready":
                continue
            revoked = await db.scalar(
                select(TenantMembership.id).where(
                    TenantMembership.user_id == box.user_id,
                    TenantMembership.tenant_id == box.org_id,
                    TenantMembership.revoked_at.is_not(None),
                )
            )
            policy = await db.scalar(
                select(DevBoxPolicy).where(
                    DevBoxPolicy.org_id == box.org_id,
                    DevBoxPolicy.deployment_id == box.deployment_id,
                )
            )
            if (
                revoked is not None
                or policy is None
                or not policy.enabled
                or (
                    box.profile == "crew" and not await ec2_crew_allowed(db, box.org_id)
                )
            ):
                await fence_host(db, box, run_store)
                await db.commit()
    if launch_template and launch_template.profile_revision:
        async with session_factory() as db:
            revoked_ingress_ids = list(
                (
                    await db.scalars(
                        select(DevBoxInstance.id)
                        .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                        .join(
                            DevBoxAccessSession,
                            and_(
                                DevBoxAccessSession.box_id == DevBox.id,
                                DevBoxAccessSession.machine_generation
                                == DevBox.machine_generation,
                            ),
                        )
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.auth_readiness == "fenced",
                            DevBox.machine_generation
                            == DevBoxInstance.machine_generation,
                            DevBoxAccessSession.terminated_at.is_not(None),
                            or_(
                                DevBoxInstance.readiness_class.is_(None),
                                DevBoxInstance.readiness_class != "host_fenced",
                            ),
                        )
                        .distinct()
                    )
                ).all()
            )
        for instance_id in revoked_ingress_ids:
            await reconcile_revoked_host_ingress(
                session_factory,
                ec2,
                ssm,
                run_store,
                instance_id,
                launch_template=launch_template,
            )
        async with session_factory() as db:
            expired_host_ids = list(
                (
                    await db.scalars(
                        select(DevBoxInstance.id)
                        .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                        .join(
                            DevBoxAccessSession,
                            and_(
                                DevBoxAccessSession.box_id == DevBox.id,
                                DevBoxAccessSession.machine_generation
                                == DevBox.machine_generation,
                            ),
                        )
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.machine_generation
                            == DevBoxInstance.machine_generation,
                            DevBoxInstance.enrollment_public_key.is_not(None),
                            or_(
                                and_(
                                    DevBoxAccessSession.terminated_at.is_(None),
                                    DevBoxAccessSession.expires_at <= datetime.now(UTC),
                                ),
                                DevBox.auth_readiness == "fenced",
                            ),
                        )
                        .distinct()
                        .limit(8)
                    )
                ).all()
            )
        for instance_id in expired_host_ids:
            await reconcile_expired_host_key(
                session_factory,
                ec2,
                ssm,
                run_store,
                instance_id,
                launch_template=launch_template,
            )
        async with session_factory() as db:
            host_ids = list(
                (
                    await db.scalars(
                        select(DevBoxInstance.id)
                        .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.machine_generation
                            == DevBoxInstance.machine_generation,
                            DevBoxInstance.enrollment_public_key.is_(None),
                        )
                        .limit(8)
                    )
                ).all()
            )
        for instance_id in host_ids:
            await reconcile_host_key(
                session_factory, ec2, ssm, instance_id, launch_template=launch_template
            )
        async with session_factory() as db:
            unpinned_ids = list(
                (
                    await db.scalars(
                        select(DevBoxInstance.id)
                        .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.machine_generation
                            == DevBoxInstance.machine_generation,
                            DevBoxInstance.certificate_fingerprint.is_(None),
                        )
                        .limit(8)
                    )
                ).all()
            )
        for instance_id in unpinned_ids:
            await reconcile_host_fingerprint(
                session_factory, ec2, ssm, instance_id, launch_template=launch_template
            )
    async with session_factory() as db:
        devices = list(
            (
                await db.scalars(
                    select(DevBoxDevice.id)
                    .where(
                        DevBoxDevice.deployment_id == deployment_id,
                        DevBoxDevice.enrollment_consumed_at.is_(None),
                        DevBoxDevice.revoked_at.is_(None),
                        DevBoxDevice.enrollment_expires_at > datetime.now(UTC),
                    )
                    .limit(32)
                )
            ).all()
        )
    for device_id in devices:
        if run_store.host_role_arn:
            await reconcile_enrollment(
                session_factory,
                ec2,
                ssm,
                run_store,
                device_id,
                launch_template=launch_template,
            )
    # Approvals that lapsed unconsumed burn the host key they named (the owner route refuses a reused
    # digest); rotate it so the owner's next approval can succeed, and close rows that can never be.
    async with session_factory() as db:
        expired_devices = list(
            (
                await db.scalars(
                    select(DevBoxDevice.id)
                    .join(DevBox, DevBox.id == DevBoxDevice.box_id)
                    .where(
                        DevBoxDevice.deployment_id == deployment_id,
                        DevBoxDevice.enrollment_consumed_at.is_(None),
                        DevBoxDevice.revoked_at.is_(None),
                        DevBoxDevice.enrollment_expires_at <= datetime.now(UTC),
                        DevBox.profile == "crew",
                        DevBox.status == "running",
                        DevBox.desired_state == "running",
                    )
                    .order_by(DevBoxDevice.enrollment_expires_at)
                    .limit(32)
                )
            ).all()
        )
    for device_id in expired_devices:
        await reconcile_expired_enrollment(
            session_factory, ec2, ssm, device_id, launch_template=launch_template
        )
    if run_store.host_role_arn and launch_template and launch_template.profile_revision:
        async with session_factory() as db:
            pending_ids = list(
                (
                    await db.scalars(
                        select(DevBoxInstance.id)
                        .join(DevBox, DevBox.id == DevBoxInstance.box_id)
                        .where(
                            DevBox.deployment_id == deployment_id,
                            DevBox.profile == "crew",
                            DevBox.auth_readiness == "pending",
                            DevBox.status == "running",
                            DevBox.desired_state == "running",
                            DevBox.machine_generation
                            == DevBoxInstance.machine_generation,
                            DevBoxInstance.certificate_fingerprint.is_not(None),
                        )
                        .limit(8)
                    )
                ).all()
            )
        for instance_id in pending_ids:
            await reconcile_host_readiness(
                session_factory,
                ec2,
                ssm,
                run_store,
                instance_id,
                launch_template=launch_template,
            )
    # #8223 cause 53: machines a verified-ready box has replaced are retired so they stop holding
    # subnet addresses and billing; the current generation and the workspace disk are never touched.
    await retire_superseded_instances(session_factory, ec2, deployment_id)
    return (
        len(operations)
        + len(revoked_box_ids)
        + (len(devices) + len(rebound) if run_store.host_role_arn else 0)
    )


async def _host_access_fence(run_store, db, box, action):
    """Revoke live host authority before the controller admits a stop/replace/release.

    ``require_access_fenced`` calls this for a box whose readiness is not already ``unknown``/
    ``fenced``; without it the controller can never stop a ``pending``/``ready`` box that the
    idle-stop scheduler did not pre-fence (e.g. an enrollment that stalled and left the box
    ``pending`` after its lease expired, seen live 2026-10-18).

    It works on the caller's session and the box row the caller already locked. The first
    version opened a second session and locked the row again, which waited on its own caller
    forever (dev 2026-10-19). Writes fence_host makes before a refusal stay in the caller's
    transaction; they mirror revocations already performed in the run registry.
    """
    from fastapi import HTTPException

    from src.cloud_desktop.host_fencing import fence_host

    if db is None or box is None:
        return False
    try:
        await fence_host(db, box, run_store, keep_devices=(action == "stop"))
    except HTTPException:
        return False
    return True


async def serve(
    session_factory,
    client_factory,
    deployment_id,
    launch_template,
    *,
    interval=10,
    fence=None,
):
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    # ``time.monotonic()`` counts from an arbitrary origin (node boot on Linux). On a node younger
    # than the 40-minute refresh window ``monotonic() - 0 >= 2400`` was False on the very first
    # tick, so the clients were never created and every tick died with UnboundLocalError on
    # ``ec2`` (seen live 2026-10-18). Track "never refreshed" explicitly.
    refreshed_at: float | None = None
    ec2 = ssm = run_store = None
    while True:
        try:
            if refreshed_at is None or time.monotonic() - refreshed_at >= 2400:
                clients = client_factory()
                ec2, ssm = clients[:2]
                run_store = clients[2] if len(clients) == 3 else None
                refreshed_at = time.monotonic()
            # The controller owns host fencing at stop time: when the caller supplied no fence
            # and a run registry is present, build one so reconcile_state can revoke authority
            # on a pending/ready box rather than refusing its stop forever.
            effective_fence = fence
            if effective_fence is None and run_store is not None:
                effective_fence = functools.partial(_host_access_fence, run_store)
            await scan_once(
                session_factory,
                ec2,
                ssm,
                deployment_id,
                launch_template,
                worker_id,
                run_store=run_store,
                fence=effective_fence,
            )
        except Exception:
            logger.exception("Dev Box reconciliation tick failed")
        await asyncio.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile Dev Box operations against the approved release"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    arguments = parser.parse_args()
    if not logging.getLogger().handlers:
        # #8037: operators read per-tick operation outcomes from the controller pod log.
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    manifest = json.loads(arguments.manifest.read_text())
    outputs = json.loads(arguments.outputs.read_text())
    deployment = os.environ["BG_DEV_BOX_DEPLOYMENT_ID"]
    launch_template = validate_release(manifest, outputs, deployment)
    asyncio.run(
        serve(
            get_session_factory(),
            lambda: controller_clients(boto3.client("sts"), manifest, outputs),
            deployment,
            launch_template,
        )
    )


if __name__ == "__main__":
    main()
