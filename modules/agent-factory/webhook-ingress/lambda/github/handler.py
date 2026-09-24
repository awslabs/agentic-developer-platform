"""Webhook ingress Lambda — unified entry point.

Handles two event sources via shape-based routing:
  1. API Gateway events (GitHub webhooks) — have `headers` + `body`
  2. EventBridge events (alarms, scheduled rules, CI) — have `source` + `detail-type` + `detail`

Issue #2154: Added EventBridge shape detection to support machine/root-triggered agents.

Target execution time: <300ms.
This Lambda does NOT clone repos, call LLMs, or do heavy computation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import UTC, datetime
import time
import uuid
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
WEBHOOK_SECRET_ARN = os.environ.get("WEBHOOK_SECRET_ARN", "")
SUBMIT_QUEUE_URL = os.environ.get("SUBMIT_QUEUE_URL", "")

# Cached webhook secret (resolved from ARN at first invocation)
_webhook_secret: str | None = None

# Lazy imports to keep cold start fast — these modules import boto3
_signature_mod = None
_secrets_mod = None
_identity_mod = None
_gateway_client_mod = None
_negative_cache_mod = None
_sqs_mod = None
_events_log_mod = None
_webhook_event_logger = None


def _get_signature():
    global _signature_mod
    if _signature_mod is None:
        from common import signature

        _signature_mod = signature
    return _signature_mod


def _get_secrets():
    global _secrets_mod
    if _secrets_mod is None:
        from common import secrets

        _secrets_mod = secrets
    return _secrets_mod


def _resolve_webhook_secret() -> str:
    """Resolve the webhook secret from Secrets Manager (cached after first call).

    Falls back to WEBHOOK_SECRET env var for local dev/testing.

    If the cached value is the Terraform placeholder, re-read from Secrets Manager
    on each call until a real value is available. This handles the race condition
    where the Lambda cold-starts before the gateway's manifest-callback writes
    the real webhook secret (fresh-deploy timing issue).
    """
    global _webhook_secret
    if _webhook_secret is not None and not _webhook_secret.startswith("PLACEHOLDER"):
        return _webhook_secret

    if WEBHOOK_SECRET_ARN:
        # Clear the secrets helper cache if we're re-reading (placeholder retry)
        if _webhook_secret is not None:
            _get_secrets().clear_cache()
        _webhook_secret = _get_secrets().get_secret(WEBHOOK_SECRET_ARN)
    else:
        # Fallback for local dev/testing: allow plaintext env var
        _webhook_secret = os.environ.get("WEBHOOK_SECRET", "")

    return _webhook_secret


def _get_identity_resolver():
    global _identity_mod
    if _identity_mod is None:
        from common import identity_resolver

        _identity_mod = identity_resolver
    return _identity_mod


def _get_negative_cache():
    global _negative_cache_mod
    if _negative_cache_mod is None:
        from common import negative_cache

        _negative_cache_mod = negative_cache
    return _negative_cache_mod


class AutoRegisterResult(NamedTuple):
    """Outcome of :func:`_auto_register_installation`.

    Issue #2724 (slice B): the two fields are deliberately separate because
    "we wrote a routable row" and "we know this org is a real ADP tenant" are
    different facts, and conflating them is what let the platform GitHub App's
    private key be copied for any org that installed the App.

    tenant_id
        The tenant that owns the installation, or ``None`` when nothing routable
        was persisted (denied by the gate, or the write failed).
    authoritative
        True only when the tenant was established via the **authoritative**
        path — an existing Postgres-owned row, or a gateway
        ``resolve-installation`` hit. False when we fell back to the raw
        ``org_login`` because the gate could not be evaluated. Callers MUST NOT
        provision per-tenant credentials on a non-authoritative result.
    """

    tenant_id: str | None
    authoritative: bool


def _auto_register_installation(
    installation_id: int, org_login: str, *, bypass_negative_cache: bool = False
) -> AutoRegisterResult:
    """Register only a currently owned, unrevoked installation.

    Canonical unavailability denies, including refreshes and delayed lifecycle
    deliveries. A permanent DDB marker blocks writes racing local revocation.
    ``bypass_negative_cache`` affects only ordinary unknown-cache maintenance;
    it cannot bypass durable denial.
    """
    if not org_login:
        return AutoRegisterResult(None, False)
    resolver = _get_identity_resolver()
    try:
        table = resolver._get_table()
        from common.installation_revocation import admit_installation, put_active_installation

        canonical, _ = admit_installation(table, installation_id)
        if canonical is None:
            return AutoRegisterResult(None, False)

        # Step 1: read-before-write.
        existing = table.get_item(
            Key={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
            }
        ).get("Item")

        if existing is not None and existing.get("org_id") != canonical["tenant_id"]:
            return AutoRegisterResult(None, False)

        # Step 2: Postgres-owned row (no auto_registered flag) → do not clobber.
        if existing is not None and not existing.get("auto_registered"):
            stored_org = existing.get("org_id", "")
            if stored_org and stored_org != org_login:
                logger.warning(
                    "InstallationTenantDrift: installation_id=%d Postgres-owned "
                    "tenant=%s but webhook org login=%s — keeping Postgres tenant",
                    installation_id,
                    stored_org,
                    org_login,
                )
                _emit_metric("InstallationTenantDrift")
            else:
                logger.info(
                    "Auto-register no-op: installation_id=%d already Postgres-owned (tenant=%s)",
                    installation_id,
                    stored_org,
                )
            # A Postgres-owned row IS the authoritative answer — the gateway
            # write-through created it, so provisioning downstream is permitted.
            return AutoRegisterResult(stored_org or None, bool(stored_org))

        # Step 3: no row → the tenant gate (#2724 slice B). See the docstring.
        # A refresh of an existing auto_registered row (step 4) reuses the
        # already-stored tenant to stay idempotent.
        neg_cache = _get_negative_cache()
        if bypass_negative_cache:
            # A fresh install must never inherit a stale "unknown" verdict.
            neg_cache.invalidate(table, installation_id)

        if existing is None:
            # Issue #4047 (#2724 slice C): a live negative row means the gateway
            # already told us (authoritatively, within the TTL) that this
            # installation is not a known tenant. Synthesize that same
            # not_found state instead of re-asking.
            # Annotated so the synthesized literal below (mixed str/bool values)
            # does not narrow pg to dict[str, object], which would make
            # pg["tenant_id"] an `object` and break tenant_id's str | None type.
            pg: dict[str, Any]
            if not bypass_negative_cache and neg_cache.is_negative_cached(table, installation_id):
                pg = {"state": "not_found", "cached": True}
            else:
                pg = canonical
                # Cache ONLY the authoritative 404. An "error" state means we do
                # not know — caching it would turn a gateway outage into a
                # TTL-long lockout for legitimate new tenants.
                if pg and pg.get("state") == "not_found":
                    neg_cache.record_not_found(table, installation_id)
            state = pg.get("state") if pg else None
            # `installation_gate` is a pure function and is imported directly (not
            # via _get_gateway_client()) so the trust decision is made by the ONE
            # shared implementation — the same one identity_resolver's backfill
            # path calls. Lazy-imported to keep cold start cheap, matching the
            # module's existing pattern.
            from common.gateway_client import TRUSTED_GATE_REASONS, installation_gate

            allowed, gate_reason = installation_gate(pg)

            if not allowed:
                # THE GATE. An authoritative "this is not a tenant we onboarded"
                # — write nothing at all, so the caller 403s
                # `unknown_installation` and no per-tenant secret is provisioned.
                logger.warning(
                    "AutoRegisterDenied: installation_id=%d org_login=%s reason=%s "
                    "(state=%s, created_via=%s) — not a known ADP tenant, writing "
                    "no identity rows",
                    installation_id,
                    org_login,
                    gate_reason,
                    state or "unknown",
                    (pg or {}).get("created_via", ""),
                )
                _emit_metric("AutoRegisterDenied")
                return AutoRegisterResult(None, False)

            # Authoritative iff the gate could actually vouch for the tenant —
            # NOT merely "allowed". The gate allows `provenance_unavailable` and
            # `gate_unavailable` so an outage or a not-yet-redeployed gateway
            # cannot reject legitimate installs, but in neither case do we know
            # whose org this is, so neither may seed the platform App private key.
            # `and pg.get("tenant_id")` is belt-and-braces: the client guarantees a
            # non-empty tenant on "resolved", but an empty one must fall back
            # rather than write an empty org_id.
            if gate_reason in TRUSTED_GATE_REASONS and (pg or {}).get("tenant_id"):
                tenant_id = pg["tenant_id"]
                authoritative = True
            else:
                # Allowed, but NOT authoritatively: the gate could not be
                # evaluated (gateway unreachable/unconfigured, or provenance
                # absent because the gateway predates the field). Fail OPEN so a
                # gateway outage never becomes "reject every new installation",
                # but LOUD — and mark the result non-authoritative so the caller
                # skips credential provisioning.
                #
                # Fallback rationale (unchanged from before the gate): if the
                # gateway is unreachable (SigV4 auth on API GW, fresh deploy) or
                # the tenant isn't in Postgres yet (user-namespace installs, new
                # orgs), register using the org_login directly. This covers a user
                # installing the app on their personal account — no org-tenant
                # shell exists in Postgres, but the install is legitimate. The
                # user still needs approval before they can trigger agents.
                logger.warning(
                    "AutoRegisterGateUnavailable: installation_id=%d org_login=%s "
                    "reason=%s (state=%s, gateway reason=%s) — failing OPEN, "
                    "registering with org_login as tenant_id, NOT provisioning "
                    "per-tenant credentials",
                    installation_id,
                    org_login,
                    gate_reason,
                    state or "unknown",
                    (pg or {}).get("reason", ""),
                )
                _emit_metric("AutoRegisterGateUnavailable")
                tenant_id = org_login
                authoritative = False
        else:
            # Step 4: idempotent refresh of an auto_registered row. Grandfathering
            # (#2724 design item 5): the gate applies to NEW registrations only,
            # so rows written before it existed keep working untouched — no
            # gateway call, no provenance check. But the refresh is not
            # authoritative on its own: an auto_registered row may itself be a
            # pre-gate org_login fallback, so we do not re-seed credentials off it.
            tenant_id = existing.get("org_id") or org_login
            authoritative = False

        now = datetime.now(UTC).isoformat()
        # Identity rows are authoritative; offboarding deletes them explicitly.
        # No TTL — writers were previously setting a 7d/30d/365d expiry assuming
        # a reconcile job would refresh, but rows GC'd silently and broke webhook
        # routing for active users (#TBD bug).
        forward_item = {
            "identity_type": "github_installation_id",
            "identity_value": str(installation_id),
            "org_id": tenant_id,
            "updated_at": now,
            "auto_registered": True,
        }
        if existing is None:
            # Non-clobber path: only write when we would not overwrite a
            # Postgres-owned row that appeared between our read and write.
            try:
                put_active_installation(
                    table,
                    installation_id,
                    forward_item,
                    condition="attribute_not_exists(identity_type)",
                )
            except Exception as cond_exc:  # noqa: BLE001
                # ConditionalCheckFailedException → a Postgres-owned row won the
                # race; respect it and no-op.
                if "ConditionalCheckFailed" in type(cond_exc).__name__ or (
                    "ConditionalCheckFailed" in str(cond_exc)
                ):
                    logger.info(
                        "Auto-register lost race for installation_id=%d — "
                        "Postgres-owned row present, no-op",
                        installation_id,
                    )
                    # The winner is a Postgres-owned row, so the mapping is
                    # authoritative regardless of how we got here.
                    return AutoRegisterResult(None, False)
                raise
        else:
            put_active_installation(table, installation_id, forward_item)

        # The forward row is now persisted, so the mapping is live: dispatch
        # reads it and routes on it. From here on a failure is PARTIAL, not
        # total — see the "Partial writes" note in the docstring. Issue #4030.
        #
        # Issue #2336: Write reverse-lookup row (org_id → installation_id) so
        # EventBridge/agent-trigger handlers can resolve a real installation_id
        # without calling the GitHub API. Guard it the same way: never clobber a
        # Postgres-owned reverse row.
        try:
            reverse_existing = table.get_item(
                Key={
                    "identity_type": "org_installation",
                    "identity_value": tenant_id,
                }
            ).get("Item")
            if reverse_existing is None or reverse_existing.get("auto_registered"):
                put_active_installation(
                    table,
                    installation_id,
                    {
                        "identity_type": "org_installation",
                        "identity_value": tenant_id,
                        "installation_id": installation_id,
                        "updated_at": now,
                        "auto_registered": True,
                    },
                )
        except Exception as rev_exc:  # noqa: BLE001
            # Issue #4030: do NOT swallow this into a None return. The forward
            # row is already written, so the tenant genuinely routes — dropping
            # it here is what left Acme with a mapping whose downstream
            # provisioning never ran, permanently (all later events resolve, so
            # the caller's `unknown_installation` self-heal branch never fires
            # again). #3860 documents forward-row-only as degraded-but-usable:
            # reverse-row absence breaks `adp-trigger`, not webhook routing.
            logger.error(
                "AutoRegister.PartialWrite: installation_id=%d → tenant=%s forward row "
                "WRITTEN but reverse (org_installation) row failed — %s. Webhook routing "
                "works; agent-trigger resolution for this org will not until healed "
                "(see #3453 reconcile).",
                installation_id,
                tenant_id,
                rev_exc,
            )
            _emit_metric("AutoRegister.PartialWrite")
            return AutoRegisterResult(tenant_id, authoritative)

        logger.info(
            "Auto-registered installation_id=%d → tenant=%s (forward + reverse, authoritative=%s)",
            installation_id,
            tenant_id,
            authoritative,
        )
        return AutoRegisterResult(tenant_id, authoritative)
    except Exception as exc:  # noqa: BLE001
        # Reaching here means we never persisted a usable forward row (read,
        # gateway resolve, or the forward put_item itself failed). Returning
        # no tenant is correct: the caller must NOT treat this installation as
        # registered, because nothing routes to it. Issue #4030.
        logger.warning("Failed to auto-register installation_id=%d: %s", installation_id, exc)
        return AutoRegisterResult(None, False)


def _get_gateway_client():
    global _gateway_client_mod
    if _gateway_client_mod is None:
        from common import gateway_client

        _gateway_client_mod = gateway_client
    return _gateway_client_mod


_metrics_mod = None


def _get_metrics():
    global _metrics_mod
    if _metrics_mod is None:
        from common.metrics import WebhookMetrics

        _metrics_mod = WebhookMetrics(region=os.environ.get("AWS_REGION", "us-east-1"))
    return _metrics_mod


# Lazy Secrets Manager client for auto-provisioning per-tenant secrets
_sm_client = None


def _get_sm_client():
    """Return a cached Secrets Manager client (lazy init to protect cold-start)."""
    global _sm_client
    if _sm_client is None:
        import boto3

        _sm_client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _sm_client


def _emit_metric(metric_name: str) -> None:
    """Emit a single CloudWatch metric under the WebhookIngress namespace.

    Best-effort — failures are logged but never block the Lambda response.
    """
    try:
        metrics = _get_metrics()
        metrics._metric_data.append(
            {
                "MetricName": metric_name,
                "Dimensions": [
                    {"Name": "Operation", "Value": "AutoRegister"},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )
        metrics.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit metric %s: %s", metric_name, exc)


def _auto_provision_tenant_github_app_secret(tenant_id: str, installation_id: int) -> None:
    """Create per-tenant GitHub App SM secret. Idempotent.

    Reads platform App credentials from the module's adp-agent-platform-* secrets,
    composes the JSON the worker pod expects, writes to adp/<env>/tenants/<tenant>/github-app.

    Failures are logged and swallowed, and emit a CloudWatch metric for operator
    visibility.

    Issue #4030 — a caveat on that swallow: "recoverable manually" was doing a
    lot of work in the original note here. A tenant whose mapping exists but
    whose secret does not is NOT self-healing: every worker pod dies at
    bootstrap ``vault_fetch``, and because this seeder only runs on a *fresh*
    registration, nothing retries it. Recovery is a human copying a secret by
    hand. Retrying the seed on later installation events is deliberately NOT
    done here — with the unconditional ``org_login`` fallback above it would
    copy the platform App key for any org that installs the App. It returns as a
    follow-up once #2724 slice B lands a real tenant-existence gate. The
    systemic heal is #3453.
    """
    sm = _get_sm_client()
    env = os.environ.get("ENVIRONMENT", "dev")
    target = f"adp/{env}/tenants/{tenant_id}/github-app"

    try:
        # Read platform App credentials (same Terraform module owns these)
        app_id_resp = sm.get_secret_value(SecretId=f"adp/{env}/github-app/adp-agent-platform-id")
        app_key_resp = sm.get_secret_value(SecretId=f"adp/{env}/github-app/adp-agent-platform-key")
        payload = json.dumps(
            {
                "app_id": app_id_resp["SecretString"],
                "private_key": app_key_resp["SecretString"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Auto-provision: failed to read platform App secrets for tenant=%s — %s",
            tenant_id,
            exc,
        )
        _emit_metric("AutoRegister.PlatformSecretReadFailed")
        return

    try:
        sm.create_secret(
            Name=target,
            Description=(
                f"GitHub App credentials for tenant {tenant_id} "
                f"(auto-provisioned via webhook auto-register)"
            ),
            SecretString=payload,
            Tags=[
                {"Key": "ManagedBy", "Value": "auto-register"},
                {"Key": "Tenant", "Value": tenant_id},
                {"Key": "InstallationId", "Value": str(installation_id)},
            ],
        )
        logger.info(
            "Auto-provisioned GitHub App secret tenant=%s path=%s installation_id=%d",
            tenant_id,
            target,
            installation_id,
        )
    except sm.exceptions.ResourceExistsException:
        logger.info(
            "GitHub App secret already exists tenant=%s path=%s — skipping",
            tenant_id,
            target,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Auto-provision: SM CreateSecret failed tenant=%s path=%s — %s",
            tenant_id,
            target,
            exc,
        )
        _emit_metric("AutoRegister.SecretCreationFailed")


# Issue #2732: sibling-App detection state.
#
# Our own GitHub App slug, resolved once per container from the
# adp-agent-platform-meta secret (PR #2701 pattern). Sentinel `None` = not yet
# resolved; "" = resolved-but-unavailable (skip detection to avoid mis-flagging
# our own traffic). A foreign ADP bot comment (marker present, login != ours)
# means a second ADP deployment's App is installed on the same repo.
_own_app_slug: str | None = None

# Dedup set for WARNING logs: (repo, sibling_login) already warned this
# container. Cheap, resets on cold start — good enough for advisory logging.
# The CloudWatch metric is still emitted on every event (it's the durable
# record); only the log line is deduped.
_sibling_warned: set[tuple[str, str]] = set()


def _get_own_app_slug() -> str:
    """Return our own GitHub App slug, cached module-level (issue #2732).

    Reads ``app_slug`` from the platform App's ``-meta`` secret
    (``adp/<env>/github-app/adp-agent-platform-meta``), matching the resolution
    used by the gateway's GitHubAppCredsProvider (PR #2701). Returns "" on any
    failure — callers treat "" as "cannot determine own slug" and skip sibling
    detection rather than risk mis-flagging our own bot's comments.
    """
    global _own_app_slug
    if _own_app_slug is not None:
        return _own_app_slug

    _own_app_slug = ""  # fail-safe default; overwritten on success
    env = os.environ.get("ENVIRONMENT", "dev")
    meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"
    try:
        resp = _get_sm_client().get_secret_value(SecretId=meta_path)
        meta = json.loads(resp["SecretString"])
        _own_app_slug = (meta.get("app_slug") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sibling detection: could not resolve own App slug: %s", exc)
    return _own_app_slug


def _detect_sibling_app(payload: dict, repo: str) -> None:
    """Advisory detection of a sibling ADP App installed on the same repo.

    Issue #2732: GitHub fans every webhook to ALL installed Apps, so when a
    second ADP deployment's App is installed on a repo we're also on, that
    deployment's agent bot-comments arrive at OUR webhook too. Those comments
    carry the ``adp-correlation:`` marker, which unambiguously identifies them
    as ADP-family traffic. We flag a sibling when ALL of:

      1. ``sender.type == "Bot"``,
      2. the comment body contains a valid ``adp-correlation:`` marker, and
      3. the sender login is NOT our own App's bot (``<slug>[bot]``).

    On detection we emit the ``SiblingAppDetected`` CloudWatch metric and log a
    deduped WARNING. This is OBSERVABILITY ONLY: it never comments on the issue
    and never blocks the event (a repo may legitimately host two Apps during a
    migration). The existing ``unknown_user`` short-circuit still applies.

    Best-effort: any exception is swallowed so detection never breaks the
    webhook path.
    """
    try:
        sender = payload.get("sender", {}) or {}
        if sender.get("type") != "Bot":
            return

        body = (payload.get("comment", {}) or {}).get("body", "") or ""
        from common.marker_parse import has_valid_marker

        if not has_valid_marker(body):
            return

        sender_login = sender.get("login", "") or ""

        # Own-slug gate: if we can't determine our own slug, skip — we must not
        # risk flagging our own bot's comments as a sibling.
        own_slug = _get_own_app_slug()
        if not own_slug:
            return
        if sender_login == f"{own_slug}[bot]":
            return  # our own traffic

        # Confirmed foreign ADP-family bot comment → sibling App on this repo.
        try:
            _get_metrics().record_sibling_app(repo=repo, sibling_login=sender_login)
            _get_metrics().flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sibling detection: metric emit failed: %s", exc)

        dedup_key = (repo, sender_login)
        if dedup_key not in _sibling_warned:
            _sibling_warned.add(dedup_key)
            logger.warning(
                "Sibling ADP App detected on repo=%s: foreign agent bot %r "
                "(carries adp-correlation marker) — a second ADP deployment's "
                "App is installed here and may execute the same triggers. "
                "Advisory only; event not blocked.",
                repo,
                sender_login,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sibling detection failed (non-fatal): %s", exc)


# Issue #3134: Author association hierarchy for min_author_association enforcement.
# Higher index = higher privilege. NONE < CONTRIBUTOR < COLLABORATOR < MEMBER < OWNER.
_AUTHOR_ASSOCIATION_LEVELS = {
    "NONE": 0,
    "CONTRIBUTOR": 1,
    "COLLABORATOR": 2,
    "MEMBER": 3,
    "OWNER": 4,
}


def _check_min_author_association(
    *,
    payload: dict,
    event_type: str,
    installation_id: int,
    tenant_item: dict | None = None,
) -> str | None:
    """Check author_association against the installation's min_author_association.

    Issue #3134: When an installation row has min_author_association set, the
    comment's author_association must meet or exceed that threshold. This check
    uses the payload's comment.author_association field (present on every
    issue_comment event) — zero extra API calls.

    Issue #3134 fix: Accepts an optional tenant_item dict (the installation row
    already fetched by the identity resolver) to avoid a duplicate DDB GetItem.
    Falls back to a direct DDB read if tenant_item is not provided.

    Returns None if the check passes (no enforcement or sufficient association),
    or the rejection reason string if the check fails.
    """
    # Only applies to issue_comment events (which carry author_association)
    if event_type != "issue_comment":
        return None

    try:
        # Use caller-provided tenant_item, or fetch from DDB as fallback
        if tenant_item is None:
            resolver = _get_identity_resolver()
            table = resolver._get_table()
            tenant_resp = table.get_item(
                Key={
                    "identity_type": "github_installation_id",
                    "identity_value": str(installation_id),
                }
            )
            tenant_item = tenant_resp.get("Item")

        if not tenant_item:
            return None

        min_assoc = tenant_item.get("min_author_association")
        if not min_assoc:
            return None  # Not configured — no enforcement

        # Get the comment's author_association from the payload
        comment = payload.get("comment", {}) or {}
        actual_assoc = comment.get("author_association", "NONE")

        # Compare levels
        min_level = _AUTHOR_ASSOCIATION_LEVELS.get(min_assoc.upper(), 0)
        actual_level = _AUTHOR_ASSOCIATION_LEVELS.get(actual_assoc.upper(), 0)

        if actual_level < min_level:
            logger.info(
                "Author association insufficient: installation_id=%d "
                "requires=%s (level=%d), actual=%s (level=%d)",
                installation_id,
                min_assoc,
                min_level,
                actual_assoc,
                actual_level,
            )
            return "insufficient_association"
    except Exception as exc:
        # Best-effort — never block on enforcement failure
        logger.warning("min_author_association check failed (non-fatal): %s", exc)

    return None


_correlation_store_mod = None


def _get_correlation_store():
    global _correlation_store_mod
    if _correlation_store_mod is None:
        from common import correlation_store

        _correlation_store_mod = correlation_store
    return _correlation_store_mod


def _resolve_chain_record(correlation_id: str) -> dict[str, Any] | None:
    """Return the newest server-written ``webhook-events`` row for this chain.

    Issue #4129: thin wrapper over ``agent_trigger._resolve_chain`` so there is
    exactly ONE ``correlation-index`` GSI query implementation in this Lambda
    (the reuse table in the issue is explicit about not adding a second one).
    Imported lazily because ``agent_trigger`` imports boto3 at call time and this
    module is on the cold-start path.
    """
    if not correlation_id:
        return None
    try:
        from agent_trigger import _resolve_chain

        return _resolve_chain(correlation_id)
    except Exception as e:  # noqa: BLE001 — resolution failure must fail closed, not 500
        logger.warning(
            "Chain resolution failed for correlation=%s: %s — failing closed",
            correlation_id,
            e,
        )
        return None


def _resolve_pointer_provenance(
    pointer: dict,
    fallback_root_human_id: str,
) -> tuple[str, bool, int | None]:
    """Resolve a pointer's chain provenance from server-written state (#4129).

    The correlation-pointers row is writable by the agent pod
    (``dynamodb:UpdateItem`` on ``adp-*-correlation-pointers``), so the three
    fields that decide **whose authority a run holds** and **how deep the chain
    is** must not be read off it. A compromised pod could otherwise set
    ``root_human_id=<victim> is_human_rooted=true chain_depth=0``, trigger the
    channel, and have the webhook persist the victim's id as a legitimate
    server-side ``authorized_user_id`` with the depth counter reset.

    Instead they come from the ``correlation-index`` GSI on ``webhook-events`` —
    written only by this Lambda — via :func:`_resolve_chain_record`.

    Fail-closed on an unresolvable chain: authority is dropped
    (``is_human_rooted=False``, root falls back to the caller-supplied bot id) but
    the chain itself is still inherited by the caller, because the pointer's
    ``correlation_id`` and parent edge are not authority-bearing. That keeps
    legitimate #1828 cross-issue lineage connected on a channel the webhook has
    never seen while granting it no vault access it hasn't earned.

    Args:
        pointer: The row returned by ``correlation_store.read_pointer``. Its
            provenance fields are deliberately IGNORED — passing a forged row is
            inert by construction, which is the property this function exists to
            provide.
        fallback_root_human_id: Root human to report when the chain cannot be
            resolved. Callers pass the resolved BOT sender id, never anything
            claimed by the event.

    Returns:
        ``(root_human_id, is_human_rooted, chain_depth)``. ``chain_depth`` is
        None when unresolvable; callers treat that as depth 0 exactly as they
        already treat a pointer with no depth.
    """
    chain = _resolve_chain_record(pointer.get("correlation_id") or "")
    if not chain:
        logger.warning(
            "No server-written chain row for correlation=%s — dropping "
            "chain authority (fail-closed); lineage is still inherited",
            pointer.get("correlation_id"),
        )
        return fallback_root_human_id, False, None

    root_human_id = chain.get("root_human_id") or ""
    is_human_rooted = bool(chain.get("is_human_rooted")) and bool(root_human_id)
    if not is_human_rooted:
        root_human_id = root_human_id or fallback_root_human_id

    chain_depth = chain.get("chain_depth")
    if chain_depth is not None:
        try:
            chain_depth = int(chain_depth)
        except (ValueError, TypeError):
            logger.warning(
                "Malformed chain_depth=%r on chain row correlation=%s — treating as unknown",
                chain_depth,
                pointer.get("correlation_id"),
            )
            chain_depth = None
    if chain_depth is not None and chain_depth < 0:
        # A negative depth would evade the runaway-chain guard on increment.
        logger.warning(
            "Negative chain_depth=%d on chain row correlation=%s — treating as unknown",
            chain_depth,
            pointer.get("correlation_id"),
        )
        chain_depth = None

    return root_human_id, is_human_rooted, chain_depth


def _pr_marker_text_with_issue_fallback(
    store, repo, pr_body, head_ref, fallback_root_human_id: str = ""
) -> tuple[str | None, bool]:
    """Resolve the marker text to use for a PR event's correlation.

    Issue #1731/#1735: the PR body usually has NO valid adp-* marker at
    pull_request.opened time — the agent self-opens the PR with its own
    descriptive body ("## Summary…") before any marker is backfilled. That body
    is NON-EMPTY, so a bare `if not pr_body` check is wrong (truthy without a
    marker). If the body lacks a VALID marker, fall back to the ISSUE's
    correlation pointer (the issue number is in the agent/issue-<N> branch; the
    developer run wrote that pointer at its "started" comment, BEFORE opening
    the PR — so it reliably exists). Synthesizes a marker string from the issue
    pointer so determine_correlation can set parent_invocation_id across the
    issue→PR boundary without depending on the racy PR-body marker.

    Issue #4129: the synthesized marker's PROVENANCE fields come from
    :func:`_resolve_pointer_provenance` (the ``webhook-events`` GSI), not from the
    pointer row. This path is trusted by construction, so reading authority off a
    pod-writable row here would forward the forgery straight into a Rule-4 spawn
    on the PR channel — the pointer supplies only the chain id and parent edge.

    Args:
        fallback_root_human_id: Root human to embed when the chain cannot be
            resolved server-side. Callers pass the resolved sender's id.

    Returns:
        A ``(marker_text, trusted)`` tuple. ``trusted`` is True ONLY for the
        synthesized-from-pointer case (issue #4128): that marker is built here,
        server-side, out of server-written state, so it is unsigned by
        construction yet carries that state's trust. Marker text that came from
        the PR body is attacker-controllable and returns False, so
        :func:`determine_correlation` verifies its signature.
    """
    from common.marker_parse import has_valid_marker

    if pr_body and has_valid_marker(pr_body):
        return pr_body, False
    m = re.match(r"agent/issue-(\d+)", head_ref or "")
    if not m:
        return pr_body, False
    issue_channel = store.channel_key("github", repo, "issue", int(m.group(1)))
    issue_pointer = store.read_pointer(issue_channel)
    if not issue_pointer:
        return pr_body, False
    root_human_id, is_human_rooted, chain_depth = _resolve_pointer_provenance(
        issue_pointer, fallback_root_human_id
    )
    return (
        f"<!-- adp-correlation:{issue_pointer['correlation_id']} "
        f"adp-root-human:{root_human_id} "
        f"adp-is-human-rooted:{'true' if is_human_rooted else 'false'} "
        f"adp-invocation:{issue_pointer.get('triggering_invocation_id') or ''} "
        f"adp-chain-depth:{chain_depth or 0} -->",
        True,
    )


def determine_correlation(
    payload: dict,
    resolved_identity,
    channel_key: str,
    marker_text: str | None = None,
    marker_trusted: bool = False,
) -> dict[str, Any]:
    """Determine correlation context for this event (read-only).

    For human senders: always starts a new chain (overrides any stale pointer).
    For bot senders: uses pointer-vs-marker precedence (issue #1696):
      - Pointer exists AND correlation_id matches marker → use pointer
      - Pointer exists but correlation_id differs from marker → use marker (cross-channel hop)
      - No pointer → use marker
      - No marker and no pointer → new chain (fallback)

    Args:
        marker_trusted: Issue #4128 — True only when ``marker_text`` was
            SYNTHESIZED SERVER-SIDE from a correlation pointer (see
            :func:`_pr_marker_text_with_issue_fallback`) rather than read from
            attacker-controllable GitHub content. Such a marker is unsigned by
            construction, but it is derived from a server-written pointer, so it
            carries the pointer's trust. Callers MUST NOT set this for any
            marker that came out of a comment body or PR body.

    Returns a dict with: correlation_id, root_human_id, triggered_by,
    is_human_rooted, is_new_chain, parent_invocation_id, chain_depth.

    ``chain_depth`` is INHERITED UNCHANGED here — it is the depth of the run this
    event came out of, not a depth for this event. Issue #4268: this function used
    to return ``inherited_depth + 1`` on every branch, which made the counter
    measure *webhook events on the chain* rather than *agent generations*. Because
    the returned value is persisted on the row for EVERY outcome, including the
    ``no_op`` rows this Lambda writes and discards, an event that started nothing
    still advanced the counter that gates starting things. An orchestrator posting
    routine status comments inflated its own chain to depth 290 against a cap of 8
    with a true generation count of 2, and was then refused with
    ``chain_depth_exceeded`` — a safety guard firing on a signal unrelated to the
    recursion it exists to bound.

    The increment now happens exactly once, in ``spawn_persona``, at the point a
    dispatch is authorised — i.e. only when one agent actually causes another to
    start. Nothing about WHERE the depth is sourced from changed: the #4129
    server-written-row resolution and the #4128 no-silent-reset hardening are
    untouched, so a caller still cannot reset or forge it.
    """
    # Human senders ALWAYS start a new chain
    if resolved_identity.user_kind == "human":
        return {
            "correlation_id": str(uuid.uuid4()),
            "root_human_id": resolved_identity.user_id,
            "triggered_by": None,
            "is_human_rooted": True,
            "is_new_chain": True,
            "parent_invocation_id": None,
            "chain_depth": 0,
        }

    # Bot sender: apply pointer-vs-marker precedence (issue #1696)
    store = _get_correlation_store()
    pointer = store.read_pointer(channel_key)

    # Parse marker from comment/PR body if provided
    marker = None
    if marker_text:
        from common.marker_parse import parse_marker

        marker = parse_marker(marker_text)

    # Issue #4128: verify the marker signature ONCE, here, and let every branch
    # below read provenance through the verdict. Previously verify_marker() ran
    # only in the Rule-4 branch, so the three other branches trusted
    # marker-borne root_human_id / is_human_rooted / invocation_id / chain_depth
    # on sight — and marker_text comes from a GitHub comment or PR body, which
    # anyone who can comment controls.
    #
    # marker_sig is the verdict from common/marker_verify.py:
    #   True  — signature valid under the current or previous key
    #   False — signature present but does NOT verify (forged/corrupted)
    #   None  — indeterminate: unsigned marker, no key configured, or (since
    #           #4128) the signing secret still holds the un-rotated placeholder
    marker_sig: bool | None = None
    if marker is not None and not marker_trusted:
        from common.marker_verify import verify_marker as _verify_marker

        marker_sig = _verify_marker(marker)
        if marker_sig is False:
            logger.warning(
                "Marker signature verification FAILED (forged): "
                "correlation_id=%s, claimed_root_human=%s, sender=%s — "
                "marker authority discarded",
                marker.get("correlation_id"),
                marker.get("root_human_id"),
                resolved_identity.user_id,
            )
    elif marker is not None:
        # Server-synthesized marker (from a server-written pointer) — unsigned
        # by construction, but not attacker-supplied. Trust it as verified.
        marker_sig = True

    # Whether marker-borne provenance may be trusted at all. A forged signature
    # is discarded outright. An indeterminate marker (unsigned / no key) may
    # still supply non-authority fields, but never an is_human_rooted=true
    # escalation — that is the fail-closed policy #3179 established for Rule 4
    # and #4128 extends to every path.
    marker_forged = marker_sig is False

    # Precedence resolution for bot senders:
    # 1. Pointer + marker with SAME correlation_id → pointer wins (authoritative)
    # 2. Pointer + marker with DIFFERENT correlation_id → marker wins (cross-channel hop)
    # 3. Pointer only (no marker) → pointer (same-channel continuation)
    # 4. Marker only (no pointer) → marker (cross-channel first hop)
    # 5. Neither → new chain

    # Issue #4128: a FORGED marker is discarded entirely, before precedence is
    # resolved. This is what closes all three previously-unverified paths at
    # once, because the marker is what SELECTS the branch:
    #   - Rule 1 (pointer + same correlation): the marker can no longer supply
    #     the parent_invocation_id fallback → pointer-only path.
    #   - Rule 2 (pointer + DIFFERENT correlation): a forged cross-channel claim
    #     can no longer redirect the chain or its root human → pointer-only path,
    #     i.e. the server-written pointer wins.
    #   - Rule 4 (marker only): unchanged from #3179 — falls through to a new
    #     bot-rooted chain.
    if marker_forged:
        marker = None

    if pointer and marker:
        if pointer["correlation_id"] == marker.get("correlation_id"):
            # Same chain — pointer is authoritative for the CHAIN (server-written).
            # But the parent edge can be missing from this channel's pointer: e.g.
            # a PR-channel pointer written by the worker without
            # triggering_invocation_id (issue #1738). The marker (synthesized from
            # the issue pointer, or carried in the PR body) holds the producing
            # run's invocation id, so fall back to it when the pointer lacks one.
            # This is what actually populates parent_invocation_id across the
            # issue→PR boundary.
            #
            # Issue #4129: root_human_id / is_human_rooted / chain_depth are
            # resolved from the webhook-events GSI, NOT read off this row. The
            # row is pod-writable, so trusting it here is the laundering hop
            # that turns a forged pointer into a server-blessed authority.
            root_human_id, is_human_rooted, pointer_depth = _resolve_pointer_provenance(
                pointer, resolved_identity.user_id
            )
            inherited_depth = pointer_depth if pointer_depth is not None else 0
            return {
                "correlation_id": pointer["correlation_id"],
                "root_human_id": root_human_id,
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": is_human_rooted,
                "is_new_chain": False,
                "parent_invocation_id": (
                    pointer.get("triggering_invocation_id") or marker.get("invocation_id")
                ),
                # Issue #4268: inherited unchanged — spawn_persona owns the increment.
                "chain_depth": inherited_depth,
                "last_triggered_persona": pointer.get("last_triggered_persona"),
                # Issue #2149: preserve cross-persona loop tracking from pointer
                "recent_triggered_personas": pointer.get("recent_triggered_personas", set()),
                "recent_trigger_count": pointer.get("recent_trigger_count", 0),
            }
        else:
            # Different correlation — marker represents cross-channel hop (Rule 2).
            #
            # Issue #4128: this is the THIRD provenance path, and it is the one
            # #4073's design did not name. It is materially the same hole as
            # Rule 4: the marker's OWN correlation_id and root_human_id win over
            # the server-written pointer's, so an unsigned marker here mints a
            # cross-channel chain under any claimed human. Apply exactly the
            # Rule-4 fail-closed policy: an unsigned marker may continue lineage
            # but may NOT claim human-rooted authority.
            marker_depth = marker.get("chain_depth")
            inherited_depth = marker_depth if marker_depth is not None else 0
            claims_human_rooted = marker.get("is_human_rooted", False)
            if marker_sig is None and claims_human_rooted:
                logger.warning(
                    "Rule-2 unsigned marker claims is_human_rooted=true — "
                    "stripping authority (fail-closed): correlation_id=%s, "
                    "claimed_root_human=%s, sender=%s",
                    marker.get("correlation_id"),
                    marker.get("root_human_id"),
                    resolved_identity.user_id,
                )
                claims_human_rooted = False
            return {
                "correlation_id": marker["correlation_id"],
                "root_human_id": marker.get("root_human_id", resolved_identity.user_id),
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": claims_human_rooted,
                "is_new_chain": False,
                "parent_invocation_id": marker.get("invocation_id"),
                # Issue #4268: inherited unchanged — spawn_persona owns the increment.
                "chain_depth": inherited_depth,
            }

    if pointer:
        # Pointer only — same-channel continuation.
        # Issue #4129: same server-side resolution as the pointer+marker branch.
        root_human_id, is_human_rooted, pointer_depth = _resolve_pointer_provenance(
            pointer, resolved_identity.user_id
        )
        inherited_depth = pointer_depth if pointer_depth is not None else 0
        return {
            "correlation_id": pointer["correlation_id"],
            "root_human_id": root_human_id,
            "triggered_by": resolved_identity.user_id,
            "is_human_rooted": is_human_rooted,
            "is_new_chain": False,
            "parent_invocation_id": pointer.get("triggering_invocation_id"),
            # Issue #4268: inherited unchanged — spawn_persona owns the increment.
            "chain_depth": inherited_depth,
            "last_triggered_persona": pointer.get("last_triggered_persona"),
            # Issue #2149: preserve cross-persona loop tracking from pointer
            "recent_triggered_personas": pointer.get("recent_triggered_personas", set()),
            "recent_trigger_count": pointer.get("recent_trigger_count", 0),
        }

    if marker:
        # Marker only — cross-channel first hop (Rule 4).
        # Issue #3179 (cred-binding S5): verify marker signature before trusting
        # marker-borne root_human_id/is_human_rooted. Unsigned or forged markers
        # in Rule-4 position confer no root-human authority → new chain.
        #
        # Issue #4128: the verdict is now computed once above (marker_sig) and
        # shared with the pointer branches, instead of being computed here only.
        # A forged marker never reaches this branch at all — it was dropped
        # before precedence resolution — so the False case is unreachable here
        # and kept only as a defensive assertion of the fail-closed intent.
        sig_result = marker_sig

        if sig_result is False:
            # Forged signature — do NOT trust marker authority. Log and fall
            # through to new-chain branch (fail-closed).
            logger.warning(
                "Rule-4 marker signature verification FAILED (forged): "
                "correlation_id=%s, sender=%s",
                marker.get("correlation_id"),
                resolved_identity.user_id,
            )
        elif sig_result is None and marker.get("is_human_rooted", False):
            # Unsigned marker claiming human-rooted authority in Rule-4 position.
            # Fail-closed: strip is_human_rooted (cannot be trusted without sig).
            logger.warning(
                "Rule-4 unsigned marker claims is_human_rooted=true — "
                "stripping authority (fail-closed): correlation_id=%s, sender=%s",
                marker.get("correlation_id"),
                resolved_identity.user_id,
            )
            marker_depth = marker.get("chain_depth")
            inherited_depth = marker_depth if marker_depth is not None else 0
            return {
                "correlation_id": marker["correlation_id"],
                "root_human_id": marker.get("root_human_id", resolved_identity.user_id),
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": False,  # Stripped — unsigned, fail-closed
                "is_new_chain": False,
                "parent_invocation_id": marker.get("invocation_id"),
                # Issue #4268: inherited unchanged — spawn_persona owns the increment.
                "chain_depth": inherited_depth,
            }
        else:
            # sig_result is True (verified) or None with is_human_rooted=False
            # (no escalation concern) — trust the marker.
            marker_depth = marker.get("chain_depth")
            inherited_depth = marker_depth if marker_depth is not None else 0
            return {
                "correlation_id": marker["correlation_id"],
                "root_human_id": marker.get("root_human_id", resolved_identity.user_id),
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": marker.get("is_human_rooted", False),
                "is_new_chain": False,
                "parent_invocation_id": marker.get("invocation_id"),
                # Issue #4268: inherited unchanged — spawn_persona owns the increment.
                "chain_depth": inherited_depth,
            }

    # No pointer, no marker — bot-initiated chain (e.g. cron-like, CI-triggered)
    return {
        "correlation_id": str(uuid.uuid4()),
        "root_human_id": resolved_identity.user_id,
        "triggered_by": None,
        "is_human_rooted": False,
        "is_new_chain": True,
        "parent_invocation_id": None,
        "chain_depth": 0,
    }


_rate_limiter = None


def _get_rate_limiter():
    """Return a cached RateLimiter bound to the configured table."""
    global _rate_limiter
    if _rate_limiter is None:
        from common.rate_limit import (
            DEFAULT_LIMIT_PER_HOUR,
            DEFAULT_LIMIT_PER_WINDOW,
            RateLimiter,
        )

        rate_limits_table = os.environ.get("RATE_LIMITS_TABLE", "")
        if not rate_limits_table:
            logger.error("RATE_LIMITS_TABLE env var is not set")

        # Env-tunable limits (per-window = 5min bucket, per-hour = 12 buckets).
        # Defaults preserve prior behavior; raise via Lambda env to clear backlogs
        # without redeploying code. Invalid values fall back to defaults.
        try:
            limit_per_window = int(
                os.environ.get("RATE_LIMIT_PER_WINDOW") or DEFAULT_LIMIT_PER_WINDOW
            )
        except ValueError:
            logger.warning("RATE_LIMIT_PER_WINDOW is not an int; using default")
            limit_per_window = DEFAULT_LIMIT_PER_WINDOW
        try:
            limit_per_hour = int(os.environ.get("RATE_LIMIT_PER_HOUR") or DEFAULT_LIMIT_PER_HOUR)
        except ValueError:
            logger.warning("RATE_LIMIT_PER_HOUR is not an int; using default")
            limit_per_hour = DEFAULT_LIMIT_PER_HOUR

        _rate_limiter = RateLimiter(
            table_name=rate_limits_table,
            region=os.environ.get("AWS_REGION", "us-east-1"),
            limit_per_window=limit_per_window,
            limit_per_hour=limit_per_hour,
        )
    return _rate_limiter


def _get_sqs_publisher():
    global _sqs_mod
    if _sqs_mod is None:
        from common import sqs_publisher

        _sqs_mod = sqs_publisher
    return _sqs_mod


def _get_events_log():
    global _events_log_mod
    if _events_log_mod is None:
        from common import webhook_events_log

        _events_log_mod = webhook_events_log
    return _events_log_mod


def _get_webhook_event_logger():
    """Lazy-init WebhookEventLogger for DynamoDB capture."""
    global _webhook_event_logger
    if _webhook_event_logger is None:
        table_name = os.environ.get("EVENTS_TABLE", "")
        if table_name:
            from common.webhook_events import WebhookEventLogger

            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            _webhook_event_logger = WebhookEventLogger(table_name=table_name, region=region)
    return _webhook_event_logger


def _is_eventbridge_event(event: dict) -> bool:
    """Detect EventBridge event shape.

    EventBridge events have `source`, `detail-type`, and `detail` at top level.
    API Gateway events have `headers` (or `requestContext`).

    Issue #2154: shape-based routing for multi-source ingress.
    """
    return (
        "source" in event
        and "detail-type" in event
        and "detail" in event
        and "headers" not in event
        and "requestContext" not in event
    )


def handler(event: dict, context) -> dict:
    """Lambda entry point — routes to GitHub or EventBridge handler by event shape.

    Args:
        event: API Gateway event (REST/HTTP API) or EventBridge event.
        context: Lambda context object.

    Returns:
        Response dict (API Gateway format or plain dict for EventBridge).
    """
    # The recovery alias, not request content, selects this privileged path.
    # Check it before generic EventBridge routing so ordinary schedules remain unchanged.
    from common import task_dispatch

    if task_dispatch.invoked_alias(context) == task_dispatch.RECOVERY_ALIAS:
        return task_dispatch.handle_recovery_event(event, context)

    # Issue #2154: shape-based routing for EventBridge events
    if _is_eventbridge_event(event):
        from eventbridge.handler import handle_eventbridge

        return handle_eventbridge(event, context)

    # Issue #2152: dispatch /agent/trigger to the IAM-authenticated handler
    # BEFORE HMAC validation (IAM auth is handled by API Gateway, not HMAC).
    resource = event.get("resource", "")
    if resource == "/agent/trigger":
        from agent_trigger import handle_agent_trigger

        return handle_agent_trigger(event, context)

    # Issue #5795: dispatch POST /v1/tasks to the Task API handler. The import
    # is local to this branch so a task-only import or initialization failure
    # cannot affect the GitHub, EventBridge or agent-trigger paths above
    # (T2-AC04); the route is authenticated by the gateway, not by HMAC.
    if resource == "/v1/tasks":
        from task_api.handler import handle_task_submit

        return handle_task_submit(event, context)

    start_time = time.time()
    print("DBG handler:start")

    # 1. Extract headers + body
    # Normalize header keys to lowercase: REST API v1 preserves original case
    # (e.g. X-Hub-Signature-256) while HTTP API v2 lowercases. This one-line
    # normalization makes the handler safe for both API types.
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    raw_body = event.get("body", "")
    is_base64 = event.get("isBase64Encoded", False)

    if is_base64:
        body_bytes = base64.b64decode(raw_body)
    else:
        body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body

    print(
        f"DBG handler:headers x-github-event={headers.get('x-github-event', '')!r} x-github-delivery={headers.get('x-github-delivery', '')!r} content-type={headers.get('content-type', '')!r} body_len={len(body_bytes)}"
    )

    # 2. Verify HMAC signature
    signature_header = headers.get("x-hub-signature-256", "")
    webhook_secret = _resolve_webhook_secret()
    print(
        f"DBG handler:sig sig_header_len={len(signature_header)} secret_len={len(webhook_secret)}"
    )
    if not _get_signature().verify_github_signature(body_bytes, signature_header, webhook_secret):
        print("DBG handler:sig FAIL invalid_signature")
        _log_outcome(
            event_type="unknown",
            action="",
            installation_id=0,
            tenant_id=None,
            repo="",
            persona=None,
            outcome="invalid_signature",
            start_time=start_time,
        )
        return _response(401, {"error": "Invalid signature"})
    print("DBG handler:sig OK")

    # 3. Parse event type from X-GitHub-Event header
    event_type = headers.get("x-github-event", "")
    if not event_type:
        print("DBG handler:no_event_type")
        return _response(400, {"error": "Missing X-GitHub-Event header"})

    # Parse body
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        print("DBG handler:invalid_json")
        return _response(400, {"error": "Invalid JSON body"})

    action = payload.get("action", "")
    print(f"DBG handler:event event_type={event_type!r} action={action!r}")

    # Issue #538: Bypass identity resolution for installation lifecycle events.
    # These arrive during onboarding before any identity row exists. We also
    # use this opportunity to auto-register the installation_id → org_id
    # mapping so subsequent webhooks (issue comments, PRs) route correctly
    # without operator intervention.
    if event_type == "installation" and action in ("created", "new_permissions_accepted"):
        install = payload.get("installation", {}) or {}
        install_id = install.get("id", 0)
        org_login = (install.get("account") or {}).get("login", "")
        if install_id and org_login:
            # Issue #4047 (#2724 slice C): `created` is a genuinely fresh install,
            # so it must always re-resolve — never inherit a negative-cache row
            # written before the tenant existed. `new_permissions_accepted` fires
            # on an EXISTING install (a scope change), so it is not a fresh
            # install and keeps using the cache.
            registered = _auto_register_installation(
                install_id,
                org_login,
                bypass_negative_cache=(action == "created"),
            )
            # Issue #2724 (slice B): seed per-tenant credentials ONLY when the
            # tenant was resolved via the authoritative path. "Registered" is not
            # enough — a non-authoritative registration is the org_login fallback
            # taken because the gate could not be evaluated, and seeding on it
            # copies the platform App's private key for an org nobody vouched for.
            if registered.tenant_id and registered.authoritative:
                _auto_provision_tenant_github_app_secret(registered.tenant_id, install_id)
            elif registered.tenant_id:
                logger.warning(
                    "Skipping per-tenant secret provisioning for installation_id=%d "
                    "tenant=%s — registration was not authoritative (see "
                    "AutoRegisterGateUnavailable)",
                    install_id,
                    registered.tenant_id,
                )
        logger.info("Installation %s event — no agent dispatch, no identity check", action)
        return _response(200, {"status": "no_op", "reason": "installation_event"})

    # 4. Extract installation_id + sender from payload
    installation_id = payload.get("installation", {}).get("id", 0)
    repo = payload.get("repository", {}).get("full_name", "")
    sender = payload.get("sender", {})
    sender_id = sender.get("id", 0)
    print(
        f"DBG handler:identity install_id={installation_id} repo={repo!r} sender_id={sender_id} sender_login={sender.get('login', '')!r}"
    )

    # 4a. Issue #2732: Advisory sibling-App detection. Run BEFORE identity
    # resolution — a foreign ADP deployment's bot comment resolves as
    # `unknown_user` and 403s below, so detection must happen first. This is
    # observability only: it emits a metric + deduped WARNING and never blocks.
    if event_type == "issue_comment":
        _detect_sibling_app(payload, repo)

    # 5. Resolve identity (tenant + sender) via identity-index
    resolved, outcome_reason = _get_identity_resolver().resolve(installation_id, sender_id)
    print(f"DBG handler:resolved resolved={resolved is not None} outcome_reason={outcome_reason!r}")

    # 5a. Self-heal unknown installation: if the webhook tells us the repo's
    # GitHub org (e.g. `acme-hackathon`) and we don't have an installation
    # row yet, auto-register it using the org login as the ADP tenant id.
    # This makes the routing "if the org is registered, it just works" —
    # no operator needs to manually map each App installation.
    if resolved is None and outcome_reason == "unknown_installation" and installation_id:
        repo_obj = payload.get("repository", {}) or {}
        org_obj = payload.get("organization") or {}
        org_login = org_obj.get("login") or (repo_obj.get("owner") or {}).get("login") or ""
        if org_login:
            registered_org = _auto_register_installation(installation_id, org_login)
            if registered_org.tenant_id:
                # Issue #2724 (slice B): same rule as the installation-event path
                # above — credentials only on an authoritative registration.
                if registered_org.authoritative:
                    _auto_provision_tenant_github_app_secret(
                        registered_org.tenant_id, installation_id
                    )
                else:
                    logger.warning(
                        "Skipping per-tenant secret provisioning for installation_id=%d "
                        "tenant=%s — registration was not authoritative (see "
                        "AutoRegisterGateUnavailable)",
                        installation_id,
                        registered_org.tenant_id,
                    )
                # Retry resolution now that the row exists
                resolved, outcome_reason = _get_identity_resolver().resolve(
                    installation_id, sender_id
                )

    # 6. Auto-provision path: if sender unknown but tenant allows auto-provision
    if resolved is None and outcome_reason == "unknown_user" and installation_id:
        # Re-check: we need the tenant item to know provisioning mode.
        # The resolver already returned the reason, so we do a targeted retry.
        # Peek at the tenant's provisioning mode by resolving just the installation.
        _resolver = _get_identity_resolver()
        table = _resolver._get_table()
        tenant_resp = table.get_item(
            Key={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
            }
        )
        tenant_item = tenant_resp.get("Item")
        if tenant_item and tenant_item.get("user_provisioning_mode") == "auto_provision":
            # Attempt auto-provision via Gateway admin API
            org_id = tenant_item["org_id"]
            provisioned = _get_gateway_client().auto_provision_user(
                org_id=org_id,
                github_id=sender_id,
                github_login=sender.get("login", ""),
            )
            if provisioned:
                # Retry resolution after provisioning
                resolved, outcome_reason = _resolver.resolve(installation_id, sender_id)

    # 7. If identity resolution failed → 403 Forbidden
    if resolved is None:
        print(f"DBG handler:reject_403 outcome={outcome_reason!r}")
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=None,
            repo=repo,
            persona=None,
            outcome=outcome_reason,
            start_time=start_time,
        )
        # Emit CloudWatch metric with RejectedReason dimension
        try:
            _get_metrics().record_rejected(reason=outcome_reason)
            _get_metrics().flush()
        except Exception:
            pass  # Best-effort — never block the response
        return _response(403, {"error": "unknown_identity", "outcome": outcome_reason})

    tenant_id = resolved.tenant_id

    # 6b. Issue #3134: Enforce min_author_association from the installation row.
    # The comment payload's comment.author_association field is checked against
    # the installation's min_author_association threshold when set. Zero-cost:
    # the field is already in every payload, currently ignored.
    # Pass the already-fetched tenant_item to avoid a duplicate DDB GetItem.
    resolver_mod = _get_identity_resolver()
    min_assoc = _check_min_author_association(
        payload=payload,
        event_type=event_type,
        installation_id=installation_id,
        tenant_item=getattr(resolver_mod, "last_tenant_item", None),
    )
    if min_assoc is not None:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=None,
            outcome="insufficient_association",
            start_time=start_time,
        )
        try:
            _get_metrics().record_rejected(reason="insufficient_association")
            _get_metrics().flush()
        except Exception:
            pass
        return _response(
            403, {"error": "insufficient_association", "outcome": "insufficient_association"}
        )

    # 7. Check rate limit (class-based API — returns a RateLimitResult)
    rate_result = _get_rate_limiter().check_and_increment(tenant_id)
    allowed = rate_result.allowed
    retry_after = rate_result.retry_after_seconds

    print(f"DBG handler:rate_limit allowed={allowed} retry_after={retry_after}")
    # 8. If rate-limited → return 429 with Retry-After
    if not allowed:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=None,
            outcome="rate_limited",
            start_time=start_time,
        )
        _capture_invocation_event(
            envelope=None,
            tenant_id=tenant_id,
            user_id=resolved.user_id,
            github_login=sender.get("login", ""),
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            repo=repo,
            persona=None,
            payload=payload,
            correlation_id=None,
            status="rate_limited",
        )
        return _response(
            429, {"error": "Rate limited", "retry_after": retry_after}, retry_after=retry_after
        )

    # 9. Determine correlation context (read-only — no DDB writes here)
    # Issue #1696: Build correlation for BOTH issue_comment AND pull_request events.
    # For bot senders, pass the comment/PR body as marker_text for cross-channel
    # lineage inheritance.
    correlation_ctx = None
    channel_key_str = ""
    if event_type == "issue_comment":
        issue_number = payload.get("issue", {}).get("number")
        if issue_number and repo:
            store = _get_correlation_store()
            channel_key_str = store.channel_key("github", repo, "issue", issue_number)
            # Extract marker text from comment body for bot senders
            marker_text = (
                payload.get("comment", {}).get("body", "")
                if sender.get("type") == "Bot" or sender.get("login", "").endswith("[bot]")
                else None
            )
            correlation_ctx = determine_correlation(
                payload, resolved, channel_key_str, marker_text=marker_text
            )
    elif event_type == "pull_request":
        # Issue #1696: PR events use pull_request.number as the channel identifier.
        pr_number = payload.get("pull_request", {}).get("number")
        if pr_number and repo:
            store = _get_correlation_store()
            channel_key_str = store.channel_key("github", repo, "pr", pr_number)
            is_bot = sender.get("type") == "Bot" or sender.get("login", "").endswith("[bot]")
            pr_body = payload.get("pull_request", {}).get("body", "") if is_bot else None
            head_ref = payload.get("pull_request", {}).get("head", {}).get("ref", "")
            # Issue #4128: marker_trusted is True only for the server-synthesized
            # fallback marker; a marker read out of the PR body is verified.
            marker_text, marker_trusted = _pr_marker_text_with_issue_fallback(
                store, repo, pr_body, head_ref, resolved.user_id
            )
            correlation_ctx = determine_correlation(
                payload,
                resolved,
                channel_key_str,
                marker_text=marker_text,
                marker_trusted=marker_trusted,
            )

    # 10. Parse intent (with correlation context for chain-aware bot logic)
    # Issue #4020: use the reason-returning entry point so a no-op's cause can be
    # persisted on the Activity row instead of only reaching CloudWatch.
    from intent_parser import extract_intent_with_reason

    intent, skip_reason = extract_intent_with_reason(
        event_type,
        payload,
        correlation_ctx=correlation_ctx,
        resolved_identity=resolved,
    )

    print(f"DBG handler:intent intent={intent!r}")
    # 11. If no actionable intent → log + return 200 (no-op)
    # IMPORTANT: Do NOT write pointer or provenance here — prevents channel poisoning
    if intent is None:
        from common import skip_reasons as skip_reasons_mod

        # Issue #4599: reuse the shared author-kind predicate rather than
        # re-deriving it. It is the same signal `spawn_persona` gates bot dispatch
        # on, so a comment that would not be allowed to spawn an agent also cannot
        # provoke an engine-command refusal reply.
        from common.spawn_persona import _is_bot_sender as _is_bot_sender_for_engine

        # Issue #4527: an `@agent-engine` comment is a no-op for THIS Lambda but not
        # for the platform. Marking the row is the entire delivery mechanism: the
        # engine tick queries for pending engine commands on its next wake, parses
        # the body and acts. Nothing is enqueued and no gateway is called from here —
        # this component holds no VPC, no DB and no gateway reach, and must never
        # gain any (#4303 closed-routes table).
        is_engine_command = skip_reason == skip_reasons_mod.ENGINE_COMMAND

        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=None,
            outcome="no_op",
            start_time=start_time,
        )
        _capture_invocation_event(
            envelope=None,
            tenant_id=tenant_id,
            user_id=resolved.user_id,
            github_login=sender.get("login", ""),
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            repo=repo,
            persona=None,
            payload=payload,
            correlation_id=correlation_ctx["correlation_id"] if correlation_ctx else None,
            status="no_op",
            parent_invocation_id=(
                correlation_ctx.get("parent_invocation_id") if correlation_ctx else None
            ),
            chain_depth=correlation_ctx.get("chain_depth") if correlation_ctx else None,
            # Issue #4020: the reason the intent parser declined to dispatch.
            skip_reason=skip_reason,
            # Issue #4527: the command text and the commenter's numeric GitHub id
            # travel ONLY on the engine path. The tick has no other way to reach
            # them; every other no-op row would be paying storage for a body
            # nothing reads.
            engine_command=is_engine_command,
            comment_body=payload.get("comment", {}).get("body") if is_engine_command else None,
            sender_github_id=str(sender_id) if is_engine_command and sender_id else None,
            # Issue #4599: author-kind, reusing the SAME predicate that gates
            # persona dispatch rather than a second copy of the rule. Bot logins
            # end in `[bot]` and carry `type == "Bot"`, which covers the platform's
            # own `aws-e-adp-agent-*` accounts without a login-prefix match that
            # would break the day one is renamed.
            sender_is_bot=_is_bot_sender_for_engine(sender) if is_engine_command else False,
            # Issue #4539: the remaining members of the signed authority tuple.
            # `delivery_id` is GitHub's own delivery identity, `repo_id` the numeric
            # repository id (a repo can be renamed, its id cannot) and `sender_type`
            # GitHub's author kind. Each is signed, so the tick decides who acted and
            # where a reply goes from values bound to a verified delivery rather than
            # from mutable row attributes.
            delivery_id=headers.get("x-github-delivery", "") if is_engine_command else "",
            sender_type=str(sender.get("type", "")) if is_engine_command else "",
            repo_id=(
                int(payload.get("repository", {}).get("id", 0) or 0) if is_engine_command else 0
            ),
        )
        # Echo the reason in the body for parity with the guard-block response
        # below, which has always included it.
        body: dict = {"status": "no_op"}
        if skip_reason:
            body["reason"] = skip_reason
        return _response(200, body)

    # 12. Intent is not None — delegate to spawn_persona() for guards + publish.
    # Issue #2151: All loop guards, pointer/provenance writes, envelope build,
    # DDB capture, and SQS publish are now in the shared spawn_persona() function.
    # This is the SINGLE enforcement point — no drift across trigger adapters.
    from common.spawn_persona import spawn_persona as _spawn_persona

    # Issue #2279: Resolve /model directive if present on intent.
    # Validate the alias inline (no gateway HTTP call). If invalid, we still
    # run the agent with the default model (lenient path — worker posts warning).
    #
    # PMM-07 keeps two answers apart here, and the distinction is load-bearing:
    #
    # * ``model_resolved`` is the LEGACY assignment — what the worker actually
    #   executes. While the posture is ``report_only`` it must stay byte-for-byte
    #   what it was before PMM-07, so the strict catalogue cannot change, or
    #   fail, a live run.
    # * ``model_canonical`` is the strict published answer, carried separately
    #   into protected authority as the *proposed* override. When it refuses, the
    #   gateway resolver records ``direct_override_unresolved`` — a refusal, not
    #   permission to fall through to a mapping or default.
    #
    # Collapsing them regressed live behaviour: a strict refusal read downstream
    # as "no directive", and the worker substituted its own default, silently
    # changing the model the user asked for.
    model_requested = intent.model  # raw alias or None
    model_resolved = None
    model_canonical = None
    if model_requested:
        from common.model_validate import resolve_canonical_override, resolve_legacy_assignment

        model_resolved = resolve_legacy_assignment(model_requested)
        model_canonical = resolve_canonical_override(model_requested)
        if model_resolved:
            logger.info(
                "handler: /model directive resolved %r -> %r (proposed=%r)",
                model_requested,
                model_resolved,
                model_canonical,
            )
        else:
            logger.info(
                "handler: /model directive %r rejected (unknown or disallowed) "
                "— proceeding with default model (lenient)",
                model_requested,
            )

    # Issue #3574: Read /aws-label from intent. Already charset-validated in
    # intent_parser — if it passed parsing, it's [A-Za-z0-9_-]{1,64}. No further
    # resolution needed (unlike /model, the label is passed as-is to the gateway;
    # the gateway 404s on unknown label).
    aws_label = intent.aws_label  # validated label or None
    if aws_label:
        logger.info("handler: /aws-label directive %r accepted", aws_label)

    # Issue #3385 (C3): Read token_source_override from the identity-index
    # tenant item. When the installation's row carries token_source_override="pat",
    # the envelope gets token_source="pat" which tells the worker to resolve a
    # PAT instead of minting an App installation token. Absent = App default.
    resolver_mod_for_token = _get_identity_resolver()
    tenant_item = getattr(resolver_mod_for_token, "last_tenant_item", None)
    token_source = tenant_item.get("token_source_override") if tenant_item else None

    # Provide a default correlation_ctx if not available (e.g. issues.labeled
    # events where we didn't compute correlation above).
    effective_correlation_ctx = correlation_ctx or {
        "correlation_id": "",
        "root_human_id": resolved.user_id,
        "is_human_rooted": True,
        "is_new_chain": True,
        "parent_invocation_id": None,
        "chain_depth": 0,
    }

    trusted_human_event = None
    if (
        os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
        and resolved.user_kind == "human"
    ):
        from common.agent_authority import AuthorityProvisionError, VerifiedHumanEvent

        try:
            trusted_human_event = VerifiedHumanEvent.from_verified_webhook(
                body=body_bytes,
                event_type=event_type,
                resolved=resolved,
                sender=sender,
                tenant_id=tenant_id,
                repo=repo,
            )
        except AuthorityProvisionError:
            return _response(403, {"error": "human_authority_refused"})

    spawn_result = _spawn_persona(
        persona=intent.persona,
        correlation_ctx=effective_correlation_ctx,
        channel_key=channel_key_str,
        resolved_identity=resolved,
        tenant_id=tenant_id,
        actor_user_id=resolved.user_id,
        actor_org_id=resolved.org_id,
        sender=sender,
        event_type=event_type,
        action=action,
        installation_id=installation_id,
        repo=repo,
        payload=payload,
        intent_trigger=intent.trigger,
        intent_label=intent.label,
        model_requested=model_requested,
        model_resolved=model_resolved,
        model_canonical=model_canonical,
        aws_label=aws_label,
        token_source=token_source,
        **({"trusted_human_event": trusted_human_event} if trusted_human_event is not None else {}),
    )

    print(
        f"DBG handler:spawn_result success={spawn_result.success} "
        f"message_id={spawn_result.message_id!r} block_reason={spawn_result.block_reason!r}"
    )

    if not spawn_result.success:
        # Spawn was blocked by a guard or SQS failure
        outcome = spawn_result.block_reason or "error"
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=intent.persona,
            outcome=outcome,
            start_time=start_time,
        )
        if spawn_result.block_reason == "sqs_publish_failed":
            return _response(500, {"error": "Failed to enqueue"})
        # Guard blocks are not errors — return 200 no_op
        return _response(200, {"status": "no_op", "reason": outcome})

    # 13. Log success + return 202
    _log_outcome(
        event_type=event_type,
        action=action,
        installation_id=installation_id,
        tenant_id=tenant_id,
        repo=repo,
        persona=intent.persona,
        outcome="published",
        start_time=start_time,
    )

    return _response(202, {"status": "accepted", "message_id": spawn_result.message_id})


def _capture_invocation_event(
    *,
    envelope: dict | None,
    tenant_id: str,
    user_id: str,
    github_login: str,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    persona: str | None,
    payload: dict,
    correlation_id: str | None,
    status: str,
    parent_invocation_id: str | None = None,
    chain_depth: int | None = None,
    root_human_id: str | None = None,
    is_human_rooted: bool | None = None,
    skip_reason: str | None = None,
    engine_command: bool = False,
    comment_body: str | None = None,
    sender_github_id: str | None = None,
    sender_is_bot: bool = False,
    delivery_id: str = "",
    sender_type: str = "",
    repo_id: int = 0,
) -> bool:
    """Write the enriched invocation row and report whether it was durable.

    Most callers retain best-effort semantics by ignoring the return value. The
    independent reviewer treats ``False`` as a dispatch failure because its
    gateway adapter uses this row as the assignment proof.

    Uses envelope's message_id/arrived_at as keys so the worker can UpdateItem
    on the same row. For terminal-at-ingress statuses (rate_limited, no_op),
    envelope is None and keys are auto-generated.

    Issue #4020: ``skip_reason`` carries WHY a non-dispatching status happened,
    so the Activity UI can explain a no_op row rather than showing a bare badge.

    Issue #4527: ``engine_command`` marks the row for the orchestration engine's
    tick to consume. ``comment_body`` and ``sender_github_id`` are forwarded ONLY
    on that path — they are the command text and the identity to resolve, and the
    tick has no other way to reach them. They are deliberately not written on
    every row: a comment body on every no-op would balloon the table and put
    arbitrary user text on rows nothing reads.

    Issue #4599: ``sender_is_bot`` travels the same path, for the same reason —
    author-kind is on the payload here and nowhere the tick can see it. The tick
    uses it to stay quiet rather than post a refusal at a bot's own comment.

    Issue #4539: on the engine path the row's authority tuple is SIGNED here, under
    a dedicated key, and the signature is stored with the marker. This is the point
    at which the row's own keys (``event_id``, ``arrived_at``) are finally known,
    and those keys are part of what is signed — a signature that did not cover them
    would be liftable onto a different row. ``delivery_id``, ``sender_type`` and
    ``repo_id`` are forwarded for the same reason ``comment_body`` is: they are
    payload facts the tick cannot see, and every one of them is in the signed set.

    Signing happens strictly after GitHub's own HMAC check (step 2 of the handler),
    so the signature attests to a verified delivery. It cannot be reached on any
    path where that check did not pass.
    """
    try:
        event_logger = _get_webhook_event_logger()
        if event_logger is None:
            return False

        # Extract keys from envelope (THE KEY CONTRACT)
        event_id = envelope["message_id"] if envelope else None
        arrived_at = envelope["arrived_at"] if envelope else None

        # Derive topic from issue/PR title
        issue_title = payload.get("issue", {}).get("title", "")
        pr_title = payload.get("pull_request", {}).get("title", "")
        topic = (issue_title or pr_title or "(untitled)")[:120]

        # Derive source_url
        issue_url = payload.get("issue", {}).get("html_url", "")
        pr_url = payload.get("pull_request", {}).get("html_url", "")
        source_url = issue_url or pr_url or None

        # Derive issue_number
        issue_number = payload.get("issue", {}).get("number")
        if issue_number is None:
            issue_number = payload.get("pull_request", {}).get("number")

        # Issue #4539: sign the authority tuple for engine commands only.
        #
        # `log_event` generates the row keys when they are absent, and the keys are
        # part of the signed set — so they are resolved HERE, before signing, and
        # passed explicitly. Otherwise the signature would cover a key the row does
        # not have, and every command would be refused.
        #
        # Every signed value is a server-side fact: the delivery header, the
        # payload of a delivery whose HMAC already verified, or the tenant this
        # handler resolved. Nothing a commenter can choose reaches the tuple except
        # the command body itself, which is exactly what the signature is meant to
        # bind to the rest.
        signature = key_id = signed_payload = None
        if engine_command:
            if not event_id:
                event_id = str(uuid.uuid4())
            if not arrived_at:
                arrived_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            signature, key_id, signed_payload = _sign_engine_command(
                delivery_id=delivery_id,
                event_type=event_type,
                event_id=event_id,
                arrived_at=arrived_at,
                tenant_id=tenant_id,
                installation_id=installation_id,
                repo_id=repo_id,
                repo=repo,
                issue_number=issue_number,
                sender_github_id=sender_github_id,
                sender_type=sender_type,
                command_body=comment_body,
            )

        result = event_logger.log_event(
            event_id=event_id,
            arrived_at=arrived_at,
            tenant_id=tenant_id,
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=str(installation_id),
            repo=repo,
            status=status,
            user_id=user_id or "unattributed",
            github_login=github_login or None,
            persona=persona,
            topic=topic,
            source_url=source_url,
            issue_number=issue_number,
            correlation_id=correlation_id,
            parent_invocation_id=parent_invocation_id,
            chain_depth=chain_depth,
            root_human_id=root_human_id,
            is_human_rooted=is_human_rooted,
            skip_reason=skip_reason,
            engine_command=engine_command,
            comment_body=comment_body,
            sender_github_id=sender_github_id,
            sender_is_bot=sender_is_bot,
            engine_command_signature=signature,
            engine_command_signing_key_id=key_id,
            engine_command_signed_payload=signed_payload,
        )
        return not result.get("write_failed", False)
    except Exception as e:
        # Best-effort — never block the webhook response
        logger.warning("Failed to capture invocation event: %s", e)
        return False


def _sign_engine_command(
    *,
    delivery_id: str,
    event_type: str,
    event_id: str,
    arrived_at: str,
    tenant_id: str,
    installation_id: int,
    repo_id: int,
    repo: str,
    issue_number: int | None,
    sender_github_id: str | None,
    sender_type: str,
    command_body: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Sign one engine command's authority tuple (issue #4539).

    Returns ``(signature, key_id, canonical_payload)``, or ``(None, None, None)``
    when the command cannot be signed honestly.

    An unsigned return is NOT an error path to be papered over: the row is written
    with its marker and without a signature, the tick refuses and quarantines it,
    and `webhook_events` counts it. That is deliberately more visible than either
    alternative — dropping the row would erase the audit record of a command a human
    really sent, and blocking the webhook response would turn a missing key into a
    GitHub-visible delivery failure for traffic that has nothing to do with the
    engine.

    Types are normalised here rather than in the signer, because the signer's
    strictness is the point: it refuses a ``str`` where an ``int`` belongs so the two
    deploy units can never disagree about which tuple they signed. The conversions
    below are the single place where payload shapes become protocol types, so a
    missing installation arrives as ``"0"`` and a missing issue as ``0`` — signed
    values that the verifier can then reject explicitly, rather than absences that
    silently skip a check.
    """
    from common import command_signing

    try:
        key_id, signature, envelope = command_signing.sign_command(
            delivery_id=delivery_id or "",
            event_type=event_type or "",
            event_id=event_id,
            arrived_at=arrived_at,
            tenant_id=tenant_id or "",
            installation_id=str(installation_id or 0),
            repo_id=int(repo_id or 0),
            repo=repo or "",
            issue_number=int(issue_number or 0),
            sender_github_id=str(sender_github_id or ""),
            sender_type=sender_type or "",
            command_body=command_body or "",
            signed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        # The stored payload is the signer's own canonical bytes, never a
        # re-serialization of the envelope here: a second `json.dumps` with
        # different arguments is exactly how the stored payload would stop
        # reproducing the signature.
        payload = command_signing.canonical_bytes(envelope).decode("utf-8")
        return signature, key_id, payload
    except Exception as e:
        logger.error(
            "Engine command %s could not be signed (%s: %s); the row will be "
            "written unsigned and the orchestration tick will refuse it",
            event_id,
            type(e).__name__,
            e,
        )
        return None, None, None


def _response(status_code: int, body: dict, *, retry_after: int = 0) -> dict:
    """Build API Gateway v2 response."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
        "headers": headers,
    }


def _log_outcome(
    *,
    event_type: str,
    action: str,
    installation_id: int,
    tenant_id: str | None,
    repo: str,
    persona: str | None,
    outcome: str,
    start_time: float,
    error: str | None = None,
) -> None:
    """Log webhook processing outcome."""
    latency_ms = (time.time() - start_time) * 1000
    try:
        _get_events_log().log_event(
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            intent_persona=persona,
            outcome=outcome,
            latency_ms=latency_ms,
            error=error,
        )
    except Exception as e:
        logger.warning("Failed to log event: %s", e)
