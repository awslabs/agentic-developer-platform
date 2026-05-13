"""Gateway credential client for fetching user-scoped credentials.

Calls the gateway's /internal/v1/credential-raw-read or
/internal/v1/credential-assume-role endpoint, scoped to the acting user
(not the tenant). Replaces the direct Secrets Manager vault lookup for
AWS credentials (issue #455).

Environment variables required:
    VAULT_GATEWAY_URL       - Base URL of the gateway (e.g. http://bedrockgateway.adp-gateway:8080)
    VAULT_INTERNAL_API_KEY  - Shared secret for internal endpoints
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class GatewayCredentialClient:
    """Fetch user-scoped credentials via the gateway's internal API."""

    def __init__(
        self,
        gateway_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self._gateway_url = (gateway_url or os.environ.get("VAULT_GATEWAY_URL", "")).rstrip("/")
        self._api_key = api_key or os.environ.get("VAULT_INTERNAL_API_KEY", "")
        self._timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Return True if the gateway URL and API key are available."""
        return bool(self._gateway_url and self._api_key)

    def raw_read(
        self,
        *,
        user_id: str,
        agent_id: str,
        task_id: str,
        service: str,
        label: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a raw credential value from the gateway.

        Args:
            user_id: Cognito sub or shadow user ID of the acting user.
            agent_id: Agent persona identifier.
            task_id: Unique task/run identifier.
            service: Credential service name (e.g. "aws_role_assume").
            label: Optional credential label (e.g. "default").
            purpose: Optional audit purpose string.

        Returns:
            Parsed JSON dict with at least {value, credential_type, provenance_id}.

        Raises:
            GatewayCredentialError: On any HTTP or network error.
        """
        endpoint = f"{self._gateway_url}/internal/v1/credential-raw-read"
        payload = {
            "user_id": user_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "service": service,
            "label": label,
            "purpose": purpose or "aws_role_assume via entrypoint",
        }

        headers = {
            "X-Internal-Api-Key": self._api_key,
            "X-Agent-Scopes": "credential:raw-read",
            "Content-Type": "application/json",
        }

        logger.info(
            "Fetching credential via gateway: user_id=%s service=%s label=%s",
            user_id,
            service,
            label,
        )

        data = json.dumps(payload).encode("utf-8")
        req = Request(endpoint, data=data, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8") if exc.fp else ""
            raise GatewayCredentialError(
                f"Gateway returned HTTP {exc.code}: {error_body}"
            ) from exc
        except URLError as exc:
            raise GatewayCredentialError(
                f"Cannot reach gateway at {self._gateway_url}: {exc.reason}"
            ) from exc


    def assume_role(
        self,
        *,
        user_id: str,
        agent_id: str,
        task_id: str,
        service: str = "aws",
        label: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Assume an AWS role via the gateway and return short-lived STS creds.

        Hits POST /internal/v1/credential-assume-role. The gateway resolves
        the credential through the user→team→org scope chain, performs STS
        AssumeRole with session tagging server-side, and returns ready-to-use
        temporary credentials. Preferred over raw_read for AWS roles because
        it doesn't require the vault-raw-read feature flag.

        Args:
            user_id: Postgres users.id (NOT Cognito sub) of the acting user.
            agent_id: Agent persona identifier.
            task_id: Unique task/run identifier.
            service: Credential service ("aws").
            label: Optional credential label (e.g. "default").
            purpose: Optional audit purpose string.

        Returns:
            Dict with {profile_name, access_key_id, secret_access_key,
            session_token, expiration, region, provenance_id}.

        Raises:
            GatewayCredentialError: On any HTTP or network error.
        """
        endpoint = f"{self._gateway_url}/internal/v1/credential-assume-role"
        payload = {
            "user_id": user_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "service": service,
            "label": label,
            "purpose": purpose or "entrypoint: assume customer AWS role",
        }
        headers = {
            "X-Internal-Api-Key": self._api_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "Assuming role via gateway: user_id=%s service=%s label=%s",
            user_id,
            service,
            label,
        )

        data = json.dumps(payload).encode("utf-8")
        req = Request(endpoint, data=data, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8") if exc.fp else ""
            raise GatewayCredentialError(
                f"Gateway returned HTTP {exc.code}: {error_body}"
            ) from exc
        except URLError as exc:
            raise GatewayCredentialError(
                f"Cannot reach gateway at {self._gateway_url}: {exc.reason}"
            ) from exc


class GatewayCredentialError(Exception):
    """Raised when the gateway credential lookup fails."""
