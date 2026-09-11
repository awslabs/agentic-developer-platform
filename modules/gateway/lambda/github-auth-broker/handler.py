"""
GitHub Auth Broker Lambda — converts GitHub OAuth flow into Cognito sessions.

Issue #520: Replaces the failed Cognito-OIDC approach from #518/#519.

Endpoints:
  GET  /start    — returns redirect URL to GitHub OAuth authorize
  GET  /callback — handles GitHub callback, provisions Cognito user, redirects to
                   the SPA with a single-use exchange code (never with tokens)
  POST /exchange — swaps that code for the Cognito tokens in a JSON body

Issue #4133: /callback used to hand the SPA its session tokens as *query
parameters*, so a working session leaked into browser history, the Referer
header, and every CDN/proxy access log on the path. The hand-off was also
unbound from the login attempt the user actually started, making login CSRF /
session fixation trivial. Tokens now move in a POST response body, keyed by a
short-lived single-use code that is itself bound to a nonce the SPA generated
before login started.

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
  AUTH_CODE_TABLE         — DynamoDB table holding pending exchange codes
                            (#4133). When UNSET the broker falls back to the
                            legacy tokens-in-URL redirect; see
                            _emit_session_handoff for why that fallback exists.
  IDENTITY_INDEX_TABLE    — Identity-index table carrying the member_org_ids
                            projection (#4849). Read-only, SHADOW MODE: the
                            verdict is logged, never enforced. Unset ⇒ the read
                            reports UNAVAILABLE and login is unaffected.
  USER_IDENTITY_INDEX_TABLE   — v2 identity-index table for the same read (#537).
  USER_IDENTITY_INDEX_V2_READ — "true" to read v2 first, legacy as fallback.
  LOG_LEVEL               — Logging level (default: INFO)
"""

import base64
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
# Issue #4133: DynamoDB table for single-use session-handoff codes.
AUTH_CODE_TABLE = os.environ.get("AUTH_CODE_TABLE", "")

# State signing key (derived from client secret for HMAC)
STATE_TTL_SECONDS = 600  # 10 minutes

# Issue #4133: an exchange code is redeemed by the SPA within milliseconds of the
# redirect landing. Two minutes covers a slow page load with room to spare while
# keeping the window a leaked code is useful in very small.
AUTH_CODE_TTL_SECONDS = 120

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


def _generate_state(app_state: str = "") -> str:
    """Generate a signed state parameter with timestamp for CSRF protection.

    Issue #4133: ``app_state`` is a nonce the SPA generated and stored in its own
    sessionStorage before starting login. Folding it into the *signed* payload
    lets the broker echo it back to the SPA at the end of the flow without a
    server-side session, and lets the SPA prove the callback belongs to the login
    attempt it started. It is signed rather than merely passed through so an
    attacker cannot swap in a nonce of their own choosing mid-flight.

    The serialised form is ``nonce.timestamp[.app_state].signature``. ``app_state``
    is validated by the caller to exclude "." so the field split stays unambiguous.
    """
    nonce = secrets.token_urlsafe(24)
    timestamp = str(int(time.time()))
    payload = f"{nonce}.{timestamp}.{app_state}" if app_state else f"{nonce}.{timestamp}"
    secret = _get_github_client_secret()
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{signature}"


def _verify_state(state: str) -> bool:
    """Verify the state parameter's signature and freshness."""
    try:
        parts = state.split(".")
        # 3 parts = no app_state (legacy / SPA that predates #4133), 4 = with it.
        if len(parts) not in (3, 4):
            return False

        *payload_parts, signature = parts
        payload = ".".join(payload_parts)
        timestamp_str = payload_parts[1]

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


def _extract_app_state(state: str) -> str:
    """Pull the SPA-supplied nonce back out of a verified state token (#4133).

    Returns "" when the state carries no app_state (a login started by a SPA
    build that predates #4133). Only ever call this on a state that
    ``_verify_state`` has already accepted — the value is trusted downstream.
    """
    parts = state.split(".")
    return parts[2] if len(parts) == 4 else ""


def _is_valid_app_state(app_state: str) -> bool:
    """Bound the SPA nonce to an unambiguous, non-abusable shape (#4133).

    "." is excluded because it is the state token's field separator: allowing it
    would let a crafted nonce forge extra fields. The length cap keeps a hostile
    caller from inflating the state token (and the GitHub authorize URL with it).
    """
    return bool(app_state) and len(app_state) <= 128 and all(c.isalnum() or c in "-_~" for c in app_state)


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

    Accepts both API Gateway payload shapes. In production this broker is
    fronted by the REST (v1) API — /auth/github/{proxy+} with an aws_proxy
    integration — which sends `path` and a top-level `httpMethod`. The v2
    (HTTP API / Function URL) shape sends `rawPath` and
    `requestContext.http.method`. Read both, always.
    """
    raw_path = event.get("rawPath", "") or event.get("path", "")
    # A v1 proxy event has NO requestContext.http, so reading only the v2
    # location would resolve to the "GET" default for every real request and
    # silently drop the OPTIONS preflight into path routing (#4133).
    http_method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()

    logger.info("Request: %s %s", http_method, raw_path)

    # Issue #4133: /exchange is the only POST route, and browsers preflight it
    # because the SPA (CloudFront) and this broker (API Gateway) are different
    # origins. Answer OPTIONS before any routing work.
    if http_method == "OPTIONS":
        return _cors_preflight_response()

    if raw_path.endswith("/start"):
        return _handle_start(event)
    elif raw_path.endswith("/callback"):
        return _handle_callback(event)
    elif raw_path.endswith("/exchange"):
        return _handle_exchange(event)
    else:
        return _response(404, {"error": "Not found"})


def _handle_start(event: dict) -> dict:
    """
    Generate GitHub OAuth authorize URL and redirect the browser.

    Sets a state cookie for CSRF verification on callback.
    """
    # Issue #4133: bind this login attempt to the nonce the SPA stored before
    # navigating here. An absent/malformed app_state is NOT fatal — a SPA build
    # that predates #4133 sends none, and failing closed here would take out
    # every login during the rollout skew (the #3999 lockout class).
    params = event.get("queryStringParameters") or {}
    app_state = (params.get("app_state") or "").strip()
    if app_state and not _is_valid_app_state(app_state):
        logger.warning("Ignoring malformed app_state on /start")
        app_state = ""

    state = _generate_state(app_state)

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
            "Referrer-Policy": "no-referrer",
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


def _log_membership_eligibility_shadow(github_id, github_login: str, denial: str | None) -> None:
    """Log what the membership-eligibility read would decide (Issue #4849).

    Shadow only — never changes the sign-in outcome. Wrapped so that a fault in
    the new read path cannot break login: this Lambda is the single enforcement
    point for GitHub sign-in, and an exception here would be a total outage for a
    code path that is not even supposed to have an opinion yet.
    """
    try:
        from membership_eligibility import check_platform_membership

        verdict = check_platform_membership(str(github_id))
        logger.info(
            "membership-eligibility SHADOW: github_id=%s login=%s verdict=%s live_outcome=%s would_agree=%s",
            github_id,
            github_login,
            verdict,
            "denied" if denial else "allowed",
            (verdict == "eligible") == (denial is None),
        )
    except Exception as e:
        logger.warning("membership-eligibility SHADOW: read raised (ignored): %s", e)


def _handle_callback(event: dict) -> dict:
    """
    Handle GitHub OAuth callback:
    1. Verify state
    2. Exchange code for GitHub token
    3. Fetch GitHub user info
    4. Allowlist check
    5. Provision Cognito user
    6. Redirect to the SPA with a single-use exchange code (#4133 — never tokens)
    """
    # Extract query parameters
    params = event.get("queryStringParameters") or {}
    code = params.get("code", "")
    state = params.get("state", "")
    error = params.get("error", "")

    if error:
        error_desc = params.get("error_description", error)

        # Issue #4017: redirect_uri_mismatch is the ONLY signal that the App's
        # callback URL has drifted. GitHub exposes no API to read an App's
        # callback URL back, so this error path is the sole place a mismatch
        # becomes observable — every other check would be guesswork.
        #
        # We log the callback we actually sent so an operator can compare it
        # against the App's settings page. We deliberately do NOT write the
        # derived value into CALLBACK_URL or any other env: that would reverse
        # #2708's runtime derivation and pin a value that goes stale silently.
        if error == "redirect_uri_mismatch":
            logger.error(
                "event=oauth_callback_drift error=redirect_uri_mismatch sent_redirect_uri=%s detail=%s "
                "remediation=update the GitHub App's Callback URL to match sent_redirect_uri",
                _derive_callback_url(event) or "<unresolved>",
                error_desc,
            )
            return _redirect_with_error("redirect_uri_mismatch")

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

        # Issue #4849, SHADOW MODE: exercise the membership-eligibility read and
        # log what it *would* decide. Deliberately does not affect `denial` —
        # T5 (#4844) is what makes this authoritative, behind a new ALLOWLIST_MODE.
        # Keeping the read live but inert is what lets the projection's accuracy be
        # measured against real sign-ins before it can lock anyone out.
        _log_membership_eligibility_shadow(github_user["id"], github_user["login"], denial)

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

        return _emit_session_handoff(tokens, _extract_app_state(state))

    except ValueError as e:
        logger.error("Auth broker error: %s", e)
        return _redirect_with_error("auth_failed")
    except Exception as e:
        logger.exception("Unexpected error in auth broker: %s", e)
        return _redirect_with_error("internal_error")


def _hash_app_state(app_state: str) -> str:
    """Hash the SPA nonce before storing it beside the tokens (#4133).

    The stored row is the one durable artifact of an in-flight login. Keeping only
    a digest means a read of the table does not yield the value an attacker would
    need to redeem the code.
    """
    return hashlib.sha256(app_state.encode()).hexdigest()


def _emit_session_handoff(tokens: dict, app_state: str) -> dict:
    """Redirect to the SPA with a single-use exchange code instead of tokens (#4133).

    Falls back to the legacy tokens-in-query redirect when AUTH_CODE_TABLE is
    unset. That fallback is deliberate rollout safety, not an oversight: this
    Lambda's code ships via github-auth-broker-deploy.yml while the table ships
    via gateway-infra-apply.yml, so there is a window where new code runs without
    its table. Failing closed there would be a *total* login outage — exactly the
    #3999 code-before-config lockout. Degrading to today's behaviour instead is
    strictly no worse than main, and self-heals the moment terraform applies.
    """
    if not AUTH_CODE_TABLE:
        logger.warning("AUTH_CODE_TABLE is not configured; falling back to the legacy tokens-in-URL redirect (see #4133)")
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
        return _spa_redirect(f"{FRONTEND_URL}/auth/callback?{callback_params}")

    code = secrets.token_urlsafe(32)
    try:
        _put_auth_code(code, tokens, app_state)
    except Exception:
        # Do NOT fall back to the URL transport here: the table exists, so this is
        # a real fault (throttle/IAM/outage), not a rollout gap. Leaking tokens
        # into the URL to paper over it would reintroduce the vulnerability.
        logger.exception("Failed to persist exchange code")
        return _redirect_with_error("handoff_failed")

    callback_params = urllib.parse.urlencode({"code": code, "state": app_state, "source": "github_broker"})
    return _spa_redirect(f"{FRONTEND_URL}/auth/callback?{callback_params}")


def _spa_redirect(redirect_url: str) -> dict:
    """302 to the SPA, clearing the OAuth state cookie."""
    return {
        "statusCode": 302,
        "headers": {
            "Location": redirect_url,
            # Clear the state cookie — this login attempt is finished.
            "Set-Cookie": "gh_oauth_state=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0",
            "Cache-Control": "no-store",
            # #4133 defence-in-depth: keep the callback URL out of the Referer
            # header sent by the landing page's subresource requests.
            "Referrer-Policy": "no-referrer",
        },
        "body": "",
    }


def _put_auth_code(code: str, tokens: dict, app_state: str) -> None:
    """Store the pending session under a single-use code (#4133)."""
    boto3.client("dynamodb").put_item(
        TableName=AUTH_CODE_TABLE,
        Item={
            "code": {"S": code},
            "id_token": {"S": tokens["id_token"]},
            "access_token": {"S": tokens["access_token"]},
            "refresh_token": {"S": tokens.get("refresh_token") or ""},
            "expires_in": {"N": str(tokens["expires_in"])},
            "app_state_hash": {"S": _hash_app_state(app_state)},
            # DynamoDB TTL reclaims rows lazily (minutes to hours), so it is a
            # storage-hygiene mechanism only. _handle_exchange enforces the real
            # deadline against expires_at on read.
            "expires_at": {"N": str(int(time.time()) + AUTH_CODE_TTL_SECONDS)},
            "ttl": {"N": str(int(time.time()) + 3600)},
        },
    )


def _handle_exchange(event: dict) -> dict:
    """Swap a single-use code for the Cognito tokens (#4133).

    Consumes the code with an atomic delete_item(ReturnValues="ALL_OLD"): the
    delete IS the read, so a code cannot be redeemed twice even under concurrent
    requests. A read-then-delete would leave a replay window.
    """
    if not AUTH_CODE_TABLE:
        logger.error("Exchange requested but AUTH_CODE_TABLE is not configured")
        return _json_response(503, {"error": "exchange_unavailable"})

    try:
        raw_body = event.get("body") or "{}"
        # API Gateway may hand the body over base64-encoded depending on the
        # integration's content handling; decode before parsing.
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode()
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _json_response(400, {"error": "invalid_body"})

    # A valid JSON document need not be an object ("[]", '"x"', "5" all parse).
    if not isinstance(body, dict):
        return _json_response(400, {"error": "invalid_body"})

    code = body.get("code") or ""
    app_state = body.get("app_state") or ""
    if not code:
        return _json_response(400, {"error": "missing_code"})

    try:
        result = boto3.client("dynamodb").delete_item(
            TableName=AUTH_CODE_TABLE,
            Key={"code": {"S": code}},
            ReturnValues="ALL_OLD",
        )
    except Exception:
        logger.exception("Failed to consume exchange code")
        return _json_response(500, {"error": "exchange_failed"})

    item = result.get("Attributes")
    if not item:
        # Unknown, already-redeemed, or TTL-reaped code.
        logger.warning("Exchange code not found or already used")
        return _json_response(400, {"error": "invalid_code"})

    if int(item.get("expires_at", {}).get("N", "0")) < time.time():
        logger.warning("Exchange code expired")
        return _json_response(400, {"error": "expired_code"})

    # A row minted for an empty app_state carries sha256("") — a publicly known
    # constant, so its nonce binding is vacuous and anyone holding the code could
    # redeem it. Nothing legitimate ever redeems one either: a login that sent no
    # app_state came from an SPA build that predates /exchange. Refuse outright
    # rather than honour an unbound code.
    expected_hash = item.get("app_state_hash", {}).get("S", "")
    if hmac.compare_digest(expected_hash, _hash_app_state("")):
        logger.warning("Exchange code was minted without an app_state nonce; refusing")
        return _json_response(400, {"error": "state_mismatch"})

    # Bind the code to the login attempt: a code lifted from history or a proxy
    # log is useless without the nonce in the victim's sessionStorage.
    if not hmac.compare_digest(expected_hash, _hash_app_state(app_state)):
        logger.warning("Exchange app_state mismatch")
        return _json_response(400, {"error": "state_mismatch"})

    return _json_response(
        200,
        {
            "id_token": item["id_token"]["S"],
            "access_token": item["access_token"]["S"],
            "refresh_token": item.get("refresh_token", {}).get("S", ""),
            "expires_in": int(item["expires_in"]["N"]),
            "token_type": "Bearer",
        },
    )


def _cors_headers() -> dict[str, str]:
    """CORS headers for the SPA→broker exchange call (#4133).

    Scoped to FRONTEND_URL rather than "*", and deliberately WITHOUT
    Allow-Credentials: the exchange authenticates with the code in the request
    body, so no cookie ever needs to ride along.
    """
    return {
        "Access-Control-Allow-Origin": FRONTEND_URL or "null",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _cors_preflight_response() -> dict:
    """Answer the browser's preflight for POST /exchange (#4133)."""
    return {"statusCode": 204, "headers": {**_cors_headers(), "Cache-Control": "no-store"}, "body": ""}


def _json_response(status_code: int, body: dict) -> dict:
    """JSON response carrying tokens or an error to the SPA (#4133)."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            **_cors_headers(),
        },
        "body": json.dumps(body),
    }


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
            "Referrer-Policy": "no-referrer",
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
