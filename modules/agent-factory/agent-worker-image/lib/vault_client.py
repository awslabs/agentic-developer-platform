"""Vault client for fetching per-tenant secrets.

Stub-compatible implementation backed by AWS Secrets Manager.

Naming convention in AWS: all ADP secrets live under an environment-scoped
prefix (e.g. `adp/dev/...`) so the runner permissions boundary can scope
access to a single prefix. Callers pass relative paths like
`tenants/sophos-test/github-app`; this client prepends `adp/<env>/`.

Vault-style relative path:   tenants/<tenant_id>/<secret_name>
Resolved Secrets Manager id: adp/<env>/tenants/<tenant_id>/<secret_name>
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)


class VaultClient:
    """Fetch tenant secrets from AWS Secrets Manager (vault stub)."""

    def __init__(self, region: str = "us-east-1", env: str | None = None) -> None:
        self._client = boto3.client("secretsmanager", region_name=region)
        self._env = env or os.environ.get("ADP_ENV") or "dev"

    def _resolve(self, path: str) -> str:
        """Prepend the env prefix unless the caller already provided one."""
        if path.startswith("adp/"):
            return path
        return f"adp/{self._env}/{path.lstrip('/')}"

    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a JSON secret by path.

        Args:
            path: Vault-style path, e.g. "tenants/acme-corp/github-app".
                  Will be resolved to `adp/<env>/tenants/acme-corp/github-app`.

        Returns:
            Parsed JSON dict with the secret contents.
        """
        full = self._resolve(path)
        logger.info("Fetching secret: %s (resolved=%s)", path, full)
        resp = self._client.get_secret_value(SecretId=full)
        return json.loads(resp["SecretString"])
