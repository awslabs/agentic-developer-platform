"""Seed per-tenant GitHub App secret in Secrets Manager.

Issue #2085: Ensures the per-tenant secret exists when a user installs the
GitHub App via Settings > Connections, so that downstream
`resolve_tenant_app_credentials()` never hits a missing-secret error.

The secret path is:  adp/<env>/tenants/<org_id>/github-app
The payload shape:   {"app_id": "...", "private_key": "..."}

This mirrors the webhook-ingress Lambda's `_auto_provision_tenant_github_app_secret`.
Credentials resolve through GitHubAppCredsProvider (Secrets Manager first, BG_ env
vars as fallback — #2594): Apps registered via the UI manifest flow exist only in
Secrets Manager, so reading settings directly would silently skip the seed and the
first worker pod would die on vault_fetch (608 deploy #3085, hand-patch #1).

Idempotent: catches ResourceExistsException (create-or-no-op; never clobbers).
Fail-soft: logs errors and returns; never blocks the install flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from .github_app_provider import get_github_app_provider

logger = logging.getLogger(__name__)


def _get_environment() -> str:
    """Return the deployment environment (dev/staging/prod)."""
    return os.environ.get("ENVIRONMENT", "dev")


def _seed_secret_sync(org_id: str, installation_id: int) -> None:
    """Synchronous implementation — called via asyncio.to_thread."""
    # Resolve via the provider (SM-first, env fallback — #2594) so Apps
    # registered through the UI manifest flow are found. settings.github_app_*
    # is empty on UI-registered accounts, which used to skip the seed silently.
    app_id, private_key = get_github_app_provider().get_credentials()

    if not app_id or not private_key:
        logger.warning(
            "Cannot seed tenant GitHub App secret: no App credentials in Secrets Manager or BG_GITHUB_APP_* env (org_id=%s)",
            org_id,
        )
        return

    env = _get_environment()
    target = f"adp/{env}/tenants/{org_id}/github-app"
    payload = json.dumps({"app_id": app_id, "private_key": private_key})

    region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("secretsmanager", region_name=region)

    try:
        sm.create_secret(
            Name=target,
            Description=(f"GitHub App credentials for tenant {org_id} (seeded via connections install flow)"),
            SecretString=payload,
            Tags=[
                {"Key": "ManagedBy", "Value": "connections-install"},
                {"Key": "Tenant", "Value": org_id},
                {"Key": "InstallationId", "Value": str(installation_id)},
            ],
        )
        logger.info(
            "Seeded GitHub App secret for tenant=%s path=%s installation_id=%d",
            org_id,
            target,
            installation_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceExistsException":
            logger.info(
                "GitHub App secret already exists for tenant=%s path=%s — skipping",
                org_id,
                target,
            )
        else:
            logger.error(
                "Failed to seed GitHub App secret for tenant=%s path=%s — %s",
                org_id,
                target,
                exc,
            )


async def seed_tenant_github_app_secret(org_id: str, installation_id: int) -> None:
    """Seed the per-tenant GitHub App secret (async, fail-soft).

    Creates adp/<env>/tenants/<org_id>/github-app with the gateway's configured
    App credentials. Idempotent (no-op if the secret already exists). Never
    raises — failures are logged for operator visibility but do not block the
    install flow.

    Parameters
    ----------
    org_id : str
        The ADP tenant (organization) ID.
    installation_id : int
        The GitHub App installation ID (stored as a tag for auditability).
    """
    try:
        await asyncio.to_thread(_seed_secret_sync, org_id, installation_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Unexpected error seeding tenant GitHub App secret org_id=%s: %s",
            org_id,
            exc,
        )
