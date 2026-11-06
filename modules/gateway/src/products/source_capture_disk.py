"""Disk-backed repository capture, verified against the immutable Git tree."""

import asyncio
import hashlib
import json
import re
import stat
import struct
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import httpx

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.products.l5_branch_sources import repository_owned_by_mapping
from src.products.source_capture import MAX_RANGE_BYTES, refused
from src.products.source_capture_metadata import (
    MAX_TREE_BYTES,
    MAX_TREE_ENTRIES,
    REPOSITORY_MAX_BYTES,
    REPOSITORY_MAX_FILES,
    canonical_archive_bytes,
    excluded_path,
    selected_repository_metadata,
)
from src.products.source_snapshots import MAX_MANIFEST_BYTES, CapturedSource

CAPTURE_SECONDS = 300


async def metadata_json(client, path, headers, *, limit, params=None):
    async with client.stream("GET", path, headers=headers, params=params) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes(MAX_RANGE_BYTES):
            if len(content) + len(chunk) > limit:
                raise refused("unsupported_source_snapshot")
            content.extend(chunk)
    return json.loads(content)


def check_zip_directory(path):
    """Bound the central directory before ZipFile allocates its entry inventory."""
    with path.open("rb") as source:
        size = source.seek(0, 2)
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        index = tail.rfind(b"PK\x05\x06")
        if index < 0 or len(tail) - index < 22:
            raise refused("unsupported_source_snapshot")
        _, disk, directory_disk, disk_count, count, directory_size, offset, comment_size = struct.unpack("<4s4H2LH", tail[index : index + 22])
        if disk or directory_disk or disk_count != count or index + 22 + comment_size != len(tail):
            raise refused("unsupported_source_snapshot")
        end_offset = size - len(tail) + index
        if count == 65535 or directory_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            if end_offset < 20:
                raise refused("unsupported_source_snapshot")
            source.seek(end_offset - 20)
            signature, zip_disk, zip_offset, disks = struct.unpack("<4sLQL", source.read(20))
            if signature == b"PK\x06\x07":
                if zip_disk or disks != 1 or zip_offset + 56 != end_offset - 20:
                    raise refused("unsupported_source_snapshot")
                source.seek(zip_offset)
                signature, record_size, _, _, disk, directory_disk, disk_count, count, directory_size, offset = struct.unpack(
                    "<4sQ2H2L4Q", source.read(56)
                )
                if signature != b"PK\x06\x06" or record_size != 44 or disk or directory_disk or disk_count != count:
                    raise refused("unsupported_source_snapshot")
                end_offset = zip_offset
            elif directory_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
                raise refused("unsupported_source_snapshot")
            # Exactly 65535 entries may still use ordinary ZIP records.
        if count > MAX_TREE_ENTRIES + 1 or directory_size > 4 * MAX_TREE_BYTES or offset + directory_size != end_offset:
            raise refused("unsupported_source_snapshot")
        # ZipFile trusts the directory's byte size when allocating entries, not
        # its declared count. Count actual records before allowing that parse.
        source.seek(offset)
        observed = 0
        while source.tell() < end_offset:
            header = source.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise refused("unsupported_source_snapshot")
            name_size, extra_size, comment_size, disk = struct.unpack("<4H", header[28:36])
            observed += 1
            if disk or observed > MAX_TREE_ENTRIES + 1 or source.tell() + name_size + extra_size + comment_size > end_offset:
                raise refused("unsupported_source_snapshot")
            source.seek(name_size + extra_size + comment_size, 1)
        if observed != count:
            raise refused("unsupported_source_snapshot")


def archive_path(entry, root):
    name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
    parts = name.split("/")
    if (
        not name
        or "\\" in name
        or any(ord(c) < 32 for c in name)
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts[:2])
        or (root is not None and parts[0] != root)
        or len("/".join(parts[1:])) > 1024
        or (len(parts) < 2 and not entry.is_dir())
    ):
        raise refused("unsupported_source_snapshot")
    return parts[0], "/".join(parts[1:])


def canonical_repository_file(archive, directory, selected, request, *, cancelled=None, deadline=None):
    """Read/write one blob at a time; omitted, extra or altered blobs are errors."""
    byte_limit, file_limit = min(request.max_bytes, REPOSITORY_MAX_BYTES), min(request.max_files, REPOSITORY_MAX_FILES)
    expected_size = canonical_archive_bytes(selected)
    if not selected or len(selected) > file_limit or expected_size > byte_limit:
        raise refused("unsupported_source_snapshot")

    def active():
        if (cancelled is not None and cancelled.is_set()) or (deadline is not None and time.monotonic() >= deadline):
            raise TimeoutError("repository_capture_deadline")

    content_path, manifest_path = Path(directory) / "content.zip", Path(directory) / "manifest.json"
    expected = {item["path"]: item for item in selected}
    if len(expected) != len(selected):
        raise refused("source_integrity_mismatch", 409)
    try:
        check_zip_directory(Path(archive))
        with zipfile.ZipFile(archive) as source:
            inventory, seen, root = {}, set(), None
            for entry in source.infolist():
                active()
                root, path = archive_path(entry, root)
                if path in seen or entry.flag_bits & 1:
                    raise refused("unsupported_source_snapshot")
                seen.add(path)
                mode = entry.external_attr >> 16
                if entry.is_dir():
                    if stat.S_IFMT(mode) not in (0, stat.S_IFDIR):
                        raise refused("unsupported_source_snapshot")
                    continue
                if excluded_path(path, request.exclusions):
                    continue
                item = expected.get(path)
                if item is None or entry.file_size != item["size"]:
                    raise refused("source_integrity_mismatch", 409)
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or mode & 0o7000 or bool(mode & 0o111) != (item["mode"] == "100755"):
                    raise refused("unsupported_source_snapshot")
                inventory[path] = entry
            if set(inventory) != set(expected):
                raise refused("source_integrity_mismatch", 409)
            # Exclusive files are private even outside the private temporary directory.
            with content_path.open("xb") as output, manifest_path.open("xb") as manifest:
                content_path.chmod(0o600)
                manifest_path.chmod(0o600)

                def write_manifest(value):
                    manifest.write(value)
                    if manifest.tell() > MAX_MANIFEST_BYTES:
                        raise refused("unsupported_source_snapshot")

                reference = request.source_ref.model_dump()

                def encode(value):
                    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

                write_manifest(b'{"exclusions":' + encode(request.exclusions) + b',"files":[')
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as target:
                    for index, (path, item) in enumerate(sorted(expected.items())):
                        active()
                        info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                        info.external_attr = int(item["mode"], 8) << 16
                        info.file_size = item["size"]
                        sha256 = hashlib.sha256()
                        # Preserve Git's object-ID format alongside the SHA-256 manifest.
                        git_sha = hashlib.sha1(b"blob " + str(item["size"]).encode() + b"\0", usedforsecurity=False)
                        size = 0
                        with source.open(inventory[path]) as blob, target.open(info, "w") as captured:
                            while chunk := blob.read(MAX_RANGE_BYTES):
                                active()
                                size += len(chunk)
                                if size > item["size"]:
                                    raise refused("source_integrity_mismatch", 409)
                                sha256.update(chunk)
                                git_sha.update(chunk)
                                captured.write(chunk)
                        if size != item["size"] or git_sha.hexdigest() != item["sha"]:
                            raise refused("source_integrity_mismatch", 409)
                        entry = {
                            "path": path,
                            "source_ref": reference,
                            "source_revision": reference["record_revision"],
                            "bytes": size,
                            "sha256": sha256.hexdigest(),
                        }
                        write_manifest((b"," if index else b"") + encode(entry))
                write_manifest(b'],"source_ref":' + encode(reference) + b"}")
                if output.tell() != expected_size:
                    raise refused("source_integrity_mismatch", 409)
        return content_path, manifest_path, len(selected)
    except TimeoutError:
        raise
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, OSError, struct.error) as exc:
        raise refused("unsupported_source_snapshot") from exc


async def capture_repository(binding, mapping, request, objects):
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
    cancelled = threading.Event()
    deadline = time.monotonic() + CAPTURE_SECONDS
    try:
        async with asyncio.timeout(CAPTURE_SECONDS):
            repository = await client.get_installation_repository(int(mapping.installation_id), int(binding.native_id))
            name = repository.get("full_name") if isinstance(repository, dict) else None
            if (
                not repository_owned_by_mapping(repository, binding, mapping)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            ):
                raise refused("not_found", 404)
            token = await client.get_installation_token(int(mapping.installation_id))
            if not token:
                raise refused("connector_unavailable", 503)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            commit = await metadata_json(client._http_client, f"/repos/{name}/git/commits/{revision}", headers, limit=256 * 1024)
            if not isinstance(commit, dict) or commit.get("sha") != revision or not isinstance(commit.get("tree"), dict):
                raise refused("immutable_source_required")
            tree_sha = commit["tree"].get("sha")
            if not isinstance(tree_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", tree_sha):
                raise refused("immutable_source_required")
            tree = await metadata_json(
                client._http_client, f"/repos/{name}/git/trees/{tree_sha}", headers, limit=MAX_TREE_BYTES, params={"recursive": "1"}
            )
            selected, _ = selected_repository_metadata(tree, tree_sha, request.exclusions)
            if (
                not selected
                or len(selected) > min(request.max_files, REPOSITORY_MAX_FILES)
                or canonical_archive_bytes(selected) > min(request.max_bytes, REPOSITORY_MAX_BYTES)
            ):
                raise refused("unsupported_source_snapshot")
            async with client._http_client.stream("GET", f"/repos/{name}/zipball/{revision}", headers=headers) as response:
                if response.status_code not in (301, 302, 307) or not response.headers.get("location"):
                    response.raise_for_status()
                    raise refused("connector_unavailable", 503)
                location = httpx.URL(response.headers["location"])
            if location.scheme != "https" or location.host != "codeload.github.com" or location.port not in (None, 443) or location.userinfo:
                raise refused("connector_unavailable", 503)
            with tempfile.TemporaryDirectory(prefix="adp-repository-capture-") as temporary:
                archive_path = Path(temporary) / "upstream.zip"
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=60) as fetch:
                    async with fetch.stream("GET", str(location)) as response:
                        response.raise_for_status()
                        size = 0
                        with archive_path.open("xb") as archive:
                            archive_path.chmod(0o600)
                            async for chunk in response.aiter_bytes(MAX_RANGE_BYTES):
                                size += len(chunk)
                                if size > REPOSITORY_MAX_BYTES:
                                    raise refused("unsupported_source_snapshot")
                                archive.write(chunk)
                job = asyncio.create_task(
                    asyncio.to_thread(canonical_repository_file, archive_path, temporary, selected, request, cancelled=cancelled, deadline=deadline)
                )
                try:
                    content, manifest, file_count = await asyncio.shield(job)
                except BaseException:
                    cancelled.set()
                    # Do not remove the temporary directory while the writer still runs.
                    try:
                        await job
                    except Exception:
                        pass
                    raise
                content_ref = await asyncio.to_thread(objects.put_file, content, "application/zip")
                manifest_ref = await asyncio.to_thread(objects.put_file, manifest, "application/json")
                return CapturedSource(
                    source_ref=request.source_ref,
                    exclusions=request.exclusions,
                    content=content_ref,
                    file_manifest=manifest_ref,
                    file_count=file_count,
                    expires_at=request.retain_until,
                )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 404):
            raise refused("not_found", 404) from exc
        raise refused("connector_unavailable", 503) from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise refused("connector_unavailable", 503) from exc
    except (ValueError, OSError) as exc:
        raise refused("unsupported_source_snapshot") from exc
    finally:
        cancelled.set()
        await client.aclose()
