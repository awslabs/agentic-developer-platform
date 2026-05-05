"""Gateway client for adp-cred CLI — signed internal-endpoint calls."""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def _get_config() -> tuple[str, str, str, str, str]:
    """Read required environment variables. Returns (base_url, api_key, user_id, agent_id, task_id).

    Exits with code 2 if any required env var is missing.
    """
    base_url = os.environ.get("VAULT_GATEWAY_URL", "")
    api_key = os.environ.get("VAULT_INTERNAL_API_KEY", "")
    user_id = os.environ.get("ADP_USER_ID", "")
    agent_id = os.environ.get("ADP_AGENT_ID", "")
    task_id = os.environ.get("ADP_TASK_ID", "")

    missing = []
    if not base_url:
        missing.append("VAULT_GATEWAY_URL")
    if not api_key:
        missing.append("VAULT_INTERNAL_API_KEY")
    if not user_id:
        missing.append("ADP_USER_ID")
    if not agent_id:
        missing.append("ADP_AGENT_ID")
    if not task_id:
        missing.append("ADP_TASK_ID")

    if missing:
        print(f"error: missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    return base_url.rstrip("/"), api_key, user_id, agent_id, task_id


def _check_enabled() -> None:
    """Exit with error if ENABLE_USER_CREDENTIALS is not enabled."""
    val = os.environ.get("ENABLE_USER_CREDENTIALS", "0")
    if val not in ("1", "true", "True"):
        print(
            json.dumps({"error": "feature_disabled", "message": "user credentials disabled in this environment"}),
            file=sys.stderr,
        )
        sys.exit(1)


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict | list:
    """Make an HTTP request to the gateway."""
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


def list_credentials() -> list:
    """List all credentials for the current user."""
    _check_enabled()
    base_url, api_key, user_id, _, _ = _get_config()
    url = f"{base_url}/internal/v1/user-credentials?user_id={user_id}"
    return _request("GET", url, api_key)  # type: ignore[return-value]


def proxy_http(method: str, url: str, service: str, label: str | None = None,
               headers: dict | None = None, body: str | None = None) -> dict:
    """Proxy an HTTP request through the vault."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id = _get_config()
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
    endpoint = f"{base_url}/internal/v1/proxy-request"
    return _request("POST", endpoint, api_key, payload)  # type: ignore[return-value]


def materialize(service: str, label: str | None = None) -> dict:
    """Materialize a file-type credential."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id = _get_config()
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
    }
    endpoint = f"{base_url}/internal/v1/credential-materialize"
    return _request("POST", endpoint, api_key, payload)  # type: ignore[return-value]


def raw_read(service: str, label: str | None = None, purpose: str | None = None) -> dict:
    """Read raw credential value."""
    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id = _get_config()
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
        "purpose": purpose,
    }
    endpoint = f"{base_url}/internal/v1/credential-raw-read"
    return _request("POST", endpoint, api_key, payload)  # type: ignore[return-value]
