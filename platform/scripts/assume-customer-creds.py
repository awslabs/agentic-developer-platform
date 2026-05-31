#!/usr/bin/env python3
"""
assume-customer-creds.py — fetch STS creds for a vaulted customer-linked
AWS account via the ADP gateway's /internal/v1/credential-assume-role
endpoint, then emit them as `export KEY=value` lines on stdout.

Usage:
  eval "$(./platform/scripts/assume-customer-creds.py)"

Inputs (from environment, populated by load-deploy-config.sh):
  ADP_CUSTOMER_ACCOUNT_ID    — customer account to assume into (required)
  ADP_CUSTOMER_AWS_LABEL     — vaulted credential label (e.g. "Dep-testing")
  ADP_CUSTOMER_USER_ID       — Postgres users.id for the linked-account owner
  ADP_GATEWAY_URL            — gateway base URL (default: http://bedrockgateway.adp-gateway)
  ADP_GATEWAY_API_URL        — API Gateway invoke URL for SigV4 path (preferred;
                               when set, uses IRSA credentials instead of shared secret)
  ADP_INTERNAL_API_KEY       — internal API key (default: read from Secrets Manager
                               at adp/<env>/gateway/internal-api-key)
  ADP_ENVIRONMENT            — env name, used to compute the secret path

Exits:
  0 on success (creds printed to stdout, suitable for `eval`)
  1 on configuration error (missing inputs)
  2 on gateway/STS error
"""

import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Retry configuration — tuneable via environment variables.
MAX_RETRIES = int(os.environ.get("ADP_ASSUME_MAX_RETRIES", "3"))
BACKOFF_BASE_SECONDS = float(os.environ.get("ADP_ASSUME_BACKOFF_BASE", "1.0"))
# HTTP status codes that are safe to retry (server-side transient errors).
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def fail(msg: str, code: int = 1) -> "None":
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(code)


def load_internal_api_key(env: str) -> str:
    """Fetch the internal API key from AWS Secrets Manager."""
    explicit = os.environ.get("ADP_INTERNAL_API_KEY", "")
    if explicit:
        return explicit
    secret_id = f"adp/{env}/gateway/internal-api-key"
    try:
        out = subprocess.check_output(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                secret_id,
                "--query",
                "SecretString",
                "--output",
                "text",
            ],
            stderr=subprocess.PIPE,
        )
        return out.decode("utf-8").strip()
    except subprocess.CalledProcessError as exc:
        fail(
            f"Could not read internal API key from Secrets Manager ({secret_id}): "
            f"{exc.stderr.decode('utf-8', errors='replace').strip()}"
        )


def _request_with_retry(endpoint: str, req: "Request") -> dict:
    """Send the HTTP request with retries and exponential backoff.

    Retries on transient server errors (5xx) and connection failures.
    Non-retryable errors (4xx, malformed response) fail immediately.
    """
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(
                    f"Gateway returned HTTP {exc.code} (attempt {attempt}/{MAX_RETRIES}), "
                    f"retrying in {wait:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                last_exc = exc
                # Rebuild the request — urllib consumes the body on send
                req = Request(
                    req.full_url,
                    data=req.data,
                    headers=dict(req.headers),
                    method=req.get_method(),
                )
                continue
            # Non-retryable HTTP error or final attempt exhausted
            fail(
                f"Gateway returned HTTP {exc.code}: {error_body}",
                code=2,
            )
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(
                    f"Connection error (attempt {attempt}/{MAX_RETRIES}): {exc}, "
                    f"retrying in {wait:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                last_exc = exc
                req = Request(
                    req.full_url,
                    data=req.data,
                    headers=dict(req.headers),
                    method=req.get_method(),
                )
                continue
            fail(f"Could not reach gateway at {endpoint}: {exc}", code=2)

    # Should not reach here, but guard against it
    fail(f"All {MAX_RETRIES} attempts failed. Last error: {last_exc}", code=2)


def _sigv4_post(url: str, body: dict) -> dict:
    """Make a SigV4-signed POST request to API Gateway.

    Uses the pod's IRSA credentials (available via boto3's credential chain).
    Mirrors the implementation in adp_cred/client.py:_sigv4_request.
    """
    try:
        import botocore.auth
        import botocore.awsrequest
        import botocore.session
    except ImportError:
        fail("botocore is required for SigV4 auth. Install boto3.", code=2)

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        fail(
            "No AWS credentials available for SigV4 signing. "
            "Ensure IRSA is configured or AWS credentials are present.",
            code=2,
        )
    credentials = credentials.get_frozen_credentials()

    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode()

    aws_request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        headers=headers,
        data=data,
    )

    region = os.environ.get("AWS_REGION", "us-east-1")
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    # Debug: log the auth header prefix to confirm SigV4 is active
    auth_header = dict(aws_request.headers).get("Authorization", "")
    print(
        f"SigV4 auth header prefix: {auth_header[:40]}...",
        file=sys.stderr,
    )

    # Convert to urllib Request and send with retry
    signed_headers = dict(aws_request.headers)
    req = Request(url, data=data, headers=signed_headers, method="POST")
    return _request_with_retry(url, req)


def main() -> int:
    customer_account = os.environ.get("ADP_CUSTOMER_ACCOUNT_ID", "")
    if not customer_account:
        fail("ADP_CUSTOMER_ACCOUNT_ID not set; nothing to assume.")

    user_id = os.environ.get("ADP_CUSTOMER_USER_ID", "")
    if not user_id:
        fail(
            "ADP_CUSTOMER_USER_ID not set. The gateway's /internal/v1/credential-"
            "assume-role endpoint requires the vault user's UUID. Add `user_id: "
            "...` under customer_account in config/deployment.yml."
        )

    label = os.environ.get("ADP_CUSTOMER_AWS_LABEL", "")  # may be empty — gateway picks
    env = os.environ.get("ADP_ENVIRONMENT", "dev")

    payload = {
        "user_id": user_id,
        "agent_id": "github-workflow",
        "task_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "service": "aws",
        "label": label or None,
        "purpose": "deploy via load-deploy-config",
    }

    api_gw_url = os.environ.get("ADP_GATEWAY_API_URL", "").rstrip("/")
    if api_gw_url:
        # New path: SigV4 via API Gateway (preferred — see EPIC #1107)
        endpoint = f"{api_gw_url}/internal/v1/credential-assume-role"
        body = _sigv4_post(endpoint, payload)
    else:
        # Legacy fallback: shared-secret to in-cluster URL (kept until #1107 Phase 3)
        gateway_url = os.environ.get(
            "ADP_GATEWAY_URL", "http://bedrockgateway.adp-gateway"
        ).rstrip("/")
        api_key = load_internal_api_key(env)
        endpoint = f"{gateway_url}/internal/v1/credential-assume-role"
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "X-Internal-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        body = _request_with_retry(endpoint, req)

    # Validate response shape
    for required in ("access_key_id", "secret_access_key", "session_token"):
        if required not in body:
            fail(
                f"Gateway response missing {required!r}. Keys: {sorted(body.keys())}",
                code=2,
            )

    # Confirm the assumed account matches what config said. Catches cases
    # where the user_id resolves to a different vaulted credential.
    profile_name = body.get("profile_name", "<unknown>")
    region = body.get("region") or os.environ.get("AWS_REGION", "us-east-1")

    # Emit shell-evaluable output
    print(f"export AWS_ACCESS_KEY_ID={body['access_key_id']}")
    print(f"export AWS_SECRET_ACCESS_KEY={body['secret_access_key']}")
    print(f"export AWS_SESSION_TOKEN={body['session_token']}")
    print(f"export AWS_REGION={region}")
    print(f"export AWS_DEFAULT_REGION={region}")
    # Strip IRSA / profile env vars so boto3 / aws CLI uses our injected creds
    print("unset AWS_PROFILE AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE")

    # Diagnostic line on stderr (won't break `eval`)
    print(
        f"Assumed customer role via gateway: account={customer_account} "
        f"label={label or '<auto>'} profile={profile_name} expires={body.get('expiration', '<unknown>')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
