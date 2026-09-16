"""Platform-membership eligibility read for the two auth Lambdas (Issue #4849).

Answers exactly one question, from DynamoDB, with no gateway call:

    does this GitHub identity hold at least one platform org membership?

The answer comes from the ``member_org_ids`` attribute on the identity-index
rows — the same projection the webhook-ingress Lambda has read fail-closed for a
cross-tenant authorization decision since #3134. This module adds a *reader*; it
does not invent a mechanism.

Why DynamoDB and not the gateway
--------------------------------
Standing platform rule from the 2026-07-05 dispatch outage: a non-VPC Lambda must
never synchronously call the internal gateway. Neither auth Lambda has a
``vpc_config``, a ``GATEWAY_API_URL``, or an admin-token ARN, and the nearest
precedent is deliberately switched off (``docs/design-notes/2951-github-org-to-adp-tenant.md:414``:
*"GATEWAY_API_URL intentionally stays "" — the Lambda must never call the internal
ALB"*). Cognito's synchronous trigger budget is 5s, non-configurable and
non-retryable, and already partly spent on a GitHub call — a VPC round-trip plus
ENI cold start inside that window would turn a cold-start blip into a denied
sign-in. Reading DynamoDB from the pre-signup trigger is already the shipped
pattern (its explicit-allowlist check does exactly that).

Fail-closed, and *stricter* than identity_resolver — deliberately
----------------------------------------------------------------
``webhook-ingress/lambda/common/identity_resolver.py`` defaults a missing
``member_org_ids`` to ``[the row's own org_id]``. That is correct for **its**
question ("may this user trigger work in org X?"), where the identity row's home
org is itself evidence of one membership.

It would be fail-**open** for **our** question. An identity row can exist for a
user who holds no ``TenantMembership`` at all (identity rows are written by
identity-creation paths, memberships by membership paths), so inferring "has a
membership" from "has an identity row" would answer yes for a user who has none.
So: a missing row, a missing attribute, and an empty list all mean NOT_ELIGIBLE.

The cost of that strictness is that a user whose membership predates consistent
write-through reads as ineligible until reconciled — which is why running
``scripts/backfill_member_org_ids.py`` is a release gate before any caller lets
this decide a sign-in. See ``docs/design-notes/4849-membership-eligibility-projection.md``.

Tri-state, not a bool
---------------------
``UNAVAILABLE`` (DDB error, unconfigured table) is reported distinctly from
``NOT_ELIGIBLE`` (read succeeded, no membership). Callers must deny on both, but
collapsing them makes a misconfigured table indistinguishable from a legitimate
denial — the exact ambiguity #3986 was filed to fix. Mirrors the
``ALLOWED``/``DENIED``/``UNVERIFIED`` shape of ``lambda/github-auth-broker/allowlist.py``.
"""

import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# Verdicts. Callers deny on NOT_ELIGIBLE *and* UNAVAILABLE; the split exists so
# "could not check" is attributable separately from "checked, not a member".
ELIGIBLE = "eligible"
NOT_ELIGIBLE = "not_eligible"
UNAVAILABLE = "unavailable"

# identity_type of user rows in the legacy table. Writer-side constant is
# IdentityIndexWriter.GITHUB_USER_TYPE (src/admin/identity/identity_index_writer.py);
# webhook-ingress's identity_resolver reads the same literal. All three must agree.
_GITHUB_USER_TYPE = "github_user"

_MEMBER_ORG_IDS_ATTR = "member_org_ids"

_dynamodb = None

# Cognito's synchronous trigger budget is 5s, non-retryable, and already partly
# spent on a GitHub call before this read runs (see module docstring). boto3's
# defaults (60s connect/read, retries) would let a DDB hang outlive the trigger
# many times over — and a try/except cannot catch a hang, so the try/except
# around the read only helps if the client gives up fast. Worst case with this
# config is ~2s (1s connect + 1s read, single attempt), leaving the caller time
# to map the failure to UNAVAILABLE instead of dying as a trigger timeout.
_BOTO_CONFIG = Config(
    connect_timeout=1,
    read_timeout=1,
    retries={"total_max_attempts": 1},
)


def _get_resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
    return _dynamodb


def _v2_read_enabled() -> bool:
    """Whether to try the v2 user-identity-index table first.

    Same env var and default as webhook-ingress's identity_resolver (#537), so a
    single flag flip moves every reader together.
    """
    return os.environ.get("USER_IDENTITY_INDEX_V2_READ", "false").lower() == "true"


def _read_v2(provider_user_id: str, provider: str) -> dict | None:
    """Read the row from the v2 table (PK=provider, SK=provider_user_id)."""
    table_name = os.environ.get("USER_IDENTITY_INDEX_TABLE", "")
    if not table_name:
        return None
    try:
        resp = _get_resource().Table(table_name).get_item(Key={"provider": provider, "provider_user_id": provider_user_id})
        return resp.get("Item")
    except Exception as e:
        # Non-fatal: the legacy table is the fallback and is still the
        # authoritative read until the v2 cutover completes.
        logger.warning(
            "membership-eligibility: v2 read failed for %s|%s: %s (falling back to legacy table)",
            provider,
            provider_user_id,
            e,
        )
        return None


def _read_legacy(provider_user_id: str) -> dict | None:
    """Read the row from the legacy identity-index table.

    Raises on a DDB error — the caller maps that to UNAVAILABLE. This read is
    load-bearing, so unlike the v2 read its failure must not look like a miss.
    """
    table_name = os.environ.get("IDENTITY_INDEX_TABLE", "")
    if not table_name:
        raise RuntimeError("IDENTITY_INDEX_TABLE is not configured")
    resp = _get_resource().Table(table_name).get_item(Key={"identity_type": _GITHUB_USER_TYPE, "identity_value": provider_user_id})
    return resp.get("Item")


def check_platform_membership(provider_user_id: str, provider: str = "github") -> str:
    """Return whether ``provider_user_id`` holds at least one platform membership.

    Args:
        provider_user_id: The provider's own id for the user — for GitHub, the
            numeric account id as a string (NOT the login). The projection is
            keyed on the id because logins are renameable.
        provider: Identity provider. Only ``github`` is written today.

    Returns:
        ``ELIGIBLE`` when the projection lists at least one org.
        ``NOT_ELIGIBLE`` when the read succeeded and it does not (no row, no
        attribute, or an empty list).
        ``UNAVAILABLE`` when the projection could not be read at all.
    """
    if not provider_user_id:
        # Not UNAVAILABLE: nothing was unreachable. A caller with no provider id
        # has no identity to check, which is a denial.
        logger.warning("membership-eligibility: empty provider_user_id; not eligible")
        return NOT_ELIGIBLE

    provider_user_id = str(provider_user_id)

    try:
        item = _read_v2(provider_user_id, provider) if _v2_read_enabled() else None
        if item is None:
            item = _read_legacy(provider_user_id)
    except Exception as e:
        logger.error(
            "membership-eligibility: projection read failed for %s|%s: %s",
            provider,
            provider_user_id,
            e,
        )
        return UNAVAILABLE

    if not item:
        logger.info(
            "membership-eligibility: no identity row for %s|%s; not eligible",
            provider,
            provider_user_id,
        )
        return NOT_ELIGIBLE

    if _MEMBER_ORG_IDS_ATTR not in item:
        # Fail-closed, and stricter than identity_resolver on purpose — see the
        # module docstring. Do NOT substitute the row's own org_id here.
        logger.info(
            "membership-eligibility: %s|%s has an identity row but no %s attribute; "
            "not eligible (run scripts/backfill_member_org_ids.py if this user predates write-through)",
            provider,
            provider_user_id,
            _MEMBER_ORG_IDS_ATTR,
        )
        return NOT_ELIGIBLE

    member_org_ids = item.get(_MEMBER_ORG_IDS_ATTR) or []
    if not isinstance(member_org_ids, list):
        logger.error(
            "membership-eligibility: %s|%s has a non-list %s (%r); not eligible",
            provider,
            provider_user_id,
            _MEMBER_ORG_IDS_ATTR,
            type(member_org_ids).__name__,
        )
        return NOT_ELIGIBLE

    orgs = [o for o in member_org_ids if o]
    if not orgs:
        logger.info(
            "membership-eligibility: %s|%s has an empty %s; not eligible",
            provider,
            provider_user_id,
            _MEMBER_ORG_IDS_ATTR,
        )
        return NOT_ELIGIBLE

    logger.info(
        "membership-eligibility: %s|%s holds %d membership(s); eligible",
        provider,
        provider_user_id,
        len(orgs),
    )
    return ELIGIBLE
