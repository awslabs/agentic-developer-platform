"""Product storage configuration and AWS-enforced, short-lived S3 sessions.

Gateway is the trusted authorization broker. STS credentials never leave it;
workers and browsers receive exact-object, short-lived links. Terraform owns
roles and managed KMS keys; these APIs never create IAM identities or keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import select, text

from src.knowledge.source_policy.s3_source_guard import canonicalize_key
from src.products.models import Product

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
LINK_SECONDS = 300
FORMATS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
    ".adoc": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}$")
KEY_ARN = re.compile(r"^arn:(aws(?:-us-gov|-cn)?):kms:([a-z0-9-]+):(\d{12}):key/([a-zA-Z0-9-]+)$")


def unavailable(message="Product Context storage is not configured for this deployment."):
    return HTTPException(503, detail={"code": "context_storage_unavailable", "message": message})


def cloud(service):
    return boto3.client(
        service, region_name=os.environ.get("AWS_REGION", "us-east-1"), config=Config(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=15)
    )


def configured():
    return bool(os.environ.get("PRODUCT_CONTEXT_STORAGE_ROLE_ARN"))


def product_prefix(tenant, product):
    if not all(isinstance(value, str) and IDENTIFIER.fullmatch(value) for value in (tenant, product)):
        raise HTTPException(400, "Invalid Product storage scope")
    return f"tenants/{tenant}/products/{product}/"


def overlaps_uploads(tenant, product, key):
    """Original objects and folder imports covering them require a receipt."""
    key = canonicalize_key(key)
    uploads = product_prefix(tenant, product) + "uploads/"
    return key is None or key == uploads.rstrip("/") or key.startswith(uploads) or (key.endswith("/") and uploads.startswith(key))


def require_verified_import(source_ref, tenant, product, *, upload_id=None):
    if not configured() or not product or not source_ref.startswith("s3://"):
        return
    source = urlsplit(source_ref)
    if (
        source.netloc.lower() == os.environ.get("AGENT_CONTEXT_S3_BUCKET", "").lower()
        and overlaps_uploads(tenant, product, source.path)
        and not upload_id
    ):
        raise HTTPException(409, "Complete each document upload before importing it; upload folders cannot be imported directly")


def managed_alias(tenant, product):
    scope = hashlib.sha256(f"{tenant}/{product}".encode()).hexdigest()
    return f"alias/{os.environ.get('PRODUCT_CONTEXT_KMS_ALIAS_PREFIX', 'adp-context')}/{scope}"


@dataclass(frozen=True)
class Storage:
    tenant_id: str
    product_id: str
    bucket: str
    role_arn: str
    kms_key_arn: str
    read_key_arns: tuple[str, ...]
    encryption: str = "shared"
    revision: int = 0

    @property
    def prefix(self):
        return product_prefix(self.tenant_id, self.product_id)

    @property
    def region(self):
        return KEY_ARN.fullmatch(self.kms_key_arn)[2]

    @property
    def partition(self):
        return KEY_ARN.fullmatch(self.kms_key_arn)[1]


async def require_storage_schema(db):
    if not await db.scalar(text("SELECT to_regclass('product_context_storage') IS NOT NULL")):
        raise unavailable("Product Context storage requires the database update.")


async def load_storage(db, tenant, product, *, lock=False):
    product_prefix(tenant, product)
    role = os.environ.get("PRODUCT_CONTEXT_STORAGE_ROLE_ARN", "")
    bucket = os.environ.get("AGENT_CONTEXT_S3_BUCKET", "")
    default_key = os.environ.get("PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN", "")
    if not role or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or not KEY_ARN.fullmatch(default_key):
        raise unavailable()
    await require_storage_schema(db)
    if lock:
        # Also serializes the initially absent configuration and upload admission.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": f"context-storage:{tenant}/{product}"})
    row = (
        (
            await db.execute(
                text("SELECT * FROM product_context_storage WHERE tenant_id=:tenant AND product_id=:product"), {"tenant": tenant, "product": product}
            )
        )
        .mappings()
        .first()
    )
    key = row["kms_key_arn"] if row else default_key
    if not KEY_ARN.fullmatch(key) or KEY_ARN.fullmatch(key).groups()[:3] != KEY_ARN.fullmatch(default_key).groups()[:3]:
        raise unavailable("The configured encryption key is outside this storage account or region.")
    return Storage(
        tenant,
        product,
        bucket,
        role,
        key,
        tuple(set((row["read_key_arns"] if row else []) + [key, default_key])),
        row["encryption"] if row else "shared",
        row["revision"] if row else 0,
    )


async def require_service_product(db, tenant, product):
    current = await db.scalar(
        select(Product.id)
        .where(Product.id == product, Product.tenant_id == tenant, Product.archived_at.is_(None), Product.team_id.is_not(None))
        .with_for_update(read=True)
    )
    if current is None:
        raise HTTPException(409, "Product Context is no longer available for ingestion")


def validate_key(storage, key, *, managed=False):
    """Only an enabled, symmetric encryption key owned by this Product is selectable."""
    candidate = managed_alias(storage.tenant_id, storage.product_id) if managed else key
    if not managed and (
        not isinstance(key, str)
        or not KEY_ARN.fullmatch(key)
        or KEY_ARN.fullmatch(key).groups()[:3] != KEY_ARN.fullmatch(storage.kms_key_arn).groups()[:3]
    ):
        raise HTTPException(400, "Choose a KMS key ARN in the Context storage account and region")
    try:
        kms = cloud("kms")
        metadata = kms.describe_key(KeyId=candidate)["KeyMetadata"]
        arn = metadata["Arn"]
        if not KEY_ARN.fullmatch(arn) or KEY_ARN.fullmatch(arn).groups()[:3] != KEY_ARN.fullmatch(storage.kms_key_arn).groups()[:3]:
            raise ValueError()
        if (
            metadata.get("KeyState") != "Enabled"
            or metadata.get("KeyUsage") != "ENCRYPT_DECRYPT"
            or metadata.get("KeySpec") != "SYMMETRIC_DEFAULT"
            or metadata.get("KeyManager") != "CUSTOMER"
        ):
            raise ValueError()
        tags = {}
        marker = None
        for _ in range(10):
            page = kms.list_resource_tags(KeyId=arn, **({"Marker": marker} if marker else {}))
            tags.update({item["TagKey"]: item["TagValue"] for item in page.get("Tags", [])})
            if not page.get("Truncated"):
                break
            marker = page.get("NextMarker")
        if tags.get("adp:tenant_id") != storage.tenant_id or tags.get("adp:product_id") != storage.product_id:
            raise ValueError()
        return arn
    except (ClientError, BotoCoreError, KeyError, ValueError):
        raise HTTPException(
            409, "An enabled encryption key assigned to this Product is required. Ask your platform administrator to configure it."
        ) from None


def session_policy(storage, key, *, write=False, version=None, encryption_key=None, listing=False, checksum=False):
    if (
        not isinstance(key, str)
        or not key.startswith(storage.prefix)
        or len(key) > 1024
        or "\\" in key
        or any(ord(c) < 32 for c in key)
        or any(part in {".", ".."} for part in key.split("/"))
    ):
        raise HTTPException(403, "Object is outside this Product's storage")
    bucket = f"arn:{storage.partition}:s3:::{storage.bucket}"
    # Object keys are server-generated or persisted source references. Reject IAM
    # pattern metacharacters so an exact-object policy cannot become a wildcard.
    if any(c in key for c in "*?${}"):
        raise HTTPException(403, "Object key cannot be represented by a bounded storage grant")
    statement = {
        "Effect": "Allow",
        "Action": ["s3:PutObject"] if write else ["s3:GetObjectVersion"] if version else ["s3:GetObject"],
        "Resource": bucket + "/" + key,
    }
    if version:
        statement["Condition"] = {"StringEquals": {"s3:VersionId": version}}
    if write:
        statement["Condition"] = {
            "StringEquals": {
                "s3:x-amz-server-side-encryption": "aws:kms",
                "s3:x-amz-server-side-encryption-aws-kms-key-id": encryption_key or storage.kms_key_arn,
            }
        }
    statements = [statement]
    if listing:
        statements.append({"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": bucket, "Condition": {"StringEquals": {"s3:prefix": key}}})
    if encryption_key:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:GenerateDataKey"] if checksum else ["kms:GenerateDataKey"] if write else ["kms:Decrypt"],
                "Resource": encryption_key,
                "Condition": {
                    "StringEquals": {"kms:ViaService": f"s3.{storage.region}.amazonaws.com" + (".cn" if storage.partition == "aws-cn" else "")},
                    "StringLike": {"kms:EncryptionContext:aws:s3:arn": [bucket, bucket + "/" + key]},
                },
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def s3_session(storage, key, *, write=False, version=None, encryption_key=None, listing=False, checksum=False):
    policy = session_policy(storage, key, write=write, version=version, encryption_key=encryption_key, listing=listing, checksum=checksum)
    response = cloud("sts").assume_role(
        RoleArn=storage.role_arn,
        RoleSessionName="context-" + hashlib.sha256(f"{storage.tenant_id}/{storage.product_id}".encode()).hexdigest()[:32],
        DurationSeconds=900,
        Policy=json.dumps(policy, separators=(",", ":")),
        Tags=[{"Key": "adp_tenant_id", "Value": storage.tenant_id}, {"Key": "adp_product_id", "Value": storage.product_id}],
        SourceIdentity="knowledge-ingestion",
    )
    creds = response["Credentials"]
    if creds["Expiration"] <= datetime.now(UTC):
        raise unavailable("Product storage credentials expired. Retry the operation.")
    return boto3.client(
        "s3",
        region_name=storage.region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 2}, connect_timeout=5, read_timeout=20),
    )


def read_session(storage, key, *, version=None):
    params = {"Bucket": storage.bucket, "Key": key, **({"VersionId": version} if version else {})}
    s3 = s3_session(storage, key, version=version)
    # HEAD without checksum mode needs no KMS decryption permission. Determine
    # the actual key so older encrypted objects remain readable after rotation.
    head = s3.head_object(**params)
    kms_key = head.get("SSEKMSKeyId")
    if kms_key:
        if kms_key not in storage.read_key_arns:
            metadata = cloud("kms").describe_key(KeyId="alias/aws/s3")["KeyMetadata"]
            # Existing bucket-default aws/s3 objects predate product keys.
            if (
                metadata.get("KeyManager") != "AWS"
                or metadata.get("Arn") != kms_key
                or not KEY_ARN.fullmatch(kms_key)
                or KEY_ARN.fullmatch(kms_key).groups()[:3] != KEY_ARN.fullmatch(storage.kms_key_arn).groups()[:3]
            ):
                raise HTTPException(403, "This object's encryption key is not assigned to the Product")
        s3 = s3_session(storage, key, version=version, encryption_key=kms_key)
    return s3, head
