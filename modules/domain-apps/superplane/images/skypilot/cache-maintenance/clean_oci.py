#!/usr/bin/env python3
"""Remove only reviewed source-backed Python caches; never approve image findings."""

import argparse
import copy
import importlib.util
import io
import json
import posixpath
import re
import subprocess
import tarfile
from pathlib import Path


def load_verifier(path):
    spec = importlib.util.spec_from_file_location("cache_cleanup_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tar_entry(name, data, mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = mode
    entry.mtime = 0
    return entry


def verify_source(d, revision):
    """Bind label to actual clean maintained source; not producer attestation."""
    d.require(re.fullmatch(r"[0-9a-f]{40}", revision), "full source revision required")
    recipe = Path(__file__).resolve()
    root = Path(
        subprocess.check_output(
            ["git", "-C", str(recipe.parent), "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    paths = [
        recipe,
        Path(d.__file__).resolve(),
        Path(d.component_verifier().__file__).resolve(),
    ]
    expected = [
        recipe,
        root / "codebuild/exact_image_disposition.py",
        root / "codebuild/exact_image_components.py",
    ]
    d.require(
        paths == expected, "recipe and verifier must use the same source checkout"
    )
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    d.require(head == revision, "source revision does not equal executed checkout HEAD")
    subprocess.run(
        ["git", "-C", str(root), "diff", "--exit-code", "HEAD", "--", "."],
        check=True,
        capture_output=True,
    )
    context = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        committed = subprocess.check_output(
            ["git", "-C", str(root), "show", "HEAD:" + relative]
        )
        d.require(
            committed == path.read_bytes(),
            "executed source differs from committed bytes",
        )
        context[relative] = d.sha(committed)
    return {"revision": revision, "executed_source_files": context}


def build(d, archive, platform, output, source_revision=None):
    """Bind all inputs, write a finite layer, reconstruct and verify the result."""
    d.require(not output.exists(), "output directory already exists")
    c = d.component_verifier()
    source_context = verify_source(d, source_revision) if source_revision else None
    source_hashes = {
        "recipe_sha256": d.file_sha(Path(__file__)),
        "verifier_sha256": d.file_sha(Path(d.__file__)),
        "component_verifier_sha256": d.file_sha(Path(c.__file__)),
    }
    config, config_digest, files, retained, _ = c.read_oci(d, archive, platform)
    expected_files, expected_retained, transition = c.clean_producer(d, files, retained)
    d.require(
        transition["removed_caches"], "no supported source-backed caches to remove"
    )
    record_headers = {}
    pending = set(transition["changed_records"])
    with tarfile.open(archive) as source:
        manifest = json.load(
            source.extractfile("blobs/sha256/" + platform.split(":")[1])
        )
        for descriptor in reversed(manifest["layers"]):
            if not pending:
                break
            stream = source.extractfile(
                "blobs/sha256/" + descriptor["digest"].split(":")[1]
            )
            with tarfile.open(fileobj=stream, mode="r|*") as inherited_layer:
                for header in inherited_layer:
                    path = "/" + header.name.removeprefix("./").lstrip("/")
                    if path in pending:
                        d.require(
                            header.isfile(), "replacement RECORD must be a regular file"
                        )
                        record_headers[path] = copy.deepcopy(header)
            pending.difference_update(record_headers)
    d.require(not pending, "replacement RECORD original ownership metadata unavailable")
    layer_stream = io.BytesIO()
    with tarfile.open(
        fileobj=layer_stream, mode="w", format=tarfile.PAX_FORMAT
    ) as layer:
        for path in sorted(transition["removed_caches"]):
            whiteout = posixpath.join(
                posixpath.dirname(path), ".wh." + posixpath.basename(path)
            )
            layer.addfile(tar_entry(whiteout.lstrip("/"), b"", 0), io.BytesIO(b""))
        for path in sorted(transition["changed_records"]):
            data = expected_retained[path]
            header = record_headers[path]
            header.name = path.lstrip("/")
            header.size = len(data)
            if "size" in header.pax_headers:
                header.pax_headers["size"] = str(len(data))
            layer.addfile(header, io.BytesIO(data))
    layer_bytes = layer_stream.getvalue()
    layer_digest = "sha256:" + d.sha(layer_bytes)
    new_config = copy.deepcopy(config)
    if source_revision:
        labels = new_config["config"].get("Labels") or {}
        new_config["config"]["Labels"] = labels
        labels["org.opencontainers.image.revision"] = source_revision
    new_config["rootfs"]["diff_ids"].append(layer_digest)
    new_config.setdefault("history", []).append(
        {
            "created_by": "ADP finite source-backed CPython 3.10 cache cleanup",
            "comment": "remove-bytecode/v1; source/native/runtime config unchanged",
        }
    )
    config_bytes = d.canonical(new_config)
    new_config_digest = "sha256:" + d.sha(config_bytes)
    output.mkdir(parents=True)
    result_archive = output / "image.tar"
    with tarfile.open(archive) as source, tarfile.open(result_archive, "w") as result:

        def read_blob(digest):
            raw = source.extractfile("blobs/sha256/" + digest.split(":")[1]).read()
            d.require("sha256:" + d.sha(raw) == digest, "input OCI blob changed")
            return raw

        manifest = json.loads(read_blob(platform))
        inherited = [manifest["config"], *manifest["layers"]]
        emitted = set()
        for descriptor in inherited:
            digest = descriptor["digest"]
            if digest in emitted:
                continue
            emitted.add(digest)
            name = "blobs/sha256/" + digest.split(":")[1]
            entry = source.getmember(name)
            d.require(
                entry.isfile() and entry.size == descriptor["size"],
                "input OCI size/type changed",
            )
            # The collector has already streamed and hashed every inherited blob.
            copied = tarfile.TarInfo(name)
            copied.mode = 0o644
            copied.size = entry.size
            result.addfile(copied, source.extractfile(entry))
        manifest["config"] = {
            **manifest["config"],
            "digest": new_config_digest,
            "size": len(config_bytes),
        }
        manifest["layers"].append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": layer_digest,
                "size": len(layer_bytes),
            }
        )
        manifest_bytes = d.canonical(manifest)
        final_platform = "sha256:" + d.sha(manifest_bytes)
        blobs = {
            layer_digest: layer_bytes,
            new_config_digest: config_bytes,
            final_platform: manifest_bytes,
        }
        for digest, raw in blobs.items():
            name = "blobs/sha256/" + digest.split(":")[1]
            d.require(digest not in emitted, "unexpected new/inherited blob collision")
            result.addfile(tar_entry(name, raw), io.BytesIO(raw))
        index = d.canonical(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": manifest.get(
                            "mediaType", "application/vnd.oci.image.manifest.v1+json"
                        ),
                        "digest": final_platform,
                        "size": len(manifest_bytes),
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            }
        )
        for name, raw in (
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", index),
        ):
            result.addfile(tar_entry(name, raw), io.BytesIO(raw))
    actual_config, actual_digest, actual_files, actual_retained, _ = c.read_oci(
        d, result_archive, final_platform
    )
    d.require(
        actual_files == expected_files,
        "cleanup changed unexpected filesystem bytes or metadata",
    )
    d.require(
        actual_config["config"] == new_config["config"],
        "cleanup changed runtime config",
    )
    d.require(actual_digest == new_config_digest, "cleanup config mismatch")
    for path in transition["changed_records"]:
        d.require(
            actual_retained[path] == expected_retained[path],
            "cleanup RECORD bytes mismatch",
        )
    d.require(
        source_hashes
        == {
            "recipe_sha256": d.file_sha(Path(__file__)),
            "verifier_sha256": d.file_sha(Path(d.__file__)),
            "component_verifier_sha256": d.file_sha(Path(c.__file__)),
        },
        "executed source changed during cleanup",
    )
    if source_revision:
        d.require(
            verify_source(d, source_revision) == source_context,
            "source checkout changed during cleanup",
        )
    receipt = {
        "schema": "adp-python-cache-cleanup/v1",
        "scope": "Transformation receipt only; no provenance or security approval",
        **source_hashes,
        "source_context": source_context,
        "input": {
            "archive_sha256": d.file_sha(archive),
            "platform_digest": platform,
            "config_digest": config_digest,
        },
        "output": {
            "archive_sha256": d.file_sha(result_archive),
            "platform_digest": final_platform,
            "config_digest": actual_digest,
        },
        "layer_digest": layer_digest,
        "transition": transition,
        "filesystem_sha256": d.object_sha(actual_files),
        "runtime_config_unchanged": actual_config["config"] == config["config"],
        "runtime_config_unchanged_except_revision_label": True,
    }
    (output / "receipt.json").write_bytes(d.canonical(receipt) + b"\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--verifier",
        required=True,
        type=Path,
        help="Independently reviewed exact_image_disposition.py with v2 provider",
    )
    parser.add_argument(
        "--source-revision",
        help="Optional actual clean checkout HEAD; set only revision label",
    )
    args = parser.parse_args()
    result = build(
        load_verifier(args.verifier),
        args.archive,
        args.platform,
        args.output,
        args.source_revision,
    )
    print(
        json.dumps(
            {
                "output": result["output"],
                "removed": len(result["transition"]["removed_caches"]),
            }
        )
    )


if __name__ == "__main__":
    main()
