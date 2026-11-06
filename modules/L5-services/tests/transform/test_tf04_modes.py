"""Bind Transform baseline permissions to the hash-verified source archive."""

import io
import json
import stat
import zipfile
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from l5_services.source_manifest import materialize_inputs
from test_tf03_attempts import NOW, SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["transform", "security"])
async def test_sealed_source_modes_come_from_verified_archive(service_id):
    files = {"src/app.py": b"regular\n", "src/tool.py": b"executable\n"}
    modes = {"src/app.py": 0o644, "src/tool.py": 0o755}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, contents in files.items():
            entry = zipfile.ZipInfo(path)
            entry.external_attr = (stat.S_IFREG | modes[path]) << 16
            archive.writestr(entry, contents)
    archive_bytes = output.getvalue()
    entries = [{"source_ref": SOURCE, "path": path, "sha256": sha256(contents).hexdigest(),
                "bytes": len(contents)} for path, contents in files.items()]
    listing = json.dumps({"source_ref": SOURCE, "exclusions": [], "files": entries}).encode()

    def reference(contents):
        return {"artifact_id": "sealed", "version": "1", "sha256": sha256(contents).hexdigest(),
                "bytes": len(contents), "media_type": "application/octet-stream"}

    snapshot = {"snapshot_id": "source", "source_ref": SOURCE, "exclusions": [], "file_count": len(files),
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "content": reference(archive_bytes), "file_manifest": reference(listing)}
    run = SimpleNamespace(service_id=service_id, product_id="product-a", project_id="project-a", run_id="run-a",
                          configuration_revision=1, binding_generation=1, input={"sources": [SOURCE]},
                          capture_limits={"max_files": 2, "max_bytes": 4096},
                          admission={"source_snapshots": [snapshot], "delegation_id": "delegation",
                                     "delegation_version": "1", "scope": {}, "configuration_hash": "a" * 64,
                                     "profile_ref": SOURCE, "adapter_revision": "1"})

    class SourceClient:
        async def read_source(self, *args):
            return snapshot

        async def read_source_manifest(self, *args):
            return listing

        async def stream_source(self, *args):
            yield archive_bytes

    manifest, contents = await materialize_inputs(run, SimpleNamespace(work_id="work", fence=1), SourceClient(), now=NOW)
    assert contents == [archive_bytes]
    if service_id == "transform":
        assert manifest["files"] == [{**entry, "mode": "100755" if entry["path"] == "src/tool.py" else "100644"}
                                      for entry in entries]
    else:
        assert manifest["files"] == entries
    schema_path = Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json"
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/FileEntry"})
    for entry in manifest["files"]:
        validator.validate(entry)
