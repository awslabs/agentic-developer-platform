"""Object and vector capabilities issued only by the trusted ingestion gateway."""

from __future__ import annotations

import hashlib
import math
import os
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.knowledge.ingestion_authority import IngestionAuthority, deny

MAX_OBJECT_BYTES = 512 * 1024 * 1024


def client(service):
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-east-1"))


def bucket_name():
    value = os.environ.get("AGENT_CONTEXT_S3_BUCKET", "")
    if not value:
        raise HTTPException(503, "Ingestion storage is not configured")
    return value


def object_command(authority: IngestionAuthority, command: str, args: dict, *, storage=None):
    bucket, key = args.get("Bucket"), args.get("Key")
    if command == "list":
        key = args.get("Prefix")
    source = urlsplit(authority.source_ref)
    source_key = source.path.lstrip("/")
    input_read = (
        authority.asset_type == "doc"
        and source.scheme == "s3"
        and bucket == source.netloc
        and isinstance(key, str)
        and (key == source_key or (source_key.endswith("/") and key.startswith(source_key)))
        and command in {"get", "head", "list"}
    )
    if not input_read and (bucket != bucket_name() or not authority.permits_object(key)):
        raise deny()
    from src.knowledge.product_storage import configured, overlaps_uploads, read_session, s3_session
    from src.knowledge.source_policy.s3_source_guard import canonicalize_key

    version = authority.source_object_version if input_read and key == source_key else None
    params = {"Bucket": bucket, "Key": key, **({"VersionId": version} if version else {})}
    scoped = storage is not None and bucket == storage.bucket
    head = None
    if input_read and storage is None and configured() and bucket == bucket_name():
        parts = (canonicalize_key(key) or "").rstrip("/").split("/")
        if parts[0] == "tenants" and ((len(parts) > 2 and parts[2] == "products") or (command == "list" and len(parts) <= 2)):
            # Legacy tenant-folder grants cannot fall back to ambient Gateway
            # credentials to read or list Product-owned objects.
            raise deny()
    if scoped and input_read and overlaps_uploads(storage.tenant_id, storage.product_id, key) and not version:
        # A previously registered folder or bulk source cannot bypass the
        # original-object receipt and its pinned version at worker read time.
        raise deny()
    if scoped:
        if command == "put":
            s3 = s3_session(storage, key, write=True, encryption_key=storage.kms_key_arn)
        elif command == "list":
            s3 = s3_session(storage, key, listing=True)
        else:
            s3, head = read_session(storage, key, version=version)
    else:
        # Explicitly admitted external sources remain external inputs. They do
        # not authorize writes or access to another Product's managed storage.
        s3 = client("s3")
    if command == "list":
        if not input_read or not source_key.endswith("/"):
            raise deny()
        result = s3.list_objects_v2(
            Bucket=bucket, Prefix=key, MaxKeys=100, **({"ContinuationToken": args["ContinuationToken"]} if args.get("ContinuationToken") else {})
        )
        return {
            "Contents": [{"Key": o["Key"], "Size": o["Size"]} for o in result.get("Contents", [])],
            "NextContinuationToken": result.get("NextContinuationToken"),
            "IsTruncated": result.get("IsTruncated", False),
        }
    if command == "head":
        result = head or s3.head_object(**params)
        return {k: result[k] for k in ("ContentLength", "ContentType", "ETag") if k in result}
    if command == "get":
        return {"url": s3.generate_presigned_url("get_object", Params=params, ExpiresIn=300)}
    if command == "put":
        length = args.get("ContentLength")
        if type(length) is not int or not 0 <= length <= MAX_OBJECT_BYTES:
            raise deny()
        content_type = args.get("ContentType", "application/octet-stream")
        if content_type not in {"application/json", "application/octet-stream", "text/plain", "text/plain; charset=utf-8", "text/markdown"}:
            raise deny()
        # S3 enforces the exact key and bounded length, including if the worker
        # bypasses its client. No write-capable AWS credential leaves the gateway.
        fields = {"Content-Type": content_type}
        if scoped:
            fields.update({"x-amz-server-side-encryption": "aws:kms", "x-amz-server-side-encryption-aws-kms-key-id": storage.kms_key_arn})
        return s3.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields=fields,
            Conditions=[{name: value} for name, value in fields.items()] + [["content-length-range", length, length]],
            ExpiresIn=300,
        )
    raise deny()


def vector_index(authority: IngestionAuthority) -> str:
    if authority.owner_sub:
        return f"personal-{authority.owner_sub}"
    if authority.tenant_id:
        return f"tenant-{authority.tenant_id}"
    shards = int(os.environ.get("S3_VECTORS_SHARD_COUNT", "4"))
    shard = int.from_bytes(hashlib.sha256(authority.source.split("/")[0].encode()).digest()[:4], "big") % shards
    return f"code-shard-{shard}"


def vector_command(authority: IngestionAuthority, command: str, args: dict):
    bucket = os.environ.get("AGENT_CONTEXT_VECTORS_BUCKET", "")
    if not bucket:
        raise HTTPException(503, "Ingestion vector storage is not configured")
    index = vector_index(authority)
    if args.get("index_name") != index:
        raise deny()
    vectors = client("s3vectors")
    base = {"vectorBucketName": bucket, "indexName": index}
    prefix = f"{authority.asset_id}:{authority.attempt_id}:"
    binding = authority.vector_metadata()
    if command == "put":
        batch = args.get("vectors")
        if not isinstance(batch, list) or not 1 <= len(batch) <= 500:
            raise deny()
        output = []
        for value in batch:
            key, embedding, metadata = value.get("key"), value.get("embedding"), value.get("metadata", {})
            if not isinstance(key, str) or not key.startswith(prefix) or len(key) > 1024:
                raise deny()
            if (
                not isinstance(embedding, list)
                or len(embedding) != 1024
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in embedding)
            ):
                raise deny()
            if not isinstance(metadata, dict):
                raise deny()
            # Ownership/source fields are authoritative even if the client lies.
            descriptive = {
                k: metadata[k]
                for k in ("source_type", "file_path", "chunk_idx", "chunk_text", "section", "section_heading", "title", "wiki_s3_key")
                if k in metadata
            }
            if "wiki_s3_key" in descriptive and not authority.permits_object(descriptive["wiki_s3_key"]):
                raise deny()
            if authority.asset_type != "repo":
                descriptive["source_ref"] = authority.source_ref
            output.append({"key": key, "data": {"float32": embedding}, "metadata": {**descriptive, **binding}})
        try:
            vectors.create_index(**base, dimension=1024, distanceMetric="cosine", dataType="float32")
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"ConflictException", "ResourceAlreadyExistsException"}:
                raise
        vectors.put_vectors(**base, vectors=output)
        return None
    if command == "count":
        query = args.get("query_vector")
        if not isinstance(query, list) or len(query) != 1024 or any(type(n) not in (float, int) or not math.isfinite(n) for n in query):
            raise deny()
        result = vectors.query_vectors(
            **base,
            queryVector={"float32": query},
            topK=1,
            filter={"$and": [{k: {"$eq": v}} for k, v in binding.items()]},
            returnMetadata=False,
            returnDistance=False,
        )
        # Read-back needs a count, never another document or raw vector.
        return len(result.get("vectors", []))
    if command in {"clear", "prepare"}:
        # Retry output is namespaced by the current attempt. No old-generation
        # or other-asset records may be deleted by clear. Prepare removes only
        # previous generations belonging to THIS asset, preserving this attempt.
        token = None
        keys = []
        while True:
            try:
                page = vectors.list_vectors(**base, returnData=False, returnMetadata=False, **({"nextToken": token} if token else {}))
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NotFoundException", "ResourceNotFoundException"}:
                    return None
                raise
            for value in page.get("vectors", []):
                key = value.get("key", "")
                if (command == "clear" and key.startswith(prefix)) or (
                    command == "prepare" and key.startswith(authority.asset_id + ":") and not key.startswith(prefix)
                ):
                    keys.append(key)
            token = page.get("nextToken")
            if not token:
                break
        for offset in range(0, len(keys), 500):
            vectors.delete_vectors(**base, keys=keys[offset : offset + 500])
        return None
    raise deny()
