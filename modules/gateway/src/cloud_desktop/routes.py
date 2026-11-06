"""Admission-only owner API; provider reconciliation is a separate service."""

import base64
import binascii
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import get_current_user_context
from src.cloud_desktop.capacity import check_capacity
from src.cloud_desktop.host_identity import current_host
from src.cloud_desktop.ssh_access import issue_ssh_credentials
from src.cloud_desktop.status import infrastructure_status
from src.crew.generation import advance_owner_generation, lock_owner_generation
from src.shared.database import get_db
from src.shared.models.base import new_uuid
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
)
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/cloud-desktops", tags=["cloud-desktops"])


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=128)
    profile: str = Field(min_length=1, max_length=128)


class HostEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SealedEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ciphertext_b64: str = Field(min_length=512, max_length=512)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_retention: Literal["retain", "delete"]


class ReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^snap-(?:[0-9a-f]{8}|[0-9a-f]{17})$")


class ActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["host", "assistant"]
    busy: bool = False
    consolidation_complete: bool | None = None


class OperationResponse(BaseModel):
    contract_version: Literal[1] = 1
    desktop_id: str
    operation_id: str
    status: str
    progress: str
    error_class: str | None = None


class EnsureResponse(BaseModel):
    contract_version: Literal[1] = 1
    desktop_id: str
    operation_id: str | None
    status: str
    progress: str
    error_class: str | None = None


def deployment_id() -> str:
    value = os.getenv("BG_DEV_BOX_DEPLOYMENT_ID", "")
    if not value or len(value) > 128:
        raise HTTPException(403, "Dev Box admission is disabled")
    return value


def crew_bootstrap_mode() -> str:
    """How the deployment's Crew profile reaches a guest (#8055): "image" (baked into the
    AMI) or "user-data" (installed at first boot from the pinned bundle). Rendered from
    SSM /adp/<env>/dev-box/crew-bootstrap by both gateway renderers; anything else
    reads as the default image mode."""
    value = os.getenv("BG_DEV_BOX_CREW_BOOTSTRAP", "image")
    return value if value in ("image", "user-data") else "image"


async def owner(db: AsyncSession, context: TokenContext) -> str:
    """The Dev Box owner is the caller's tenant member row -- the same resolver as the
    Crew owner and the chat principal (``tenant_member_principal``, #8108), so a box,
    its Crew lease and the owner's chat turns are all keyed on one ``users.id``."""
    # Imported here: chat_authority reaches this module through the crew proxy.
    from src.agentauth.chat_authority import tenant_member_principal
    from src.agentauth.chat_capability import ChatAuthorizationRefusedError

    if context.account_type != "human" or not context.org_id:
        raise HTTPException(403, "An active owner membership is required")
    try:
        user = await tenant_member_principal(db, context.org_id, context.user_id, username=context.cognito_username)
    except ChatAuthorizationRefusedError:
        raise HTTPException(403, "An active owner membership is required") from None
    if user.is_shadow:
        raise HTTPException(403, "An active owner membership is required")
    return user.id


def scope(box: DevBox) -> dict:
    return dict(box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id, machine_generation=box.machine_generation)


# #8037: a launch the controller has stopped retrying must not park the owner's box forever. These bounds mirror
# controller/launch.py (modules/user-services/dev-box); both sides read the same environment variable because the
# controller code ships as a ConfigMap on the gateway image and cannot be imported from here.
DEV_BOX_LAUNCH_RETRY_BOUND = int(os.environ.get("DEV_BOX_LAUNCH_RETRY_BOUND", "12"))
DEV_BOX_LAUNCH_TOKEN_WINDOW = timedelta(hours=23)
# #8100: the controller's reconcile interval (controller/worker.py `serve(interval=...)`), used only to tell the
# owner roughly how long an unresolved setup/replace keeps blocking release and replace.
DEV_BOX_RECONCILE_INTERVAL_SECONDS = int(os.environ.get("DEV_BOX_RECONCILE_INTERVAL_SECONDS", "10"))
ACTIVE_OPERATION = "A desktop operation is already active"


def launch_abandoned(operation: DevBoxOperation, now: datetime) -> bool:
    """True for an `unknown` setup/replace past the retry bound or token window with no live lease.

    This covers a launch the controller stopped re-sending (#8037) and a setup/replace parked on an unresolved
    provider observation such as `workspace_attached_elsewhere` (#8100): the controller fails those itself once
    the same observation outlives the bound, and this is the owner-side fence should that tick never commit.
    """
    if operation.status != "unknown" or operation.action not in ("setup", "replace"):
        return False
    lease = operation.lease_expires_at
    if lease is not None and lease.replace(tzinfo=lease.tzinfo or UTC) > now:
        return False
    created = operation.created_at.replace(tzinfo=operation.created_at.tzinfo or UTC)
    return operation.retries > DEV_BOX_LAUNCH_RETRY_BOUND or now - created >= DEV_BOX_LAUNCH_TOKEN_WINDOW


def active_operation_detail(operation: DevBoxOperation, now: datetime) -> str:
    """409 detail for a release/replace refused by a live setup/replace: what is unresolved and when it may be superseded (#8100)."""
    if operation.action not in ("setup", "replace") or operation.status != "unknown":
        return ACTIVE_OPERATION
    remaining = max(DEV_BOX_LAUNCH_RETRY_BOUND + 1 - operation.retries, 1)
    return (
        f"{ACTIVE_OPERATION} ({operation.action} {operation.status}: {operation.error_class or operation.progress}, "
        f"reconcile {operation.retries} of {DEV_BOX_LAUNCH_RETRY_BOUND}); if it stays unresolved the controller fails it and "
        f"release or replace may supersede it in about {remaining * DEV_BOX_RECONCILE_INTERVAL_SECONDS} s"
    )


def receipt(operation: DevBoxOperation) -> OperationResponse:
    return OperationResponse(
        desktop_id=operation.box_id,
        operation_id=operation.id,
        status=operation.status,
        progress=operation.progress,
        error_class=operation.error_class,
    )


def digest(action: str, data: dict) -> str:
    return hashlib.sha256(json.dumps({"action": action, **data}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def policy_for_update(db: AsyncSession, org_id: str, deployment: str) -> DevBoxPolicy:
    if db.bind.dialect.name == "postgresql":
        await db.execute(select(func.pg_advisory_xact_lock(func.hashtext("dev-box:" + deployment))))
    policy = await db.scalar(select(DevBoxPolicy).where(DevBoxPolicy.org_id == org_id, DevBoxPolicy.deployment_id == deployment).with_for_update())
    if (
        not policy
        or not policy.enabled
        or policy.max_live_boxes <= 0
        or policy.max_running_boxes <= 0
        or policy.max_disk_gb <= 0
        or not policy.max_storage_gb
        or policy.max_storage_gb <= 0
        or not policy.max_running_hours
        or policy.max_running_hours <= 0
        or not policy.budget_usd
        or policy.budget_usd <= 0
        or any(
            rate is None or rate <= 0
            for rate in (
                policy.instance_hour_rate_usd,
                policy.disk_gb_hour_rate_usd,
                policy.snapshot_gb_hour_rate_usd,
            )
        )
        or not policy.allowed_sizes
        or not policy.max_lease_minutes
        or policy.max_lease_minutes <= 0
        or (policy.idle_stop_minutes is not None and policy.idle_stop_minutes <= 0)
    ):
        raise HTTPException(403, "Dev Box policy is disabled or incomplete")
    return policy


async def replay(db: AsyncSession, user_id: str, org_id: str, deployment: str, key: str, request_digest: str) -> DevBoxOperation | None:
    operation = await db.scalar(
        select(DevBoxOperation).where(
            DevBoxOperation.user_id == user_id,
            DevBoxOperation.org_id == org_id,
            DevBoxOperation.deployment_id == deployment,
            DevBoxOperation.idempotency_key == key,
        )
    )
    if operation and operation.request_digest != request_digest:
        raise HTTPException(409, "Idempotency key has a different request")
    return operation


async def owned_box(db: AsyncSession, desktop_id: str, user_id: str, org_id: str, deployment: str, *, lock: bool = False) -> DevBox:
    query = select(DevBox).where(DevBox.id == desktop_id, DevBox.user_id == user_id, DevBox.org_id == org_id, DevBox.deployment_id == deployment)
    box = await db.scalar(query.with_for_update() if lock else query)
    if box is None:
        raise HTTPException(404, "Desktop not found")
    return box


async def save_operation(
    db: AsyncSession, box: DevBox, action: str, key: str, request_digest: str, retention_choice: str | None = None, snapshot_id: str | None = None
) -> DevBoxOperation:
    operation_id = new_uuid()
    operation = DevBoxOperation(
        id=operation_id,
        **scope(box),
        action=action,
        idempotency_key=key,
        request_digest=request_digest,
        actor_user_id=box.user_id,
        provider_token=digest("provider", {"operation_id": operation_id}),
        retention_choice=retention_choice,
        status="pending",
    )
    db.add(operation)
    try:
        await db.flush()
        db.add(DevBoxEvent(id=new_uuid(), **scope(box), operation_id=operation.id, event_type=f"{action}_admitted"))
        if snapshot_id is not None:
            db.add(DevBoxEvent(id=new_uuid(), **scope(box), operation_id=operation.id, event_type=f"restore:{snapshot_id}"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A desktop operation is already active") from None
    return operation


@router.post("/ensure", response_model=EnsureResponse, status_code=status.HTTP_202_ACCEPTED)
async def ensure_box(
    request: SetupRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    deployment = deployment_id()
    user_id = await owner(db, context)
    if request.profile == "crew":
        from src.cloud_desktop.host_identity import ec2_crew_allowed

        if not await ec2_crew_allowed(db, context.org_id):
            raise HTTPException(403, "EC2 Crew is not permitted")
    await policy_for_update(db, context.org_id, deployment)
    box = await db.scalar(
        select(DevBox)
        .where(
            DevBox.user_id == user_id,
            DevBox.org_id == context.org_id,
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
        )
        .with_for_update()
    )
    if box is None:
        return await setup(request, idempotency_key, context, db)
    template = await db.get(DevBoxTemplate, box.template_id)
    if template is None or template.name != request.template or box.profile != request.profile:
        raise HTTPException(409, "The canonical desktop has a different profile or template")
    previous = await db.scalar(
        select(DevBoxOperation).where(
            DevBoxOperation.user_id == user_id,
            DevBoxOperation.org_id == context.org_id,
            DevBoxOperation.deployment_id == deployment,
            DevBoxOperation.idempotency_key == idempotency_key,
        )
    )
    if previous is not None:
        expected = digest(previous.action, request.model_dump() if previous.action == "setup" else {"desktop_id": box.id})
        if previous.box_id != box.id or previous.request_digest != expected or previous.action not in ("setup", "start"):
            raise HTTPException(409, "Idempotency key has a different request")
        if previous.status in ("pending", "unknown"):
            return receipt(previous)
        if box.status == "running" and box.observed_state == "running" and box.desired_state == "running":
            response.status_code = status.HTTP_200_OK
            return EnsureResponse(desktop_id=box.id, operation_id=None, status="succeeded", progress="last_observed_running")
        raise HTTPException(409, "Use a new key for a new ensure attempt")
    active = await db.scalar(
        select(DevBoxOperation)
        .where(
            DevBoxOperation.box_id == box.id,
            DevBoxOperation.status.in_(("pending", "unknown")),
        )
        .order_by(DevBoxOperation.created_at.desc(), DevBoxOperation.id.desc())
        .limit(1)
    )
    if active is not None:
        return receipt(active)
    if box.status == "stopped" and box.observed_state == "stopped":
        return await change_state(box.id, "start", None, idempotency_key, context, db)
    if box.status == "running" and box.observed_state == "running" and box.desired_state == "running":
        response.status_code = status.HTTP_200_OK
        return EnsureResponse(desktop_id=box.id, operation_id=None, status="succeeded", progress="last_observed_running")
    if await setup_launch_failed(db, box):
        # #8423: the provider refused the launch (or it was never confirmed) and nothing exists for this
        # generation, so a new ensure is a retry: a fresh setup on the next generation, under the same
        # capacity rules as the first. Replace failures are not retried here (they rebind to the
        # previous machine); a succeeded-but-unverified operation keeps the 409 below.
        policy = await policy_for_update(db, context.org_id, deployment)
        # The box already holds its live and running slot (desired running since the first setup).
        await check_capacity(db, policy, deployment, new_box=False, admitted=True)
        box.machine_generation = advance_owner_generation(await lock_owner_generation(db, context.org_id, user_id))
        box.observed_state = "unknown"
        box.status = "requested"
        box.desired_state = "running"
        box.lease_expires_at = datetime.now(UTC) + timedelta(minutes=policy.max_lease_minutes)
        box.last_activity_at = datetime.now(UTC)
        operation = await save_operation(db, box, "setup", idempotency_key, digest("setup", request.model_dump()))
        db.add(DevBoxEvent(id=new_uuid(), **scope(box), operation_id=operation.id, event_type="setup_retry_admitted"))
        await db.commit()
        return receipt(operation)
    raise HTTPException(409, "The canonical desktop state has not been verified")


LAUNCH_FAILURE_CLASSES = ("launch_unconfirmed", "launch_abandoned", "workspace_zone_unavailable")


async def setup_launch_failed(db: AsyncSession, box: DevBox) -> bool:
    """True when the box never got a machine because its latest operation, a setup, failed at the launch (#8423)."""
    if box.status != "requested":
        return False
    latest = await db.scalar(
        select(DevBoxOperation)
        .where(DevBoxOperation.box_id == box.id)
        .order_by(DevBoxOperation.created_at.desc(), DevBoxOperation.id.desc())
        .limit(1)
    )
    if latest is None or latest.action != "setup" or latest.status != "failed":
        return False
    failure = latest.error_class or ""
    if not (failure.startswith("provider_refused:") or failure in LAUNCH_FAILURE_CLASSES):
        return False
    launched = await db.scalar(
        select(DevBoxInstance.id).where(
            DevBoxInstance.box_id == box.id,
            DevBoxInstance.machine_generation == box.machine_generation,
            DevBoxInstance.provider_instance_id.is_not(None),
        )
    )
    return launched is None


@router.post("", response_model=OperationResponse, status_code=status.HTTP_202_ACCEPTED)
async def setup(
    request: SetupRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    deployment = deployment_id()
    user_id = await owner(db, context)
    from src.cloud_desktop.host_identity import ec2_crew_allowed

    if request.profile == "crew" and not await ec2_crew_allowed(db, context.org_id):
        raise HTTPException(403, "EC2 crew is not permitted by organization policy")
    policy = await policy_for_update(db, context.org_id, deployment)
    request_digest = digest("setup", request.model_dump())
    previous = await replay(db, user_id, context.org_id, deployment, idempotency_key, request_digest)
    if previous:
        return receipt(previous)
    template = await db.scalar(
        select(DevBoxTemplate)
        .where(DevBoxTemplate.name == request.template, DevBoxTemplate.profile == request.profile, DevBoxTemplate.approved.is_(True))
        .order_by(DevBoxTemplate.revision.desc(), DevBoxTemplate.id)
    )
    if not template or template.instance_size not in policy.allowed_sizes or not 0 < template.disk_gb <= policy.max_disk_gb:
        raise HTTPException(403, "Template or requested capacity is not permitted")
    existing = await db.scalar(
        select(DevBox.id).where(
            DevBox.user_id == user_id, DevBox.org_id == context.org_id, DevBox.deployment_id == deployment, DevBox.status != "released"
        )
    )
    if existing:
        raise HTTPException(409, "A desktop is already reserved for this owner")
    await check_capacity(db, policy, deployment, new_box=True, requested_disk_gb=template.disk_gb)
    generation = advance_owner_generation(await lock_owner_generation(db, context.org_id, user_id))
    box = DevBox(
        id=new_uuid(),
        user_id=user_id,
        org_id=context.org_id,
        deployment_id=deployment,
        template_id=template.id,
        template_revision=template.revision,
        profile=template.profile,
        status="requested",
        desired_state="running",
        observed_state="unknown",
        machine_generation=generation,
        last_activity_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=policy.max_lease_minutes),
    )
    db.add(box)
    return receipt(await save_operation(db, box, "setup", idempotency_key, request_digest))


async def host_readiness_refusal(db: AsyncSession, box: DevBox) -> str | None:
    """The controller's named readiness refusal for this generation's machine, else ``None``.

    Set by the controller on the instance row while the host refuses readiness (class only,
    e.g. ``host_refused:InvalidCheckpoint``), cleared the moment readiness succeeds, so the owner's
    page can say why "lease fenced" persists instead of waiting silently (#8223).
    """
    if box.observed_state != "running":
        return None
    instance = await db.scalar(
        select(DevBoxInstance).where(DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == box.machine_generation)
    )
    if instance is None or instance.readiness_class == "crew_ready" or not instance.error_class:
        return None
    return instance.error_class if instance.error_class.startswith("host_refused:") else None


async def host_binding_state(db: AsyncSession, box: DevBox) -> str:
    """Where this generation's host stands with the owner's browser and the controller.

    ``none``: no device approved for this generation yet (the browser must enroll once);
    ``pending_enrollment``: an approval is awaiting the controller; ``enrolled``: the browser's
    approval is consumed and the controller owns the binding (it re-binds a run after every
    start by itself); ``active``: a live host run exists. The UI enrolls only on ``none`` (#8223).
    """
    now = datetime.now(UTC)
    live = await db.scalar(
        select(DevBoxAccessSession.id).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
            DevBoxAccessSession.expires_at > now,
        )
    )
    if live is not None:
        return "active"
    devices = (
        await db.scalars(
            select(DevBoxDevice).where(
                DevBoxDevice.box_id == box.id,
                DevBoxDevice.machine_generation == box.machine_generation,
                DevBoxDevice.revoked_at.is_(None),
            )
        )
    ).all()
    if any(device.enrollment_consumed_at for device in devices):
        return "enrolled"
    if any(
        device.enrollment_expires_at and device.enrollment_expires_at.replace(tzinfo=device.enrollment_expires_at.tzinfo or UTC) > now
        for device in devices
    ):
        return "pending_enrollment"
    return "none"


async def box_response(db: AsyncSession, box: DevBox) -> dict:
    template = await db.get(DevBoxTemplate, box.template_id)
    return {
        "contract_version": 1,
        "desktop_id": box.id,
        "name": box.name,
        "status": box.status,
        "desired_state": box.desired_state,
        "observed_state": box.observed_state,
        "machine_generation": box.machine_generation,
        "template_revision": box.template_revision,
        "profile": box.profile,
        "template": template.name if template else None,
        "lease_expires_at": box.lease_expires_at,
        "crew_bootstrap": crew_bootstrap_mode(),
        "host_readiness_refusal": await host_readiness_refusal(db, box),
        "host_binding": await host_binding_state(db, box),
        "infrastructure_cost": await infrastructure_status(db, box),
    }


@router.get("")
async def list_desktops(context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    deployment = deployment_id()
    user_id = await owner(db, context)
    boxes = await db.scalars(select(DevBox).where(DevBox.user_id == user_id, DevBox.org_id == context.org_id, DevBox.deployment_id == deployment))
    return {"contract_version": 1, "items": [await box_response(db, box) for box in boxes]}


@router.get("/policy")
async def get_policy_status(context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    deployment = deployment_id()
    user_id = await owner(db, context)
    try:
        policy = await policy_for_update(db, context.org_id, deployment)
        existing = await db.scalar(
            select(DevBox.id).where(
                DevBox.user_id == user_id,
                DevBox.org_id == context.org_id,
                DevBox.deployment_id == deployment,
                DevBox.status != "released",
            )
        )
        if existing:
            raise HTTPException(409, "A desktop is already reserved for this owner")
        candidates = list(
            (
                await db.scalars(
                    select(DevBoxTemplate)
                    .where(
                        DevBoxTemplate.approved.is_(True),
                        DevBoxTemplate.disk_gb > 0,
                        DevBoxTemplate.disk_gb <= policy.max_disk_gb,
                    )
                    .order_by(DevBoxTemplate.disk_gb, DevBoxTemplate.id)
                )
            ).all()
        )
        template = next((item for item in candidates if item.instance_size in policy.allowed_sizes), None)
        if template is None:
            raise HTTPException(403, "No approved Dev Box template is available")
        await check_capacity(db, policy, deployment, new_box=True, requested_disk_gb=template.disk_gb)
    except HTTPException as error:
        return {"contract_version": 1, "admission_allowed": False, "reason": error.detail}
    return {"contract_version": 1, "admission_allowed": True, "reason": None}


@router.get("/crew-template")
async def get_crew_template(context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    from src.cloud_desktop.host_identity import ec2_crew_allowed

    if not await ec2_crew_allowed(db, context.org_id):
        raise HTTPException(403, "EC2 Crew is not permitted")
    deployment = deployment_id()
    user_id = await owner(db, context)
    policy = await policy_for_update(db, context.org_id, deployment)
    existing = await db.scalar(
        select(DevBox.id).where(
            DevBox.user_id == user_id,
            DevBox.org_id == context.org_id,
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
        )
    )
    if existing:
        raise HTTPException(409, "A desktop is already reserved for this owner")
    candidates = (
        await db.scalars(
            select(DevBoxTemplate)
            .where(
                DevBoxTemplate.profile == "crew",
                DevBoxTemplate.approved.is_(True),
            )
            .order_by(DevBoxTemplate.name, DevBoxTemplate.revision.desc(), DevBoxTemplate.id)
        )
    ).all()
    latest = {}
    for candidate in candidates:
        latest.setdefault(candidate.name, candidate)
    template = next(
        (
            candidate
            for candidate in sorted(latest.values(), key=lambda item: (item.disk_gb, item.id))
            if candidate.instance_size in policy.allowed_sizes and 0 < candidate.disk_gb <= policy.max_disk_gb
        ),
        None,
    )
    if template is None:
        raise HTTPException(403, "No approved Crew template is available")
    await check_capacity(db, policy, deployment, new_box=True, requested_disk_gb=template.disk_gb)
    return {"contract_version": 1, "template": template.name}


@router.get("/{desktop_id}")
async def get_desktop(desktop_id: str, context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    deployment = deployment_id()
    user_id = await owner(db, context)
    return await box_response(db, await owned_box(db, desktop_id, user_id, context.org_id, deployment))


@router.get("/{desktop_id}/host-enrollment-key")
async def host_enrollment_key(
    desktop_id: str,
    response: Response,
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    from src.cloud_desktop.host_identity import current_host, ec2_crew_allowed

    user_id = await owner(db, context)
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment_id())
    if not await ec2_crew_allowed(db, box.org_id):
        raise HTTPException(403, "Crew EC2 policy is unavailable")
    instance = await db.scalar(
        select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id,
            DevBoxInstance.machine_generation == box.machine_generation,
        )
    )
    if (
        box.profile != "crew"
        or instance is None
        or not instance.enrollment_public_key
        or await current_host(
            db,
            box_id=box.id,
            generation=box.machine_generation,
            provider_instance_id=instance.provider_instance_id,
            user_id=user_id,
            org_id=box.org_id,
            deployment_id=box.deployment_id,
            owner=context,
        )
        is None
    ):
        raise HTTPException(409, "Current host key is not available")
    response.headers["Cache-Control"] = "no-store"
    return {
        "public_key_b64": base64.b64encode(instance.enrollment_public_key).decode(),
        "public_key_digest": hashlib.sha256(instance.enrollment_public_key).hexdigest(),
        "generation": box.machine_generation,
    }


@router.post("/{desktop_id}/host-enrollment", status_code=201)
async def approve_host_enrollment(
    desktop_id: str,
    body: HostEnrollmentRequest,
    response: Response,
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    from src.cloud_desktop.host_identity import current_host, ec2_crew_allowed

    deployment = deployment_id()
    user_id = await owner(db, context)
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment, lock=True)
    if not await ec2_crew_allowed(db, box.org_id):
        raise HTTPException(403, "Crew EC2 policy is unavailable")
    instance = await db.scalar(
        select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id,
            DevBoxInstance.machine_generation == box.machine_generation,
        )
    )
    if (
        instance is None
        or await current_host(
            db,
            box_id=box.id,
            generation=box.machine_generation,
            provider_instance_id=instance.provider_instance_id,
            user_id=user_id,
            org_id=box.org_id,
            deployment_id=deployment,
            owner=context,
        )
        is None
    ):
        raise HTTPException(409, "Current host has not been verified")
    if box.profile != "crew" or (
        instance.enrollment_public_key is not None and hashlib.sha256(instance.enrollment_public_key).hexdigest() != body.public_key_digest
    ):
        raise HTTPException(409, "Current host key has changed")
    now = datetime.now(UTC)
    existing = await db.scalar(
        select(DevBoxDevice).where(
            DevBoxDevice.box_id == box.id,
            DevBoxDevice.machine_generation == box.machine_generation,
            DevBoxDevice.enrollment_consumed_at.is_(None),
            DevBoxDevice.revoked_at.is_(None),
            DevBoxDevice.enrollment_expires_at > now,
        )
    )
    if existing is not None:
        raise HTTPException(409, "A host enrollment is pending")
    active = await db.scalar(
        select(DevBoxAccessSession.id).where(
            DevBoxAccessSession.box_id == box.id,
            DevBoxAccessSession.machine_generation == box.machine_generation,
            DevBoxAccessSession.terminated_at.is_(None),
            DevBoxAccessSession.expires_at > now,
        )
    )
    if active is not None:
        raise HTTPException(409, "A host run is already active")
    reused_key = await db.scalar(
        select(DevBoxDevice.id).where(
            DevBoxDevice.box_id == box.id,
            DevBoxDevice.machine_generation == box.machine_generation,
            DevBoxDevice.public_key_digest == body.public_key_digest,
        )
    )
    if reused_key is not None:
        raise HTTPException(409, "Host enrollment requires a new public key")
    challenge = secrets.token_urlsafe(32)
    device = DevBoxDevice(
        id=new_uuid(),
        **scope(box),
        instance_id=instance.id,
        public_key_digest=body.public_key_digest,
        enrollment_digest=hashlib.sha256(challenge.encode()).hexdigest(),
        enrollment_expires_at=now + timedelta(minutes=5),
        auth_epoch=0,
    )
    db.add(device)
    from src.crew.host_lease import retire_pod_lease
    from src.crew.inventory import CrewExecutorInventory, CrewLeaseFencedError

    try:
        await CrewExecutorInventory.bind_host(db, box, retire_pod=retire_pod_lease)
        await db.commit()
    except (IntegrityError, CrewLeaseFencedError):
        await db.rollback()
        raise HTTPException(409, "Host enrollment or executor generation changed during admission") from None
    response.headers["Cache-Control"] = "no-store"
    return {"device_id": device.id, "challenge": challenge, "expires_at": device.enrollment_expires_at.isoformat()}


@router.post("/{desktop_id}/host-enrollment/{device_id}/sealed", status_code=202)
async def submit_sealed_enrollment(
    desktop_id: str,
    device_id: str,
    body: SealedEnrollmentRequest,
    response: Response,
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    from src.cloud_desktop.host_identity import current_host, ec2_crew_allowed

    try:
        ciphertext = base64.b64decode(body.ciphertext_b64, validate=True)
    except binascii.Error:
        raise HTTPException(422, "Invalid sealed enrollment") from None
    if len(ciphertext) != 384:
        raise HTTPException(422, "Invalid sealed enrollment")
    user_id = await owner(db, context)
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment_id(), lock=True)
    if not await ec2_crew_allowed(db, box.org_id):
        raise HTTPException(403, "Crew EC2 policy is unavailable")
    device = await db.get(DevBoxDevice, device_id, with_for_update=True)
    instance = await db.scalar(
        select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id,
            DevBoxInstance.machine_generation == box.machine_generation,
        )
    )
    now = datetime.now(UTC)
    if (
        box.profile != "crew"
        or device is None
        or instance is None
        or device.box_id != box.id
        or device.user_id != user_id
        or device.org_id != box.org_id
        or device.deployment_id != box.deployment_id
        or device.machine_generation != box.machine_generation
        or device.instance_id != instance.id
        or device.revoked_at
        or device.enrollment_consumed_at
        or not device.enrollment_expires_at
        or device.enrollment_expires_at.replace(tzinfo=device.enrollment_expires_at.tzinfo or UTC) <= now
        or not instance.enrollment_public_key
        or hashlib.sha256(instance.enrollment_public_key).hexdigest() != device.public_key_digest
        or await current_host(
            db,
            box_id=box.id,
            generation=box.machine_generation,
            provider_instance_id=instance.provider_instance_id,
            user_id=user_id,
            org_id=box.org_id,
            deployment_id=box.deployment_id,
            owner=context,
        )
        is None
    ):
        raise HTTPException(409, "Host enrollment is not current")
    if device.sealed_enrollment is not None and device.sealed_enrollment != ciphertext:
        raise HTTPException(409, "Host enrollment has a different sealed challenge")
    device.sealed_enrollment = ciphertext
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"status": "pending"}


@router.get("/{desktop_id}/operations/{operation_id}")
async def get_operation(
    desktop_id: str, operation_id: str, context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)
):
    deployment = deployment_id()
    user_id = await owner(db, context)
    await owned_box(db, desktop_id, user_id, context.org_id, deployment)
    operation = await db.scalar(
        select(DevBoxOperation).where(
            DevBoxOperation.id == operation_id,
            DevBoxOperation.box_id == desktop_id,
            DevBoxOperation.user_id == user_id,
            DevBoxOperation.org_id == context.org_id,
            DevBoxOperation.deployment_id == deployment,
        )
    )
    if not operation:
        raise HTTPException(404, "Operation not found")
    return receipt(operation)


@router.post("/{desktop_id}/ssh-access")
async def ssh_access(
    desktop_id: str,
    response: Response,
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = await owner(db, context)
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment_id(), lock=True)
    if (
        box.status != "running"
        or box.observed_state != "running"
        or (box.lease_expires_at and box.lease_expires_at.replace(tzinfo=UTC) <= datetime.now(UTC))
    ):
        raise HTTPException(409, "Desktop is not running")
    instance = await db.scalar(
        select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id,
            DevBoxInstance.user_id == box.user_id,
            DevBoxInstance.org_id == box.org_id,
            DevBoxInstance.deployment_id == box.deployment_id,
            DevBoxInstance.machine_generation == box.machine_generation,
            DevBoxInstance.observed_state == "running",
        )
    )
    if instance is None:
        raise HTTPException(409, "Current Dev Box instance is unavailable for SSH")
    if (
        await current_host(
            db,
            box_id=box.id,
            generation=box.machine_generation,
            provider_instance_id=instance.provider_instance_id,
            user_id=user_id,
            org_id=box.org_id,
            deployment_id=box.deployment_id,
            owner=context,
        )
        is None
    ):
        raise HTTPException(409, "Current Dev Box host is not authorized for SSH")
    grant = await issue_ssh_credentials(instance, box.id, box.machine_generation)
    operation_id = new_uuid()
    db.add(
        DevBoxOperation(
            id=operation_id,
            **scope(box),
            action="ssh_access",
            idempotency_key=operation_id,
            request_digest=digest("ssh_access", {"instance_id": instance.provider_instance_id}),
            actor_user_id=user_id,
            provider_token=digest("provider", {"operation_id": operation_id}),
            status="succeeded",
            progress="credentials_issued",
        )
    )
    await db.flush()
    db.add(DevBoxEvent(id=new_uuid(), **scope(box), operation_id=operation_id, event_type="ssh_access_issued"))
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"contract_version": 1, "desktop_id": box.id, "operation_id": operation_id, **grant}


async def accept_box_activity(db: AsyncSession, box: DevBox, request: ActivityRequest, now: datetime) -> None:
    if box.lease_expires_at and box.lease_expires_at.replace(tzinfo=UTC) <= now:
        raise HTTPException(409, "Desktop lease expired")
    pending = await db.scalar(
        select(DevBoxOperation)
        .where(
            DevBoxOperation.box_id == box.id,
            DevBoxOperation.action == "stop",
            DevBoxOperation.status.in_(("pending", "unknown")),
        )
        .with_for_update()
    )
    if box.status == "released" or (box.desired_state != "running" and not pending):
        raise HTTPException(409, "Desktop is not available for activity")
    if pending:
        if not pending.idempotency_key.startswith("auto-stop:") or pending.progress != "admitted":
            raise HTTPException(409, "Stop already in progress")
        pending.status = "failed"
        pending.error_class = "superseded_by_activity"
        box.desired_state = "running"
    if request.kind == "host":
        if request.busy or request.consolidation_complete is not None:
            raise HTTPException(422, "Developer activity cannot report assistant state")
        box.last_activity_at = now
    else:
        if request.busy and request.consolidation_complete:
            raise HTTPException(422, "Busy assistant cannot report consolidation complete")
        box.assistant_activity_at = now
        box.assistant_busy_until_at = now + timedelta(minutes=15) if request.busy else None
        box.assistant_consolidated_at = now if request.consolidation_complete is True else None


@router.post("/{desktop_id}/activity", status_code=status.HTTP_202_ACCEPTED)
async def report_activity(
    desktop_id: str, request: ActivityRequest, context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)
):
    deployment = deployment_id()
    user_id = await owner(db, context)
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment, lock=True)
    await accept_box_activity(db, box, request, datetime.now(UTC))
    await db.commit()
    return {"contract_version": 1, "accepted": True}


@router.post("/{desktop_id}/{action}", response_model=OperationResponse, status_code=status.HTTP_202_ACCEPTED)
async def change_state(
    desktop_id: str,
    action: Literal["start", "stop", "replace", "release"],
    request: ReleaseRequest | ReplaceRequest | None = Body(default=None),
    idempotency_key: str = Header(min_length=1, max_length=128),
    context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    deployment = deployment_id()
    user_id = await owner(db, context)
    policy = await policy_for_update(db, context.org_id, deployment) if action in ("start", "replace") else None
    box = await owned_box(db, desktop_id, user_id, context.org_id, deployment, lock=True)
    if action == "release" and not isinstance(request, ReleaseRequest):
        raise HTTPException(422, "An explicit workspace retention choice is required")
    if action == "replace" and request is not None and not isinstance(request, ReplaceRequest):
        raise HTTPException(422, "Only a snapshot choice is valid for replacement")
    if action in ("start", "stop") and request is not None:
        raise HTTPException(422, "A request body is not valid for this action")
    request_digest = digest(action, {"desktop_id": desktop_id, **(request.model_dump() if request else {})})
    previous = await replay(db, user_id, context.org_id, deployment, idempotency_key, request_digest)
    if previous:
        return receipt(previous)
    if box.status == "released":
        raise HTTPException(409, "Desktop was released")
    active = await db.scalar(select(DevBoxOperation).where(DevBoxOperation.box_id == box.id, DevBoxOperation.status.in_(["pending", "unknown"])))
    if active is not None:
        if action not in ("release", "replace"):
            raise HTTPException(409, ACTIVE_OPERATION)
        if not launch_abandoned(active, datetime.now(UTC)):
            raise HTTPException(409, active_operation_detail(active, datetime.now(UTC)))
        # The controller fences on status, so marking the abandoned launch failed also stops any late commit.
        active.status = "failed"
        active.error_class = active.error_class or "launch_abandoned"
        active.lease_owner = None
        active.lease_expires_at = None
        db.add(DevBoxEvent(id=new_uuid(), **scope(box), operation_id=active.id, event_type="launch_abandoned"))
        await db.flush()
    if action in ("stop", "release"):
        from src.cloud_desktop.host_fencing import fence_host

        # A stop keeps the generation's approved device: the controller re-binds a host run on
        # the next start without another browser enrollment. Release revokes it (#8223).
        await fence_host(db, box, keep_devices=(action == "stop"))
    if policy is not None:
        await check_capacity(db, policy, deployment, new_box=False)
        box.lease_expires_at = datetime.now(UTC) + timedelta(minutes=policy.max_lease_minutes)
        box.last_activity_at = datetime.now(UTC)
    if action == "replace":
        disk = await db.scalar(
            select(DevBoxResource)
            .where(
                DevBoxResource.box_id == box.id,
                DevBoxResource.resource_kind == "workspace_disk",
                DevBoxResource.encrypted.is_(True),
                DevBoxResource.retention_state == "retained",
                DevBoxResource.provider_resource_id.like("vol-%"),
                DevBoxResource.deletion_state == "present",
            )
            .order_by(DevBoxResource.machine_generation.desc())
        )
        if box.status != "stopped" or box.observed_state != "stopped" or box.auth_readiness not in ("unknown", "fenced") or disk is None:
            raise HTTPException(409, "Stop and verify the owned workspace before replacement")
        if isinstance(request, ReplaceRequest):
            snapshot = await db.scalar(
                select(DevBoxResource).where(
                    DevBoxResource.box_id == box.id,
                    DevBoxResource.user_id == user_id,
                    DevBoxResource.org_id == context.org_id,
                    DevBoxResource.deployment_id == deployment,
                    DevBoxResource.resource_kind == "snapshot",
                    DevBoxResource.provider_resource_id == request.snapshot_id,
                    DevBoxResource.encrypted.is_(True),
                    DevBoxResource.retention_state == "retained",
                    DevBoxResource.deletion_state == "present",
                    DevBoxResource.last_observed_state == "present",
                    DevBoxResource.size_gb == disk.size_gb,
                )
            )
            if snapshot is None:
                raise HTTPException(404, "Snapshot not found for this desktop")
        from src.cloud_desktop.host_fencing import fence_host

        await fence_host(db, box)
        box.machine_generation = advance_owner_generation(await lock_owner_generation(db, context.org_id, user_id))
        box.observed_state = "unknown"
        box.status = "requested"
    if action == "release" and isinstance(request, ReleaseRequest) and request.workspace_retention == "delete":
        resources = await db.scalars(
            select(DevBoxResource).where(
                DevBoxResource.box_id == box.id,
                DevBoxResource.resource_kind == "workspace_disk",
            )
        )
        for resource in resources:
            if resource.retention_state == "retained":
                resource.retention_state = "delete_requested"
    if action == "release":
        advance_owner_generation(await lock_owner_generation(db, context.org_id, user_id))
    box.desired_state = {"start": "running", "stop": "stopped", "replace": "running", "release": "released"}[action]
    return receipt(
        await save_operation(
            db,
            box,
            action,
            idempotency_key,
            request_digest,
            retention_choice=request.workspace_retention if isinstance(request, ReleaseRequest) else None,
            snapshot_id=request.snapshot_id if isinstance(request, ReplaceRequest) else None,
        )
    )


@router.get("/{desktop_id}/snapshots")
async def get_snapshots(desktop_id: str, context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    deployment = deployment_id()
    user_id = await owner(db, context)
    await owned_box(db, desktop_id, user_id, context.org_id, deployment)
    resources = await db.scalars(
        select(DevBoxResource)
        .where(
            DevBoxResource.box_id == desktop_id,
            DevBoxResource.user_id == user_id,
            DevBoxResource.org_id == context.org_id,
            DevBoxResource.deployment_id == deployment,
            DevBoxResource.resource_kind == "snapshot",
            DevBoxResource.deletion_state != "verified_deleted",
            DevBoxResource.encrypted.is_(True),
        )
        .order_by(DevBoxResource.snapshot_created_at.desc().nulls_last(), DevBoxResource.id)
    )
    return {
        "contract_version": 1,
        "items": [
            {
                "snapshot_id": resource.provider_resource_id,
                "machine_generation": resource.machine_generation,
                "size_gb": resource.size_gb,
                "state": resource.last_observed_state or "pending",
                "created_at": (
                    resource.snapshot_created_at.replace(tzinfo=resource.snapshot_created_at.tzinfo or UTC) if resource.snapshot_created_at else None
                ),
                "observed_at": resource.last_observed_at,
            }
            for resource in resources
        ],
    }


@router.get("/{desktop_id}/resources")
async def get_resources(desktop_id: str, context: TokenContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    deployment = deployment_id()
    user_id = await owner(db, context)
    await owned_box(db, desktop_id, user_id, context.org_id, deployment)
    resources = await db.scalars(
        select(DevBoxResource)
        .where(
            DevBoxResource.box_id == desktop_id,
            DevBoxResource.user_id == user_id,
            DevBoxResource.org_id == context.org_id,
            DevBoxResource.deployment_id == deployment,
        )
        .order_by(DevBoxResource.id)
    )
    return {
        "contract_version": 1,
        "items": [
            {
                "resource_id": resource.id,
                "resource_kind": resource.resource_kind,
                "provider_resource_id": resource.provider_resource_id,
                "machine_generation": resource.machine_generation,
                "retention_state": resource.retention_state,
                "deletion_state": resource.deletion_state,
                "encrypted": resource.encrypted,
                "size_gb": resource.size_gb,
            }
            for resource in resources
        ],
    }
