"""Tenant-scoped object access resolver for cyber analysis workers.

Issue #5616 (S17), covering findings #4729 and #4730.

Every object a worker fetches — the sample under analysis and, in Mode B, the
analysis script — must be provably bound to the job that asked for it. Before
this module, each handler did its own string surgery on a caller-supplied
location::

    bucket, key = sample_s3_uri.replace("s3://", "", 1).split("/", 1)

That accepts any location the message names. Combined with a worker IAM grant
whose key pattern wildcarded the org segment (``.../o/*/in/*``), one tenant's
analysis job could read another tenant's uploaded attachment and have its
contents returned in an ordinary-looking findings report.

This module is the single place where "may this job touch this object?" is
decided. Both workers call it; any future worker must too. It is deliberately
the only code path that parses a location, so the two handlers cannot drift.

Design notes that matter for review:

* The permitted prefix is **derived** from trusted job identity, never read
  from the location. A location is a *claim* to be verified, not an
  instruction to follow.
* Containment is compared **segment-wise**, never with ``str.startswith``.
  A raw string prefix test would accept ``o/acme-evil/...`` as inside
  ``o/acme/...``. See ``_is_within``.
* Rejections carry a reason code and never echo the rejected location.
  Echoing it would turn a blocked cross-tenant read into a disclosure of
  another tenant's key naming — moving the problem rather than closing it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Reason codes — stable strings. Callers surface these to the requester; they
# must stay free of any caller-supplied value.
# ---------------------------------------------------------------------------

REASON_MISSING_IDENTITY = "identity_missing"
REASON_MALFORMED_IDENTITY = "identity_malformed"
REASON_MALFORMED_LOCATION = "location_malformed"
REASON_BUCKET_NOT_ALLOWED = "bucket_not_allowed"
REASON_OUTSIDE_TENANT_PREFIX = "outside_tenant_prefix"
REASON_SCRIPT_PREFIX_NOT_ALLOWED = "script_prefix_not_allowed"

# Identity segments become path components of an S3 key, so they are
# constrained to a conservative character set. This rejects "..", "/", and
# anything else that could relocate the derived prefix, before it is ever
# used to build a path.
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Key layout established by the chat artifact store. Keep in sync with
# modules/agent-factory/agent/src/complex-task-chat/artifacts/s3-artifact-store.ts
_TENANT_PREFIX_TEMPLATE = "o/{org_id}/t/{team_id}/u/{user_id}/"

# Mode B scripts must live under a prefix the pipeline designates for scripts.
# Passing the tenant check proves whose space an object is in; this additionally
# constrains *where within that space* an executable may be fetched from.
DEFAULT_SCRIPT_PREFIXES = ("scripts/",)


class AccessDenied(Exception):
    """A requested object is not bound to the authorized job context.

    ``reason`` is a stable code safe to surface. The rejected location is
    intentionally not carried on this exception.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"access denied: {reason}" + (f" ({detail})" if detail else ""))


@dataclass(frozen=True)
class JobContext:
    """Trusted identity for one analysis job.

    Built from a broker-only SQS manifest after registered_job() checks its
    lifetime and stage. The queue resource policy denies every other producer. The worker treats these as
    the authority and derives the permitted keyspace from them.
    """

    org_id: str
    team_id: str
    user_id: str

    @property
    def tenant_prefix(self) -> str:
        """The only key prefix this job may read from."""
        return _TENANT_PREFIX_TEMPLATE.format(
            org_id=self.org_id, team_id=self.team_id, user_id=self.user_id
        )


@dataclass(frozen=True)
class ObjectRef:
    """A bucket/key pair that has passed every authorization check."""

    bucket: str
    key: str


def allowed_buckets() -> frozenset[str]:
    """Buckets the workers may read, from configuration.

    Comes from ``CYBER_ALLOWED_BUCKETS`` (comma-separated). An empty or unset
    value yields an empty set, so every location is refused — configuration
    failure fails closed rather than falling back to "allow anything".
    """
    raw = os.environ.get("CYBER_ALLOWED_BUCKETS", "")
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


def script_prefixes() -> tuple[str, ...]:
    """Key prefixes under which an executable analysis script may live."""
    raw = os.environ.get("CYBER_SCRIPT_PREFIXES", "")
    configured = tuple(p.strip() for p in raw.split(",") if p.strip())
    return configured or DEFAULT_SCRIPT_PREFIXES


def job_context(body: dict) -> JobContext:
    """Extract and validate trusted identity from a job body.

    Raises ``AccessDenied`` when identity is absent or not well-formed. A job
    that does not say who it acts for cannot have its permitted keyspace
    derived, so it is refused rather than analysed with an assumed identity.
    """
    if not isinstance(body, dict):
        raise AccessDenied(REASON_MALFORMED_IDENTITY, "job body is not an object")

    fields = {}
    for name in ("org_id", "team_id", "user_id"):
        value = body.get(name)
        if value is None or value == "":
            raise AccessDenied(REASON_MISSING_IDENTITY, f"{name} absent")
        if not isinstance(value, str):
            raise AccessDenied(REASON_MALFORMED_IDENTITY, f"{name} not a string")
        if not _IDENTITY_RE.match(value):
            # Do not echo the value — it is caller-influenced.
            raise AccessDenied(REASON_MALFORMED_IDENTITY, f"{name} has illegal characters")
        fields[name] = value

    return JobContext(**fields)


def _split_s3_uri(uri: object) -> tuple[str, str]:
    """Parse an ``s3://bucket/key`` location with a strict parser.

    Uses ``urlparse`` rather than string surgery so that a location which does
    not cleanly resolve is refused instead of being coerced into something
    plausible.
    """
    if not isinstance(uri, str) or not uri:
        raise AccessDenied(REASON_MALFORMED_LOCATION, "location absent or not a string")

    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise AccessDenied(REASON_MALFORMED_LOCATION, "scheme is not s3")
    if parsed.params or parsed.query or parsed.fragment:
        raise AccessDenied(REASON_MALFORMED_LOCATION, "location carries extra components")

    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise AccessDenied(REASON_MALFORMED_LOCATION, "bucket or key empty")

    # Reject relative segments outright. Segment-wise containment alone is not
    # enough: a ".." placed *after* the tenant prefix leaves the leading
    # segments matching, so the boundary test would pass while any consumer
    # that normalises the path would resolve outside the tenant space. S3 keys
    # are opaque strings and legitimate keys here never contain these, so
    # refusing is free.
    if any(segment in (".", "..") for segment in key.split("/")):
        raise AccessDenied(REASON_MALFORMED_LOCATION, "key contains relative segments")

    return bucket, key


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _is_within(key: str, prefix: str) -> bool:
    """Whole-segment containment test.

    This is the check that actually enforces the tenant boundary, so it does
    not use ``str.startswith``: ``"o/acme-evil/..."`` starts with ``"o/acme"``
    but belongs to a different organisation.

    Note this test alone does not stop traversal — a ".." after the prefix
    leaves the leading segments matching. Relative segments are rejected
    earlier, in ``_split_s3_uri``.
    """
    key_segments = _segments(key)
    prefix_segments = _segments(prefix)

    if len(key_segments) <= len(prefix_segments):
        # Must be strictly deeper than the prefix — the prefix itself is a
        # directory, not a readable object.
        return False
    return key_segments[: len(prefix_segments)] == prefix_segments


def resolve_sample(body: dict, ctx: JobContext) -> ObjectRef:
    """Authorize the sample location carried by a job.

    Verifies the claimed location resolves inside the requesting org/team/user
    space and names a configured bucket. Raises ``AccessDenied`` before the
    caller has any chance to fetch.
    """
    bucket, key = _split_s3_uri(body.get("sample_s3_uri"))
    return _authorize(bucket, key, ctx)


def resolve_script(body: dict, ctx: JobContext) -> ObjectRef:
    """Authorize the Mode B script location carried by a job.

    Applies the identical tenant rule as the sample — the most likely way this
    fix fails is guarding one dispatch path and missing another — and then
    additionally requires the key to sit under a pipeline-designated script
    prefix, so an arbitrary object inside the tenant's own space cannot be
    executed.
    """
    bucket, key = _split_s3_uri(body.get("script_s3_uri"))
    ref = _authorize(bucket, key, ctx)

    # Take the remainder after the tenant prefix by segment count rather than
    # by string length, so the comparison stays consistent with _is_within.
    depth = len(_segments(ctx.tenant_prefix))
    relative = "/".join(_segments(key)[depth:])
    if not any(_is_within(relative, prefix) for prefix in script_prefixes()):
        raise AccessDenied(REASON_SCRIPT_PREFIX_NOT_ALLOWED, "not under a script prefix")
    return ref


def _authorize(bucket: str, key: str, ctx: JobContext) -> ObjectRef:
    if bucket not in allowed_buckets():
        raise AccessDenied(REASON_BUCKET_NOT_ALLOWED, "bucket not in configured set")
    if not _is_within(key, ctx.tenant_prefix):
        raise AccessDenied(REASON_OUTSIDE_TENANT_PREFIX, "key outside requester space")
    return ObjectRef(bucket=bucket, key=key)
