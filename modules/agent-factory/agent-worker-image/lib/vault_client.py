"""Vault client for fetching per-tenant secrets.

Stub-compatible implementation backed by AWS Secrets Manager.
Path convention: tenants/<tenant_id>/<secret_name>
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)


class VaultClient:
    """Fetch tenant secrets from AWS Secrets Manager (vault stub)."""

    def __init__(self, region: str = "us-east-1") -> None:
        self._client = boto3.client("secretsmanager", region_name=region)

    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a JSON secret by path.

        Args:
            path: Vault-style path, e.g. "tenants/acme-corp/github-app"

        Returns:
            Parsed JSON dict with the secret contents.
        """
        logger.info("Fetching secret: %s", path)
        resp = self._client.get_secret_value(SecretId=path)
        return json.loads(resp["SecretString"])
