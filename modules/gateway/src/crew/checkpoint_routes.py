"""Owner admission and separately verified, role-free helper checkpoint routes."""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.chat_crew_run import current_crew_store
from src.agentauth.model_policy_keys import model_policy_keys
from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.cloud_desktop.host_identity import current_host, ec2_crew_allowed
from src.crew.checkpoint_identity import (
    CheckpointHelperVerifier,
    CheckpointIdentityRefusedError,
    CheckpointIdentityUnavailableError,
    RestoreHelperVerifier,
)
from src.crew.checkpoint_objects import COMPATIBLE_CREW_VERSION, InvalidCheckpointObjectsError, VersionedCheckpointObjects, checkpoint_s3_client
from src.crew.checkpoint_store import CheckpointConflictError, CheckpointMissingError, CrewCheckpointStore
from src.crew.host_client_identity import gateway_client_identity
from src.crew.host_routes import host_run_route
from src.crew.proxy import current_crew_owner
from src.crew.targets import CrewLeaseFencedError, CrewOwner
from src.proxy.routes import get_token_context
from src.shared.database import get_session_factory
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxDevice, DevBoxInstance
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["crew-checkpoints"])
NO_CACHE = {"Cache-Control": "no-store"}


def enabled():
    if os.environ.get("ADP_CREW_CHECKPOINTS_ENABLED") != "true":
        raise HTTPException(503, detail={"error": "crew_checkpoints_disabled"}, headers=NO_CACHE)


def get_store():
    retention = os.environ.get("ADP_CREW_CHECKPOINT_RETENTION_SECONDS")
    try:
        return CrewCheckpointStore(get_session_factory(), retention_seconds=int(retention) if retention is not None else None)
    except (ValueError, OverflowError):
        raise HTTPException(503, detail={"error": "checkpoint_retention_unavailable"}, headers=NO_CACHE) from None


def get_objects():
    bucket = os.environ.get("ADP_CREW_CHECKPOINT_BUCKET", "")
    if not bucket:
        raise HTTPException(503, detail={"error": "checkpoint_storage_unavailable"}, headers=NO_CACHE)
    try:
        return VersionedCheckpointObjects(checkpoint_s3_client(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")), bucket)
    except InvalidCheckpointObjectsError:
        raise HTTPException(503, detail={"error": "checkpoint_storage_unavailable"}, headers=NO_CACHE) from None


@lru_cache(maxsize=1)
def get_verifier():
    try:
        return CheckpointHelperVerifier.in_cluster()
    except CheckpointIdentityRefusedError:
        raise HTTPException(503, detail={"error": "checkpoint_identity_unavailable"}, headers=NO_CACHE) from None


@lru_cache(maxsize=1)
def get_restore_verifier():
    try:
        return RestoreHelperVerifier.in_cluster()
    except CheckpointIdentityRefusedError:
        raise HTTPException(503, detail={"error": "checkpoint_identity_unavailable"}, headers=NO_CACHE) from None


async def restore_identity(request: Request, verifier=Depends(get_restore_verifier)):
    return await _verify_helper(request, verifier)


async def helper_identity(request: Request, verifier=Depends(get_verifier)):
    return await _verify_helper(request, verifier)


async def _verify_helper(request, verifier):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 8192:
        raise HTTPException(401, detail={"error": "helper_identity_invalid"}, headers=NO_CACHE)
    try:
        return await run_in_threadpool(verifier.verify, token)
    except CheckpointIdentityRefusedError:
        raise HTTPException(404, detail={"error": "checkpoint_unavailable"}, headers=NO_CACHE) from None
    except CheckpointIdentityUnavailableError:
        raise HTTPException(503, detail={"error": "checkpoint_identity_unavailable"}, headers=NO_CACHE) from None


COMPATIBLE_CREW_VERSION_PATTERN = rf"^{re.escape(COMPATIBLE_CREW_VERSION)}$"


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_predecessor_id: str | None = Field(default=None, max_length=36)


class UploadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_key: str = Field(pattern=r"^[a-f0-9]{52}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_size: int = Field(strict=True, gt=0)
    manifest_size: int = Field(strict=True, gt=0)
    crew_version: str = Field(pattern=COMPATIBLE_CREW_VERSION_PATTERN)


class ObjectReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket: str = Field(min_length=3, max_length=255)
    key: str = Field(min_length=1, max_length=1024)
    version_id: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(strict=True, gt=0)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_key: str = Field(pattern=r"^[a-f0-9]{52}$")
    objects: dict[str, ObjectReceipt]


def _map_error(error):
    if isinstance(error, CheckpointMissingError | CheckpointIdentityRefusedError):
        return HTTPException(404, detail={"error": "checkpoint_unavailable"}, headers=NO_CACHE)
    if isinstance(error, CheckpointConflictError | CrewLeaseFencedError):
        return HTTPException(409, detail={"error": "checkpoint_conflict"}, headers=NO_CACHE)
    if isinstance(error, InvalidCheckpointObjectsError):
        return HTTPException(422, detail={"error": "checkpoint_verification_failed"}, headers=NO_CACHE)
    return HTTPException(503, detail={"error": "checkpoint_unavailable"}, headers=NO_CACHE)


class HostRestoreGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint_id: str = Field(min_length=1, max_length=36)


async def _host_restore_binding(
    request: Request, context: TokenContext, store: CrewCheckpointStore, checkpoint_id: str | None, *, run_id: str | None = None
):
    """Bind the calling host to its run; ``checkpoint_id`` is ``None`` only for a first host with nothing to restore (#8171)."""
    run_id = run_id or request.headers.get("x-agent-runid")
    caller = verified_caller_identity(request)
    instance_id = caller.rsplit("/", 1)[-1] if caller else None
    if (
        context.auth_source != "iam"
        or context.account_type != "service"
        or not context.requires_host_identity
        or not run_id
        or not caller
        or not caller.startswith("arn:aws:sts::")
        or ":assumed-role/" not in caller
        or not instance_id
        or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id)
    ):
        raise HTTPException(403, detail={"error": "host_restore_denied"}, headers=NO_CACHE)
    row = await asyncio.to_thread(current_crew_store().read_run, run_id)
    now = int(datetime.now(UTC).timestamp())
    if (
        row is None
        or row.get("status") != "provisioning"
        or row.get("kind") != "crew"
        or row.get("target_kind") != "ec2-instance"
        or row.get("executor_scope") != "session"
        or row.get("root_human_id") != row.get("user_id")
        or row.get("target_ref") != instance_id
        or row.get("host_role_arn") != parse_assumed_role_arn(caller)
        or isinstance(row.get("lease_generation"), bool)
        or not isinstance(row.get("lease_generation"), int | Decimal)
        or int(row["lease_generation"]) != row["lease_generation"]
        or row["lease_generation"] < 1
        or not row.get("deployment_id")
        or int(row.get("lease_expires_at", 0)) <= now
    ):
        raise HTTPException(403, detail={"error": "host_restore_denied"}, headers=NO_CACHE)
    owner = CrewOwner(row["tenant_id"], row["user_id"])
    box_id = row["session_id"]
    generation = int(row["lease_generation"])
    deployment_id = row.get("deployment_id")
    async with store.session_factory() as db:
        box = await db.get(DevBox, box_id)
        executor = await db.scalar(
            select(CrewExecutor).where(
                CrewExecutor.tenant_id == owner.tenant_id,
                CrewExecutor.user_id == owner.user_id,
                CrewExecutor.dev_box_id == box_id,
                CrewExecutor.generation == generation,
                CrewExecutor.target_kind == "ec2-instance",
                CrewExecutor.state == "provisioning",
            )
        )
        instance = await db.scalar(
            select(DevBoxInstance).where(
                DevBoxInstance.box_id == box_id,
                DevBoxInstance.machine_generation == generation,
                DevBoxInstance.provider_instance_id == instance_id,
                DevBoxInstance.observed_state == "running",
            )
        )
        access = await db.scalar(
            select(DevBoxAccessSession)
            .join(DevBoxDevice, DevBoxDevice.id == DevBoxAccessSession.device_id)
            .where(
                DevBoxAccessSession.box_id == box_id,
                DevBoxAccessSession.user_id == owner.user_id,
                DevBoxAccessSession.org_id == owner.tenant_id,
                DevBoxAccessSession.deployment_id == deployment_id,
                DevBoxAccessSession.machine_generation == generation,
                DevBoxAccessSession.provider_session_ref == run_id,
                DevBoxAccessSession.terminated_at.is_(None),
                DevBoxAccessSession.expires_at > datetime.now(UTC),
                DevBoxDevice.revoked_at.is_(None),
                DevBoxDevice.machine_generation == generation,
                DevBoxDevice.enrollment_consumed_at.is_not(None),
            )
        )
        if (
            box is None
            or box.user_id != owner.user_id
            or box.org_id != owner.tenant_id
            or box.deployment_id != deployment_id
            or box.profile != "crew"
            or box.auth_readiness != "pending"
            or box.machine_generation != generation
            or box.status != "running"
            or box.desired_state != "running"
            or box.observed_state != "running"
            or executor is None
            or executor.latest_verified_checkpoint_id != checkpoint_id
            or instance is None
            or not re.fullmatch(r"[0-9a-f]{64}", instance.certificate_fingerprint or "")
            or access is None
            or await current_host(
                db,
                box_id=box_id,
                generation=generation,
                provider_instance_id=instance_id,
                user_id=owner.user_id,
                org_id=owner.tenant_id,
                deployment_id=deployment_id,
            )
            is None
        ):
            raise HTTPException(409, detail={"error": "host_restore_fenced"}, headers=NO_CACHE)
    return owner, box_id, generation, deployment_id


# Host-facing routes are reached through the gateway REST API's ``/agent/{proxy+}`` method,
# whose integration forwards ``/{proxy}`` to the gateway: the ``/agent`` prefix is stripped at
# the edge, exactly as ``/agent/model/*/invoke`` arrives as ``/model/...`` and
# ``/agent/crew/model-token/refresh`` as ``/crew/model-token/refresh``. Declaring these routes
# with the prefix made every Crew host bootstrap 404 on dev (2026-10-19, #8223); the IAM grant
# the guest holds names the edge path (``.../POST/agent/crew/host-restore/*``) and is unchanged.
@router.post(host_run_route("/crew/host-restore/context"), dependencies=[Depends(enabled)])
async def host_restore_context(
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
):
    caller = verified_caller_identity(request)
    instance_id = caller.rsplit("/", 1)[-1] if caller else None
    if (
        context.auth_source != "iam"
        or context.account_type != "service"
        or not context.requires_host_identity
        or not caller
        or not caller.startswith("arn:aws:sts::")
        or ":assumed-role/" not in caller
        or not instance_id
        or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id)
        or request.headers.get("x-agent-runid")
    ):
        raise HTTPException(403, detail={"error": "host_restore_denied"}, headers=NO_CACHE)
    async with store.session_factory() as db:
        instances = list(
            (
                await db.scalars(
                    select(DevBoxInstance)
                    .where(
                        DevBoxInstance.provider_instance_id == instance_id,
                        DevBoxInstance.observed_state == "running",
                    )
                    .limit(2)
                )
            ).all()
        )
        if len(instances) != 1:
            raise HTTPException(403, detail={"error": "host_restore_denied"}, headers=NO_CACHE)
        instance = instances[0]
        accesses = list(
            (
                await db.scalars(
                    select(DevBoxAccessSession)
                    .where(
                        DevBoxAccessSession.box_id == instance.box_id,
                        DevBoxAccessSession.machine_generation == instance.machine_generation,
                        DevBoxAccessSession.terminated_at.is_(None),
                        DevBoxAccessSession.expires_at > datetime.now(UTC),
                    )
                    .limit(2)
                )
            ).all()
        )
        if len(accesses) != 1 or not accesses[0].provider_session_ref:
            raise HTTPException(403, detail={"error": "host_restore_denied"}, headers=NO_CACHE)
        run_id = accesses[0].provider_session_ref
        executor = await db.scalar(
            select(CrewExecutor).where(
                CrewExecutor.dev_box_id == instance.box_id,
                CrewExecutor.generation == instance.machine_generation,
                CrewExecutor.target_kind == "ec2-instance",
                CrewExecutor.state == "provisioning",
            )
        )
        if executor is None:
            raise HTTPException(409, detail={"error": "host_restore_fenced"}, headers=NO_CACHE)
        # A first host has no verified checkpoint (#8171): it receives a fresh scope (no pins,
        # manifest version 0) and never asks for a restore grant.
        checkpoint_id = executor.latest_verified_checkpoint_id
    owner, box_id, generation, deployment_id = await _host_restore_binding(
        request,
        context,
        store,
        checkpoint_id,
        run_id=run_id,
    )
    async with store.session_factory() as db:
        checkpoint = await db.get(CrewCheckpoint, checkpoint_id) if checkpoint_id else None
        box = await db.get(DevBox, box_id)
        instance = await db.scalar(
            select(DevBoxInstance).where(
                DevBoxInstance.box_id == box_id,
                DevBoxInstance.machine_generation == generation,
                DevBoxInstance.provider_instance_id == instance_id,
            )
        )
        if (
            (
                checkpoint_id is not None
                and (
                    checkpoint is None
                    or checkpoint.status != "verified"
                    or checkpoint.restore_pins
                    or not checkpoint.manifest_sha256
                    or not checkpoint.manifest_version_id
                )
            )
            or box is None
            or box.auth_readiness != "pending"
            or box.machine_generation != generation
            or instance is None
            or not instance.private_address
        ):
            raise HTTPException(409, detail={"error": "host_restore_fenced"}, headers=NO_CACHE)
        try:
            client = gateway_client_identity()
            keys = json.loads((await model_policy_keys()).body)["keys"]
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, detail={"error": "host_restore_trust_unavailable"}, headers=NO_CACHE) from None
        run = await asyncio.to_thread(current_crew_store().read_run, run_id)
        if (
            run is None
            or run.get("status") != "provisioning"
            or run.get("target_ref") != instance_id
            or run.get("lease_generation") != generation
            or int(run.get("lease_expires_at", 0)) <= int(datetime.now(UTC).timestamp())
        ):
            raise HTTPException(409, detail={"error": "host_restore_fenced"}, headers=NO_CACHE)
        return JSONResponse(
            {
                "run_id": run_id,
                "tenant_id": owner.tenant_id,
                "user_id": owner.user_id,
                "box_id": box_id,
                "instance_ref": instance_id,
                "private_ip": instance.private_address,
                "lease_generation": generation,
                "checkpoint_id": checkpoint_id,
                "manifest_sha256": checkpoint.manifest_sha256 if checkpoint else None,
                "restore_manifest_version": checkpoint.sequence if checkpoint else 0,
                "deployment_id": deployment_id,
                "lease_expires_at": int(run["lease_expires_at"]),
                "client_certificate": client.certificate,
                "gateway_fingerprint": client.fingerprint,
                "verification_keys": keys,
            },
            headers=NO_CACHE,
        )


@router.post(host_run_route("/crew/host-restore/grant"), dependencies=[Depends(enabled)])
async def host_restore_grant(
    request: Request,
    body: HostRestoreGrant,
    context: Annotated[TokenContext, Depends(get_token_context)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    storage: Annotated[VersionedCheckpointObjects, Depends(get_objects)],
):
    owner, box_id, generation, deployment_id = await _host_restore_binding(request, context, store, body.checkpoint_id)
    try:
        record, token = await store.start_host_restore(
            owner,
            body.checkpoint_id,
            box_id=box_id,
            generation=generation,
            deployment_id=deployment_id,
        )
        try:
            objects = await run_in_threadpool(storage.grant_read, record)
        except (InvalidCheckpointObjectsError, BotoCoreError, ClientError):
            await store.end_host_restore(owner, body.checkpoint_id, box_id=box_id, generation=generation, token=token)
            raise
        return JSONResponse(
            {
                "id": body.checkpoint_id,
                "restore_token": token,
                "objects": objects,
                "crew_version": record.crew_version,
                "schema_version": record.schema_version,
                "manifest_version": record.sequence,
            },
            headers=NO_CACHE,
        )
    except (CheckpointConflictError, CheckpointMissingError, InvalidCheckpointObjectsError, BotoCoreError, ClientError, SQLAlchemyError) as error:
        raise _map_error(error) from None


class HostRestoreRelease(HostRestoreGrant):
    restore_token: str = Field(min_length=32, max_length=128)


@router.post(host_run_route("/crew/host-restore/release"), dependencies=[Depends(enabled)])
async def host_restore_release(
    request: Request,
    body: HostRestoreRelease,
    context: Annotated[TokenContext, Depends(get_token_context)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
):
    owner, box_id, generation, _ = await _host_restore_binding(request, context, store, body.checkpoint_id)
    try:
        await store.end_host_restore(owner, body.checkpoint_id, box_id=box_id, generation=generation, token=body.restore_token)
    except (CheckpointMissingError, SQLAlchemyError) as error:
        raise _map_error(error) from None
    return JSONResponse({"id": body.checkpoint_id, "status": "released"}, headers=NO_CACHE)


@dataclass(frozen=True)
class HostCheckpointIdentity:
    """The store's helper-identity shape for an EC2 host: owner key and the candidate it may touch.

    The pod path proves these through the checkpoint-helper projected token; a host proves its
    instance identity at the edge and the gateway derives the owner from its own records, so
    the identity is built here, never parsed from the request.
    """

    owner_key: str
    checkpoint_id: str
    checkpoint_hash: str


class HostCheckpointBegin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_.-]+$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_size: int = Field(strict=True, gt=0)
    manifest_size: int = Field(strict=True, gt=0)
    crew_version: str = Field(pattern=COMPATIBLE_CREW_VERSION_PATTERN)


class HostCheckpointComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint_id: str = Field(min_length=1, max_length=36)
    objects: dict[str, ObjectReceipt]


async def _host_checkpoint_binding(request: Request, context: TokenContext, store: CrewCheckpointStore):
    """Bind an uploading host to the owner whose box it is (#8379).

    The caller proves it is EC2 instance ``i-…`` holding the registered guest role (edge-asserted
    IAM identity, ``requires_host_identity``). The gateway maps that instance to exactly one running
    ``DevBoxInstance``, its box and the owner's ``ready`` executor for that machine generation; the
    owner is never taken from the request. Unlike the restore binding this does NOT require the
    live run or access session: both stop paths (owner stop in the API, idle stop in the
    controller) fence host authority at admission, and the final capture runs after that fence
    while the machine is still up. Publication stays fenced on the current generation
    (``CrewExecutorInventory.lock_current_generation``), so a retired generation cannot publish.
    """
    caller = verified_caller_identity(request)
    instance_id = caller.rsplit("/", 1)[-1] if caller else None
    if (
        context.auth_source != "iam"
        or context.account_type != "service"
        or not context.requires_host_identity
        or not caller
        or not caller.startswith("arn:aws:sts::")
        or ":assumed-role/" not in caller
        or not instance_id
        or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id)
    ):
        raise HTTPException(403, detail={"error": "host_checkpoint_denied"}, headers=NO_CACHE)
    now = int(time.time())
    async with store.session_factory() as db:
        instances = list(
            (
                await db.scalars(
                    select(DevBoxInstance)
                    .where(DevBoxInstance.provider_instance_id == instance_id, DevBoxInstance.observed_state == "running")
                    .limit(2)
                )
            ).all()
        )
        if len(instances) != 1:
            raise HTTPException(403, detail={"error": "host_checkpoint_denied"}, headers=NO_CACHE)
        instance = instances[0]
        box = await db.get(DevBox, instance.box_id)
        if (
            box is None
            or box.profile != "crew"
            or box.status == "released"
            or box.observed_state != "running"
            or box.machine_generation != instance.machine_generation
            or box.user_id != instance.user_id
            or box.org_id != instance.org_id
            or box.deployment_id != instance.deployment_id
            or not await ec2_crew_allowed(db, box.org_id)
        ):
            raise HTTPException(409, detail={"error": "host_checkpoint_fenced"}, headers=NO_CACHE)
        executor = await db.scalar(
            select(CrewExecutor).where(
                CrewExecutor.tenant_id == box.org_id,
                CrewExecutor.user_id == box.user_id,
                CrewExecutor.target_kind == "ec2-instance",
                CrewExecutor.dev_box_id == box.id,
                CrewExecutor.generation == box.machine_generation,
                CrewExecutor.state == "ready",
            )
        )
        if (
            executor is None
            or not executor.owner_key
            or executor.lease_generation != box.machine_generation
            or executor.lease_expires_at is None
            or executor.lease_expires_at <= now
        ):
            raise HTTPException(409, detail={"error": "host_checkpoint_fenced"}, headers=NO_CACHE)
        return CrewOwner(box.org_id, box.user_id), executor.owner_key, executor.latest_verified_checkpoint_id


@router.post(host_run_route("/crew/host-checkpoint/begin"), dependencies=[Depends(enabled)])
async def host_checkpoint_begin(
    request: Request,
    body: HostCheckpointBegin,
    context: Annotated[TokenContext, Depends(get_token_context)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    storage: Annotated[VersionedCheckpointObjects, Depends(get_objects)],
):
    """Admit a host capture and grant its direct-to-S3 uploads; a replayed key returns the prior row."""
    owner, owner_key, predecessor = await _host_checkpoint_binding(request, context, store)
    try:
        checkpoint, _created = await store.admit(owner, body.idempotency_key, expected_predecessor_id=predecessor, with_creation=True)
        if checkpoint.status == "verified":
            return JSONResponse({"id": checkpoint.id, "status": "verified", "sequence": checkpoint.sequence}, headers=NO_CACHE)
        identity = HostCheckpointIdentity(owner_key, checkpoint.id, hashlib.sha256(checkpoint.id.encode()).hexdigest())
        grant = await store.grant(
            checkpoint.id,
            identity,
            {
                "archive": {"sha256": body.archive_sha256, "size": body.archive_size},
                "manifest": {"sha256": body.manifest_sha256, "size": body.manifest_size},
            },
            storage,
        )
        return JSONResponse(
            {
                "id": checkpoint.id,
                "status": "uploading",
                "sequence": checkpoint.sequence,
                "archive": grant["archive"],
                "manifest": grant["manifest"],
            },
            headers=NO_CACHE,
        )
    except (
        CheckpointMissingError,
        CheckpointConflictError,
        InvalidCheckpointObjectsError,
        CrewLeaseFencedError,
        SQLAlchemyError,
        BotoCoreError,
        ClientError,
    ) as error:
        raise _map_error(error) from None


@router.post(host_run_route("/crew/host-checkpoint/complete"), dependencies=[Depends(enabled)])
async def host_checkpoint_complete(
    request: Request,
    body: HostCheckpointComplete,
    context: Annotated[TokenContext, Depends(get_token_context)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    storage: Annotated[VersionedCheckpointObjects, Depends(get_objects)],
):
    """Verify the uploaded exact versions and publish the checkpoint as the owner's latest."""
    _owner, owner_key, _predecessor = await _host_checkpoint_binding(request, context, store)
    if set(body.objects) != {"archive", "manifest"}:
        raise _map_error(CheckpointMissingError("incomplete receipt"))
    identity = HostCheckpointIdentity(owner_key, body.checkpoint_id, hashlib.sha256(body.checkpoint_id.encode()).hexdigest())
    try:
        record = await store.complete(body.checkpoint_id, identity, {label: item.model_dump() for label, item in body.objects.items()}, storage)
        return JSONResponse({"id": record.id, "status": record.status}, headers=NO_CACHE)
    except (
        CheckpointMissingError,
        CheckpointConflictError,
        InvalidCheckpointObjectsError,
        CrewLeaseFencedError,
        SQLAlchemyError,
        BotoCoreError,
        ClientError,
    ) as error:
        raise _map_error(error) from None


@router.post("/v1/crew/checkpoints", dependencies=[Depends(enabled)])
async def admit_checkpoint(
    body: Admission, owner: Annotated[CrewOwner, Depends(current_crew_owner)], store: Annotated[CrewCheckpointStore, Depends(get_store)]
):
    try:
        checkpoint, created = await store.admit(owner, body.idempotency_key, expected_predecessor_id=body.expected_predecessor_id, with_creation=True)
    except (CheckpointConflictError, SQLAlchemyError) as error:
        raise _map_error(error) from None
    return JSONResponse(
        {"id": checkpoint.id, "status": checkpoint.status, "sequence": checkpoint.sequence}, status_code=202 if created else 200, headers=NO_CACHE
    )


@router.get("/v1/crew/checkpoints/selection", dependencies=[Depends(enabled)])
async def checkpoint_selection(owner: Annotated[CrewOwner, Depends(current_crew_owner)], store: Annotated[CrewCheckpointStore, Depends(get_store)]):
    try:
        return JSONResponse(await store.selection_for_owner(owner), headers=NO_CACHE)
    except SQLAlchemyError as error:
        raise _map_error(error) from None


@router.get("/v1/crew/checkpoints/coverage", dependencies=[Depends(enabled)])
async def checkpoint_coverage(owner: Annotated[CrewOwner, Depends(current_crew_owner)], store: Annotated[CrewCheckpointStore, Depends(get_store)]):
    try:
        return JSONResponse(await store.coverage_for_owner(owner), headers=NO_CACHE)
    except SQLAlchemyError as error:
        raise _map_error(error) from None


@router.get("/v1/crew/checkpoints", dependencies=[Depends(enabled)])
async def list_checkpoints(owner: Annotated[CrewOwner, Depends(current_crew_owner)], store: Annotated[CrewCheckpointStore, Depends(get_store)]):
    try:
        return JSONResponse({"checkpoints": await store.list_for_owner(owner)}, headers=NO_CACHE)
    except SQLAlchemyError as error:
        raise _map_error(error) from None


@router.get("/v1/crew/conversations", dependencies=[Depends(enabled)])
async def list_conversations(
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    source: str | None = None,
    limit: int = 50,
):
    if source and source not in ("kiro_crew", "claude_code", "codex") or not 1 <= limit <= 100:
        raise HTTPException(422, detail={"error": "invalid_conversation_filter"}, headers=NO_CACHE)
    try:
        return JSONResponse({"conversations": await store.list_conversations(owner, source=source, limit=limit)}, headers=NO_CACHE)
    except SQLAlchemyError as error:
        raise _map_error(error) from None


@router.get("/v1/crew/conversations/{source}/{conversation_id}", dependencies=[Depends(enabled)])
async def conversation_detail(
    source: str,
    conversation_id: str,
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    storage: Annotated[VersionedCheckpointObjects, Depends(get_objects)],
    offset: int = 0,
    limit: int = 50,
):
    if source not in ("kiro_crew", "claude_code", "codex") or not 0 <= offset <= 100000 or not 1 <= limit <= 100:
        raise HTTPException(422, detail={"error": "invalid_conversation_filter"}, headers=NO_CACHE)
    try:
        found = await store.conversation_for_owner(owner, source, conversation_id)
        if found is None:
            raise HTTPException(404, detail={"error": "conversation_unavailable"}, headers=NO_CACHE)
        row, checkpoint = found
        if row.unreadable:
            return JSONResponse({"turns": [], "has_more": False, "unreadable": True, "partial": source == "codex"}, headers=NO_CACHE)
        turns, has_more = await run_in_threadpool(storage.conversation_turns, checkpoint, row.archive_path, source, conversation_id, offset, limit)
        return JSONResponse({"turns": turns, "has_more": has_more, "unreadable": False, "partial": source == "codex"}, headers=NO_CACHE)
    except SQLAlchemyError as error:
        raise _map_error(error) from None
    except (InvalidCheckpointObjectsError, ClientError, BotoCoreError):
        raise HTTPException(503, detail={"error": "conversation_unavailable"}, headers=NO_CACHE) from None


@router.get("/v1/crew/checkpoints/{checkpoint_id}", dependencies=[Depends(enabled)])
async def checkpoint_status(
    checkpoint_id: str, owner: Annotated[CrewOwner, Depends(current_crew_owner)], store: Annotated[CrewCheckpointStore, Depends(get_store)]
):
    try:
        return JSONResponse(await store.status_for_owner(owner, checkpoint_id), headers=NO_CACHE)
    except (CheckpointMissingError, SQLAlchemyError) as error:
        raise _map_error(error) from None


@router.delete("/v1/crew/checkpoints/{checkpoint_id}", dependencies=[Depends(enabled)])
async def request_checkpoint_deletion(
    checkpoint_id: str,
    owner: Annotated[CrewOwner, Depends(current_crew_owner)],
    store: Annotated[CrewCheckpointStore, Depends(get_store)],
    storage: Annotated[VersionedCheckpointObjects, Depends(get_objects)],
):
    try:
        status = await store.request_delete(owner, checkpoint_id)
        if status != "deleted":
            status = await store.reconcile_delete(owner, checkpoint_id, storage)
        return JSONResponse({"id": checkpoint_id, "status": status}, headers=NO_CACHE)
    except (CheckpointMissingError, CheckpointConflictError, InvalidCheckpointObjectsError, BotoCoreError, ClientError, SQLAlchemyError) as error:
        raise _map_error(error) from None


class RestoreRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restore_token: str = Field(min_length=32, max_length=128)


@router.post("/internal/v1/agent/crew/checkpoints/{checkpoint_id}/restore-grant", dependencies=[Depends(enabled)])
async def restore_grant(
    checkpoint_id: str,
    identity=Depends(restore_identity),
    store: CrewCheckpointStore = Depends(get_store),
    storage=Depends(get_objects),
    verifier: RestoreHelperVerifier = Depends(get_restore_verifier),
):
    try:
        owner = await store.restore_owner(checkpoint_id, identity)
        record, token = await store.start_restore(
            owner, checkpoint_id, owner_key=identity.owner_key, pod_uid=identity.pod_uid, pod_name=identity.pod_name, pod_is_gone=verifier.pod_is_gone
        )
        try:
            objects = storage.grant_read(record)
        except (InvalidCheckpointObjectsError, BotoCoreError, ClientError):
            await store.end_restore(owner, checkpoint_id, token)
            raise
        return JSONResponse(
            {
                "id": checkpoint_id,
                "restore_token": token,
                "objects": objects,
                "crew_version": record.crew_version,
                "schema_version": record.schema_version,
            },
            headers=NO_CACHE,
        )
    except (
        CheckpointMissingError,
        CheckpointConflictError,
        InvalidCheckpointObjectsError,
        CheckpointIdentityUnavailableError,
        BotoCoreError,
        ClientError,
        SQLAlchemyError,
    ) as error:
        raise _map_error(error) from None


@router.post("/internal/v1/agent/crew/checkpoints/{checkpoint_id}/restore-release", dependencies=[Depends(enabled)])
async def restore_release(
    checkpoint_id: str, body: RestoreRelease, identity=Depends(restore_identity), store: CrewCheckpointStore = Depends(get_store)
):
    try:
        owner = await store.restore_owner(checkpoint_id, identity, require_selected=False)
        await store.end_restore(owner, checkpoint_id, body.restore_token)
        return JSONResponse({"id": checkpoint_id, "status": "released"}, headers=NO_CACHE)
    except (CheckpointMissingError, SQLAlchemyError) as error:
        raise _map_error(error) from None


@router.post("/internal/v1/agent/crew/checkpoints/{checkpoint_id}/upload-grant", dependencies=[Depends(enabled)])
async def upload_grant(
    checkpoint_id: str,
    body: UploadGrant,
    identity=Depends(helper_identity),
    store: CrewCheckpointStore = Depends(get_store),
    storage=Depends(get_objects),
):
    if body.owner_key != identity.owner_key:
        raise _map_error(CheckpointMissingError("foreign helper"))
    try:
        result = await store.grant(
            checkpoint_id,
            identity,
            {
                "archive": {"sha256": body.archive_sha256, "size": body.archive_size},
                "manifest": {"sha256": body.manifest_sha256, "size": body.manifest_size},
            },
            storage,
        )
        return JSONResponse(result, headers=NO_CACHE)
    except (
        CheckpointMissingError,
        CheckpointConflictError,
        InvalidCheckpointObjectsError,
        CrewLeaseFencedError,
        SQLAlchemyError,
        BotoCoreError,
        ClientError,
    ) as error:
        raise _map_error(error) from None


@router.post("/internal/v1/agent/crew/checkpoints/{checkpoint_id}/complete", dependencies=[Depends(enabled)])
async def complete_checkpoint(
    checkpoint_id: str,
    body: Completion,
    identity=Depends(helper_identity),
    store: CrewCheckpointStore = Depends(get_store),
    storage=Depends(get_objects),
):
    if body.owner_key != identity.owner_key or set(body.objects) != {"archive", "manifest"}:
        raise _map_error(CheckpointMissingError("foreign or incomplete receipt"))
    try:
        record = await store.complete(checkpoint_id, identity, {label: item.model_dump() for label, item in body.objects.items()}, storage)
        return JSONResponse({"id": record.id, "status": record.status}, headers=NO_CACHE)
    except (
        CheckpointMissingError,
        CheckpointConflictError,
        InvalidCheckpointObjectsError,
        CrewLeaseFencedError,
        SQLAlchemyError,
        BotoCoreError,
        ClientError,
    ) as error:
        raise _map_error(error) from None
