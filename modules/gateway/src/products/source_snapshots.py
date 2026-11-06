"""Durable, immutable receipts for independently captured Product sources."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import ProductSourceSnapshot
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import CredentialServiceDelegation

MAX_RETENTION = timedelta(days=90)
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    source_connection_id: str = Field(min_length=1, max_length=255)
    record_type: str = Field(min_length=1, max_length=255)
    record_id: str = Field(min_length=1, max_length=255)
    record_revision: str = Field(min_length=1, max_length=512)

    @field_validator("record_revision")
    @classmethod
    def require_explicit_revision(cls, revision: str) -> str:
        if revision.casefold() in {"head", "latest", "null"}:
            raise ValueError("An immutable source revision is required")
        return revision


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    media_type: str = Field(min_length=1, max_length=128)
    bytes: int = Field(ge=0)


def validate_exclusions(exclusions: list[str]) -> list[str]:
    if any(not value or len(value) > 1024 for value in exclusions):
        raise ValueError("Exclusions must be nonempty patterns of at most 1024 characters")
    return exclusions


class SourceSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str = Field(min_length=1, max_length=255)
    delegation_revision: int = Field(ge=1)
    source_ref: SourceRef
    exclusions: list[str] = Field(max_length=100)
    max_bytes: int = Field(ge=1, le=1073741824)
    max_files: int = Field(ge=1, le=100000)
    retain_until: AwareDatetime

    _valid_exclusions = field_validator("exclusions")(validate_exclusions)


class SourceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str = Field(min_length=1, max_length=255)
    delegation_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    lease_fence: int = Field(ge=1)
    lease_owner: str | None = Field(default=None, min_length=1, max_length=255)


class CapturedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: SourceRef
    content: ArtifactRef
    file_manifest: ArtifactRef
    file_count: int = Field(ge=1, le=100000)
    exclusions: list[str] = Field(max_length=100)
    expires_at: AwareDatetime

    _valid_exclusions = field_validator("exclusions")(validate_exclusions)


class SourceSnapshot(CapturedSource):
    snapshot_id: str = Field(min_length=1, max_length=255)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stored_receipt(row: ProductSourceSnapshot) -> SourceSnapshot:
    receipt = SourceSnapshot.model_validate(row.receipt)
    if receipt.snapshot_id != row.id or receipt.source_ref.model_dump() != row.source_ref or receipt.expires_at != _aware(row.expires_at):
        raise HTTPException(409, detail={"code": "source_integrity_mismatch"})
    return receipt


async def store_snapshot(
    db: AsyncSession,
    grant: CredentialServiceDelegation,
    request: SourceSnapshotRequest,
    captured: CapturedSource,
    idempotency_key: str,
) -> tuple[SourceSnapshot, bool]:
    now = utcnow()
    source_ref = request.source_ref.model_dump()
    if (
        grant.id != request.delegation_id
        or grant.revision != request.delegation_revision
        or grant.revoked_at is not None
        or _aware(grant.expires_at) <= now
        or "source:pin" not in grant.operations
        or source_ref not in (grant.sources or [])
        or source_ref["tenant_id"] != grant.tenant_id
        or source_ref["project_id"] != grant.project_id
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    if not idempotency_key or len(idempotency_key) > 255:
        raise HTTPException(422, detail={"code": "unsupported_source_snapshot"})
    request_hash = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def existing_receipt():
        return await db.scalar(
            select(ProductSourceSnapshot)
            .where(
                ProductSourceSnapshot.tenant_id == grant.tenant_id,
                ProductSourceSnapshot.delegation_id == grant.id,
                ProductSourceSnapshot.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )

    previous = await existing_receipt()
    if previous is not None:
        if previous.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if _aware(previous.expires_at) <= now:
            raise HTTPException(410, detail={"code": "snapshot_expired"})
        return stored_receipt(previous), False
    if (
        request.retain_until <= now
        or request.retain_until > now + MAX_RETENTION
        or captured.expires_at < request.retain_until
        or captured.expires_at > now + MAX_RETENTION
        or captured.source_ref != request.source_ref
        or captured.exclusions != request.exclusions
        or captured.content.bytes > request.max_bytes
        or captured.file_count > request.max_files
        or captured.file_manifest.bytes > MAX_MANIFEST_BYTES
    ):
        raise HTTPException(422, detail={"code": "unsupported_source_snapshot"})
    receipt = SourceSnapshot(snapshot_id=new_uuid(), **captured.model_dump())
    row = ProductSourceSnapshot(
        tenant_id=grant.tenant_id,
        id=receipt.snapshot_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        source_ref=source_ref,
        receipt=receipt.model_dump(mode="json"),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        retain_until=request.retain_until,
        expires_at=receipt.expires_at,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        previous = await existing_receipt()
        if previous is None or previous.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
        if _aware(previous.expires_at) <= now:
            raise HTTPException(410, detail={"code": "snapshot_expired"}) from None
        return stored_receipt(previous), False
    return receipt, True
