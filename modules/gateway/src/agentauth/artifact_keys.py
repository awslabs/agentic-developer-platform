"""Server-derived artifact namespaces; no caller-selected run, tenant or key."""

import hashlib
import re


def artifact_prefix(record) -> str:
    tenant = hashlib.sha256(record.tenant_id.encode()).hexdigest()
    run = hashlib.sha256(record.invocation_id.encode()).hexdigest()
    return f"runs/{tenant}/{run}/attempt-{record.current_attempt}/"


def own_transcript_key(record, key: str) -> bool:
    return isinstance(key, str) and re.fullmatch(re.escape(artifact_prefix(record)) + r"transcript/[a-f0-9]{64}\.md", key) is not None
