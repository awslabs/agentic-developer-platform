"""Identity resolution: maps channel identifiers to tenant + user via identity-index.

Phase B.1 (Issue #402): Replaces tenant_resolver.py.
Issue #537: Feature-flag-gated reads from new user-identity-index table.
Issue #702: Postgres safety-net via POST /internal/v1/resolve-user — cross-validates
v2 DDB result against Postgres, trusts Postgres on disagreement (IdentityIndexDrift).

Reads from adp-dev-identity-index DynamoDB table (PK=identity_type, SK=identity_value).
When USER_IDENTITY_INDEX_V2_READ=true, reads user from new table first with fallback.
When RESOLVE_CANONICAL_VIA_GATEWAY=true, cross-validates against Postgres.

Flow:
  1. Resolve tenant via identity_type="github_installation_id"
  2. Resolve sender via new table (flag-gated) or old table (fallback)
  3. Postgres safety-net: cross-validate canonical user_id (flag-gated)
  4. Cross-check: sender's org_id must match installation's org_id
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import boto3

logger = logging.getLogger(__name__)

# Env var set by Terraform in lambdas.tf
IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "")
USER_IDENTITY_INDEX_TABLE = os.environ.get("USER_IDENTITY_INDEX_TABLE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = None
_cloudwatch = None


@dataclass
class ResolvedIdentity:
    """Result of a successful identity resolution."""

    tenant_id: str
    org_id: str
    user_id: str
    user_provisioning_mode: str  # "strict" | "auto_provision"


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(IDENTITY_INDEX_TABLE)


def _get_user_identity_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table_name = USER_IDENTITY_INDEX_TABLE or "adp-dev-user-identity-index"
    return _dynamodb.Table(table_name)


def _get_cloudwatch():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    return _cloudwatch


def _v2_read_enabled() -> bool:
    """Check if reads from user-identity-index are enabled."""
    return os.environ.get("USER_IDENTITY_INDEX_V2_READ", "false").lower() == "true"


def _resolve_canonical_via_gateway_enabled() -> bool:
    """Check if Postgres cross-validation via gateway is enabled."""
    return os.environ.get("RESOLVE_CANONICAL_VIA_GATEWAY", "false").lower() == "true"


def _emit_cross_tenant_metric() -> None:
    """Emit CloudWatch metric on cross-tenant mismatch."""
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "CrossTenantMismatch",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit CrossTenantMismatch metric: %s", e)


def _emit_identity_index_drift_metric() -> None:
    """Emit CloudWatch metric when v2 DDB and Postgres disagree on user_id."""
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "IdentityIndexDrift",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit IdentityIndexDrift metric: %s", e)


def _resolve_user_from_new_table(sender_id: int) -> dict | None:
    """Attempt to resolve user from the new user-identity-index table."""
    try:
        table = _get_user_identity_table()
        resp = table.get_item(
            Key={
                "provider": "github",
                "provider_user_id": str(sender_id),
            }
        )
        return resp.get("Item")
    except Exception as e:
        logger.warning(
            "user-identity-index read failed for sender_id=%d: %s (falling back to old table)",
            sender_id,
            e,
        )
        return None


def _resolve_user_from_old_table(sender_id: int) -> dict | None:
    """Resolve user from the existing identity-index table."""
    table = _get_table()
    resp = table.get_item(
        Key={
            "identity_type": "github_user",
            "identity_value": str(sender_id),
        }
    )
    return resp.get("Item")


def resolve(
    installation_id: int, sender_id: int
) -> tuple[ResolvedIdentity | None, str]:
    """Resolve installation + sender to a full identity.

    Args:
        installation_id: GitHub App installation ID from webhook payload.
        sender_id: GitHub sender.id from webhook payload.

    Returns:
        Tuple of (ResolvedIdentity or None, outcome_reason).
        outcome_reason is one of: "ok", "unknown_installation", "unknown_user",
        "cross_tenant_identity".
    """
    if not IDENTITY_INDEX_TABLE:
        logger.error("IDENTITY_INDEX_TABLE env var is not set")
        return None, "unknown_installation"

    try:
        table = _get_table()

        # Step 1: Resolve tenant from installation
        tenant_resp = table.get_item(
            Key={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
            }
        )
        tenant_item = tenant_resp.get("Item")
        if not tenant_item:
            logger.info(
                "Unknown installation_id=%d — no identity-index entry",
                installation_id,
            )
            return None, "unknown_installation"

        org_id = tenant_item["org_id"]
        user_provisioning_mode = tenant_item.get("user_provisioning_mode", "strict")

        # Step 2: Resolve sender (feature-flag-gated, Issue #537)
        user_item = None
        if _v2_read_enabled():
            # Try new table first
            user_item = _resolve_user_from_new_table(sender_id)
            # Fall back to old table if not found in new table
            if not user_item:
                user_item = _resolve_user_from_old_table(sender_id)
        else:
            # Flag off: read from old table only (default behavior)
            user_item = _resolve_user_from_old_table(sender_id)

        # Step 2b: Postgres safety-net (Issue #702)
        # Cross-validate against Postgres via POST /internal/v1/resolve-user.
        # Trusts Postgres on disagreement (canonical source of truth).
        if _resolve_canonical_via_gateway_enabled():
            from common.gateway_client import resolve_user_by_identity

            pg_result = resolve_user_by_identity("github", str(sender_id))

            if pg_result and user_item:
                # Both returned a result — check for drift
                if pg_result["user_id"] != user_item.get("user_id"):
                    logger.warning(
                        "IdentityIndexDrift: v2/legacy user_id=%s, Postgres user_id=%s "
                        "for sender_id=%d — trusting Postgres",
                        user_item.get("user_id"),
                        pg_result["user_id"],
                        sender_id,
                    )
                    _emit_identity_index_drift_metric()
                    # Trust Postgres — overwrite user_item
                    user_item = pg_result
            elif pg_result and not user_item:
                # v2/legacy missed but Postgres has it (write-through lag)
                logger.info(
                    "Postgres resolved sender_id=%d (user_id=%s) but DDB missed — "
                    "using Postgres result",
                    sender_id,
                    pg_result["user_id"],
                )
                user_item = pg_result
            # If pg_result is None but user_item exists: Postgres miss/error,
            # keep using the DDB result (fail-open, same as today).

        if not user_item:
            logger.info(
                "Unknown sender_id=%d — no identity-index entry", sender_id
            )
            return None, "unknown_user"

        # Step 3: Cross-tenant membership
        # A user's `user_identities` row pins them to ONE home tenant (the one
        # where admin approval happened). But GitHub users can legitimately
        # belong to many orgs — a sophos employee has a personal GitHub too.
        # When they comment on a repo in another ADP tenant, we route the
        # event to the REPO's tenant (installation.org_id), not the sender's
        # home tenant. The commenter just has to be a known ADP user.
        #
        # Trade-off (accepted): billing/quota attaches to the repo's tenant,
        # not the commenter's. Suits hackathon/dev; if production tenants
        # later need stricter membership, we'd add an opt-in flag on the
        # installation row (e.g. "only members of home tenant can trigger").
        #
        # The mismatch is still logged + metric-emitted for audit trails.
        if user_item["org_id"] != org_id:
            logger.info(
                "Cross-tenant ok: sender_id=%d (home org=%s) triggering in "
                "installation_id=%d org=%s — routing to repo tenant",
                sender_id,
                user_item["org_id"],
                installation_id,
                org_id,
            )
            _emit_cross_tenant_metric()

        return (
            ResolvedIdentity(
                tenant_id=org_id,
                org_id=org_id,
                user_id=user_item["user_id"],
                user_provisioning_mode=user_provisioning_mode,
            ),
            "ok",
        )

    except Exception as e:
        logger.error(
            "Identity resolution failed for installation_id=%d sender_id=%d: %s",
            installation_id,
            sender_id,
            e,
        )
        return None, "unknown_installation"
