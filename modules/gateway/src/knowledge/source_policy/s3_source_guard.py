#!/usr/bin/env python3
"""Ownership checks for caller-supplied S3 sources (#5658).

The problem
-----------
``ingest-doc.py --source s3://bucket/key`` takes the bucket and key from the
request. The ingestion task's IAM role can read more than the buckets this
pipeline is supposed to serve, so "whatever the role can read" was the effective
authorisation boundary: a request naming another tenant's bucket had its objects
downloaded, converted and published into the shared content store, at which point
they were readable through the Door.

An S3 URI also has more ways to name an object than it first appears:
``s3://bucket/a/../../other/key`` and ``s3://bucket//key`` normalise to keys that
are not the ones a naive prefix comparison saw, so the key is canonicalised before
it is compared.

What this enforces
------------------
The pipeline's bucket is partitioned by the validated ingestion scope: tenant
and personal sources must remain in that tenant's or owner's subtree. Configuring
the bucket is never permission to read every customer in it. Other sources need
an explicit, prefix-bounded shared-source entry in ``s3_source_allowlist``.
Unlabelled objects in the pipeline bucket are not implicitly shared.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .scope import IngestionScope

# S3 bucket naming rules (the DNS-compatible subset, which is all that is valid
# for new buckets). Enforced so a bucket field cannot smuggle a path separator or
# a hostname into code that later interpolates it.
_BUCKET_RE = re.compile(r"\A[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]\Z")


@dataclass(frozen=True)
class S3SourceDecision:
    """Outcome of an S3 source ownership check."""

    allowed: bool
    bucket: str = ""
    key: str = ""
    reason: str = ""
    reason_code: str = "ok"


@dataclass(frozen=True)
class _AllowEntry:
    bucket: str
    prefix: str  # "" means the whole bucket


def canonicalize_key(key: str) -> str | None:
    """Return the canonical form of an S3 key, or None if it escapes the root.

    Percent-decoding happens first because an S3 URI may arrive URL-encoded, and
    ``%2e%2e%2f`` is ``../`` — comparing before decoding compares the wrong
    string. ``posixpath.normpath`` then collapses ``.``, ``..`` and duplicate
    slashes. A key that normalises above the root is rejected rather than clamped:
    clamping silently turns a traversal attempt into a valid read, which is a
    worse outcome than refusing it.
    """
    decoded = unquote(key).lstrip("/")
    if not decoded:
        return None

    normalized = posixpath.normpath(decoded)
    if normalized in (".", "/"):
        return None
    if normalized.startswith("../") or normalized == "..":
        return None

    # normpath strips a meaningful trailing slash; S3 "folder" keys end in one and
    # the caller distinguishes them, so it is restored.
    if decoded.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def parse_allowlist(raw: str, default_bucket: str = "") -> list[_AllowEntry]:
    """Parse ``bucket`` / ``bucket/prefix`` entries from a comma-separated string.

    Entries describe positively identified shared sources. Whole-bucket entries
    are rejected: a shared source must be prefix bounded. ``default_bucket`` is
    retained for API compatibility; ownership there is checked separately.
    """
    entries: list[_AllowEntry] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        item = item.removeprefix("s3://")
        bucket, _, prefix = item.partition("/")
        bucket = bucket.strip().lower()
        if not _BUCKET_RE.match(bucket):
            # A malformed allowlist entry is dropped rather than approximated. It
            # cannot match a real bucket, and guessing what was meant would be
            # guessing about an authorisation rule.
            continue
        prefix = prefix.strip().strip("/")
        canonical = canonicalize_key(prefix)
        if not canonical or canonical != prefix:
            continue
        entries.append(_AllowEntry(bucket=bucket, prefix=prefix))

    return entries


def _prefix_matches(key: str, prefix: str) -> bool:
    """Whether ``key`` lies under ``prefix``, on a path-segment boundary.

    The boundary matters: a raw ``startswith`` would let prefix ``tenant-a`` match
    key ``tenant-abc/secret``, which is a different tenant. Requiring the next
    character to be ``/`` (or an exact match) makes the comparison mean what it
    reads as.
    """
    if not prefix:
        return True
    prefix = prefix.rstrip("/")
    return key == prefix or key.startswith(prefix + "/")


def check_s3_source(
    s3_uri: str,
    allowlist_raw: str = "",
    default_bucket: str = "",
    *,
    scope: IngestionScope | None = None,
) -> S3SourceDecision:
    """Decide whether ``s3_uri`` may be read as a document source."""
    if not s3_uri.startswith("s3://"):
        return S3SourceDecision(
            allowed=False,
            reason=f"not an S3 URI: {s3_uri!r}",
            reason_code="not_s3_uri",
        )

    parts = urlsplit(s3_uri)
    bucket = (parts.netloc or "").lower()

    # Userinfo or a port in the authority means the "bucket" is not a bucket.
    # Rejected explicitly so such a URI cannot be reinterpreted downstream.
    if "@" in bucket or ":" in bucket:
        return S3SourceDecision(
            allowed=False,
            reason="S3 URI authority contains userinfo or a port",
            reason_code="malformed_bucket",
        )

    if not _BUCKET_RE.match(bucket):
        return S3SourceDecision(
            allowed=False,
            reason=f"invalid S3 bucket name: {bucket!r}",
            reason_code="malformed_bucket",
        )

    key = canonicalize_key(parts.path)
    if key is None:
        return S3SourceDecision(
            allowed=False,
            bucket=bucket,
            reason="S3 key is empty or escapes the bucket root",
            reason_code="malformed_key",
        )

    # These namespaces contain customer-owned content. Even an accidentally
    # broad operator allowlist must not override their owner boundary.
    if bucket == default_bucket.strip().lower():
        root = ""
        if scope is not None:
            if scope.visibility == "tenant":
                component = scope.tenant_id
                kind = "tenants"
            elif scope.visibility == "personal":
                component = scope.owner_sub
                kind = "users"
            else:
                component = None
                kind = ""
            if isinstance(component, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]*", component):
                root = f"{kind}/{component}"
        if root and _prefix_matches(key, root):
            return S3SourceDecision(allowed=True, bucket=bucket, key=key)
        if key.split("/", 1)[0] in {"tenants", "users"}:
            return S3SourceDecision(
                allowed=False,
                bucket=bucket,
                key=key,
                reason="S3 source is outside the verified tenant or owner scope",
                reason_code="scope_not_allowed",
            )
        # Shared paths still require an explicit prefix entry below. The bucket
        # itself cannot provide evidence of shared ownership.

    entries = parse_allowlist(allowlist_raw)
    if not entries:
        return S3SourceDecision(
            allowed=False,
            bucket=bucket,
            key=key,
            reason="no S3 source allowlist is configured",
            reason_code="no_allowlist",
        )

    for entry in entries:
        if entry.bucket == bucket and _prefix_matches(key, entry.prefix):
            return S3SourceDecision(allowed=True, bucket=bucket, key=key)

    return S3SourceDecision(
        allowed=False,
        bucket=bucket,
        key=key,
        reason=f"s3://{bucket}/{key} is not within any allowed bucket/prefix",
        reason_code="bucket_not_allowed",
    )


def safe_download_name(candidate: str, fallback: str = "document") -> str:
    """Reduce a caller-influenced filename to a single safe path component.

    Applied to ``Content-Disposition`` filenames and S3 key basenames before they
    are joined to a download directory. ``basename`` alone is not enough — a
    Windows-style ``..\\..\\etc\\passwd`` has no POSIX separator, so basename
    returns it whole and the join then escapes the directory.
    """
    name = unquote(candidate or "").strip().strip('"').strip("'")
    name = name.replace("\\", "/")
    name = posixpath.basename(name)
    name = name.replace("\x00", "")

    if name in ("", ".", ".."):
        return fallback

    # Collapse anything outside a conservative filename alphabet.
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = name.lstrip(".") or fallback
    return name[:200]
