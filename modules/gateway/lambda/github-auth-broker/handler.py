"""
GitHub Auth Broker Lambda — converts GitHub OAuth flow into Cognito sessions.

Issue #520: Replaces the failed Cognito-OIDC approach from #518/#519.

Endpoints:
  GET /start    — returns redirect URL to GitHub OAuth authorize
  GET /callback — handles GitHub callback, provisions Cognito user, returns tokens

Environment variables:
  GITHUB_CLIENT_ID        — GitHub OAuth App client ID (fallback; the OAuth
                            secret is the authoritative source — see #2708)
  GITHUB_CLIENT_SECRET_ARN — Secrets Manager ARN for OAuth credentials
                            (JSON with both client_id and client_secret)
  COGNITO_USER_POOL_ID    — Cognito User Pool ID
  COGNITO_CLIENT_ID       — Cognito App Client ID (public, ADMIN_USER_PASSWORD_AUTH enabled)
  CALLBACK_URL            — Full URL of this Lambda's /callback endpoint. Optional:
                            when unset it is derived from the request context
                            (domainName + stage) at runtime (#2708).
  FRONTEND_URL            — Frontend origin (e.g., https://d1g6cal2ts4iis.cloudfront.net)
  ALLOWLIST_MODE          — "org" (default), "open", or "explicit". Anything
                            other than "org" denies sign-in; see #3986.
  ALLOWED_ORGS            — Comma-separated list of allowed GitHub orgs.
                            Required for ALLOWLIST_MODE=org; empty denies.
  ALLOW_OPEN_SIGNUP       — "true" to honour ALLOWLIST_MODE=open. Without it,
                            "open" is treated as a misconfiguration and denied.
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
from allowlist import ALLOWED, UNVERIFIED, check_org_membership
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
# Issue #3986: default fail-closed. An unset ALLOWLIST_MODE used to mean "open"
# (any GitHub user gets a provisioned Cognito user), so the shipped default
# provisioned accounts for the entire internet.
ALLOWLIST_MODE = os.environ.get("ALLOWLIST_MODE", "org")
ALLOWED_ORGS = os.environ.get("ALLOWED_ORGS", "")
ALLOW_OPEN_SIGNUP = os.environ.get("ALLOW_OPEN_SIGNUP", "").lower() == "true"
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")

# State signing key (derived from client secret for HMAC)
STATE_TTL_SECONDS = 600  # 10 minutes

# Terraform seeds the OAuth secret with this literal before real credentials
# are wired (gateway-infra-apply.yml). It must never be treated as a real value.
_PLACEHOLDER = "PLACEHOLDER"

# Cached secrets
_github_oauth_creds: dict[str, str] | None = None
_github_oauth_creds_ts: float = 0  # epoch timestamp of last fetch
_OAUTH_CREDS_TTL = 300  # re-read from Secrets Manager every 5 minutes
_github_org_token: str | None = None


def _get_github_oauth_creds() -> dict[str, str]:
    """Retrieve the GitHub OAuth credentials dict from Secrets Manager (TTL-cached).

    The secret at adp/<env>/cognito/github-oauth-credentials is a JSON blob with
    both ``client_id`` and ``client_secret``. Issue #2708 makes this secret the
    single source of truth for the OAuth identity so login works immediately
    after App registration (which writes both keys) without mutating Lambda env.

    Cached for 5 minutes so that re-registering a GitHub App (which updates the
    secret) takes effect without needing a manual Lambda recycle.
    """
    global _github_oauth_creds, _github_oauth_creds_ts
    now = time.time()
    if _github_oauth_creds is not None and (now - _github_oauth_creds_ts) < _OAUTH_CREDS_TTL:
        return _github_oauth_creds

    if not GITHUB_CLIENT_SECRET_ARN:
        raise ValueError("GITHUB_CLIENT_SECRET_ARN not configured")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=GITHUB_CLIENT_SECRET_ARN)
    secret_data = json.loads(response["SecretString"])
    _github_oauth_creds = {
        "client_id": secret_data.get("client_id", ""),
        "client_secret": secret_data.get("client_secret", ""),
    }
    _github_oauth_creds_ts = now
    return _github_oauth_creds


def _get_github_client_secret() -> str:
    """Retrieve GitHub OAuth client secret from Secrets Manager (cached)."""
    return _get_github_oauth_creds().get("client_secret", "")


def _get_github_client_id() -> str:
    """Resolve the GitHub OAuth client_id.

    Issue #2708: prefer the value stored in the OAuth secret (written by the
    register flow), falling back to the ``GITHUB_CLIENT_ID`` env var. The env
    fallback keeps env-var-configured deployments (embark1) working unchanged.
    """
    try:
        secret_client_id = _get_github_oauth_creds().get("client_id", "")
    except Exception as exc:  # noqa: BLE001 — fall back to env on any read error
        logger.warning("Could not read client_id from OAuth secret: %s", exc)
        secret_client_id = ""

    if secret_client_id and secret_client_id != _PLACEHOLDER:
        return secret_client_id
    return GITHUB_CLIENT_ID


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


def _derive_callback_url(event: dict) -> str:
    """Resolve the OAuth callback URL for the GitHub authorize redirect.

    Issue #2708: Derive the callback URL at runtime from the incoming request's
    ``requestContext`` (``domainName`` + ``stage``) so Terraform no longer needs
    to set CALLBACK_URL statically (broker ↔ api-gateway modules would cycle) and
    the register flow no longer needs to mutate Lambda env. The ``CALLBACK_URL``
    env var, when set, always wins (keeps env-var-configured deployments working).

    Returns "" when neither the env var nor the request context can supply one;
    the caller lets GitHub reject the request rather than build a broken redirect.
    """
    if CALLBACK_URL:
        return CALLBACK_URL

    request_context = event.get("requestContext") or {}
    domain_name = request_context.get("domainName", "")
    stage = request_context.get("stage", "")
    if not domain_name:
        return ""

    # API Gateway's "$default" stage is not part of the invoke path.
    if stage and stage != "$default":
        return f"https://{domain_name}/{stage}/auth/github/callback"
    return f"https://{domain_name}/auth/github/callback"


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
            "client_id": _get_github_client_id(),
            "redirect_uri": _derive_callback_url(event),
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


def _check_allowlist(github_login: str, github_token: str) -> str | None:
    """Decide whether a GitHub user may sign in.

    Issue #3986: fail closed. Only ``org`` mode grants access; every other mode
    — including an unset, typo'd, or explicitly ``open`` ALLOWLIST_MODE — denies,
    mirroring the pre-signup trigger's unknown-mode→deny behaviour.

    Returns None when the user is allowed, otherwise the error code to redirect
    with. ``org_check_unavailable`` distinguishes "we could not verify" from
    ``not_authorized`` ("verified, not a member") so a missing org token or an
    unapproved OAuth App doesn't look like a legitimate denial.
    """
    mode = ALLOWLIST_MODE.strip().lower()

    if mode == "org":
        orgs = [o.strip() for o in ALLOWED_ORGS.split(",") if o.strip()]
        org_token = _get_github_org_token()
        if not org_token:
            # Falling back to the user's own OAuth token works only when the
            # OAuth App is org-approved; log it so a 302/404 from GitHub is
            # attributable (#3986).
            logger.warning("GITHUB_TOKEN_SECRET_ARN is not configured; falling back to the user's OAuth token for the org check")
            org_token = github_token
        result = check_org_membership(github_login, orgs, org_token)
        if result == ALLOWED:
            return None
        if result == UNVERIFIED:
            return "org_check_unavailable"
        return "not_authorized"

    if mode == "open":
        if ALLOW_OPEN_SIGNUP:
            logger.warning("ALLOWLIST_MODE=open with ALLOW_OPEN_SIGNUP=true: allowing %s with NO allowlist enforcement", github_login)
            return None
        logger.error("ALLOWLIST_MODE=open without ALLOW_OPEN_SIGNUP=true is a misconfiguration; denying sign-in")
        return "not_authorized"

    if mode == "explicit":
        # Out of scope for #3986; the DynamoDB allowlist reuse path is documented
        # in the issue for whoever implements it. Deny until then.
        logger.error("ALLOWLIST_MODE=explicit is not implemented in the broker; denying sign-in")
        return "not_authorized"

    logger.error("Unknown ALLOWLIST_MODE %r; denying sign-in", ALLOWLIST_MODE)
    return "not_authorized"


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
        github_token = exchange_code_for_token(code, _get_github_client_id(), client_secret)

        # Fetch GitHub user info
        github_user = get_github_user(github_token)
        logger.info("GitHub user: id=%s login=%s", github_user["id"], github_user["login"])

        # Allowlist check — must run before provisioning. admin_create_user does
        # not fire PreSignUp_ExternalProvider, and the pre-signup trigger
        # deliberately passes PreSignUp_AdminCreateUser through, so the broker is
        # the only enforcement point for GitHub sign-in (#3986).
        denial = _check_allowlist(github_user["login"], github_token)
        if denial:
            return _redirect_with_error(denial)

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
