"""Gateway credential client for fetching user-scoped credentials.

Calls the gateway's /internal/v1/credential-raw-read or
/internal/v1/credential-assume-role endpoint, scoped to the acting user
(not the tenant). Replaces the direct Secrets Manager vault lookup for
AWS credentials (issue #455).

Issue #575 / #1103: Supports two transport modes based on environment:
  - SigV4 via API Gateway (when ADP_GATEWAY_ENDPOINT is set) — IRSA-based, no shared secret
  - Shared-secret via direct URL (when VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY are set) — legacy
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _sigv4_sign_request(method: str, url: str, headers: dict, data: bytes | None) -> dict:
    """Sign a request with SigV4 using pod IRSA credentials. Returns signed headers."""
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        raise GatewayCredentialError("No AWS credentials available for SigV4 signing")
    credentials = credentials.get_frozen_credentials()

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    region = os.environ.get("AWS_REGION", "us-east-1")
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    return dict(aws_request.headers)


class GatewayCredentialClient:
    """Fetch user-scoped credentials via the gateway's internal API."""

    def __init__(
        self,
        gateway_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        # SigV4 mode: ADP_GATEWAY_ENDPOINT points at the API Gateway invoke URL
        self._gateway_endpoint = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
        # Legacy mode: direct URL + shared secret
        self._gateway_url = (gateway_url or os.environ.get("VAULT_GATEWAY_URL", "")).rstrip("/")
        self._api_key = api_key or os.environ.get("VAULT_INTERNAL_API_KEY", "")
        self._timeout = timeout

    @property
    def _use_sigv4(self) -> bool:
        """Return True if SigV4 mode is active (ADP_GATEWAY_ENDPOINT set)."""
        return bool(self._gateway_endpoint)

    @property
    def _base_url(self) -> str:
        """Return the base URL for requests based on the active mode."""
        if self._use_sigv4:
            return self._gateway_endpoint.rstrip("/") + "/agent"
        return self._gateway_url

    @property
    def is_configured(self) -> bool:
        """Return True if the client can make requests.

        SigV4 mode: needs ADP_GATEWAY_ENDPOINT (IRSA provides credentials).
        Legacy mode: needs VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY.
        """
        if self._use_sigv4:
            return bool(self._gateway_endpoint)
        return bool(self._gateway_url and self._api_key)

    def _make_request(self, endpoint: str, payload: dict, extra_headers: dict | None = None) -> dict[str, Any]:
        """Make an authenticated request to the gateway.

        Uses SigV4 when ADP_GATEWAY_ENDPOINT is set, shared-secret otherwise.
        """
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        data = json.dumps(payload).encode("utf-8")

        if self._use_sigv4:
            headers = _sigv4_sign_request("POST", endpoint, headers, data)
        else:
            headers["X-Internal-Api-Key"] = self._api_key

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
                f"Cannot reach gateway at {self._base_url}: {exc.reason}"
            ) from exc

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
        endpoint = f"{self._base_url}/internal/v1/credential-raw-read"
        payload = {
            "user_id": user_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "service": service,
            "label": label,
            "purpose": purpose or "aws_role_assume via entrypoint",
        }

        logger.info(
            "Fetching credential via gateway (%s mode): user_id=%s service=%s label=%s",
            "sigv4" if self._use_sigv4 else "legacy",
            user_id,
            service,
            label,
        )

        return self._make_request(
            endpoint, payload, extra_headers={"X-Agent-Scopes": "credential:raw-read"}
        )

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
        the credential through the user->team->org scope chain, performs STS
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
        endpoint = f"{self._base_url}/internal/v1/credential-assume-role"
        payload = {
            "user_id": user_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "service": service,
            "label": label,
            "purpose": purpose or "entrypoint: assume customer AWS role",
        }

        logger.info(
            "Assuming role via gateway (%s mode): user_id=%s service=%s label=%s",
            "sigv4" if self._use_sigv4 else "legacy",
            user_id,
            service,
            label,
        )

        return self._make_request(endpoint, payload)


class GatewayCredentialError(Exception):
    """Raised when the gateway credential lookup fails."""
