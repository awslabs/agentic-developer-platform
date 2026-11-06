"""Bounded GitHub Actions log extraction; provider URLs and tokens stay in gateway."""

from __future__ import annotations

import heapq
import io
import re
import zipfile
from urllib.parse import urlsplit

import httpx

from src.agentauth.github_operations import OperationRefusedError

MAX_LOG_TAIL_BYTES = 64 * 1024
DEFAULT_LOG_EXCERPT_BYTES = 8 * 1024
MAX_LOG_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_LOG_TEXT_BYTES = 16 * 1024 * 1024
MAX_LOG_FILES = 500

_SECRET_PATTERNS = (
    re.compile(rb"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}", re.IGNORECASE),
    re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(rb"\bBearer\s+[^\s'\"]+", re.IGNORECASE),
    re.compile(rb"\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
)


def redact_text(value: str, *, max_bytes: int = 256) -> str:
    text = value.encode("utf-8")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(b"[REDACTED]", text)
    return text[:max_bytes].decode("utf-8", errors="ignore")


def redact_log_tail(archive: bytes, *, failure_focus: bool = False) -> tuple[str, bool]:
    if len(archive) > MAX_LOG_ARCHIVE_BYTES:
        raise OperationRefusedError("log archive exceeds bound")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            files = [entry for entry in contents.infolist() if not entry.is_dir()]
            if not files or len(files) > MAX_LOG_FILES or sum(entry.file_size for entry in files) > MAX_LOG_TEXT_BYTES:
                raise OperationRefusedError("log archive exceeds bound")
            parts = []
            for entry in files:
                if entry.file_size > MAX_LOG_TEXT_BYTES or entry.flag_bits & 0x1:
                    raise OperationRefusedError("log archive is unavailable")
                with contents.open(entry) as source:
                    part = source.read(entry.file_size + 1)
                if len(part) != entry.file_size:
                    raise OperationRefusedError("log archive is invalid")
                parts.append((f"\n--- {entry.filename} ---\n".encode() if failure_focus else b"") + part)
            text = b"\n".join(parts)
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError):
        raise OperationRefusedError("log archive is invalid") from None
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(b"[REDACTED]", text)
    if failure_focus:
        # SDK-only workers cannot read files where oversized tool responses are
        # persisted. Find failure context before tailing: successful jobs often
        # follow the first failed job and would hide its error in a plain tail.
        def failure_order(match):
            prefix = text[max(0, match.start() - 80) : match.start()]
            timestamp = re.search(rb"\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s*$", prefix)
            return (timestamp.group() if timestamp else b"9999", match.start())

        # Archives are not ordered by job time and include aggregate and step
        # copies. Prioritize the earliest failures over downstream guard errors.
        failures = heapq.nsmallest(8, re.finditer(rb"##\[error\]|::error(?:\s|::)", text), key=failure_order)
        windows, ranges = [], []
        for match in failures:
            if any(start <= match.start() < end for start, end in ranges):
                continue
            start, end = max(0, match.start() - 2048), min(len(text), match.end() + 768)
            windows.append(text[start:end])
            ranges.append((start, end))
            if len(windows) == 4:
                break
        excerpt = b"\n[... failure context ...]\n".join(windows) if windows else text[-DEFAULT_LOG_EXCERPT_BYTES:]
        encoded = excerpt.decode("utf-8", errors="replace").encode("utf-8")
        bounded = encoded[:DEFAULT_LOG_EXCERPT_BYTES].decode("utf-8", errors="ignore")
        return bounded, excerpt != text or len(encoded) > DEFAULT_LOG_EXCERPT_BYTES
    truncated = len(text) > MAX_LOG_TAIL_BYTES
    encoded = text[-MAX_LOG_TAIL_BYTES:].decode("utf-8", errors="replace").encode("utf-8")
    return encoded[-MAX_LOG_TAIL_BYTES:].decode("utf-8", errors="ignore"), truncated


def _approved_archive_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and port in (None, 443)
        # GitHub serves run-log archives from rotating hosts under
        # actions.githubusercontent.com (pipelines.*, results-receiver.*) or Azure
        # blob storage; pinning one hostname refused every log read in October 2026.
        and (host.endswith(".blob.core.windows.net") or host == "actions.githubusercontent.com" or host.endswith(".actions.githubusercontent.com"))
    )


async def fetch_log_tail(provider, binding, *, run_id: int, token: str, reauthorize, failure_focus: bool = False):
    response = await provider.request(
        binding,
        "GET",
        f"/repos/{binding.repo}/actions/runs/{run_id}/logs",
        token=token,
        max_bytes=MAX_LOG_ARCHIVE_BYTES,
        allow_download_redirect=True,
    )
    if response.status_code == 200:
        archive = response.content
    elif response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location", "")
        if not _approved_archive_url(location):
            raise OperationRefusedError("log archive location is unavailable")
        await reauthorize()
        archive_bytes = bytearray()
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", location) as redirected:
                if redirected.status_code != 200:
                    raise OperationRefusedError("log archive is unavailable")
                async for chunk in redirected.aiter_bytes():
                    if len(archive_bytes) + len(chunk) > MAX_LOG_ARCHIVE_BYTES:
                        raise OperationRefusedError("log archive exceeds bound")
                    archive_bytes.extend(chunk)
        archive = bytes(archive_bytes)
    else:
        raise OperationRefusedError("log archive is unavailable")
    return redact_log_tail(archive, failure_focus=failure_focus)
