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

# ---------------------------------------------------------------------------
# Cross-tenant trigger policy vocabulary (issue #3134)
# ---------------------------------------------------------------------------
# Issue #4029: these were bare string literals here and duplicated as bare
# literals in the gateway's provenance authority gate
# (modules/gateway/src/internal/provenance_routes.py). The two sides disagreed
# about the DEFAULT, so the platform permitted a cross-tenant run to execute and
# then refused to record its provenance — an audit hole exactly where
# cross-tenant activity happens.
#
# They cannot share a module: package-lambdas.sh roots the Lambda zip at
# lambda/, so nothing outside it is importable or readable at Lambda runtime.
# The drift guard is therefore a lockstep test that imports both sides and
# asserts these names and DEFAULT_TRIGGER_POLICY agree:
#   modules/gateway/tests/internal/test_provenance_policy_lockstep.py
# If you change the vocabulary or the default here, that test fails until the
# gateway follows.
TRIGGER_POLICY_ANY_ADP_USER = "any_adp_user"
TRIGGER_POLICY_HOME_TENANT_ONLY = "home_tenant_only"

# Absent attribute means any known ADP user may trigger. Load-bearing: it is the
# posture of every tenant that never configured the setting.
DEFAULT_TRIGGER_POLICY = TRIGGER_POLICY_ANY_ADP_USER

# ---------------------------------------------------------------------------
# Which identity links are proof of ownership (issue #5664, A10)
# ---------------------------------------------------------------------------
# What this resolver returns becomes `resolved.user_id`, which the handler feeds
# to `agent_authority.VerifiedHumanEvent.from_verified_webhook` to mint human
# dispatch authority. So "which platform user is this GitHub sender?" is an
# authorization answer, not a lookup convenience.
#
# The DDB identity projection carries no provenance at all: a row written from a
# link a user merely ASSERTED about themselves is byte-identical to one the
# provider confirmed. Anyone who could name someone else's GitHub user id could
# therefore have their comments attributed to that person — and act with their
# authority — with nothing in the row to distinguish the two.
#
# This vocabulary must equal the gateway's `PROVEN_METHODS`
# (modules/gateway/src/shared/identity/verification.py). It cannot import it:
# package-lambdas.sh roots the Lambda zip at lambda/, so nothing outside it is
# importable at runtime. The drift guard is the lockstep test
#   modules/gateway/tests/internal/test_provenance_policy_lockstep.py
# which imports both sides and asserts the sets agree. If you change this set,
# that test fails until the gateway follows — do not "fix" it by editing one
# side's expected value.
PROVEN_VERIFICATION_METHODS = frozenset(
    {
        "oauth",
        "org_placement",
        "admin_manual",
        "magic_link_confirmed",
    }
)


def _is_proven(verification_method: str | None) -> bool:
    """Fail-closed: anything not explicitly proven is not proof.

    Covers ``None`` and ``""`` — a DDB row written before provenance was projected,
    or a gateway not yet redeployed with the field. In neither case has this
    process observed evidence the sender controls the account, and a new method is
    inert here until it is deliberately declared proven on both sides.
    """
    return verification_method in PROVEN_VERIFICATION_METHODS


_dynamodb = None
_cloudwatch = None
# Exposed for callers that need the tenant_item after resolve() completes
# (e.g. handler's _check_min_author_association). Set during resolve().
last_tenant_item: dict | None = None


@dataclass
class ResolvedIdentity:
    """Result of a successful identity resolution."""

    tenant_id: str
    org_id: str
    user_id: str
    user_provisioning_mode: str  # "strict" | "auto_provision"
    user_kind: str = "human"  # "human" | "bot"
    bot_kind: str = ""  # e.g. "agent-developer", "" for humans
    # Issue #5664 (A10): HOW the sender's identity link was established, carried
    # so that consumers granting authority from `user_id` can tell an
    # provider-confirmed link from one the user asserted about themselves. Defaults
    # to "" — unknown provenance, which `identity_proven` treats as NOT proof.
    verification_method: str = ""

    @property
    def identity_proven(self) -> bool:
        """Whether this identity is evidence the sender controls the account.

        Consumers that mint authority from ``user_id`` (notably
        ``agent_authority.VerifiedHumanEvent``) must gate on this rather than on
        the mere existence of a resolution. Resolving a sender and being entitled
        to act as them are different questions.
        """
        return _is_proven(self.verification_method)


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


def _emit_cross_tenant_denied_metric() -> None:
    """Emit CloudWatch metric when cross-tenant trigger is denied by policy."""
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "CrossTenantDenied",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit CrossTenantDenied metric: %s", e)


def _emit_bot_action_metric(bot_kind: str, org_id: str) -> None:
    """Emit CloudWatch metric when a bot identity triggers an action."""
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "BotActionTriggered",
                    "Dimensions": [
                        {"Name": "bot_kind", "Value": bot_kind},
                        {"Name": "org_id", "Value": org_id},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit BotActionTriggered metric: %s", e)


def _emit_installation_tenant_drift_metric() -> None:
    """Emit CloudWatch metric when DDB and Postgres disagree on a tenant.

    Issue #2769: fired when the installation → tenant mapping resolved from the
    identity-index disagrees with Postgres (the source of truth).
    """
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "InstallationTenantDrift",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit InstallationTenantDrift metric: %s", e)


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


def _emit_auto_register_denied_metric() -> None:
    """Emit CloudWatch metric when the tenant gate denies a backfill.

    Issue #2724 (slice B): mirrors the handler's ``AutoRegisterDenied`` so denials
    from either write path are countable. Best-effort.
    """
    try:
        cw = _get_cloudwatch()
        cw.put_metric_data(
            Namespace="ADP/IdentityResolver",
            MetricData=[
                {
                    "MetricName": "AutoRegisterDenied",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to emit AutoRegisterDenied metric: %s", e)


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
            "user-identity-index read failed for sender_id=%d: %s (falling back to old table)",  # noqa: E501
            sender_id,
            e,
        )
        return None


def _resolve_user_from_old_table(sender_id: int) -> dict | None:
    """Resolve user from the existing identity-index table.

    Issue #5664 (A10): rows in this table carry no ``verification_method`` — the
    attribute was never written here. They are returned as-is; ``_is_proven``
    treats the absent value as unproven, so a legacy row can still IDENTIFY a
    sender but cannot by itself authorize one. That is the intended asymmetry: the
    table predates provenance, and inferring proof from its silence is exactly the
    permissive fallback this issue removes.
    """
    table = _get_table()
    resp = table.get_item(
        Key={
            "identity_type": "github_user",
            "identity_value": str(sender_id),
        }
    )
    return resp.get("Item")


def _backfill_installation_identity(installation_id: int, org_id: str, table) -> None:
    """Backfill a missing DDB identity-index row for an installation.

    Issue #2950: When the Postgres fallback resolves a tenant that DDB missed,
    write the row back so subsequent webhook deliveries resolve from DDB
    directly (O(1) instead of a gateway HTTP call).

    Best-effort — failures are logged but do not affect the current resolution.
    """
    import time

    try:
        table.put_item(
            Item={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
                "org_id": org_id,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        logger.info(
            "Backfilled identity-index: github_installation_id=%d → org=%s",
            installation_id,
            org_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to backfill identity-index for installation_id=%d: %s",
            installation_id,
            e,
        )


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
        "cross_tenant_identity", "cross_tenant_denied".
    """
    global last_tenant_item

    if not IDENTITY_INDEX_TABLE:
        logger.error("IDENTITY_INDEX_TABLE env var is not set")
        last_tenant_item = None
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
        last_tenant_item = tenant_item
        if not tenant_item:
            # Issue #2950: DDB miss — fall through to Postgres via the gateway
            # internal API when the flag is enabled. This covers installations
            # written to Postgres (via install-callback) before the DDB dual-write
            # was added, or where the DDB write failed. On a Postgres hit we
            # backfill the DDB row so subsequent lookups are fast again.
            if _resolve_canonical_via_gateway_enabled():
                from common import negative_cache
                from common.gateway_client import (
                    INSTALLATION_NOT_FOUND,
                    installation_gate,
                    resolve_installation_by_id,
                )

                # Issue #4047 (#2724 slice C): this is the SECOND gateway-resolve
                # path for an unknown installation (the architect ruling on #2724
                # flagged that it bypasses _auto_register_installation entirely),
                # so it needs the same negative cache or the mitigation is trivially
                # sidestepped on any cold/evicted row. A live negative row means the
                # gateway already answered "not a known tenant" within the TTL.
                if negative_cache.is_negative_cached(table, installation_id):
                    logger.info(
                        "Unknown installation_id=%d — no identity-index entry and a "
                        "live negative-cache row (gateway resolve skipped)",
                        installation_id,
                    )
                    return None, "unknown_installation"

                pg_install = resolve_installation_by_id(str(installation_id)) or {}
                # Issue #4046 (#2724 slice A): pg_install carries a "state" of
                # resolved / not_found / error. A resolved result always carries a
                # non-empty tenant_id.
                #
                # Issue #4047: cache ONLY the authoritative not_found. An "error"
                # means we could not find out, and caching it would lock out a
                # legitimate new tenant for the whole TTL window.
                if pg_install.get("state") == INSTALLATION_NOT_FOUND:
                    negative_cache.record_not_found(table, installation_id)

                # Issue #2724 (slice B): this is the SECOND write path — it
                # backfills a DDB identity row independently of the handler's
                # _auto_register_installation, so gating only the handler would
                # leave it trivially circumventable on any cold or evicted row.
                # Run the same gate. A denied installation writes nothing and
                # resolves as unknown_installation.
                #
                # Note this path only ever writes the *Postgres* tenant (there is
                # no org_login fallback here), so the gate can only deny on an
                # untrusted-provenance `resolved` result; not_found/error already
                # fall through to unknown_installation below.
                allowed, gate_reason = installation_gate(pg_install)
                if not allowed:
                    logger.warning(
                        "AutoRegisterDenied (resolver backfill): installation_id=%d "
                        "reason=%s created_via=%s — not a known ADP tenant, not "
                        "backfilling",
                        installation_id,
                        gate_reason,
                        pg_install.get("created_via", ""),
                    )
                    _emit_auto_register_denied_metric()
                    return None, "unknown_installation"

                if pg_install.get("tenant_id"):
                    pg_tenant = pg_install["tenant_id"]
                    logger.info(
                        "installation_id=%d resolved via Postgres fallback "
                        "(tenant=%s) — backfilling DDB",
                        installation_id,
                        pg_tenant,
                    )
                    # Backfill DDB so future lookups don't need the gateway call
                    _backfill_installation_identity(installation_id, pg_tenant, table)
                    org_id = pg_tenant
                    user_provisioning_mode = "strict"
                else:
                    logger.info(
                        "Unknown installation_id=%d — no identity-index entry "
                        "and Postgres fallback returned no match",
                        installation_id,
                    )
                    return None, "unknown_installation"
            else:
                logger.info(
                    "Unknown installation_id=%d — no identity-index entry",
                    installation_id,
                )
                return None, "unknown_installation"
        else:
            org_id = tenant_item["org_id"]
            user_provisioning_mode = tenant_item.get("user_provisioning_mode", "strict")

        # Step 1b: Installation-tenant drift safety-net (Issue #2769).
        # Cross-check the DDB installation → tenant mapping against Postgres
        # (the source of truth) via POST /internal/v1/resolve-installation.
        # On disagreement: trust Postgres, emit InstallationTenantDrift, log
        # both tenants. On gateway miss/error: keep the DDB answer (fail-open —
        # no hard RDS dependency on the webhook path). Flag-gated by the same
        # RESOLVE_CANONICAL_VIA_GATEWAY switch as the user safety-net (#702).
        #
        # Issue #4046 (#2724 slice A): the client distinguishes not_found from
        # error; this drift check is behavior-neutral and keeps the DDB answer for
        # both (only a "resolved" result carries a tenant_id).
        if _resolve_canonical_via_gateway_enabled():
            from common.gateway_client import resolve_installation_by_id

            pg_install = resolve_installation_by_id(str(installation_id)) or {}
            if pg_install.get("tenant_id"):
                pg_tenant = pg_install["tenant_id"]
                if pg_tenant != org_id:
                    logger.warning(
                        "InstallationTenantDrift: installation_id=%d DDB tenant=%s, "
                        "Postgres tenant=%s — trusting Postgres",
                        installation_id,
                        org_id,
                        pg_tenant,
                    )
                    _emit_installation_tenant_drift_metric()
                    org_id = pg_tenant

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

            # Issue #5664 (A10): scope the lookup to the tenant this installation
            # belongs to. `user_identities` is unique per (provider,
            # provider_user_id, org_id), so an account linked in several tenants is
            # ambiguous unscoped and the gateway declines to guess. The
            # installation already told us the tenant, so the question is
            # answerable — asking it unscoped was throwing away the answer.
            pg_result = resolve_user_by_identity(
                "github", str(sender_id), org_id=org_id
            )

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
                    # Trust Postgres for canonical fields but preserve DDB-only
                    # attrs (member_org_ids, user_kind) that PG doesn't carry.
                    user_item = {**user_item, **pg_result}
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
            logger.info("Unknown sender_id=%d — no identity-index entry", sender_id)
            return None, "unknown_user"

        # Step 3: Cross-tenant membership enforcement (Issue #3134)
        # A user's identity row pins them to ONE home tenant. When they comment
        # on a repo in another ADP tenant, we check the tenant's trigger_policy:
        #
        # - "any_adp_user" (default, absent attr): allow any known ADP user to
        #   trigger (today's behavior). Log + emit CrossTenantMismatch metric.
        # - "home_tenant_only": allow only if the repo's org_id is in the
        #   user's member_org_ids list. Fail-closed: missing member_org_ids
        #   is treated as [user's home org_id] only.
        #
        # HARD CONSTRAINT: no new gateway calls — membership data comes from
        # the DDB rows already fetched (tenant_item + user_item).
        if user_item["org_id"] != org_id:
            trigger_policy = (
                tenant_item.get("trigger_policy", DEFAULT_TRIGGER_POLICY)
                if tenant_item
                else DEFAULT_TRIGGER_POLICY
            )

            if trigger_policy == TRIGGER_POLICY_HOME_TENANT_ONLY:
                # Check membership: user must have org_id in their member_org_ids
                member_org_ids = user_item.get("member_org_ids", [user_item["org_id"]])
                if org_id not in member_org_ids:
                    logger.info(
                        "Cross-tenant DENIED: sender_id=%d (home org=%s) blocked "
                        "from triggering in installation_id=%d org=%s — "
                        "policy=home_tenant_only, member_org_ids=%s",
                        sender_id,
                        user_item["org_id"],
                        installation_id,
                        org_id,
                        member_org_ids,
                    )
                    _emit_cross_tenant_denied_metric()
                    return None, "cross_tenant_denied"

                # User is a member of the target org — allow
                logger.info(
                    "Cross-tenant allowed (membership): sender_id=%d (home org=%s) "
                    "triggering in installation_id=%d org=%s — org in member_org_ids",
                    sender_id,
                    user_item["org_id"],
                    installation_id,
                    org_id,
                )
                _emit_cross_tenant_metric()
            else:
                # Default policy: any_adp_user — allow with audit log
                logger.info(
                    "Cross-tenant ok: sender_id=%d (home org=%s) triggering in "
                    "installation_id=%d org=%s — routing to repo tenant",
                    sender_id,
                    user_item["org_id"],
                    installation_id,
                    org_id,
                )
                _emit_cross_tenant_metric()

        user_kind = user_item.get("user_kind", "human")

        # Emit metric for bot-triggered actions (observability for agent-to-agent flows)
        if user_kind == "bot":
            bot_kind = user_item.get("bot_kind", "unknown")
            _emit_bot_action_metric(bot_kind, org_id)

        return (
            ResolvedIdentity(
                tenant_id=org_id,
                org_id=org_id,
                user_id=user_item["user_id"],
                user_provisioning_mode=user_provisioning_mode,
                user_kind=user_kind,
                bot_kind=user_item.get("bot_kind", ""),
                # Issue #5664 (A10): absent on every DDB row (the attribute is not
                # projected) and present only when the Postgres safety-net answered.
                # "" is unknown provenance, which `identity_proven` treats as not
                # proof — so resolution still IDENTIFIES the sender while authority
                # is withheld until the link's origin is actually known.
                verification_method=user_item.get("verification_method", ""),
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
