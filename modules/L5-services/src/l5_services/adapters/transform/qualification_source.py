"""Read retained Product sources without a worker lease or provider credentials."""

import io
import json
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import quote

from l5_services.source_manifest import SourceIntegrityError, _safe_path, _verify_artifact
from l5_services.workers.execution import AuthorityRevoked


class TransformQualificationAuthority:
    def __init__(self, run, source, *, clock=None):
        self.scope = dict(run.admission["scope"])
        self.source_request = {"operation_id": run.run_id}
        self.capture_limits = dict(run.capture_limits)
        self.provider_identity = dict(run.admission["provider_identity"])
        self.source, self.manifest = source, run.manifest
        self.clock = clock or (lambda: datetime.now(UTC))
        self.expires_at = self.clock() + timedelta(minutes=5)

    def assert_current(self):
        if self.clock() >= self.expires_at:
            raise AuthorityRevoked("Transform qualification read authority expired")

    async def revalidate(self):
        self.assert_current()
        await self.source.read_receipt(manifest=self.manifest, authority=self)
        self.assert_current()


class RetainedProductSource:
    def __init__(self, client, scope, run_id):
        self.client = client
        self.scope, self.run_id = dict(scope), run_id
        self.run_root = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/transform/runs/"
                     f"{quote(run_id, safe='')}")
        self.root = self.run_root + "/source-snapshots/"

    async def _get(self, path, maximum):
        async with self.client.stream("GET", path, follow_redirects=False) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > maximum:
                    raise SourceIntegrityError("Retained Product source exceeds its bound")
            return bytes(content)

    async def read_receipt(self, *, manifest, authority):
        authority.assert_current()
        if (not isinstance(authority, TransformQualificationAuthority)
                or manifest.get("scope") != self.scope or authority.scope != self.scope
                or manifest.get("run_id") != self.run_id
                or authority.source_request != {"operation_id": self.run_id}
                or len(manifest.get("sources", [])) != 1
                or len(manifest.get("source_snapshot_ids", [])) != 1):
            raise AuthorityRevoked("Retained source differs from the selected Product run")
        path = self.root + quote(manifest["source_snapshot_ids"][0], safe="")
        receipt = json.loads(await self._get(path, 1024 * 1024))
        if (receipt.get("snapshot_id") != manifest["source_snapshot_ids"][0]
                or receipt.get("source_ref") != manifest["sources"][0]
                or receipt.get("content") != manifest.get("archive")
                or receipt.get("exclusions") != manifest.get("exclusions", [])):
            raise SourceIntegrityError("Retained Product snapshot differs from the sealed source")
        authority.assert_current()
        return receipt

    async def read_sealed_source(self, *, manifest, authority):
        authority.assert_current()
        if json.loads(await self._get(self.run_root + "/source-manifest", 64 * 1024 * 1024)) != manifest:
            raise SourceIntegrityError("Retained run manifest differs from durable qualification inputs")
        receipt = await self.read_receipt(manifest=manifest, authority=authority)
        limits = authority.capture_limits
        maximum, count = limits.get("max_bytes"), limits.get("max_files")
        if (type(maximum) is not int or not 1 <= maximum <= 32 * 1024 * 1024
                or type(count) is not int or not 1 <= count <= 10000):
            raise SourceIntegrityError("Retained source capture limits are invalid")
        path = self.root + quote(receipt["snapshot_id"], safe="")
        inventory_bytes = await self._get(path + "/manifest", 64 * 1024 * 1024)
        _verify_artifact(inventory_bytes, receipt["file_manifest"], 64 * 1024 * 1024)
        inventory = json.loads(inventory_bytes)
        pinned = {_safe_path(entry["path"]): entry for entry in manifest["files"]}
        files = inventory["files"]
        if (inventory.get("source_ref") != manifest["sources"][0]
                or inventory.get("exclusions") != receipt["exclusions"]
                or not 1 <= len(files) == receipt["file_count"] == len(pinned) == len(manifest["files"]) <= count
                or len({_safe_path(entry["path"]) for entry in files}) != len(files)
                or any(entry["source_ref"] != manifest["sources"][0] for entry in pinned.values())
                or any(pinned.get(entry["path"], {}).get(key) != entry[key]
                       for entry in files for key in ("sha256", "bytes"))):
            raise SourceIntegrityError("Retained file inventory differs from the sealed source")
        content = await self._get(path + "/content", maximum + 64 * 1024 * 1024)
        _verify_artifact(content, receipt["content"], maximum + 64 * 1024 * 1024)
        result = {}
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > count * 2 + 32:
                raise SourceIntegrityError("Retained archive has too many entries")
            for entry in archive.infolist():
                name = _safe_path(entry.filename.removesuffix("/") if entry.is_dir() else entry.filename)
                if stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise SourceIntegrityError("Retained archive contains a link")
                if entry.is_dir():
                    continue
                if name not in pinned or name in result or entry.file_size > maximum:
                    raise SourceIntegrityError("Retained archive coverage differs")
                with archive.open(entry) as stream:
                    data = stream.read(pinned[name]["bytes"] + 1)
                mode = "100755" if entry.external_attr >> 16 & 0o111 else "100644"
                if (len(data) != entry.file_size or len(data) != pinned[name]["bytes"]
                        or sha256(data).hexdigest() != pinned[name]["sha256"] or mode != pinned[name]["mode"]):
                    raise SourceIntegrityError("Retained file content or mode differs")
                result[name] = data
        if set(result) != set(pinned) or sum(map(len, result.values())) > maximum:
            raise SourceIntegrityError("Retained source file coverage differs")
        authority.assert_current()
        return result
