"""Read immutable captured inputs using current Product run visibility."""

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import HTTPException, Response

from src.products.models import ProductSourceSnapshot
from src.products.source_capture import MAX_RANGE_BYTES, get_snapshot_objects
from src.products.source_capture_metadata import MAX_CONTENT
from src.products.source_snapshots import _aware, stored_receipt


def denied():
    return HTTPException(404, detail={"code": "not_found"})


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def manifest_identity(manifest):
    """Hash the full immutable representation without building a large response."""
    digest, size, parts = hashlib.sha256(), 0, []
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    for chunk in encoder.iterencode(manifest):
        encoded = chunk.encode("utf-8")
        digest.update(encoded)
        size += len(encoded)
        if size <= MAX_CONTENT:
            parts.append(encoded)
        else:
            parts.clear()
    return digest.hexdigest(), size, b"".join(parts) if size <= MAX_CONTENT else None


def manifest_page(manifest, digest, limit, cursor):
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HTTPException(409, detail={"code": "source_integrity_mismatch"})
    offset = 0
    if cursor is not None:
        match = re.fullmatch(r"([a-f0-9]{64})\.([0-9]{1,12})", cursor)
        if not match:
            raise HTTPException(422, detail={"code": "invalid_source_manifest_cursor"})
        if match[1] != digest:
            raise HTTPException(409, detail={"code": "source_manifest_changed"})
        offset = int(match[2])
        if offset >= len(files) and (offset != 0 or files):
            raise HTTPException(422, detail={"code": "invalid_source_manifest_cursor"})
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HTTPException(422, detail={"code": "invalid_source_manifest_limit"})
    result = {
        "manifest": {**manifest, "files": []},
        "manifest_sha256": digest,
        "file_count": len(files),
        "offset": offset,
        "next_cursor": None,
    }
    # Reserve space for the longest continuation cursor before adding entries.
    size = len(json_bytes(result)) + 128
    if size > MAX_RANGE_BYTES:
        raise HTTPException(413, detail={"code": "source_manifest_metadata_too_large", "max_page_bytes": MAX_RANGE_BYTES})
    selected = result["manifest"]["files"]
    for entry in files[offset : offset + limit]:
        addition = len(json_bytes(entry)) + bool(selected)
        if size + addition > MAX_RANGE_BYTES:
            break
        selected.append(entry)
        size += addition
    if not selected and offset < len(files):
        raise HTTPException(413, detail={"code": "source_manifest_entry_too_large", "max_page_bytes": MAX_RANGE_BYTES})
    next_offset = offset + len(selected)
    if next_offset < len(files):
        result["next_cursor"] = f"{digest}.{next_offset}"
    return json_bytes(result)


async def selected_manifest(backend, product_id, project_id, service_id, run_id):
    # This is the same authorization used to view the run. A completed worker
    # lease is neither revived nor used as a human's source-read credential.
    if backend.actor.account_type != "human":
        raise denied()
    # This GET has no pending writes. Discard cached authorization rows so the
    # second read observes revocation committed while object storage was read.
    backend.db.expire_all()
    run = await backend.get_run(product_id, project_id, service_id, run_id)
    manifest = deepcopy(run.manifest)
    scope = {"tenant_id": backend.actor.org_id, "product_id": product_id, "project_id": project_id}
    sources = run.input.get("sources")
    if run.service_id == "devops" and run.input.get("capability") == "release_readiness":
        sources = [run.input.get("devops_input", {}).get("repository_ref")]
    if (
        (run.tenant_id, run.product_id, run.project_id, run.service_id, run.run_id)
        != (scope["tenant_id"], product_id, project_id, service_id, run_id)
        or not isinstance(manifest, dict)
        or manifest.get("run_id") != run_id
        or not isinstance(manifest.get("scope"), dict)
        or any(manifest["scope"].get(key) != value for key, value in scope.items())
        or not isinstance(sources, list)
        or not sources
        or manifest.get("sources") != sources
        or not isinstance(manifest.get("source_snapshot_ids"), list)
        or len(manifest["source_snapshot_ids"]) != len(sources)
        or any(not isinstance(value, str) or not value for value in manifest["source_snapshot_ids"])
        or len(set(manifest["source_snapshot_ids"])) != len(sources)
    ):
        raise denied()
    return run, manifest


async def manifest_response(backend, product_id, project_id, service_id, run_id, *, limit=None, cursor=None):
    _, manifest = await selected_manifest(backend, product_id, project_id, service_id, run_id)
    digest, size, inline = await asyncio.to_thread(manifest_identity, manifest)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Manifest-Sha256": digest}
    data = inline
    if limit is not None or cursor is not None:
        data = await asyncio.to_thread(manifest_page, manifest, digest, limit if limit is not None else 1000, cursor)
    _, current = await selected_manifest(backend, product_id, project_id, service_id, run_id)
    if current != manifest:
        raise HTTPException(409, detail={"code": "source_integrity_mismatch"}, headers=headers)
    if data is None:
        root = "/v1/products/{}/projects/{}/services/{}/runs/{}/source-manifest".format(
            *(quote(value, safe="") for value in (product_id, project_id, service_id, run_id))
        )
        raise HTTPException(
            413,
            detail={
                "code": "source_manifest_pagination_required",
                "message": (
                    "This source manifest requires pagination. Read every page using next_cursor until it is null; "
                    "verify manifest_sha256 before using the complete inventory."
                ),
                "manifest_sha256": digest,
                "manifest_bytes": size,
                "file_count": len(manifest.get("files", [])),
                "source_snapshot_ids": manifest["source_snapshot_ids"],
                "next_page": f"{root}?limit=1000&cursor={digest}.0",
                "max_page_bytes": MAX_RANGE_BYTES,
            },
            headers=headers,
        )
    return Response(content=data, media_type="application/json", headers=headers)


async def selected_snapshot(backend, product_id, project_id, service_id, run_id, snapshot_id):
    run, manifest = await selected_manifest(backend, product_id, project_id, service_id, run_id)
    ids = manifest["source_snapshot_ids"]
    if snapshot_id not in ids:
        raise denied()
    row = await backend.db.get(ProductSourceSnapshot, (backend.actor.org_id, snapshot_id), populate_existing=True)
    admission = run.admission or {}
    if (
        row is None
        or (row.tenant_id, row.product_id, row.project_id) != (backend.actor.org_id, product_id, project_id)
        or row.delegation_id != admission.get("delegation_id")
        or row.source_ref != manifest["sources"][ids.index(snapshot_id)]
    ):
        raise denied()
    receipt = stored_receipt(row)
    if _aware(row.expires_at) <= datetime.now(UTC):
        raise HTTPException(410, detail={"code": "snapshot_expired"})
    return manifest, receipt


async def snapshot_response(backend, product_id, project_id, service_id, run_id, snapshot_id, part, *, range_header=None):
    arguments = (backend, product_id, project_id, service_id, run_id, snapshot_id)
    manifest, receipt = await selected_snapshot(*arguments)
    if part == "receipt":
        return receipt.model_dump(mode="json")
    if part not in {"content", "manifest"}:
        raise denied()
    artifact = receipt.content if part == "content" else receipt.file_manifest
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "ETag": f'"{artifact.sha256}"',
        "Accept-Ranges": "bytes",
        "X-Artifact-Size": str(artifact.bytes),
        "X-Artifact-Sha256": artifact.sha256,
        "X-Artifact-Version": artifact.version,
    }
    status = 200
    if range_header is not None:
        match = re.fullmatch(r"bytes=([0-9]{1,12})-([0-9]{1,12})", range_header)
        start, end = map(int, match.groups()) if match else (-1, -1)
        if start < 0 or start > end or end >= artifact.bytes or end - start + 1 > MAX_RANGE_BYTES:
            raise HTTPException(
                416,
                detail={
                    "code": "invalid_source_range",
                    "message": "Send one explicit Range: bytes=start-end of at most 1048576 bytes within the retained artifact size.",
                    "artifact_bytes": artifact.bytes,
                    "max_range_bytes": MAX_RANGE_BYTES,
                },
                headers={**headers, "Content-Range": f"bytes */{artifact.bytes}"},
            )
        # Range reads pin the stored version and validate storage length/range
        # metadata. Clients verify the full SHA-256 after assembling all ranges.
        data = await asyncio.to_thread(get_snapshot_objects().read_range, artifact, start, end)
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact.bytes}"
    else:
        if artifact.bytes > MAX_CONTENT:
            raise HTTPException(
                416,
                detail={
                    "code": "source_range_required",
                    "message": (
                        "This retained artifact requires bounded reads. Send Range: bytes=0-1048575, "
                        "then request consecutive ranges of at most 1048576 bytes and verify the receipt SHA-256."
                    ),
                    "artifact_bytes": artifact.bytes,
                    "max_range_bytes": MAX_RANGE_BYTES,
                },
                headers={**headers, "Content-Range": f"bytes */{artifact.bytes}"},
            )
        # Small whole reads retain their existing byte count and digest check.
        data = await asyncio.to_thread(get_snapshot_objects().get, artifact)
    current_manifest, current_receipt = await selected_snapshot(*arguments)
    if current_manifest != manifest or current_receipt != receipt:
        raise HTTPException(409, detail={"code": "source_integrity_mismatch"})
    return Response(
        content=data,
        status_code=status,
        media_type=artifact.media_type,
        headers=headers,
    )
