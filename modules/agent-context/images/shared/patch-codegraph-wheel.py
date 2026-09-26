#!/usr/bin/env python3
"""Rebuild the verified CGC wheel's legacy SCIP bindings for modern protobuf.

Keeps the exact serialized schema and upstream version. Only generated Python
bindings, the protobuf runtime requirement, RECORD, and explicit patch provenance
change. Never bypass pip dependency checks or set the pure-Python fallback.
"""
import ast
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

UPSTREAM_SHA256 = "2804ef44530aee8b768d6d116b108c86885e4e775e878a38a20ba720003b99ca"
MODULE = "codegraphcontext/tools/scip_pb2.py"
DIST = "codegraphcontext-0.6.13.dist-info"


def patch_wheel(source: Path, destination: Path) -> None:
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("unexpected upstream CodeGraphContext wheel hash")
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    tree = ast.parse(files[MODULE])
    descriptors = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "FileDescriptor"
    ]
    if len(descriptors) != 1:
        raise ValueError("expected exactly one upstream serialized SCIP descriptor")
    descriptor = next(
        ast.literal_eval(argument.value) for argument in descriptors[0].keywords
        if argument.arg == "serialized_pb"
    )
    if not isinstance(descriptor, bytes):
        raise ValueError("SCIP serialized descriptor must be bytes")
    metadata = files[f"{DIST}/METADATA"].decode()
    old_requirement = "Requires-Dist: protobuf<3.21,>=3.20"
    if metadata.count(old_requirement) != 1:
        raise ValueError("unexpected upstream protobuf requirement")
    files[f"{DIST}/METADATA"] = metadata.replace(
        old_requirement, "Requires-Dist: protobuf>=5.29.6,<8"
    ).encode()
    # This is the same descriptor-pool/builder form emitted by modern protoc.
    files[MODULE] = (
        '# Generated from the exact upstream SCIP serialized descriptor.\n'
        '# Local compatibility patch: see dist-info/adp-protobuf-migration.json.\n'
        'from google.protobuf import descriptor_pool as _descriptor_pool\n'
        'from google.protobuf.internal import builder as _builder\n\n'
        f'DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile({descriptor!r})\n'
        '_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())\n'
        '_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, __name__, globals())\n'
    ).encode()
    files[f"{DIST}/adp-protobuf-migration.json"] = json.dumps({
        "upstream_wheel_sha256": UPSTREAM_SHA256,
        "upstream_scip_module_sha256": hashlib.sha256(archive_module(original)).hexdigest(),
        "serialized_descriptor_sha256": hashlib.sha256(descriptor).hexdigest(),
        "change": "modern descriptor-pool/builder binding; identical serialized schema",
        "upstream_version_unchanged": "0.6.13",
        "protobuf_requirement": ">=5.29.6,<8",
    }, indent=2).encode() + b"\n"
    record = f"{DIST}/RECORD"
    rows = []
    for name, data in sorted(files.items()):
        if name == record:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append((name, "sha256=" + digest, str(len(data))))
    rows.append((record, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    files[record] = output.getvalue().encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rebuilt:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 9, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            rebuilt.writestr(info, data)


def archive_module(original: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        return archive.read(MODULE)


if __name__ == "__main__":
    patch_wheel(Path(sys.argv[1]), Path(sys.argv[2]))
