"""Bounded, immutable Product source captures with connector-owned credentials."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

import boto3
import httpx
from botocore.config import Config
from fastapi import HTTPException

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.products.github_documents import document_location, read_document_bytes
from src.products.l5_branch_sources import repository_owned_by_mapping
from src.products.source_capture_metadata import (
    MAX_ARCHIVE,
    MAX_CONTENT,
    MAX_FILES,
    MAX_TREE_BYTES,
    REPOSITORY_MAX_BYTES,
    excluded_path,
    selected_repository_metadata,
    validate_exclusions,
)
from src.products.source_snapshots import MAX_MANIFEST_BYTES, ArtifactRef, CapturedSource, SourceSnapshotRequest
from src.shared.models.base import new_uuid


def refused(code: str, status: int = 422) -> HTTPException:
    return HTTPException(status, detail={"code": code})


MAX_RANGE_BYTES = 1024 * 1024


class SnapshotObjects:
    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def put(self, content: bytes, media_type: str) -> ArtifactRef:
        key = f"products/source-snapshots/{new_uuid()}"
        try:
            result = self.s3.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=media_type, IfNoneMatch="*")
        except Exception as exc:
            raise refused("connector_unavailable", 503) from exc
        version = result.get("VersionId")
        if not version or version == "null":
            raise refused("connector_unavailable", 503)
        return ArtifactRef(artifact_id=key, version=version, sha256=hashlib.sha256(content).hexdigest(), media_type=media_type, bytes=len(content))

    def get(self, artifact: ArtifactRef) -> bytes:
        if not artifact.artifact_id.startswith("products/source-snapshots/") or artifact.bytes > MAX_CONTENT:
            raise refused("source_integrity_mismatch", 409)
        try:
            result = self.s3.get_object(Bucket=self.bucket, Key=artifact.artifact_id, VersionId=artifact.version)
            stream = result["Body"]
            try:
                data = stream.read(artifact.bytes + 1)
            finally:
                stream.close()
        except Exception as exc:
            raise refused("connector_unavailable", 503) from exc
        if len(data) != artifact.bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise refused("source_integrity_mismatch", 409)
        return data

    def put_file(self, path: Path, media_type: str) -> ArtifactRef:
        """Upload a private seekable file without materializing it in memory."""
        limit = {"application/json": MAX_MANIFEST_BYTES, "application/zip": REPOSITORY_MAX_BYTES}.get(media_type, MAX_CONTENT)
        key = f"products/source-snapshots/{new_uuid()}"
        digest, size = hashlib.sha256(), 0
        try:
            with Path(path).open("rb") as content:
                if not stat.S_ISREG(os.fstat(content.fileno()).st_mode):
                    raise refused("unsupported_source_snapshot")
                while chunk := content.read(MAX_RANGE_BYTES):
                    size += len(chunk)
                    if size > limit:
                        raise refused("unsupported_source_snapshot")
                    digest.update(chunk)
                content.seek(0)
                result = self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentLength=size,
                    ContentType=media_type,
                    ChecksumSHA256=base64.b64encode(digest.digest()).decode("ascii"),
                    Metadata={"sha256": digest.hexdigest()},
                    IfNoneMatch="*",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise refused("connector_unavailable", 503) from exc
        version = result.get("VersionId")
        if not version or version == "null":
            raise refused("connector_unavailable", 503)
        return ArtifactRef(artifact_id=key, version=version, sha256=digest.hexdigest(), media_type=media_type, bytes=size)

    def read_range(self, artifact: ArtifactRef, start: int, end: int) -> bytes:
        """Read at most 1 MiB of the exact retained object version."""
        if not artifact.artifact_id.startswith("products/source-snapshots/") or artifact.version == "null" or artifact.bytes > REPOSITORY_MAX_BYTES:
            raise refused("source_integrity_mismatch", 409)
        if type(start) is not int or type(end) is not int or start < 0 or end < start or end >= artifact.bytes or end - start + 1 > MAX_RANGE_BYTES:
            raise refused("invalid_source_range", 416)
        length = end - start + 1
        try:
            result = self.s3.get_object(Bucket=self.bucket, Key=artifact.artifact_id, VersionId=artifact.version, Range=f"bytes={start}-{end}")
            stream = result["Body"]
            try:
                if (
                    result.get("VersionId") != artifact.version
                    or result.get("ContentLength") != length
                    or result.get("ContentRange") != f"bytes {start}-{end}/{artifact.bytes}"
                    or result.get("Metadata", {}).get("sha256", artifact.sha256) != artifact.sha256
                ):
                    raise refused("source_integrity_mismatch", 409)
                data = stream.read(length)
                if stream.read(1):
                    raise refused("source_integrity_mismatch", 409)
            finally:
                stream.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise refused("connector_unavailable", 503) from exc
        if len(data) != length:
            raise refused("source_integrity_mismatch", 409)
        return data


def get_snapshot_objects() -> SnapshotObjects:
    bucket = os.environ.get("PRODUCT_SOURCE_SNAPSHOT_BUCKET", "")
    if not bucket:
        raise refused("connector_unavailable", 503)
    return SnapshotObjects(boto3.client("s3", config=Config(signature_version="s3v4")), bucket)


def canonical_repository(archive: bytes, request: SourceSnapshotRequest) -> tuple[bytes, bytes, int]:
    validate_repository_exclusions(request.exclusions)
    files = {}
    root = None
    byte_limit = min(request.max_bytes, MAX_CONTENT)
    expanded_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            for entry in source.infolist():
                if entry.is_dir():
                    continue
                parts = entry.filename.split("/")
                path = "/".join(parts[1:])
                if root is None:
                    root = parts[0]
                if (
                    len(parts) < 2
                    or root in {"", ".", ".."}
                    or parts[0] != root
                    or not path
                    or len(path) > 1024
                    or ":" in parts[1]
                    or any(part in {"", ".", ".."} for part in parts)
                    or "\\" in entry.filename
                    or path.startswith("/")
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                    or path in files
                ):
                    raise refused("unsupported_source_snapshot")
                if excluded_repository_path(path, request.exclusions):
                    continue
                if len(files) >= min(request.max_files, MAX_FILES) or entry.file_size > byte_limit - expanded_bytes:
                    raise refused("unsupported_source_snapshot")
                with source.open(entry) as item:
                    data = item.read(byte_limit - expanded_bytes + 1)
                if len(data) != entry.file_size or len(data) > byte_limit - expanded_bytes:
                    raise refused("unsupported_source_snapshot")
                expanded_bytes += len(data)
                files[path] = (data, bool((entry.external_attr >> 16) & 0o111))
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError) as exc:
        raise refused("unsupported_source_snapshot") from exc
    if not files:
        raise refused("unsupported_source_snapshot")
    output = io.BytesIO()
    entries = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as target:
        for path, (data, executable) in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            target.writestr(info, data)
            entries.append(
                {
                    "path": path,
                    "source_ref": request.source_ref.model_dump(),
                    "source_revision": request.source_ref.record_revision,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    content = output.getvalue()
    if len(content) > MAX_CONTENT:
        raise refused("unsupported_source_snapshot")
    manifest = json.dumps(
        {"source_ref": request.source_ref.model_dump(), "exclusions": request.exclusions, "files": entries},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return content, manifest, len(entries)


def validate_repository_exclusions(exclusions):
    try:
        validate_exclusions(exclusions)
    except ValueError as exc:
        raise refused("unsupported_source_snapshot") from exc


def excluded_repository_path(path, exclusions):
    return excluded_path(path, exclusions)


async def github_selected_tree(client, name, headers, commit, request):
    """Read selected immutable blobs without downloading a large repository archive."""
    validate_repository_exclusions(request.exclusions)
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if not isinstance(tree_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", tree_sha):
        raise refused("immutable_source_required")
    response = await client._http_client.get(f"/repos/{name}/git/trees/{tree_sha}", params={"recursive": "1"}, headers=headers)
    response.raise_for_status()
    if len(response.content) > MAX_TREE_BYTES:
        raise refused("unsupported_source_snapshot")
    try:
        selected, _ = selected_repository_metadata(response.json(), tree_sha, request.exclusions)
    except ValueError as exc:
        raise refused("unsupported_source_snapshot") from exc
    if (
        not selected
        or sum(item["size"] for item in selected) > min(request.max_bytes, MAX_CONTENT)
        or len(selected) > min(request.max_files, MAX_FILES)
    ):
        raise refused("unsupported_source_snapshot")
    concurrency = asyncio.Semaphore(4)

    async def fetch(item):
        async with concurrency:
            result = await client._http_client.get(f"/repos/{name}/git/blobs/{item['sha']}", headers=headers)
            result.raise_for_status()
            if len(result.content) > item["size"] * 2 + 4096:
                raise refused("unsupported_source_snapshot")
            blob = result.json()
            if (
                not isinstance(blob, dict)
                or blob.get("sha") != item["sha"]
                or blob.get("size") != item["size"]
                or blob.get("encoding") != "base64"
                or not isinstance(blob.get("content"), str)
            ):
                raise refused("source_integrity_mismatch", 409)
            data = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
            # Git's object ID is SHA-1; the captured manifest separately pins SHA-256.
            if (
                len(data) != item["size"]
                or hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest() != item["sha"]
            ):
                raise refused("source_integrity_mismatch", 409)
            return item, data

    files = await asyncio.gather(*(fetch(item) for item in selected), return_exceptions=True)
    for result in files:
        if isinstance(result, BaseException):
            raise result
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        for item, data in files:
            info = zipfile.ZipInfo("selected/" + item["path"])
            info.external_attr = int(item["mode"], 8) << 16
            output.writestr(info, data)
    return canonical_repository(archive.getvalue(), request)


async def github_capture(binding, mapping, request: SourceSnapshotRequest) -> tuple[bytes, bytes, int]:
    revision = request.source_ref.record_revision
    if (
        binding.kind != "repository"
        or binding.provider != "github"
        or binding.installation_id != mapping.id
        or mapping.org_id != binding.tenant_id
        or mapping.provider != "github"
        or mapping.ownership_disputed
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
        or github_account_id(mapping.installation_id) is None
        or github_account_id(binding.native_id) is None
    ):
        raise refused("immutable_source_required")
    from src.admin.connections.service import _get_github_app_credentials

    try:
        app_id, private_key = _get_github_app_credentials()
    except Exception as exc:
        raise refused("connector_unavailable", 503) from exc
    if not app_id or not private_key:
        raise refused("connector_unavailable", 503)
    client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(25):
            installation_id = int(mapping.installation_id)
            repository = await client.get_installation_repository(installation_id, int(binding.native_id))
            name = repository.get("full_name")
            if (
                not repository_owned_by_mapping(repository, binding, mapping)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            ):
                raise refused("not_found", 404)
            token = await client.get_installation_token(installation_id)
            if not token:
                raise refused("connector_unavailable", 503)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            commit = await client._http_client.get(f"/repos/{name}/commits/{revision}", headers=headers)
            commit.raise_for_status()
            if commit.json().get("sha") != revision:
                raise refused("immutable_source_required")
            if request.exclusions:
                return await github_selected_tree(client, name, headers, commit.json(), request)
            async with client._http_client.stream("GET", f"/repos/{name}/zipball/{revision}", headers=headers) as response:
                if response.status_code not in (301, 302, 307) or not response.headers.get("location"):
                    raise refused("connector_unavailable", 503)
                location = httpx.URL(response.headers["location"])
            if location.scheme != "https" or location.host != "codeload.github.com":
                raise refused("connector_unavailable", 503)
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=25) as fetch:
                async with fetch.stream("GET", str(location)) as archive_response:
                    archive_response.raise_for_status()
                    archive = bytearray()
                    async for chunk in archive_response.aiter_bytes():
                        archive.extend(chunk)
                        if len(archive) > MAX_ARCHIVE:
                            raise refused("unsupported_source_snapshot")
            return canonical_repository(bytes(archive), request)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 404):
            raise refused("not_found", 404) from exc
        raise refused("connector_unavailable", 503) from exc
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        raise refused("connector_unavailable", 503) from exc
    finally:
        await client.aclose()


async def github_document_capture(binding, mapping, request: SourceSnapshotRequest) -> tuple[bytes, bytes, str]:
    reference = request.source_ref
    if (
        reference.record_type != "document"
        or reference.record_id != binding.native_id
        or reference.tenant_id != binding.tenant_id
        or reference.project_id != binding.project_id
        or reference.source_connection_id != binding.installation_id
        or not re.fullmatch(r"[a-f0-9]{40}", reference.record_revision)
    ):
        raise refused("immutable_source_required")
    if request.exclusions:
        raise refused("unsupported_source_snapshot")
    try:
        _, path = document_location(binding)
        content, revision = await read_document_bytes(binding, mapping)
    except HTTPException:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        raise refused("connector_unavailable", 503) from exc
    except ValueError as exc:
        raise refused("unsupported_source_snapshot") from exc
    if revision != reference.record_revision:
        raise refused("source_integrity_mismatch", 409)
    if not content or len(content) > min(request.max_bytes, MAX_CONTENT):
        raise refused("unsupported_source_snapshot")
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise refused("unsupported_source_snapshot")
        media_type = "application/pdf"
    else:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise refused("unsupported_source_snapshot") from exc
        media_type = "text/markdown" if suffix in {".md", ".markdown"} else "text/plain"
    manifest = json.dumps(
        {
            "source_ref": reference.model_dump(),
            "exclusions": request.exclusions,
            "files": [
                {
                    "path": path,
                    "source_ref": reference.model_dump(),
                    "source_revision": revision,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return content, manifest, media_type


async def capture_source(binding, mapping, request: SourceSnapshotRequest, objects: SnapshotObjects) -> CapturedSource:
    if binding.provider != "github" or mapping is None:
        raise refused("immutable_source_required")
    if binding.kind == "document":
        content, manifest, media_type = await github_document_capture(binding, mapping, request)
        file_count = 1
    elif binding.kind == "repository":
        if request.max_bytes > MAX_CONTENT or request.max_files > MAX_FILES:
            from src.products.source_capture_disk import capture_repository

            return await capture_repository(binding, mapping, request, objects)
        content, manifest, file_count = await github_capture(binding, mapping, request)
        media_type = "application/zip"
    else:
        raise refused("immutable_source_required")
    content_ref = await asyncio.to_thread(objects.put, content, media_type)
    manifest_ref = await asyncio.to_thread(objects.put, manifest, "application/json")
    return CapturedSource(
        source_ref=request.source_ref,
        exclusions=request.exclusions,
        content=content_ref,
        file_manifest=manifest_ref,
        file_count=file_count,
        expires_at=request.retain_until,
    )
