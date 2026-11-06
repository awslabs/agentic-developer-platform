"""Product Context storage configuration and browser/CLI document transfers."""

import asyncio
import base64
import json
import re
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.knowledge import product_storage as storage
from src.knowledge.product_access import require_product_access
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/agent-context/products/{product_id}/storage", tags=["product-context-storage"])


class EncryptionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    encryption: str = Field(pattern="^(shared|managed|customer)$")
    kms_key_arn: str | None = Field(None, max_length=256)


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    content_length: int = Field(gt=0, le=storage.MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")


async def native(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except (ClientError, BotoCoreError):
        # Native error bodies can contain deployment details. Do not return
        # credentials, signed links or provider exception text in failures.
        raise storage.unavailable("Product storage could not complete the request. Check its access configuration and retry.") from None


def can_configure(actor, access):
    return access.product.archived_at is None and (access.product.owner_user_id == access.member_id or actor.is_admin is True)


def describe(value, actor, access):
    return {
        "product_id": value.product_id,
        "bucket": value.bucket,
        "prefix": value.prefix,
        "location": f"s3://{value.bucket}/{value.prefix}",
        "revision": value.revision,
        "encryption": value.encryption,
        "kms_key_arn": value.kms_key_arn,
        "can_configure": can_configure(actor, access),
        "can_upload": access.product.archived_at is None,
        "max_upload_bytes": storage.MAX_UPLOAD_BYTES,
        "extensions": sorted(storage.FORMATS),
    }


@router.get("")
async def get_storage(
    product_id: str,
    response: Response,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    access = await require_product_access(gateway, actor, product_id)
    value = await storage.load_storage(db, actor.org_id, product_id)
    response.headers["Cache-Control"] = "no-store"
    return describe(value, actor, access)


@router.put("")
async def configure_storage(
    product_id: str,
    body: EncryptionSettings,
    response: Response,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    access = await require_product_access(gateway, actor, product_id, write=True, lock=True)
    if not can_configure(actor, access):
        raise HTTPException(403, "Only the Product owner or a platform administrator can configure storage")
    value = await storage.load_storage(db, actor.org_id, product_id, lock=True)
    if body.revision != value.revision:
        raise HTTPException(409, "Storage settings changed. Refresh before saving.")
    if body.encryption == "shared":
        if body.kms_key_arn:
            raise HTTPException(400, "Shared encryption does not accept a custom key")
        import os

        key = os.environ["PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN"]
    else:
        if body.encryption == "managed" and body.kms_key_arn:
            raise HTTPException(400, "Managed encryption selects the key assigned to this Product")
        key = await native(storage.validate_key, value, body.kms_key_arn, managed=body.encryption == "managed")
    keys = sorted(set(value.read_key_arns) | {key})
    await db.execute(
        text("""INSERT INTO product_context_storage
        (tenant_id,product_id,revision,encryption,kms_key_arn,read_key_arns,updated_by)
        VALUES (:tenant,:product,:revision,:mode,:key,CAST(:keys AS jsonb),:actor)
        ON CONFLICT (tenant_id,product_id) DO UPDATE SET revision=EXCLUDED.revision,
        encryption=EXCLUDED.encryption,kms_key_arn=EXCLUDED.kms_key_arn,
        read_key_arns=EXCLUDED.read_key_arns,updated_by=EXCLUDED.updated_by,updated_at=NOW()"""),
        {
            "tenant": actor.org_id,
            "product": product_id,
            "revision": value.revision + 1,
            "mode": body.encryption,
            "key": key,
            "keys": json.dumps(keys),
            "actor": access.member_id,
        },
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return describe(await storage.load_storage(db, actor.org_id, product_id), actor, access)


def upload_link(value, upload):
    checksum = base64.b64encode(bytes.fromhex(upload["sha256"])).decode()
    s3 = storage.s3_session(value, upload["object_key"], write=True, encryption_key=upload["kms_key_arn"])
    params = {
        "Bucket": value.bucket,
        "Key": upload["object_key"],
        "ContentLength": upload["content_length"],
        "ContentType": upload["content_type"],
        "ChecksumSHA256": checksum,
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": upload["kms_key_arn"],
        "BucketKeyEnabled": False,
    }
    return {
        "url": s3.generate_presigned_url("put_object", Params=params, ExpiresIn=storage.LINK_SECONDS),
        "headers": {
            "Content-Type": upload["content_type"],
            "x-amz-checksum-sha256": checksum,
            "x-amz-server-side-encryption": "aws:kms",
            "x-amz-server-side-encryption-aws-kms-key-id": upload["kms_key_arn"],
            "x-amz-server-side-encryption-bucket-key-enabled": "false",
        },
    }


@router.post("/uploads", status_code=201)
async def begin_upload(
    product_id: str,
    body: UploadRequest,
    response: Response,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.knowledge.routes import _require_scoped_imports

    _require_scoped_imports()
    access = await require_product_access(gateway, actor, product_id, write=True, lock=True)
    value = await storage.load_storage(db, actor.org_id, product_id, lock=True)
    filename = body.filename
    if any(ord(c) < 32 for c in filename) or any(c in filename for c in "/\\") or PurePosixPath(filename).suffix.lower() not in storage.FORMATS:
        raise HTTPException(400, "Choose a supported document filename without a directory path")
    # Pending transfers are bounded separately from the indexed-asset quota.
    pending = await db.scalar(
        text(
            "SELECT COUNT(*) FROM product_context_uploads WHERE tenant_id=:tenant AND product_id=:product AND asset_id IS NULL AND expires_at>NOW()"
        ),
        {"tenant": actor.org_id, "product": product_id},
    )
    if pending >= 20:
        raise HTTPException(429, "Finish existing document uploads before starting more")
    upload_id = str(uuid.uuid4())
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename).lstrip(".") or "document"
    upload = {
        "id": upload_id,
        "tenant_id": actor.org_id,
        "product_id": product_id,
        "bucket": value.bucket,
        "object_key": value.prefix + f"uploads/{upload_id}/{safe_name}",
        "filename": filename,
        "content_type": storage.FORMATS[PurePosixPath(filename).suffix.lower()],
        "content_length": body.content_length,
        "sha256": body.sha256,
        "kms_key_arn": value.kms_key_arn,
        "requested_by": access.member_id,
        "expires_at": utcnow() + timedelta(hours=24),
    }
    link = await native(upload_link, value, upload)
    await db.execute(
        text("""INSERT INTO product_context_uploads
        (id,tenant_id,product_id,bucket,object_key,filename,content_type,content_length,sha256,kms_key_arn,requested_by,expires_at)
        VALUES (:id,:tenant_id,:product_id,:bucket,:object_key,:filename,:content_type,:content_length,:sha256,
          :kms_key_arn,:requested_by,:expires_at)"""),
        upload,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {
        "product_id": product_id,
        "upload_id": upload_id,
        "source_ref": f"s3://{value.bucket}/{upload['object_key']}",
        "expires_in": storage.LINK_SECONDS,
        **link,
    }


async def upload_record(db, tenant, product, upload_id, access, *, lock=True):
    try:
        uuid.UUID(str(upload_id))
    except ValueError:
        raise HTTPException(404, "Upload not found") from None
    query = "SELECT * FROM product_context_uploads WHERE id=:id AND tenant_id=:tenant AND product_id=:product" + (" FOR UPDATE" if lock else "")
    row = (await db.execute(text(query), {"id": upload_id, "tenant": tenant, "product": product})).mappings().first()
    if row is None:
        raise HTTPException(404, "Upload not found")
    if access.member_id not in {row["requested_by"], access.product.owner_user_id}:
        raise HTTPException(403, "Only the contributor or Product owner can complete this upload")
    return row


def verify_upload(value, row):
    s3 = storage.s3_session(value, row["object_key"], encryption_key=row["kms_key_arn"], checksum=True)
    info = s3.head_object(Bucket=row["bucket"], Key=row["object_key"], ChecksumMode="ENABLED")
    expected = base64.b64encode(bytes.fromhex(row["sha256"])).decode()
    if (
        info.get("ContentLength") != row["content_length"]
        or info.get("ChecksumSHA256") != expected
        or info.get("SSEKMSKeyId") != row["kms_key_arn"]
        or info.get("ServerSideEncryption") != "aws:kms"
        or info.get("BucketKeyEnabled", False) is not False
    ):
        raise HTTPException(409, "The uploaded file does not match its approved content and encryption settings")
    version = info.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise storage.unavailable("Product storage requires S3 object versioning before uploads can be indexed.")
    return version


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(
    product_id: str,
    upload_id: str,
    response: Response,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.knowledge.routes import _can_manage, _fetch_asset_by_id, _row_to_response, register_asset
    from src.knowledge.schemas import AssetCreateRequest

    access = await require_product_access(gateway, actor, product_id, write=True, lock=True)
    value = await storage.load_storage(db, actor.org_id, product_id)
    row = await upload_record(db, actor.org_id, product_id, upload_id, access)
    response.headers["Cache-Control"] = "no-store"
    if row["asset_id"]:
        asset = await _fetch_asset_by_id(db, str(row["asset_id"]))
        if asset is None:
            raise HTTPException(410, "This upload's Context entry was removed")
        return _row_to_response(asset, can_manage=_can_manage(asset, actor, access))
    if row["expires_at"] <= utcnow():
        raise HTTPException(410, "This upload expired. Start a new upload.")
    version = await native(verify_upload, value, row)
    await db.execute(text("UPDATE product_context_uploads SET source_version_id=:version WHERE id=:id"), {"version": version, "id": upload_id})
    return await register_asset(
        AssetCreateRequest(
            asset_type="doc",
            source_ref=f"s3://{row['bucket']}/{row['object_key']}",
            display_name=row["filename"],
            scope="product",
            product_id=product_id,
            source_upload_id=upload_id,
        ),
        db,
        gateway,
        actor,
    )


@router.post("/assets/{asset_id}/download")
async def download_original(
    product_id: str,
    asset_id: str,
    response: Response,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    await require_product_access(gateway, actor, product_id, lock=True)
    value = await storage.load_storage(db, actor.org_id, product_id)
    row = (
        (
            await db.execute(
                text("""SELECT u.* FROM product_context_uploads u JOIN knowledge_assets a ON a.id=u.asset_id
        WHERE a.id::text=:asset AND a.tenant_id=:tenant AND a.product_id=:product AND a.status!='removed'
          AND u.tenant_id=a.tenant_id AND u.product_id=a.product_id AND a.source_upload_id=u.id
        FOR SHARE OF a,u"""),
                {"asset": asset_id, "tenant": actor.org_id, "product": product_id},
            )
        )
        .mappings()
        .first()
    )
    if not row or not row["source_version_id"]:
        raise HTTPException(404, "Original document not found")

    def link():
        s3, _ = storage.read_session(value, row["object_key"], version=row["source_version_id"])
        return s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": row["bucket"],
                "Key": row["object_key"],
                "VersionId": row["source_version_id"],
                "ResponseContentDisposition": "attachment; filename*=UTF-8''" + quote(row["filename"], safe=""),
            },
            ExpiresIn=storage.LINK_SECONDS,
        )

    response.headers["Cache-Control"] = "no-store"
    return {"product_id": product_id, "asset_id": asset_id, "expires_in": storage.LINK_SECONDS, "url": await native(link)}
