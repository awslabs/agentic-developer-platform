"""Gateway credential client for fetching user-scoped credentials.

Calls the gateway's /internal/v1/credential-raw-read or
/internal/v1/credential-assume-role endpoint, scoped to the acting user
(not the tenant). Replaces the direct Secrets Manager vault lookup for
AWS credentials (issue #455).

Issue #575 / #1103: Supports two transport modes based on environment:
  - SigV4 via API Gateway (when ADP_GATEWAY_ENDPOINT is set) — IRSA-based, no shared secret
  - Shared-secret via direct URL (when VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY are set) — legacy

Issue #4343: in SigV4 mode the endpoints above are addressed as
<ADP_GATEWAY_ENDPOINT>/internal/v1/... so they match the API Gateway
/internal/{proxy+} route, which is wired to the internal-plane ALB. They must
NOT go through /agent/{proxy+}: that route's edge ALB 403s /internal/* (#4010).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

logger = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _worker_identity_headers() -> dict[str, str]:
    headers = {}
    for variable, header in (
        ("ADP_RUN_CREDENTIAL_FILE", "X-Adp-Run-Credential"),
        ("ADP_WORKLOAD_TOKEN_FILE", "X-Adp-Workload-Token"),
    ):
        try:
            fd = os.open(os.environ[variable], os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("not a file")
                raw = source.read(16387)
            token = raw.decode("ascii").rstrip("\r\n")
            if not token or len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("invalid token")
            headers[header] = token
        except (OSError, KeyError, ValueError):
            raise GatewayCredentialError("Worker identity unavailable") from None
    return headers


def _sigv4_sign_request(method: str, url: str, headers: dict, data: bytes | None) -> dict:
    """Sign a request with SigV4 using pod IRSA credentials. Returns signed headers."""
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    session = botocore.session.get_session()
    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    credentials = worker_credentials(session)
    if credentials is None:
        raise GatewayCredentialError("No AWS credentials available for SigV4 signing")
    credentials = credentials.get_frozen_credentials()

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    region = gateway_signing_region(url)
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
        """Return the base URL for requests based on the active mode.

        SigV4 mode returns the bare API Gateway endpoint — NO ``/agent``
        segment. Every endpoint this client calls is under ``/internal/v1/``,
        which must reach the API Gateway ``/internal/{proxy+}`` route (wired to
        the internal-plane ALB). Issue #4343: ``/agent/{proxy+}`` integrates
        with the EDGE ALB, where issue #4010's ``edge-internal-deny`` patch
        answers ``403 "Not available from the edge"`` for any ``/internal/*``
        path — which took down all agent dispatch. Do not re-add ``/agent``.
        """
        if self._use_sigv4:
            return self._gateway_endpoint.rstrip("/")
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

        authority = os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
        if authority:
            url = urlparse(endpoint)
            if not self._use_sigv4 or url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise GatewayCredentialError("Worker credential broker requires HTTPS and SigV4")
            headers.update(_worker_identity_headers())

        data = json.dumps(payload).encode("utf-8")

        if self._use_sigv4:
            headers = _sigv4_sign_request("POST", endpoint, headers, data)
        else:
            headers["X-Internal-Api-Key"] = self._api_key

        req = Request(endpoint, data=data, headers=headers, method="POST")

        try:
            opener = build_opener(_NoRedirect()).open if authority or (extra_headers or {}).get("X-Adp-Report-Credential") else urlopen
            with opener(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise GatewayCredentialError(
                f"Gateway returned HTTP {exc.code}"
            ) from None
        except URLError as exc:
            raise GatewayCredentialError(
                "Cannot reach gateway"
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GatewayCredentialError("Credential gateway returned invalid JSON") from None

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
        invocation_id = os.environ.get("ADP_MESSAGE_ID")
        if invocation_id:
            payload["invocation_id"] = invocation_id

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

    def github_installation_token(
        self,
        *,
        installation_id: int,
        repo_owner: str,
        repo_name: str,
        invocation_id: str | None = None,
        purpose: str | None = None,
        identity: str | None = None,
    ) -> dict[str, Any]:
        """Mint a repo-scoped GitHub App installation token via the gateway.

        Issue #4272: the GitHub-token gatekeeper. The platform App private key
        stays inside the gateway; this asks the gateway to mint on the run's
        behalf, scoped to the run's own org and the single repo it was assigned.

        Note what is NOT sent: a tenant. The gateway resolves the tenant from the
        run's webhook-events row and refuses to mint for an installation the run
        is not bound to, so a compromised worker cannot name someone else's org.

        In SigV4 mode the path rides the existing ``/internal/{proxy+}`` API
        Gateway route (internal-plane ALB), not ``/agent/{proxy+}`` — see
        ``_base_url`` and issue #4343. No new route is needed; the worker's
        execute-api grant covers ``/agent/*`` and ``/internal/*``.

        Args:
            installation_id: The run's GitHub App installation id.
            repo_owner: Owner of the repo this run operates on.
            repo_name: Name of the repo this run operates on.
            invocation_id: The run's invocation id. Defaults to ADP_MESSAGE_ID.
                The gateway rejects a request without one (fail-closed binding).
            purpose: Optional audit purpose string.
            identity: Which App identity to mint as (issue #5350). ``None`` (the
                default, used by every bootstrap caller) leaves the field off the
                request entirely, so the gateway applies its own default and old
                gateways are unaffected. ``"review"`` asks for the distinct reviewer
                App so a review is not a self-review.

        Returns:
            ``{"token": "ghs_...", "expires_at": "<iso8601>", "app_id": "<id>",
            "identity": "default"|"review"}``.
            ``app_id`` is the App's PUBLIC identifier (not a credential) — the
            caller needs it for the bot commit identity and for GH_APP_ID.
            ``identity`` is the identity the gateway ACTUALLY used, which may not be
            the one requested: a "review" request falls back to the authoring
            identity when no reviewer App is configured. Callers that need a formal
            verdict must read it rather than assume they got what they asked for.
            Absent when talking to a gateway that predates #5350.

        Raises:
            GatewayCredentialError: On any HTTP or network error. Deliberately
                NOT caught here and NOT fallen back to a local mint — a silent
                fallback would defeat the entire change.
        """
        endpoint = f"{self._base_url}/internal/v1/github-installation-token"
        payload: dict[str, Any] = {
            "installation_id": int(installation_id),
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "purpose": purpose or "agent run GitHub token (broker)",
        }
        resolved_invocation_id = invocation_id or os.environ.get("ADP_MESSAGE_ID")
        if resolved_invocation_id:
            payload["invocation_id"] = resolved_invocation_id
        # Omitted rather than defaulted, so a request to a pre-#5350 gateway is
        # byte-for-byte what it was before.
        if identity:
            payload["identity"] = identity

        logger.info(
            "Minting GitHub installation token via gateway (%s mode): installation_id=%s repo=%s/%s identity=%s",
            "sigv4" if self._use_sigv4 else "legacy",
            installation_id,
            repo_owner,
            repo_name,
            identity or "default",
        )

        report_path = os.environ.get("ADP_RUN_REPORT_CREDENTIAL_FILE")
        extra_headers = None
        if identity == "review" and report_path:
            parsed = urlparse(endpoint)
            if not self._use_sigv4 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise GatewayCredentialError("Shared review identity requires HTTPS and SigV4")
            try:
                fd = os.open(report_path, os.O_RDONLY | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("not a file")
                    raw = source.read(16387)
                credential = raw.decode("ascii").rstrip("\r\n")
                if not credential or len(credential) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in credential):
                    raise ValueError("invalid credential")
            except (OSError, ValueError):
                raise GatewayCredentialError("Shared review identity unavailable") from None
            extra_headers = {"X-Adp-Report-Credential": credential}
        result = self._make_request(endpoint, payload, extra_headers=extra_headers) if extra_headers else self._make_request(endpoint, payload)

        if not result.get("token"):
            raise GatewayCredentialError("Gateway returned no token for the installation-token request")

        return result

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
        invocation_id = os.environ.get("ADP_MESSAGE_ID")
        if invocation_id:
            payload["invocation_id"] = invocation_id

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
