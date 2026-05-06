"""
GitHub Auth Broker Lambda — converts GitHub OAuth flow into Cognito sessions.

Issue #520: Replaces the failed Cognito-OIDC approach from #518/#519.

Endpoints:
  GET /start    — returns redirect URL to GitHub OAuth authorize
  GET /callback — handles GitHub callback, provisions Cognito user, returns tokens

Environment variables:
  GITHUB_CLIENT_ID        — GitHub OAuth App client ID
  GITHUB_CLIENT_SECRET_ARN — Secrets Manager ARN for OAuth credentials
  COGNITO_USER_POOL_ID    — Cognito User Pool ID
  COGNITO_CLIENT_ID       — Cognito App Client ID (public, ADMIN_USER_PASSWORD_AUTH enabled)
  CALLBACK_URL            — Full URL of this Lambda's /callback endpoint
  FRONTEND_URL            — Frontend origin (e.g., https://d1g6cal2ts4iis.cloudfront.net)
  ALLOWLIST_MODE          — "open", "org", or "explicit"
  ALLOWED_ORGS            — Comma-separated list of allowed GitHub orgs
  GITHUB_TOKEN_SECRET_ARN — Secrets Manager ARN for org-check GitHub token
  LOG_LEVEL               — Logging level (default: INFO)
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse

import boto3
from allowlist import check_org_membership
from cognito_provisioner import provision_and_authenticate
from github_oauth import exchange_code_for_token, get_github_user

# Configure logging
logger = logging.getLogger(__name__)
log_level = os.environ.get("LOG_LEVEL", "INFO")
logger.setLevel(getattr(logging, log_level, logging.INFO))

# Environment configuration
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET_ARN = os.environ.get("GITHUB_CLIENT_SECRET_ARN", "")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "")
ALLOWLIST_MODE = os.environ.get("ALLOWLIST_MODE", "open")
ALLOWED_ORGS = os.environ.get("ALLOWED_ORGS", "")
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")

# State signing key (derived from client secret for HMAC)
STATE_TTL_SECONDS = 600  # 10 minutes

# Cached secrets
_github_client_secret: str | None = None
_github_org_token: str | None = None


def _get_github_client_secret() -> str:
    """Retrieve GitHub OAuth client secret from Secrets Manager (cached)."""
    global _github_client_secret
    if _github_client_secret is not None:
        return _github_client_secret

    if not GITHUB_CLIENT_SECRET_ARN:
        raise ValueError("GITHUB_CLIENT_SECRET_ARN not configured")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=GITHUB_CLIENT_SECRET_ARN)
    secret_data = json.loads(response["SecretString"])
    _github_client_secret = secret_data.get("client_secret", "")
    return _github_client_secret


def _get_github_org_token() -> str:
    """Retrieve GitHub token for org membership checks (cached)."""
    global _github_org_token
    if _github_org_token is not None:
        return _github_org_token

    if not GITHUB_TOKEN_SECRET_ARN:
        return ""

    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        secret_string = response["SecretString"]
        try:
            parsed = json.loads(secret_string)
            _github_org_token = parsed.get("token", secret_string)
        except (json.JSONDecodeError, TypeError):
            _github_org_token = secret_string
        return _github_org_token
    except Exception as e:
        logger.error("Failed to retrieve org check token: %s", e)
        return ""


def _generate_state() -> str:
    """Generate a signed state parameter with timestamp for CSRF protection."""
    nonce = secrets.token_urlsafe(24)
    timestamp = str(int(time.time()))
    payload = f"{nonce}.{timestamp}"
    secret = _get_github_client_secret()
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{signature}"


def _verify_state(state: str) -> bool:
    """Verify the state parameter's signature and freshness."""
    try:
        parts = state.split(".")
        if len(parts) != 3:
            return False

        nonce, timestamp_str, signature = parts
        payload = f"{nonce}.{timestamp_str}"

        # Verify signature
        secret = _get_github_client_secret()
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(signature, expected):
            logger.warning("State signature mismatch")
            return False

        # Verify freshness
        timestamp = int(timestamp_str)
        if abs(time.time() - timestamp) > STATE_TTL_SECONDS:
            logger.warning("State expired")
            return False

        return True
    except (ValueError, TypeError) as e:
        logger.warning("State verification error: %s", e)
        return False


def handler(event: dict, context) -> dict:
    """
    Lambda handler — routes to /start or /callback based on path.

    Expects API Gateway v2 (HTTP API) or Lambda Function URL event format.
    """
    raw_path = event.get("rawPath", "") or event.get("path", "")
    http_method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    logger.info("Request: %s %s", http_method, raw_path)

    if raw_path.endswith("/start"):
        return _handle_start(event)
    elif raw_path.endswith("/callback"):
        return _handle_callback(event)
    else:
        return _response(404, {"error": "Not found"})


def _handle_start(event: dict) -> dict:
    """
    Generate GitHub OAuth authorize URL and redirect the browser.

    Sets a state cookie for CSRF verification on callback.
    """
    state = _generate_state()

    params = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": CALLBACK_URL,
            "scope": "user:email read:org",
            "state": state,
        }
    )
    authorize_url = f"https://github.com/login/oauth/authorize?{params}"

    # Set state in a cookie for verification on callback
    cookie = f"gh_oauth_state={state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={STATE_TTL_SECONDS}"

    return {
        "statusCode": 302,
        "headers": {
            "Location": authorize_url,
            "Set-Cookie": cookie,
            "Cache-Control": "no-store",
        },
        "body": "",
    }


def _handle_callback(event: dict) -> dict:
    """
    Handle GitHub OAuth callback:
    1. Verify state
    2. Exchange code for GitHub token
    3. Fetch GitHub user info
    4. Allowlist check
    5. Provision Cognito user
    6. Return tokens via redirect with URL fragment
    """
    # Extract query parameters
    params = event.get("queryStringParameters") or {}
    code = params.get("code", "")
    state = params.get("state", "")
    error = params.get("error", "")

    if error:
        error_desc = params.get("error_description", error)
        logger.error("GitHub returned error: %s", error_desc)
        return _redirect_with_error(f"github_error: {error_desc}")

    if not code:
        return _redirect_with_error("missing_code")

    # Verify state from cookie
    cookies = _parse_cookies(event)
    cookie_state = cookies.get("gh_oauth_state", "")

    if not state or not cookie_state:
        logger.warning("Missing state parameter or cookie")
        return _redirect_with_error("missing_state")

    if not hmac.compare_digest(state, cookie_state):
        logger.warning("State mismatch: param vs cookie")
        return _redirect_with_error("state_mismatch")

    if not _verify_state(state):
        return _redirect_with_error("invalid_state")

    try:
        # Exchange code for GitHub access token
        client_secret = _get_github_client_secret()
        github_token = exchange_code_for_token(code, GITHUB_CLIENT_ID, client_secret)

        # Fetch GitHub user info
        github_user = get_github_user(github_token)
        logger.info("GitHub user: id=%s login=%s", github_user["id"], github_user["login"])

        # Allowlist check
        if ALLOWLIST_MODE.lower() == "org":
            orgs = [o.strip() for o in ALLOWED_ORGS.split(",") if o.strip()]
            org_token = _get_github_org_token() or github_token
            if not check_org_membership(github_user["login"], orgs, org_token):
                return _redirect_with_error("not_authorized")
        elif ALLOWLIST_MODE.lower() == "explicit":
            # For explicit mode, deny by default in the broker
            # (would need DynamoDB allowlist check — same as pre-signup)
            logger.warning("Explicit allowlist mode not yet implemented in broker")
            return _redirect_with_error("not_authorized")
        # mode == "open" allows everyone

        # Provision Cognito user and get tokens
        tokens = provision_and_authenticate(
            user_pool_id=COGNITO_USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            github_id=github_user["id"],
            github_login=github_user["login"],
            email=github_user["email"],
            name=github_user["name"],
            avatar_url=github_user["avatar_url"],
        )

        # Redirect to frontend callback with tokens in query params
        # The frontend's AuthCallback page will read and store them
        callback_params = urllib.parse.urlencode(
            {
                "id_token": tokens["id_token"],
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expires_in": str(tokens["expires_in"]),
                "token_type": "Bearer",
                "source": "github_broker",
            }
        )
        redirect_url = f"{FRONTEND_URL}/auth/callback?{callback_params}"

        # Clear the state cookie
        clear_cookie = "gh_oauth_state=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"

        return {
            "statusCode": 302,
            "headers": {
                "Location": redirect_url,
                "Set-Cookie": clear_cookie,
                "Cache-Control": "no-store",
            },
            "body": "",
        }

    except ValueError as e:
        logger.error("Auth broker error: %s", e)
        return _redirect_with_error("auth_failed")
    except Exception as e:
        logger.exception("Unexpected error in auth broker: %s", e)
        return _redirect_with_error("internal_error")


def _parse_cookies(event: dict) -> dict[str, str]:
    """Parse cookies from the Lambda event."""
    cookies: dict[str, str] = {}
    # Lambda Function URL / API Gateway v2 format
    cookie_list = event.get("cookies", [])
    if cookie_list:
        for cookie_str in cookie_list:
            if "=" in cookie_str:
                key, _, value = cookie_str.partition("=")
                cookies[key.strip()] = value.strip()
        return cookies

    # API Gateway v1 / headers format
    headers = event.get("headers") or {}
    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


def _redirect_with_error(error: str) -> dict:
    """Redirect to frontend login page with error parameter."""
    params = urllib.parse.urlencode({"error": error})
    redirect_url = f"{FRONTEND_URL}/login?{params}"
    return {
        "statusCode": 302,
        "headers": {
            "Location": redirect_url,
            "Cache-Control": "no-store",
        },
        "body": "",
    }


def _response(status_code: int, body: dict) -> dict:
    """Return a JSON response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
