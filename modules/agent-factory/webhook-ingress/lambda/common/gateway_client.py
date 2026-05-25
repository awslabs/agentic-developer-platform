"""Gateway API client for identity resolution and auto-provisioning.

Phase B.1 (Issue #402): Used by the identity resolver when a tenant has
user_provisioning_mode="auto_provision". Calls the Gateway admin API to
create a minimal user record, which writes both Postgres + DDB identity-index.

Issue #702: Added resolve_user_by_identity() to call the existing
POST /internal/v1/resolve-user endpoint as a Postgres safety-net for
canonical user_id resolution.

Only invoked from the webhook Lambda when:
  1. Tenant is resolved (installation is known)
  2. Sender is NOT resolved (unknown_user)
  3. Tenant's user_provisioning_mode == "auto_provision"
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

GATEWAY_API_URL = os.environ.get("GATEWAY_API_URL", "")
GATEWAY_ADMIN_TOKEN_ARN = os.environ.get("GATEWAY_ADMIN_TOKEN_ARN", "")
INTERNAL_API_KEY_ARN = os.environ.get("INTERNAL_API_KEY_ARN", "")

_admin_token: str | None = None
_internal_api_key: str | None = None


def _resolve_admin_token() -> str:
    """Resolve the platform admin token from Secrets Manager (cached)."""
    global _admin_token
    if _admin_token is not None:
        return _admin_token

    if GATEWAY_ADMIN_TOKEN_ARN:
        import boto3

        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        resp = client.get_secret_value(SecretId=GATEWAY_ADMIN_TOKEN_ARN)
        _admin_token = resp["SecretString"]
    else:
        _admin_token = os.environ.get("GATEWAY_ADMIN_TOKEN", "")

    return _admin_token


def _resolve_internal_api_key() -> str:
    """Resolve the internal API key from Secrets Manager (cached for Lambda lifetime)."""
    global _internal_api_key
    if _internal_api_key is not None:
        return _internal_api_key

    arn = os.environ.get("INTERNAL_API_KEY_ARN", "")
    if arn:
        import boto3

        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        resp = client.get_secret_value(SecretId=arn)
        _internal_api_key = resp["SecretString"]
    else:
        _internal_api_key = os.environ.get("BG_INTERNAL_API_KEY", "")

    return _internal_api_key


def resolve_user_by_identity(
    provider: str, provider_user_id: str
) -> dict | None:
    """Call POST /internal/v1/resolve-user to resolve canonical user via Postgres.

    Returns dict with keys {user_id, org_id, team_id, is_shadow} on success,
    or None on 404 / error.
    """
    if not GATEWAY_API_URL:
        logger.warning("GATEWAY_API_URL not set — cannot resolve user via gateway")
        return None

    url = f"{GATEWAY_API_URL}/internal/v1/resolve-user"
    body = {
        "provider": provider,
        "provider_user_id": provider_user_id,
    }

    api_key = _resolve_internal_api_key()
    if not api_key:
        logger.warning("Internal API key not available — cannot resolve user via gateway")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": api_key,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "user_id": data.get("user_id", ""),
                    "org_id": data.get("org_id", ""),
                    "team_id": data.get("team_id", ""),
                    "is_shadow": data.get("is_shadow", False),
                }
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info(
                "resolve_user_by_identity: 404 for provider=%s provider_user_id=%s",
                provider,
                provider_user_id,
            )
            return None
        logger.error(
            "resolve_user_by_identity HTTP error %d for provider=%s provider_user_id=%s: %s",
            e.code,
            provider,
            provider_user_id,
            e.reason,
        )
        return None
    except Exception as e:
        logger.error(
            "resolve_user_by_identity failed for provider=%s provider_user_id=%s: %s",
            provider,
            provider_user_id,
            e,
        )
        return None


def post_provenance(
    actor_user_id: str,
    triggered_by: str | None,
    root_human_id: str,
    is_human_rooted: bool,
    action_kind: str,
    source_event: dict,
    correlation_id: str,
    org_id: str,
) -> str | None:
    """POST to gateway /internal/v1/provenance.

    Returns provenance_id on success, None on failure.

    Fail-soft: on gateway 5xx or timeout, log + emit metric, don't crash.
    Uses 5s timeout (non-blocking to webhook flow).

    # TODO: Once Phase 2-d ships and consumers rely on provenance rows,
    # evaluate fail-hard or circuit-breaker for write failures.
    """
    if not GATEWAY_API_URL:
        logger.warning("GATEWAY_API_URL not set — cannot post provenance")
        return None

    url = f"{GATEWAY_API_URL}/internal/v1/provenance"
    body = {
        "actor_user_id": actor_user_id,
        "triggered_by": triggered_by,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "action_kind": action_kind,
        "source_event": source_event,
        "correlation_id": correlation_id,
        "org_id": org_id,
    }

    api_key = _resolve_internal_api_key()
    if not api_key:
        logger.warning("Internal API key not available — cannot post provenance")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": api_key,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 201:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("id")
            logger.warning(
                "post_provenance unexpected status %d for correlation=%s",
                resp.status,
                correlation_id,
            )
            return None
    except urllib.error.HTTPError as e:
        logger.error(
            "post_provenance HTTP error %d for correlation=%s: %s",
            e.code,
            correlation_id,
            e.reason,
        )
        return None
    except Exception as e:
        logger.error(
            "post_provenance failed for correlation=%s: %s",
            correlation_id,
            e,
        )
        return None


def auto_provision_user(
    org_id: str,
    github_id: int,
    github_login: str,
) -> bool:
    """Call Gateway admin API to create a minimal user with GitHub identity.

    POST /api/admin/identity/organizations/{org_id}/users

    Returns True if the user was created (or already exists), False on error.
    """
    if not GATEWAY_API_URL:
        logger.error("GATEWAY_API_URL env var is not set — cannot auto-provision")
        return False

    url = f"{GATEWAY_API_URL}/api/admin/identity/organizations/{org_id}/users"
    body = {
        "email": f"{github_login}@github.auto-provision.adp.internal",
        "name": github_login,
        "role": "developer",
        "identities": [
            {
                "provider": "github",
                "provider_user_id": str(github_id),
                "provider_username": github_login,
            }
        ],
        "send_invite": False,
    }

    token = _resolve_admin_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            if status in (200, 201):
                logger.info(
                    "Auto-provisioned user: github_login=%s org_id=%s",
                    github_login,
                    org_id,
                )
                return True
            logger.warning(
                "Auto-provision returned unexpected status %d for %s",
                status,
                github_login,
            )
            return False
    except urllib.error.HTTPError as e:
        # 409 = user already exists — treat as success
        if e.code == 409:
            logger.info(
                "Auto-provision: user already exists github_login=%s org_id=%s",
                github_login,
                org_id,
            )
            return True
        logger.error(
            "Auto-provision HTTP error %d for github_login=%s: %s",
            e.code,
            github_login,
            e.reason,
        )
        return False
    except Exception as e:
        logger.error(
            "Auto-provision failed for github_login=%s: %s", github_login, e
        )
        return False
