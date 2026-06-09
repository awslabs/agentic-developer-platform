"""GitHub webhook Lambda — full handler.

Entry point for the GitHub channel webhook Lambda. Validates signatures,
parses events into agent intents, and publishes normalized envelopes to SQS.

Target execution time: <300ms.
This Lambda does NOT clone repos, call LLMs, or do heavy computation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
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
        logger.info(
            "Auto-registered installation_id=%d → org_id=%s",
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


_correlation_store_mod = None


def _get_correlation_store():
    global _correlation_store_mod
    if _correlation_store_mod is None:
        from common import correlation_store

        _correlation_store_mod = correlation_store
    return _correlation_store_mod


def determine_correlation(payload: dict, resolved_identity, channel_key: str) -> dict[str, Any]:
    """Determine correlation context for this event (read-only).

    For human senders: always starts a new chain (overrides any stale pointer).
    For bot senders: tries to inherit from DDB pointer, then falls back to
    starting a new bot-initiated chain.

    Returns a dict with: correlation_id, root_human_id, triggered_by,
    is_human_rooted, is_new_chain.
    """
    # Human senders ALWAYS start a new chain
    if resolved_identity.user_kind == "human":
        return {
            "correlation_id": str(uuid.uuid4()),
            "root_human_id": resolved_identity.user_id,
            "triggered_by": None,
            "is_human_rooted": True,
            "is_new_chain": True,
        }

    # Bot sender: try to inherit from upstream DDB pointer
    store = _get_correlation_store()
    pointer = store.read_pointer(channel_key)
    if pointer:
        return {
            "correlation_id": pointer["correlation_id"],
            "root_human_id": pointer["root_human_id"],
            "triggered_by": resolved_identity.user_id,
            "is_human_rooted": pointer["is_human_rooted"],
            "is_new_chain": False,
        }

    # No pointer found — bot-initiated chain (e.g. cron-like, CI-triggered)
    return {
        "correlation_id": str(uuid.uuid4()),
        "root_human_id": resolved_identity.user_id,
        "triggered_by": None,
        "is_human_rooted": False,
        "is_new_chain": True,
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


def handler(event: dict, context) -> dict:
    """Lambda entry point for GitHub webhook processing.

    Args:
        event: API Gateway event (REST API v1 or HTTP API v2).
        context: Lambda context object.

    Returns:
        API Gateway response dict.
    """
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
        return _response(
            429, {"error": "Rate limited", "retry_after": retry_after}, retry_after=retry_after
        )

    # 9. Determine correlation context (read-only — no DDB writes here)
    correlation_ctx = None
    channel_key_str = ""
    if event_type == "issue_comment":
        issue_number = payload.get("issue", {}).get("number")
        if issue_number and repo:
            store = _get_correlation_store()
            channel_key_str = store.channel_key("github", repo, "issue", issue_number)
            correlation_ctx = determine_correlation(payload, resolved, channel_key_str)

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
        return _response(200, {"status": "no_op"})

    # 12. Intent is not None — write provenance + pointer (fail-soft) BEFORE SQS publish
    if correlation_ctx and channel_key_str:
        # Post provenance record (fail-soft)
        try:
            _get_gateway_client().post_provenance(
                actor_user_id=resolved.user_id,
                triggered_by=correlation_ctx.get("triggered_by"),
                root_human_id=correlation_ctx["root_human_id"],
                is_human_rooted=correlation_ctx["is_human_rooted"],
                action_kind="webhook_trigger",
                source_event={
                    "event_type": event_type,
                    "action": action,
                    "repo": repo,
                    "issue": payload.get("issue", {}).get("number"),
                },
                correlation_id=correlation_ctx["correlation_id"],
                org_id=resolved.org_id,
            )
        except Exception as e:
            logger.warning("post_provenance failed (fail-soft): %s", e)

        # Write correlation pointer (fail-soft)
        try:
            _get_correlation_store().write_pointer(
                key=channel_key_str,
                correlation_id=correlation_ctx["correlation_id"],
                root_human_id=correlation_ctx["root_human_id"],
                is_human_rooted=correlation_ctx["is_human_rooted"],
            )
        except Exception as e:
            logger.warning("write_pointer failed (fail-soft): %s", e)

    # 13. Build envelope + publish to SQS
    # Issue #1289: include cognito_sub for personal-context identity propagation.
    # For human users resolved via identity_resolver, user_id is the platform
    # user ID (which maps 1:1 to a Cognito sub). For bot/service-account
    # senders, cognito_sub is empty — the MCP server will fail-closed (no
    # personal-context access for bots, by design).
    cognito_sub = resolved.user_id if resolved.user_kind == "human" else ""
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": tenant_id,
        "cognito_sub": cognito_sub,
        "persona": intent.persona,
        "actor": {
            "user_id": resolved.user_id,
            "org_id": resolved.org_id,
            "github_id": sender.get("id", 0),
            "github_login": sender.get("login", ""),
            "is_bot": sender.get("type") == "Bot",  # Deprecated
        },
        "source_ref": {
            "installation_id": installation_id,
            "repo": repo,
            "issue": payload.get("issue", {}).get("number") if "issue" in payload else None,
            "pr": payload.get("pull_request", {}).get("number")
            if "pull_request" in payload
            else None,
            "sha": payload.get("pull_request", {}).get("head", {}).get("sha")
            if "pull_request" in payload
            else None,
        },
        "intent": {
            "trigger": intent.trigger,
            "label": intent.label,
            "persona": intent.persona,
        },
        "correlation": {
            "correlation_id": correlation_ctx["correlation_id"] if correlation_ctx else "",
            "root_human_id": correlation_ctx["root_human_id"] if correlation_ctx else "",
            "is_human_rooted": correlation_ctx["is_human_rooted"] if correlation_ctx else True,
        },
        "payload": payload,
        "arrived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    envelope["message_id"] = str(uuid.uuid4())
    print(
        f"DBG handler:publish_attempt message_id={envelope['message_id']} persona={intent.persona}"
    )

    message_id = _get_sqs_publisher().publish_envelope(envelope)
    print(f"DBG handler:publish_result sqs_message_id={message_id!r}")
    if not message_id:
        _log_outcome(
            event_type=event_type,
            action=action,
            installation_id=installation_id,
            tenant_id=tenant_id,
            repo=repo,
            persona=intent.persona,
            outcome="error",
            start_time=start_time,
            error="SQS publish failed",
        )
        return _response(500, {"error": "Failed to enqueue"})

    # 14. Log event
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

    # 15. Return 202
    return _response(202, {"status": "accepted", "message_id": message_id})


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
