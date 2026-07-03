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
from typing import Any

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
    """
    global _webhook_secret
    if _webhook_secret is not None:
        return _webhook_secret

    if WEBHOOK_SECRET_ARN:
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


def _auto_register_installation(installation_id: int, org_login: str) -> str | None:
    """Write an installation_id → org_id row to the identity-index.

    Called when webhooks reveal a previously-unknown installation. We default
    the ADP org_id to the GitHub org login (e.g. `sophos-hackathon`). If that
    GitHub org's login isn't a known ADP tenant, we skip the write (caller
    will 403 with "unknown_installation"). This keeps the "if the org is
    registered we just work" contract without requiring ops to manually map
    each installation.

    Returns the org_id we wrote, or None if we couldn't determine a tenant.
    """
    if not org_login:
        return None
    resolver = _get_identity_resolver()
    try:
        table = resolver._get_table()
        # Idempotent write — if the row already exists, overwrite with the
        # same data. We use the GitHub org login as the ADP tenant id by
        # convention; if the operator wants a different tenant mapping,
        # they can overwrite this row explicitly.
        now = datetime.now(UTC).isoformat()
        # Identity rows are authoritative; offboarding deletes them explicitly.
        # No TTL — writers were previously setting a 7d/30d/365d expiry assuming
        # a reconcile job would refresh, but rows GC'd silently and broke webhook
        # routing for active users (#TBD bug).
        table.put_item(
            Item={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
                "org_id": org_login,
                "updated_at": now,
                "auto_registered": True,
            }
        )
        # Issue #2336: Write reverse-lookup row (org_id → installation_id)
        # so EventBridge/agent-trigger handlers can resolve a real
        # installation_id without calling the GitHub API.
        table.put_item(
            Item={
                "identity_type": "org_installation",
                "identity_value": org_login,
                "installation_id": installation_id,
                "updated_at": now,
                "auto_registered": True,
            }
        )
        logger.info(
            "Auto-registered installation_id=%d → org_id=%s (forward + reverse)",
            installation_id,
            org_login,
        )
        return org_login
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to auto-register installation_id=%d: %s", installation_id, exc)
        return None


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

    Failures are logged and swallowed — auto-register's DDB write has already
    succeeded; first-task crash is recoverable manually. Emits CloudWatch metric
    on failure for operator visibility.
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


_correlation_store_mod = None


def _get_correlation_store():
    global _correlation_store_mod
    if _correlation_store_mod is None:
        from common import correlation_store

        _correlation_store_mod = correlation_store
    return _correlation_store_mod


def _pr_marker_text_with_issue_fallback(store, repo, pr_body, head_ref) -> str | None:
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

    Returns marker text (PR body if it already has a valid marker, else a
    synthesized marker from the issue pointer), or pr_body unchanged when no
    fallback applies.
    """
    from common.marker_parse import has_valid_marker

    if pr_body and has_valid_marker(pr_body):
        return pr_body
    m = re.match(r"agent/issue-(\d+)", head_ref or "")
    if not m:
        return pr_body
    issue_channel = store.channel_key("github", repo, "issue", int(m.group(1)))
    issue_pointer = store.read_pointer(issue_channel)
    if not issue_pointer:
        return pr_body
    return (
        f"<!-- adp-correlation:{issue_pointer['correlation_id']} "
        f"adp-root-human:{issue_pointer['root_human_id']} "
        f"adp-is-human-rooted:{'true' if issue_pointer.get('is_human_rooted') else 'false'} "
        f"adp-invocation:{issue_pointer.get('triggering_invocation_id') or ''} "
        f"adp-chain-depth:{issue_pointer.get('chain_depth') or 0} -->"
    )


def determine_correlation(
    payload: dict,
    resolved_identity,
    channel_key: str,
    marker_text: str | None = None,
) -> dict[str, Any]:
    """Determine correlation context for this event (read-only).

    For human senders: always starts a new chain (overrides any stale pointer).
    For bot senders: uses pointer-vs-marker precedence (issue #1696):
      - Pointer exists AND correlation_id matches marker → use pointer
      - Pointer exists but correlation_id differs from marker → use marker (cross-channel hop)
      - No pointer → use marker
      - No marker and no pointer → new chain (fallback)

    Returns a dict with: correlation_id, root_human_id, triggered_by,
    is_human_rooted, is_new_chain, parent_invocation_id, chain_depth.
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

    # Precedence resolution for bot senders:
    # 1. Pointer + marker with SAME correlation_id → pointer wins (authoritative)
    # 2. Pointer + marker with DIFFERENT correlation_id → marker wins (cross-channel hop)
    # 3. Pointer only (no marker) → pointer (same-channel continuation)
    # 4. Marker only (no pointer) → marker (cross-channel first hop)
    # 5. Neither → new chain

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
            pointer_depth = pointer.get("chain_depth")
            inherited_depth = pointer_depth if pointer_depth is not None else 0
            return {
                "correlation_id": pointer["correlation_id"],
                "root_human_id": pointer["root_human_id"],
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": pointer["is_human_rooted"],
                "is_new_chain": False,
                "parent_invocation_id": (
                    pointer.get("triggering_invocation_id") or marker.get("invocation_id")
                ),
                "chain_depth": inherited_depth + 1,
                "last_triggered_persona": pointer.get("last_triggered_persona"),
                # Issue #2149: preserve cross-persona loop tracking from pointer
                "recent_triggered_personas": pointer.get("recent_triggered_personas", set()),
                "recent_trigger_count": pointer.get("recent_trigger_count", 0),
            }
        else:
            # Different correlation — marker represents cross-channel hop
            marker_depth = marker.get("chain_depth")
            inherited_depth = marker_depth if marker_depth is not None else 0
            return {
                "correlation_id": marker["correlation_id"],
                "root_human_id": marker.get("root_human_id", resolved_identity.user_id),
                "triggered_by": resolved_identity.user_id,
                "is_human_rooted": marker.get("is_human_rooted", False),
                "is_new_chain": False,
                "parent_invocation_id": marker.get("invocation_id"),
                "chain_depth": inherited_depth + 1,
            }

    if pointer:
        # Pointer only — same-channel continuation
        pointer_depth = pointer.get("chain_depth")
        inherited_depth = pointer_depth if pointer_depth is not None else 0
        return {
            "correlation_id": pointer["correlation_id"],
            "root_human_id": pointer["root_human_id"],
            "triggered_by": resolved_identity.user_id,
            "is_human_rooted": pointer["is_human_rooted"],
            "is_new_chain": False,
            "parent_invocation_id": pointer.get("triggering_invocation_id"),
            "chain_depth": inherited_depth + 1,
            "last_triggered_persona": pointer.get("last_triggered_persona"),
            # Issue #2149: preserve cross-persona loop tracking from pointer
            "recent_triggered_personas": pointer.get("recent_triggered_personas", set()),
            "recent_trigger_count": pointer.get("recent_trigger_count", 0),
        }

    if marker:
        # Marker only — cross-channel first hop
        marker_depth = marker.get("chain_depth")
        inherited_depth = marker_depth if marker_depth is not None else 0
        return {
            "correlation_id": marker["correlation_id"],
            "root_human_id": marker.get("root_human_id", resolved_identity.user_id),
            "triggered_by": resolved_identity.user_id,
            "is_human_rooted": marker.get("is_human_rooted", False),
            "is_new_chain": False,
            "parent_invocation_id": marker.get("invocation_id"),
            "chain_depth": inherited_depth + 1,
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
            registered = _auto_register_installation(install_id, org_login)
            if registered:
                _auto_provision_tenant_github_app_secret(registered, install_id)
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
    # GitHub org (e.g. `sophos-hackathon`) and we don't have an installation
    # row yet, auto-register it using the org login as the ADP tenant id.
    # This makes the routing "if the org is registered, it just works" —
    # no operator needs to manually map each App installation.
    if resolved is None and outcome_reason == "unknown_installation" and installation_id:
        repo_obj = payload.get("repository", {}) or {}
        org_obj = payload.get("organization") or {}
        org_login = org_obj.get("login") or (repo_obj.get("owner") or {}).get("login") or ""
        if org_login:
            registered_org = _auto_register_installation(installation_id, org_login)
            if registered_org:
                _auto_provision_tenant_github_app_secret(registered_org, installation_id)
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
            marker_text = _pr_marker_text_with_issue_fallback(store, repo, pr_body, head_ref)
            correlation_ctx = determine_correlation(
                payload, resolved, channel_key_str, marker_text=marker_text
            )

    # 10. Parse intent (with correlation context for chain-aware bot logic)
    from intent_parser import extract_intent

    intent = extract_intent(
        event_type,
        payload,
        correlation_ctx=correlation_ctx,
        resolved_identity=resolved,
    )

    print(f"DBG handler:intent intent={intent!r}")
    # 11. If no actionable intent → log + return 200 (no-op)
    # IMPORTANT: Do NOT write pointer or provenance here — prevents channel poisoning
    if intent is None:
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
        )
        return _response(200, {"status": "no_op"})

    # 12. Intent is not None — delegate to spawn_persona() for guards + publish.
    # Issue #2151: All loop guards, pointer/provenance writes, envelope build,
    # DDB capture, and SQS publish are now in the shared spawn_persona() function.
    # This is the SINGLE enforcement point — no drift across trigger adapters.
    from common.spawn_persona import spawn_persona as _spawn_persona

    # Issue #2279: Resolve /model directive if present on intent.
    # Validate the alias inline (no gateway HTTP call). If invalid, we still
    # run the agent with the default model (lenient path — worker posts warning).
    model_requested = intent.model  # raw alias or None
    model_resolved = None
    if model_requested:
        from common.model_validate import resolve_and_validate

        model_resolved = resolve_and_validate(model_requested)
        if model_resolved:
            logger.info(
                "handler: /model directive resolved %r -> %r",
                model_requested,
                model_resolved,
            )
        else:
            logger.info(
                "handler: /model directive %r rejected (unknown or disallowed) "
                "— proceeding with default model (lenient)",
                model_requested,
            )

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
) -> None:
    """Write enriched invocation row to DynamoDB (best-effort).

    Uses envelope's message_id/arrived_at as keys so the worker can UpdateItem
    on the same row. For terminal-at-ingress statuses (rate_limited, no_op),
    envelope is None and keys are auto-generated.
    """
    try:
        event_logger = _get_webhook_event_logger()
        if event_logger is None:
            return

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

        event_logger.log_event(
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
        )
    except Exception as e:
        # Best-effort — never block the webhook response
        logger.warning("Failed to capture invocation event: %s", e)


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
