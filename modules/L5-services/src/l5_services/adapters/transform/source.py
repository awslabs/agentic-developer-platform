"""Read a sealed Transform repository through its current Product work grant."""

import io
import json
import stat
import zipfile
from hashlib import sha256

from l5_services.source_manifest import SourceIntegrityError, _safe_path, _verify_artifact


class ProductSealedSource:
    def __init__(self, client):
        self.client = client

    async def read_sealed_source(self, *, manifest, authority):
        scope = getattr(authority, "scope", None)
        request = getattr(authority, "source_request", None)
        limits = getattr(authority, "capture_limits", None)
        if (not isinstance(manifest, dict) or not isinstance(scope, dict)
                or manifest.get("scope") != scope or not isinstance(request, dict)
                or request.get("operation_id") != manifest.get("run_id")
                or not request.get("delegation_id") or not request.get("attempt_id")
                or not request.get("lease_owner") or type(request.get("lease_fence")) is not int
                or request["lease_fence"] < 1 or not isinstance(limits, dict)
                or type(limits.get("max_bytes")) is not int or limits["max_bytes"] < 1
                or type(limits.get("max_files")) is not int or not 1 <= limits["max_files"] <= 10000
                or not isinstance(manifest.get("sources"), list) or len(manifest["sources"]) != 1
                or not isinstance(manifest.get("source_snapshot_ids"), list)
                or len(manifest["source_snapshot_ids"]) != 1):
            raise SourceIntegrityError("Transform source work or snapshot scope differs")
        source = manifest["sources"][0]
        if (not isinstance(source, dict) or source.get("record_type") != "repository_commit"
                or source.get("tenant_id") != scope.get("tenant_id")
                or source.get("project_id") != scope.get("project_id")):
            raise SourceIntegrityError("Transform source is not a scoped repository revision")
        product_id, project_id = scope["product_id"], scope["project_id"]
        snapshot_id = manifest["source_snapshot_ids"][0]
        receipt = await self.client.read_source(product_id, project_id, snapshot_id, request)
        if (not isinstance(receipt, dict) or receipt.get("snapshot_id") != snapshot_id
                or receipt.get("source_ref") != source
                or receipt.get("content") != manifest.get("archive")
                or receipt.get("exclusions") != manifest.get("exclusions", [])):
            raise SourceIntegrityError("Product snapshot differs from the committed source")
        manifest_bytes = await self.client.read_source_manifest(product_id, project_id, snapshot_id, request)
        _verify_artifact(manifest_bytes, receipt["file_manifest"], 64 * 1024 * 1024)
        try:
            file_manifest = json.loads(manifest_bytes)
            product_files = file_manifest["files"]
            pinned_files = manifest["files"]
            if (file_manifest["source_ref"] != source
                    or file_manifest["exclusions"] != receipt["exclusions"]
                    or len(product_files) != receipt["file_count"]
                    or not 1 <= len(product_files) <= limits["max_files"]
                    or len(pinned_files) != len(product_files)):
                raise SourceIntegrityError("Product file manifest differs from sealed inventory")
            pinned = {}
            for entry in pinned_files:
                path = _safe_path(entry["path"])
                if path in pinned or entry["source_ref"] != source:
                    raise SourceIntegrityError("sealed file coverage differs")
                pinned[path] = entry
            if (len({_safe_path(entry["path"]) for entry in product_files}) != len(product_files)
                    or any(pinned.get(entry["path"], {}).get(key) != entry[key]
                           for entry in product_files for key in ("sha256", "bytes"))):
                raise SourceIntegrityError("Product file metadata differs from sealed inventory")
        except (KeyError, TypeError, ValueError) as error:
            raise SourceIntegrityError("Product file manifest is incomplete") from error
        content = bytearray()
        async for chunk in self.client.stream_source(product_id, project_id, snapshot_id, request):
            content.extend(chunk)
            if len(content) > limits["max_bytes"] + 64 * 1024 * 1024:
                raise SourceIntegrityError("Transform source archive exceeds capture limit")
        source_bytes = bytes(content)
        _verify_artifact(source_bytes, receipt["content"], limits["max_bytes"] + 64 * 1024 * 1024)
        files = {}
        try:
            with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
                entries = archive.infolist()
                if len(entries) > limits["max_files"] * 2 + 32:
                    raise SourceIntegrityError("Transform source archive has too many entries")
                for entry in entries:
                    path = _safe_path(entry.filename.removesuffix("/") if entry.is_dir() else entry.filename)
                    file_type = stat.S_IFMT(entry.external_attr >> 16)
                    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise SourceIntegrityError("Transform source archive contains a link")
                    if entry.is_dir():
                        continue
                    if path not in pinned or path in files or entry.file_size > limits["max_bytes"]:
                        raise SourceIntegrityError("Transform source archive coverage differs")
                    with archive.open(entry) as stream:
                        contents = stream.read(pinned[path]["bytes"] + 1)
                    expected_mode = "100755" if (entry.external_attr >> 16) & 0o111 else "100644"
                    if (len(contents) != entry.file_size or len(contents) != pinned[path]["bytes"]
                            or sha256(contents).hexdigest() != pinned[path]["sha256"]
                            or pinned[path].get("mode") != expected_mode):
                        raise SourceIntegrityError("Transform source bytes or modes differ")
                    files[path] = contents
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise SourceIntegrityError("Transform source archive is invalid") from error
        if set(files) != set(pinned) or sum(len(content) for content in files.values()) > limits["max_bytes"]:
            raise SourceIntegrityError("Transform source file coverage differs")
        return files
