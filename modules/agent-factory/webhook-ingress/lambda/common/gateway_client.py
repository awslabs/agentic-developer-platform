"""Gateway admin API client for auto-provisioning users.

Phase B.1 (Issue #402): Used by the identity resolver when a tenant has
user_provisioning_mode="auto_provision". Calls the Gateway admin API to
create a minimal user record, which writes both Postgres + DDB identity-index.

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

_admin_token: str | None = None


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
