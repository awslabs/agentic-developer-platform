"""Real deterministic archives behind mocked, version-specific source reads."""

from hashlib import sha256

from l5_services.source_manifest import package_architecture_sources


ARCHIVES = {}


def source_fixture(source, scope, run_id, *, text="Encrypted architecture source"):
    content = text.encode()
    packed, architecture = package_architecture_sources([{
        "source_ref": source, "snapshot_id": "snapshot-" + run_id,
        "revision": source["record_revision"], "selected_paths": ["architecture.txt"],
        "selected_hashes": {"architecture.txt": sha256(content).hexdigest()},
        "files": [{"path": "architecture.txt", "content": content, "component": "service",
                   "exclusion": None, "entry_point": True}], "unresolved": [],
    }], scope=scope, max_files=1, max_bytes=8192)
    reference = {"artifact_id": f"l5/well_architected/work/{run_id}/archive.zip",
                 "media_type": "application/zip",
                 "version": "version-" + run_id, "sha256": sha256(packed).hexdigest(), "bytes": len(packed)}
    uri = "s3://fictional-wa-bucket/" + reference["artifact_id"]
    ARCHIVES[(uri, reference["version"])] = packed
    manifest = {"archive": reference, "architecture": architecture, "provider_source_uri": uri,
                "provider_coverage": "provider_coverage_unverified"}
    entry = architecture["files"][0]
    citation = {key: entry[key] for key in ("source_ref", "revision", "path", "sha256")}
    return manifest, {**citation, "line": 1, "quote": text}


def read_archive(uri, reference):
    return ARCHIVES[(uri, reference["version"])]
