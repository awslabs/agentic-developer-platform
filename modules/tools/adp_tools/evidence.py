"""Preserve bounded binary or text tool evidence using generic JSON artifacts.

The existing Task artifact API accepts JSON/text, so binary content is encoded in
bounded chunks with a manifest. No domain-specific artifact endpoint is needed.
"""

import base64
import hashlib
import json

CHUNK_BYTES = 512 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024


def put_blob(authority, identity, content, media_type):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BLOB_BYTES:
        raise ValueError("Tool evidence exceeds blob bound")
    parts = []
    for offset in range(0, len(content), CHUNK_BYTES):
        chunk = content[offset : offset + CHUNK_BYTES]
        value = json.dumps(
            {"encoding": "base64", "data": base64.b64encode(chunk).decode()},
            separators=(",", ":"),
        ).encode()
        artifact = authority.put_run_artifact(
            attempt=identity,
            content=value,
            content_type="application/json",
            digest=hashlib.sha256(value).hexdigest(),
        )
        parts.append(
            {
                "artifact_id": artifact.artifact_id,
                "offset": offset,
                "byte_length": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "tool-blob/1",
        "media_type": media_type,
        "encoding": "base64-chunks",
        "byte_length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "parts": parts,
    }
    value = json.dumps(manifest, sort_keys=True).encode()
    artifact = authority.put_run_artifact(
        attempt=identity,
        content=value,
        content_type="application/json",
        digest=hashlib.sha256(value).hexdigest(),
    )
    return {"artifact_id": artifact.artifact_id, **manifest}
