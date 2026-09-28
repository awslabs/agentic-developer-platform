"""Negative cache for installations the gateway authoritatively does not know.

Issue #4047 (#2724 slice C). Every webhook delivery from an installation that is
not a known ADP tenant currently re-asks the gateway the same question
(``POST /internal/v1/resolve-installation``), which filters organizations in
Python — a full table read per call, on the webhook hot path. An attacker with a
public-App install can therefore amplify one cheap webhook into unbounded
gateway load.

This module memoizes the ONE authoritative answer that is safe to memoize:
``not_found``. It writes a short-lived row into the existing identity-index
table (PK ``identity_type`` / SK ``identity_value``), which already has TTL
enabled on attribute ``ttl`` — so no new table and no new IAM action beyond the
``GetItem``/``PutItem`` the Lambda already holds on that table.

Row shape::

    {
        "identity_type":  "github_installation_negative",
        "identity_value": "<installation_id>",
        "ttl":            <epoch seconds, now + TTL>,
        "cached_at":      "<ISO8601>",
    }

The key is a DISTINCT ``identity_type`` from the ``github_installation_id``
forward rows that dispatch routes on, so a negative row can never be mistaken
for a tenant mapping by any existing reader. Readers that enumerate forward rows
filter on ``identity_type = "github_installation_id"``
(``installation_resolver._forward_scan_fallback``,
``scripts/backfill-installation-tenants.py``) and are unaffected.

**What must NEVER be cached: the ``error`` state.** ``error`` means "we could not
find out" (gateway down, missing config, timeout). Caching it would convert a
transient gateway outage into a TTL-long lockout of legitimate new tenants —
exactly the availability failure that slice A's three-state split exists to
prevent. Only an authoritative gateway 404 is cacheable.

**TTL is validated on read.** DynamoDB's TTL deletion is asynchronous and
best-effort — AWS documents deletion "typically within 48 hours" of expiry, so a
``get_item`` can and does return rows whose ``ttl`` has already passed. Treating
a row's mere presence as a cache hit would let the cache outlive its configured
TTL by hours and delay legitimate onboarding retries far beyond the "minutes"
this slice promises. ``is_negative_cached`` therefore compares ``ttl`` against
the current time itself and treats an expired (or malformed, or absent) ``ttl``
as a MISS. DDB's reaper is a storage-cost optimization here, not the correctness
mechanism.

Every function is best-effort: a DynamoDB failure degrades to "not cached" (we
re-ask the gateway, i.e. today's behavior) and never propagates. A cache is an
optimization; it must not be able to fail a webhook.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Distinct PK value — never collides with the github_installation_id rows that
# carry real tenant mappings.
NEGATIVE_IDENTITY_TYPE = "github_installation_negative"

# Keep this SHORT (minutes). It is the delay a legitimate brand-new tenant can
# experience between installing and their first webhook resolving, so it trades
# probe-amplification protection against onboarding latency. 5 minutes bounds a
# hostile event stream to ~1 gateway call per installation per 5 minutes while
# staying inside the window a human spends finishing an install flow.
DEFAULT_TTL_SECONDS = 300


def ttl_seconds() -> int:
    """Return the configured negative-cache TTL in seconds.

    ``INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS <= 0`` disables the cache entirely
    (see :func:`enabled`) — an ops kill-switch that needs no second flag. A
    malformed value falls back to the default rather than disabling the cache
    silently: a typo in a TTL should not quietly turn a security-relevant
    mitigation off.
    """
    raw = os.environ.get("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Invalid INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS=%r — using default %ds",
            raw,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS


def enabled() -> bool:
    """Whether the negative cache is active (TTL > 0)."""
    return ttl_seconds() > 0


def _key(installation_id: str | int) -> dict:
    return {
        "identity_type": NEGATIVE_IDENTITY_TYPE,
        "identity_value": str(installation_id),
    }


def is_negative_cached(table, installation_id: str | int) -> bool:
    """Return True iff a live negative row exists for ``installation_id``.

    A row whose ``ttl`` has already passed is a MISS even though DynamoDB has
    not reaped it yet (see the module docstring on lazy TTL deletion).

    Best-effort: any DynamoDB error returns False, so the caller falls through
    to the gateway exactly as it does today.
    """
    if not enabled():
        return False

    try:
        item = table.get_item(Key=_key(installation_id)).get("Item")
    except Exception as exc:
        logger.warning(
            "Negative-cache read failed for installation_id=%s: %s "
            "(treating as miss — will consult the gateway)",
            installation_id,
            exc,
        )
        return False

    if not item:
        return False

    # DDB may hand back an expired row; enforce the TTL ourselves.
    raw_ttl = item.get("ttl")
    try:
        expires_at = int(raw_ttl)
    except (TypeError, ValueError):
        # No/garbage ttl — cannot prove the row is live, so do not trust it.
        logger.warning(
            "Negative-cache row for installation_id=%s has invalid ttl=%r — "
            "treating as miss",
            installation_id,
            raw_ttl,
        )
        return False

    if expires_at <= int(time.time()):
        logger.info(
            "Negative-cache row for installation_id=%s expired at %d (DDB has not "
            "reaped it yet) — treating as miss",
            installation_id,
            expires_at,
        )
        return False

    logger.info(
        "Negative-cache HIT for installation_id=%s (expires_at=%d) — skipping "
        "gateway resolve-installation call",
        installation_id,
        expires_at,
    )
    return True


def record_not_found(table, installation_id: str | int) -> None:
    """Cache the authoritative ``not_found`` answer for ``installation_id``.

    Call this ONLY for the three-state client's ``not_found`` state (an
    authoritative gateway 404). Never for ``error`` — see the module docstring.

    Best-effort: failures are logged and swallowed. A cache that cannot be
    written just means the next event re-asks the gateway.
    """
    if not enabled():
        return

    expires_at = int(time.time()) + ttl_seconds()
    try:
        table.put_item(
            Item={
                **_key(installation_id),
                "ttl": expires_at,
                "cached_at": datetime.now(UTC).isoformat(),
            }
        )
        logger.info(
            "Negative-cached unknown installation_id=%s for %ds (expires_at=%d)",
            installation_id,
            ttl_seconds(),
            expires_at,
        )
    except Exception as exc:
        logger.warning(
            "Failed to write negative-cache row for installation_id=%s: %s",
            installation_id,
            exc,
        )


def invalidate(table, installation_id: str | int) -> None:
    """Drop any negative row for ``installation_id`` (fresh install must re-resolve).

    Implemented as an overwrite with an already-past ``ttl`` rather than a
    ``DeleteItem``. Two reasons:

      * The Lambda's ``IdentityIndexReadWrite`` policy grants ``GetItem``/
        ``PutItem`` on the identity-index but NOT ``DeleteItem``. Adding delete
        would let the webhook path remove *any* row on that table — including
        the live ``github_installation_id`` rows dispatch routes on. Widening
        write authority on the tenant-mapping table is the wrong trade for a
        cache invalidation, especially in a security-hardening slice.
      * Because :func:`is_negative_cached` validates ``ttl`` on read, a past-TTL
        row is already an immediate miss. The overwrite is exactly as effective
        as a delete, and DDB's reaper collects the row on its own schedule.

    Best-effort: failures are logged and swallowed.
    """
    if not enabled():
        return

    try:
        table.put_item(
            Item={
                **_key(installation_id),
                # Already expired: an immediate miss on the read path.
                "ttl": int(time.time()) - 1,
                "cached_at": datetime.now(UTC).isoformat(),
                "invalidated": True,
            }
        )
        logger.info(
            "Invalidated negative-cache row for installation_id=%s", installation_id
        )
    except Exception as exc:
        logger.warning(
            "Failed to invalidate negative-cache row for installation_id=%s: %s",
            installation_id,
            exc,
        )
