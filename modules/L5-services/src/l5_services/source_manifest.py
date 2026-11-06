"""Verify admitted snapshot bytes before sealing an assessment manifest."""

import io
import json
import re
import stat
import struct
import tempfile
from pathlib import Path
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath


class SourceIntegrityError(ValueError):
    pass


_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"(?i)\b(?:aws_secret_access_key|api[_-]?key|password)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+=_-]{16,}"),
)


def _digest(content):
    return sha256(content).hexdigest()


def _safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value
            or "\x00" in value or value.startswith("/") or "//" in value
            or ":" in value.split("/", 1)[0]
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise SourceIntegrityError("unsafe source path")
    return str(PurePosixPath(value))


def _verify_artifact(content, reference, limit):
    if (len(content) > limit or len(content) != reference["bytes"]
            or _digest(content) != reference["sha256"]):
        raise SourceIntegrityError("source integrity mismatch")


def package_architecture_sources(sources, *, scope, max_files, max_bytes):
    """Package pinned source bytes with explicit selected-tree and entry coverage."""
    if (not sources or not isinstance(scope, dict) or not scope.get("tenant_id")
            or not scope.get("project_id") or max_files < 1 or max_bytes < 1):
        raise SourceIntegrityError("invalid architecture source bounds")
    inventory = []
    archive_files = []
    seen_snapshots = set()
    total_bytes = 0
    for index, source in enumerate(sources):
        source_ref = source["source_ref"]
        snapshot_id = source["snapshot_id"]
        if (not isinstance(source_ref, dict)
                or source_ref.get("tenant_id") != scope["tenant_id"]
                or source_ref.get("project_id") != scope["project_id"]
                or source_ref.get("record_type") not in {"document", "repository_commit"}
                or source_ref.get("record_revision") != source["revision"]
                or not snapshot_id or snapshot_id in seen_snapshots):
            raise SourceIntegrityError("duplicate or missing pinned source")
        seen_snapshots.add(snapshot_id)
        selected = [_safe_path(path) for path in source["selected_paths"]]
        if len(selected) != len(set(selected)):
            raise SourceIntegrityError("duplicate selected path")
        selected_hashes = source["selected_hashes"]
        if set(selected_hashes) != set(selected):
            raise SourceIntegrityError("selected source digest coverage mismatch")
        entries = source["files"]
        if len(inventory) + len(entries) > max_files or len(entries) != len(selected):
            raise SourceIntegrityError("source file count or coverage mismatch")
        actual = set()
        for entry in entries:
            path = _safe_path(entry["path"])
            if path in actual:
                raise SourceIntegrityError("duplicate source path")
            actual.add(path)
            if path not in selected_hashes:
                raise SourceIntegrityError("selected source path coverage mismatch")
            content = entry["content"]
            if not isinstance(content, bytes) or len(content) > max_bytes - total_bytes:
                raise SourceIntegrityError("source exceeds byte limit")
            if (entry.get("mode", stat.S_IFREG) != stat.S_IFREG
                    or any(pattern.search(content) for pattern in _SECRET_PATTERNS)
                    or content.startswith((b"PK\x03\x04", b"\x1f\x8b"))):
                raise SourceIntegrityError("unsafe or secret-bearing source")
            if _digest(content) != selected_hashes[path]:
                raise SourceIntegrityError("selected source digest mismatch")
            total_bytes += len(content)
            component = entry.get("component")
            exclusion = entry.get("exclusion")
            if (bool(component) == bool(exclusion)
                    or not isinstance(entry.get("entry_point"), bool)):
                raise SourceIntegrityError("source has no unambiguous coverage decision")
            archive_path = f"sources/{index}/{path}"
            inventory.append({
                "source_ref": source_ref, "snapshot_id": snapshot_id,
                "revision": source["revision"], "path": path,
                "bytes": len(content), "sha256": _digest(content),
                "component": component, "exclusion": exclusion,
                "entry_point": entry["entry_point"],
                "archive_path": None if exclusion else archive_path,
            })
            if not exclusion:
                archive_files.append((archive_path, content))
        if actual != set(selected):
            raise SourceIntegrityError("selected source path coverage mismatch")
    manifest = {
        "schema_version": "1.0", "sources": [
            {"source_ref": source["source_ref"], "snapshot_id": source["snapshot_id"],
             "revision": source["revision"], "selected_paths": sorted(source["selected_paths"]),
             "selected_hashes": source["selected_hashes"],
             "unresolved": sorted(source.get("unresolved", []))}
            for source in sources
        ],
        "files": sorted(inventory, key=lambda item: (item["snapshot_id"], item["path"])),
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
    archive_files.append(("manifest.json", manifest_bytes))
    if sum(len(content) for _, content in archive_files) > max_bytes:
        raise SourceIntegrityError("expanded source exceeds byte limit")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(archive_files):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    archived = output.getvalue()
    if len(archived) > max_bytes:
        raise SourceIntegrityError("compressed source exceeds byte limit")
    return archived, manifest


async def verify_provider_source_receipt(manifest, provider_reader):
    """Validate an independently read provider receipt, not an upload or Start response."""
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("archive"), dict)
            or not callable(getattr(provider_reader, "read_source_receipt", None))):
        raise SourceIntegrityError("provider source consumption unverified")
    receipt = await provider_reader.read_source_receipt(manifest.get("archive"))
    architecture = manifest.get("architecture") or {}
    archive = manifest.get("archive")
    readback = manifest.get("archive_readback") or {}
    if (not isinstance(archive, dict)
            or manifest.get("provider_coverage") != "provider_coverage_unverified"
            or not isinstance(receipt, dict) or receipt.get("state") != "consumed"
            or receipt.get("scope") != manifest.get("scope")
            or receipt.get("artifact") != archive or readback.get("artifact") != archive
            or not isinstance(readback.get("sha256"), str)
            or not isinstance(readback.get("bytes"), int)
            or readback["sha256"] != archive.get("sha256")
            or receipt.get("sha256") != readback.get("sha256")
            or receipt.get("bytes") != readback.get("bytes")
            or not isinstance(receipt.get("paths"), list)
            or receipt["paths"] != sorted(entry["archive_path"] for entry in architecture.get("files", [])
                                          if entry.get("archive_path"))):
        raise SourceIntegrityError("provider source consumption unverified")
    return {"provider_coverage": "verified", "provider_source_receipt": receipt}


STREAM_CHUNK_BYTES = 64 * 1024


def verify_archive_file(path, reference, limit):
    """Hash a seekable artifact without allocating its contents."""
    digest, size = sha256(), 0
    with open(path, "rb") as content:
        while chunk := content.read(STREAM_CHUNK_BYTES):
            size += len(chunk)
            if size > limit:
                raise SourceIntegrityError("source exceeds byte limit")
            digest.update(chunk)
    if size != reference["bytes"] or digest.hexdigest() != reference["sha256"]:
        raise SourceIntegrityError("source integrity mismatch")
    return size, digest.hexdigest()


def _bounded_zip_directory(path, max_entries):
    """Bound directory allocation before ZipFile parses untrusted ZIP metadata."""
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise SourceIntegrityError("invalid source archive")
        record = struct.unpack("<4s4H2LH", tail[offset:offset + 22])
        if record[1] or record[2] or offset + 22 + record[7] != len(tail):
            raise SourceIntegrityError("unsupported ZIP directory")
        count, directory_size, directory_offset = record[4], record[5], record[6]
        if count == 65535 or directory_size == 4294967295 or directory_offset == 4294967295:
            end_offset = size - len(tail) + offset
            if end_offset < 20:
                raise SourceIntegrityError("invalid ZIP64 directory")
            stream.seek(end_offset - 20)
            locator = struct.unpack("<4sLQL", stream.read(20))
            if (locator[0] != b"PK\x06\x07" or locator[1] != 0 or locator[3] != 1
                    or locator[2] > size - 56):
                raise SourceIntegrityError("unsupported ZIP64 directory")
            stream.seek(locator[2])
            raw = stream.read(56)
            if len(raw) != 56:
                raise SourceIntegrityError("invalid ZIP64 directory")
            extended = struct.unpack("<4sQ2H2L4Q", raw)
            if extended[0] != b"PK\x06\x06" or extended[1] < 44 or extended[4] or extended[5]:
                raise SourceIntegrityError("unsupported ZIP64 directory")
            count, directory_size, directory_offset = extended[7], extended[8], extended[9]
        if (count > max_entries or directory_size > min(256 * 1024 * 1024, max_entries * 1152)
                or directory_offset + directory_size > size):
            raise SourceIntegrityError("source archive has too many entries or oversized directory")


async def _materialize_code_review(run, attempt, source_client, archive_store, now):
    """Verify every pinned file while retaining source contents only on disk."""
    from l5_services.source_archive import verify_review_selection

    verify_review_selection(run)
    if not callable(getattr(archive_store, "put_archive_file", None)):
        raise SourceIntegrityError("code review requires a file-backed archive store")
    admission, sources = run.admission, run.input["sources"]
    snapshots = admission["source_snapshots"]
    limits = run.capture_limits
    if (not limits or not snapshots or len(snapshots) != len(sources)
            or len({item["snapshot_id"] for item in snapshots}) != len(snapshots)):
        raise SourceIntegrityError("admitted source identity mismatch")
    max_files, max_bytes = limits["max_files"], limits["max_bytes"]
    if (type(max_files) is not int or not 1 <= max_files <= 100000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024):
        raise SourceIntegrityError("invalid source capture limits")
    files, total_bytes = [], 0
    with tempfile.TemporaryDirectory(prefix="l5-security-source-") as directory:
        output = Path(directory) / "review.zip"
        combined = zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) if len(sources) > 1 else None
        try:
            for index, (snapshot, source) in enumerate(zip(snapshots, sources, strict=True)):
                expires_at = datetime.fromisoformat(snapshot["expires_at"].replace("Z", "+00:00"))
                if (snapshot["source_ref"] != source or expires_at <= now
                        or snapshot["exclusions"] != admission.get("exclusions", [])):
                    raise SourceIntegrityError("admitted source identity mismatch")
                request = {"delegation_id": admission["delegation_id"],
                           "delegation_revision": admission["delegation_version"],
                           "operation_id": run.run_id, "attempt_id": attempt.work_id,
                           "lease_fence": attempt.fence, "lease_owner": getattr(attempt, "owner", None)}
                args = (run.product_id, run.project_id, snapshot["snapshot_id"], request)
                if await source_client.read_source(*args) != snapshot:
                    raise SourceIntegrityError("snapshot receipt changed")
                raw_manifest = await source_client.read_source_manifest(*args)
                _verify_artifact(raw_manifest, snapshot["file_manifest"], 64 * 1024 * 1024)
                inventory = json.loads(raw_manifest)
                entries = inventory["files"]
                del raw_manifest
                if (inventory["source_ref"] != source
                        or inventory["exclusions"] != admission.get("exclusions", [])
                        or not entries or len(entries) != snapshot["file_count"] or len(files) + len(entries) > max_files):
                    raise SourceIntegrityError("file manifest does not match admission")
                expected = {}
                for entry in entries:
                    path = _safe_path(entry["path"])
                    if (entry["source_ref"] != source or path in expected
                            or entry.get("source_revision", source["record_revision"]) != source["record_revision"]
                            or type(entry["bytes"]) is not int or entry["bytes"] < 0
                            or any(path == excluded or path.startswith(excluded.rstrip("/") + "/")
                                   for excluded in admission.get("exclusions", []))):
                        raise SourceIntegrityError("source file identity mismatch")
                    expected[path] = entry
                source_path = Path(directory) / "snapshot.zip" if combined else output
                digest, size = sha256(), 0
                with source_path.open("wb") as target:
                    async for chunk in source_client.stream_source(*args):
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourceIntegrityError("source exceeds byte limit")
                        digest.update(chunk)
                        target.write(chunk)
                if size != snapshot["content"]["bytes"] or digest.hexdigest() != snapshot["content"]["sha256"]:
                    raise SourceIntegrityError("source integrity mismatch")
                seen = set()
                _bounded_zip_directory(source_path, max_files * 2 + 32)
                with zipfile.ZipFile(source_path) as archive:
                    if len(archive.infolist()) > max_files * 2 + 32:
                        raise SourceIntegrityError("source archive has too many entries")
                    for item in archive.infolist():
                        path = _safe_path(item.filename.removesuffix("/") if item.is_dir() else item.filename)
                        mode = stat.S_IFMT(item.external_attr >> 16)
                        if mode not in {0, stat.S_IFDIR, stat.S_IFREG}:
                            raise SourceIntegrityError("unsupported archive entry type")
                        if item.is_dir():
                            if item.file_size or mode == stat.S_IFREG:
                                raise SourceIntegrityError("unsupported archive directory")
                            continue
                        entry = expected.get(path)
                        if (mode == stat.S_IFDIR or path in seen or entry is None
                                or item.file_size != entry["bytes"] or item.file_size > max_bytes - total_bytes):
                            raise SourceIntegrityError("source file coverage mismatch")
                        seen.add(path)
                        entry_digest, entry_size = sha256(), 0
                        target = None
                        if combined:
                            info = zipfile.ZipInfo(f"{index}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
                            info.external_attr = (0o100755 if (item.external_attr >> 16) & 0o111 else 0o100644) << 16
                            target = combined.open(info, "w")
                        try:
                            with archive.open(item) as data:
                                while chunk := data.read(STREAM_CHUNK_BYTES):
                                    entry_size += len(chunk)
                                    if entry_size > entry["bytes"] or total_bytes + entry_size > max_bytes:
                                        raise SourceIntegrityError("expanded content exceeds byte limit")
                                    entry_digest.update(chunk)
                                    if target:
                                        target.write(chunk)
                        finally:
                            if target:
                                target.close()
                        if entry_size != entry["bytes"] or entry_digest.hexdigest() != entry["sha256"]:
                            raise SourceIntegrityError("source integrity mismatch")
                        total_bytes += entry_size
                    if seen != set(expected):
                        raise SourceIntegrityError("source file coverage mismatch")
                files.extend(entries)
                if combined:
                    source_path.unlink()
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise SourceIntegrityError("invalid source archive") from error
        finally:
            if combined:
                combined.close()
        limit = max_bytes + 64 * 1024 * 1024 if len(sources) > 1 else max_bytes
        if output.stat().st_size > limit:
            raise SourceIntegrityError("source archive exceeds byte limit")
        archive_ref = await archive_store.put_archive_file(run.run_id, output)
        verify_archive_file(output, archive_ref, limit)
    manifest = {
        "schema_version": "1.0", "scope": admission["scope"], "run_id": run.run_id,
        "configuration_revision": run.configuration_revision,
        "configuration_hash": admission["configuration_hash"], "binding_generation": run.binding_generation,
        "profile_ref": admission["profile_ref"], "sources": sources, "files": files, "archive": archive_ref,
        "coverage_mode": "full_snapshot", "exclusions": admission.get("exclusions", []),
        "materializer_revision": "2", "adapter_revision": admission["adapter_revision"],
        "sealed_at": now.isoformat().replace("+00:00", "Z"),
        "admission_hash": _digest(json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()),
        "source_snapshot_ids": [snapshot["snapshot_id"] for snapshot in snapshots],
    }
    return manifest, []


async def materialize_inputs(run, attempt, source_client, *, archive_store=None, now=None):
    """Return verified, bounded source bytes and a sealed manifest; never re-pin."""
    now = now or datetime.now(UTC)
    if run.service_id == "security" and run.input.get("capability") == "code_review":
        return await _materialize_code_review(run, attempt, source_client, archive_store, now)
    admission = run.admission
    snapshots = admission["source_snapshots"]
    if run.service_id == "devops" and run.input.get("capability") == "release_readiness":
        options = run.input["devops_input"]
        head = options["repository_ref"]
        sources = [{**head, "record_revision": options["base_revision"]}, head]
        if run.admission.get("release_eligibility", {}).get("status") != "ready":
            raise SourceIntegrityError("release source eligibility unavailable")
    else:
        sources = run.input["sources"]
    if not snapshots or len(snapshots) != len(sources):
        raise SourceIntegrityError("admitted source identity mismatch")
    if run.capture_limits is None:
        raise SourceIntegrityError("missing pinned capture limits")
    architecture_coverage = admission.get("architecture_coverage") if run.service_id == "well_architected" else None
    if run.service_id == "well_architected" and (
            not isinstance(architecture_coverage, dict)
            or set(architecture_coverage) != {snapshot["snapshot_id"] for snapshot in snapshots}
            or len(architecture_coverage) != len(snapshots)):
        raise SourceIntegrityError("architecture coverage does not match admitted snapshots")
    max_files = run.capture_limits["max_files"]
    max_bytes = run.capture_limits["max_bytes"]
    files = []
    content_by_source = []
    archive_entries = []
    seen_paths = set()
    total_bytes = 0
    architecture_sources = []
    for snapshot, source in zip(snapshots, sources, strict=True):
        expires_at = datetime.fromisoformat(snapshot["expires_at"].replace("Z", "+00:00"))
        if (snapshot["source_ref"] != source or snapshot["exclusions"] != admission.get("exclusions", [])
                or expires_at <= now):
            raise SourceIntegrityError("admitted source identity mismatch")
        request = {
            "delegation_id": admission["delegation_id"],
            "delegation_revision": admission["delegation_version"],
            "operation_id": run.run_id,
            "attempt_id": attempt.work_id,
            "lease_fence": attempt.fence,
            "lease_owner": getattr(attempt, "owner", None),
        }
        args = (run.product_id, run.project_id, snapshot["snapshot_id"], request)
        receipt = await source_client.read_source(*args)
        if receipt != snapshot:
            raise SourceIntegrityError("snapshot receipt changed")
        manifest_bytes = await source_client.read_source_manifest(*args)
        _verify_artifact(manifest_bytes, snapshot["file_manifest"], 64 * 1024 * 1024)
        file_manifest = json.loads(manifest_bytes)
        entries = file_manifest["files"]
        if (file_manifest["source_ref"] != source
                or file_manifest["exclusions"] != admission.get("exclusions", [])
                or len(entries) != snapshot["file_count"] or len(files) + len(entries) > max_files):
            raise SourceIntegrityError("file manifest does not match admission")
        content = bytearray()
        async for chunk in source_client.stream_source(*args):
            content.extend(chunk)
            if len(content) > max_bytes:
                raise SourceIntegrityError("source exceeds byte limit")
        source_bytes = bytes(content)
        _verify_artifact(source_bytes, snapshot["content"], max_bytes)
        if source["record_type"] == "document":
            if len(entries) != 1:
                raise SourceIntegrityError("document must have one file")
            contents = {entries[0]["path"]: source_bytes}
            modes = {entries[0]["path"]: "100644"}
        elif source["record_type"] == "repository_commit":
            contents = {}
            modes = {}
            try:
                with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
                    items = archive.infolist()
                    if len(items) > max_files * 2 + 32:
                        raise SourceIntegrityError("source archive has too many entries")
                    for item in items:
                        path = _safe_path(item.filename.removesuffix("/") if item.is_dir()
                                          else item.filename)
                        file_type = stat.S_IFMT(item.external_attr >> 16)
                        if file_type not in {0, stat.S_IFDIR, stat.S_IFREG}:
                            raise SourceIntegrityError("unsupported archive entry type")
                        if item.is_dir():
                            if item.file_size or file_type == stat.S_IFREG:
                                raise SourceIntegrityError("unsupported archive directory")
                            continue
                        if (file_type == stat.S_IFDIR or path in contents
                                or item.file_size > max_bytes - total_bytes):
                            raise SourceIntegrityError("unsupported or oversized archive entry")
                        with archive.open(item) as entry:
                            data = entry.read(max_bytes - total_bytes + 1)
                        if len(data) != item.file_size or len(data) > max_bytes - total_bytes:
                            raise SourceIntegrityError("expanded content exceeds byte limit")
                        contents[path] = data
                        modes[path] = "100755" if (item.external_attr >> 16) & 0o111 else "100644"
                        total_bytes += len(data)
            except (zipfile.BadZipFile, RuntimeError) as error:
                raise SourceIntegrityError("invalid source archive") from error
        else:
            raise SourceIntegrityError("unsupported source kind")
        entry_paths = set()
        for entry in entries:
            path = _safe_path(entry["path"])
            if (entry["source_ref"] != source or path in entry_paths
                    or (snapshot["snapshot_id"], path) in seen_paths
                    or path not in contents or any(path == excluded or path.startswith(excluded.rstrip("/") + "/")
                                                        for excluded in admission.get("exclusions", []))):
                raise SourceIntegrityError("source file identity mismatch")
            entry_paths.add(path)
            seen_paths.add((snapshot["snapshot_id"], path))
            _verify_artifact(contents[path], entry, max_bytes)
            archive_entries.append((len(content_by_source), path, contents[path]))
            if source["record_type"] == "document":
                total_bytes += len(contents[path])
            files.append({**entry, "mode": modes[path]} if run.service_id == "transform" else entry)
        if set(contents) != entry_paths or total_bytes > max_bytes:
            raise SourceIntegrityError("source file coverage mismatch")
        if run.service_id == "well_architected":
            coverage = architecture_coverage[snapshot["snapshot_id"]]
            if (not isinstance(coverage, dict) or not isinstance(coverage.get("files"), dict)
                    or set(coverage["files"]) != entry_paths
                    or not isinstance(coverage.get("unresolved"), list)):
                raise SourceIntegrityError("architecture coverage does not match pinned snapshot")
            architecture_sources.append({
                "source_ref": source, "snapshot_id": snapshot["snapshot_id"],
                "revision": source["record_revision"],
                "selected_paths": sorted(entry_paths),
                "selected_hashes": {entry["path"]: entry["sha256"] for entry in entries},
                "files": [{"path": entry["path"], "content": contents[entry["path"]],
                           **coverage["files"][entry["path"]]} for entry in entries],
                "unresolved": coverage["unresolved"],
            })
        content_by_source.append(source_bytes)
    archive_ref = snapshots[0]["content"]
    if run.service_id == "well_architected":
        if archive_store is None or not callable(getattr(archive_store, "get_archive", None)):
            raise SourceIntegrityError("architecture archive store or coverage unavailable")
        archived, architecture_manifest = package_architecture_sources(
            architecture_sources, scope=admission["scope"], max_files=max_files, max_bytes=max_bytes)
        archive_ref = await archive_store.put_archive(run.run_id, archived)
        _verify_artifact(archived, archive_ref, max_bytes)
        stored = await archive_store.get_archive(run.run_id, archive_ref)
        if not isinstance(stored, bytes) or stored != archived:
            raise SourceIntegrityError("architecture archive readback mismatch")
    elif len(snapshots) != 1:
        if archive_store is None:
            raise SourceIntegrityError("multiple snapshots require a qualified archive store")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source_index, path, content in archive_entries:
                info = zipfile.ZipInfo(f"{source_index}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        archived = output.getvalue()
        archive_ref = await archive_store.put_archive(run.run_id, archived)
        _verify_artifact(archived, archive_ref, max_bytes + 64 * 1024 * 1024)
    elif run.input.get("capability") == "code_review":
        if archive_store is None:
            raise SourceIntegrityError("code review requires a qualified archive store")
        archived = content_by_source[0]
        archive_ref = await archive_store.put_archive(run.run_id, archived)
        _verify_artifact(archived, archive_ref, max_bytes)
    manifest = {
        "schema_version": "1.0", "scope": admission["scope"], "run_id": run.run_id,
        "configuration_revision": run.configuration_revision,
        "configuration_hash": admission["configuration_hash"],
        "binding_generation": run.binding_generation, "profile_ref": admission["profile_ref"],
        "sources": sources, "files": files, "archive": archive_ref,
        "coverage_mode": ("selected_snapshot" if run.service_id == "well_architected" else
                          "documents" if sources[0]["record_type"] == "document" else "full_snapshot"),
        "exclusions": admission.get("exclusions", []), "materializer_revision": "1",
        "adapter_revision": admission["adapter_revision"],
        "sealed_at": now.isoformat().replace("+00:00", "Z"),
        "admission_hash": _digest(json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()),
        "source_snapshot_ids": [snapshot["snapshot_id"] for snapshot in snapshots],
    }
    if run.service_id == "well_architected":
        manifest["architecture"] = architecture_manifest
        bucket = getattr(archive_store, "bucket", None)
        key = archive_ref.get("artifact_id")
        if (not isinstance(bucket, str) or not bucket or not isinstance(key, str)
                or not key.startswith(f"l5/well_architected/work/{run.run_id}/")
                or "/" in bucket):
            raise SourceIntegrityError("qualified S3 source location unavailable")
        manifest["provider_source_uri"] = f"s3://{bucket}/{key}"
        manifest["archive_readback"] = {"sha256": _digest(stored), "bytes": len(stored),
                                        "artifact": archive_ref}
        manifest["provider_coverage"] = "provider_coverage_unverified"
    return manifest, content_by_source
