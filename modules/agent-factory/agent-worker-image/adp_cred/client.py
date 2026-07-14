"""Gateway client for adp-cred CLI — signed internal-endpoint calls.

Issue #575: Supports two transport modes based on environment:
  - SigV4 via API Gateway (when ADP_GATEWAY_ENDPOINT is set) — IRSA-based, no shared secret
  - Shared-secret via direct URL (when VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY are set) — legacy
"""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def _get_config() -> tuple[str, str | None, str, str, str, bool]:
    """Read required environment variables.

    Returns (base_url, api_key_or_none, user_id, agent_id, task_id, use_sigv4).

    When ADP_GATEWAY_ENDPOINT is set, uses SigV4 auth (no shared secret needed).
    Otherwise falls back to VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY (legacy).

    Exits with code 2 if required env vars are missing for the chosen mode.
    """
    gateway_endpoint = os.environ.get("ADP_GATEWAY_ENDPOINT", "")
    base_url = os.environ.get("VAULT_GATEWAY_URL", "")
    api_key = os.environ.get("VAULT_INTERNAL_API_KEY", "")
    user_id = os.environ.get("ADP_USER_ID", "")
    agent_id = os.environ.get("ADP_AGENT_ID", "")
    task_id = os.environ.get("ADP_TASK_ID", "")

    use_sigv4 = bool(gateway_endpoint)

    missing = []
    if use_sigv4:
        # SigV4 mode: only need the API Gateway endpoint + identity vars
        base_url = gateway_endpoint.rstrip("/") + "/agent"
    else:
        # Legacy mode: need direct URL + shared secret
        if not base_url:
            missing.append("VAULT_GATEWAY_URL (or set ADP_GATEWAY_ENDPOINT for IRSA auth)")
        if not api_key:
            missing.append("VAULT_INTERNAL_API_KEY (or set ADP_GATEWAY_ENDPOINT for IRSA auth)")

    if not user_id:
        missing.append("ADP_USER_ID")
    if not agent_id:
        missing.append("ADP_AGENT_ID")
    if not task_id:
        missing.append("ADP_TASK_ID")

    if missing:
        print(
            f"error: missing required environment variables: {', '.join(missing)}", file=sys.stderr
        )
        sys.exit(2)

    return (
        base_url.rstrip("/"),
        api_key if not use_sigv4 else None,
        user_id,
        agent_id,
        task_id,
        use_sigv4,
    )


def _check_enabled() -> None:
    """Exit with error if ENABLE_USER_CREDENTIALS is not enabled."""
    val = os.environ.get("ENABLE_USER_CREDENTIALS", "0")
    if val not in ("1", "true", "True"):
        print(
            json.dumps(
                {
                    "error": "feature_disabled",
                    "message": "user credentials disabled in this environment",
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)


def _sigv4_request(method: str, url: str, body: dict | None = None) -> dict | list:
    """Make a SigV4-signed HTTP request to API Gateway.

    Uses the pod's IRSA credentials (available via boto3's credential chain).
    """
    try:
        import botocore.auth
        import botocore.awsrequest
        import botocore.session
    except ImportError:
        print("error: botocore is required for SigV4 auth. Install boto3.", file=sys.stderr)
        sys.exit(1)

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        print("error: no AWS credentials available for SigV4 signing", file=sys.stderr)
        sys.exit(1)
    credentials = credentials.get_frozen_credentials()

    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    region = os.environ.get("AWS_REGION", "us-east-1")
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    # Convert to urllib Request
    signed_headers = dict(aws_request.headers)
    req = Request(url, data=data, headers=signed_headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        print(f"error: gateway returned {exc.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as exc:
        print(f"error: cannot reach gateway: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict | list:
    """Make an HTTP request to the gateway using shared-secret auth (legacy)."""
    headers = {
        "X-Internal-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        print(f"error: gateway returned {exc.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as exc:
        print(f"error: cannot reach gateway: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _do_request(
    method: str, url: str, api_key: str | None, use_sigv4: bool, body: dict | None = None
) -> dict | list:
    """Dispatch to SigV4 or shared-secret request based on config."""
    if use_sigv4:
        return _sigv4_request(method, url, body)
    return _request(method, url, api_key, body)  # type: ignore[arg-type]


def list_credentials() -> list:
    """List all credentials for the current user."""
    _check_enabled()
    base_url, api_key, user_id, _, _, use_sigv4 = _get_config()
    url = f"{base_url}/internal/v1/user-credentials?user_id={user_id}"
    invocation_id = os.environ.get("ADP_MESSAGE_ID")
    if invocation_id:
        url += f"&invocation_id={invocation_id}"
    return _do_request("GET", url, api_key, use_sigv4)  # type: ignore[return-value]


def proxy_http(
    method: str,
    url: str,
    service: str,
    label: str | None = None,
    headers: dict | None = None,
    body: str | None = None,
) -> dict:
    """Proxy an HTTP request through the vault."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id, use_sigv4 = _get_config()
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
        "method": method,
        "url": url,
        "headers": headers,
        "body": body,
    }
    invocation_id = os.environ.get("ADP_MESSAGE_ID")
    if invocation_id:
        payload["invocation_id"] = invocation_id
    endpoint = f"{base_url}/internal/v1/proxy-request"
    return _do_request("POST", endpoint, api_key, use_sigv4, payload)  # type: ignore[return-value]


def materialize(service: str, label: str | None = None) -> dict:
    """Materialize a file-type credential."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id, use_sigv4 = _get_config()
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
    }
    invocation_id = os.environ.get("ADP_MESSAGE_ID")
    if invocation_id:
        payload["invocation_id"] = invocation_id
    endpoint = f"{base_url}/internal/v1/credential-materialize"
    return _do_request("POST", endpoint, api_key, use_sigv4, payload)  # type: ignore[return-value]


def raw_read(service: str, label: str | None = None, purpose: str | None = None) -> dict:
    """Read raw credential value."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id, use_sigv4 = _get_config()
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
        "purpose": purpose,
    }
    invocation_id = os.environ.get("ADP_MESSAGE_ID")
    if invocation_id:
        payload["invocation_id"] = invocation_id
    endpoint = f"{base_url}/internal/v1/credential-raw-read"
    return _do_request("POST", endpoint, api_key, use_sigv4, payload)  # type: ignore[return-value]
