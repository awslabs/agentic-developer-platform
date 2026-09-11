"""
Pre Sign-Up Lambda Trigger for AWS Cognito.

Controls which GitHub users can create accounts via the "Sign in with GitHub" flow.
Supports four modes:
- open: Any GitHub user can sign in (requires ALLOW_OPEN_SIGNUP=true; see below)
- org: Only members of specified GitHub orgs can sign in
- platform: Only users holding at least one platform org membership can sign in
- explicit: Only users in the DynamoDB allowlist table can sign in

Anything else — unset, typo'd, or a mode this copy does not know — denies.

Issue #314: GitHub-based authentication across ADP web UIs

Issue #4844 adds ``platform`` mode here for PARITY, not for enforcement. This
trigger is NOT the live gate for GitHub sign-in: ``admin_create_user`` (what the
auth broker calls) does not fire ``PreSignUp_ExternalProvider``, and this handler
passes ``PreSignUp_AdminCreateUser`` straight through, so the broker is the only
enforcement point (#3986, and ``lambda/github-auth-broker/handler.py``). The mode
logic is kept identical anyway so a future trigger change cannot resurrect a
divergent rule — the drift between these copies is what #4848/#4849 were filed
over. A green parity matrix here proves nothing about live sign-in; the
behavioral gate test targets the broker.

Issue #4844 also aligns ``open`` mode with the broker's fail-closed semantics.
This copy used to auto-confirm ``open`` with no safety flag while the broker
required ``ALLOW_OPEN_SIGNUP=true`` — a real, verified divergence. It now requires
the same flag. Terraform passes the flag from the same root-module variable that
feeds the broker, so no environment's behaviour changes.

The ``explicit`` divergence is deliberately NOT "aligned": this copy has a
working DynamoDB allowlist implementation and the broker denies the mode as
unimplemented. Deleting a working implementation to match a stub would be a
regression, so it is documented here and in the parity matrix instead.
"""

import json
import logging
import os
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
log_level = os.environ.get("LOG_LEVEL", "INFO")
logger.setLevel(getattr(logging, log_level, logging.INFO))

# Configuration from environment variables
ALLOWLIST_MODE = os.environ.get("ALLOWLIST_MODE", "org")
ALLOWED_ORGS = os.environ.get("ALLOWED_ORGS", "")
ALLOWLIST_TABLE = os.environ.get("ALLOWLIST_TABLE", "")
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")
# Issue #4844: same escape hatch, same spelling, same default as the broker's
# ALLOW_OPEN_SIGNUP (#3986). Without it, ALLOWLIST_MODE=open is a misconfiguration
# rather than an instruction, in both copies.
ALLOW_OPEN_SIGNUP = os.environ.get("ALLOW_OPEN_SIGNUP", "").lower() == "true"

# Lazy-initialized clients
_dynamodb = None
_secrets_client = None
_github_token = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_github_token() -> str:
    """Retrieve the GitHub API token from Secrets Manager (cached)."""
    global _github_token
    if _github_token is not None:
        return _github_token

    if not GITHUB_TOKEN_SECRET_ARN:
        logger.warning("GITHUB_TOKEN_SECRET_ARN not set; org membership checks will fail")
        return ""

    try:
        client = _get_secrets_client()
        response = client.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        secret_string = response["SecretString"]
        # Support both plain token and JSON {"token": "..."} formats
        try:
            parsed = json.loads(secret_string)
            _github_token = parsed.get("token", secret_string)
        except (json.JSONDecodeError, TypeError):
            _github_token = secret_string
        return _github_token
    except ClientError as e:
        logger.error(f"Failed to retrieve GitHub token from Secrets Manager: {e}")
        return ""


def handler(event: dict, context) -> dict:
    """
    Pre Sign-Up Lambda handler.

    Triggered by Cognito PreSignUp_ExternalProvider event when a user signs in
    via an external identity provider (GitHub).

    Args:
        event: Cognito Pre Sign-Up trigger event
        context: Lambda context

    Returns:
        Modified event (autoConfirmUser=True if allowed)

    Raises:
        Exception: If user is not allowed to sign up (Cognito denies the sign-up)
    """
    trigger_source = event.get("triggerSource", "")
    logger.info(f"Pre Sign-Up trigger: {trigger_source}")
    logger.debug(f"Full event: {json.dumps(event, default=str)}")

    # Only gate external provider sign-ups (GitHub OAuth)
    if trigger_source != "PreSignUp_ExternalProvider":
        logger.info(f"Trigger source {trigger_source} is not external provider, allowing")
        return event

    user_attributes = event.get("request", {}).get("userAttributes", {})
    username = event.get("userName", "")

    # Extract GitHub username from the federated identity
    # userName format for external providers: "GitHub_<github_user_id>"
    # The preferred_username or email may also be available depending on IdP config
    github_username = _extract_github_username(username, user_attributes)

    logger.info(f"Processing sign-up for GitHub user: {github_username}")

    # Issue #4849, SHADOW MODE: exercise the membership-eligibility read and log
    # what it would decide. Does not affect the outcome below — T5 (#4844) is what
    # makes it authoritative. See _log_membership_eligibility_shadow.
    _log_membership_eligibility_shadow(username, github_username)

    # Issue #4844: .strip() as well as .lower(), matching the broker's
    # `ALLOWLIST_MODE.strip().lower()`. Without the strip, a mode with stray
    # whitespace (trivially easy to introduce in tfvars or a hand-patched Lambda
    # env) parsed as a KNOWN mode in the broker and an UNKNOWN one here — the two
    # copies disagreeing on the same string. Caught by the parity matrix in
    # tests/lambda/test_allowlist_mode_parity.py.
    mode = ALLOWLIST_MODE.strip().lower()

    if mode == "open":
        # Issue #4844: aligned with the broker — 'open' without the explicit
        # acknowledgement flag is treated as a misconfiguration, not an
        # instruction. See the module docstring.
        if not ALLOW_OPEN_SIGNUP:
            logger.error("ALLOWLIST_MODE=open without ALLOW_OPEN_SIGNUP=true is a misconfiguration; denying sign-up")
            raise Exception("Sign-up is currently disabled due to misconfiguration.")
        logger.warning("Allowlist mode is 'open' with ALLOW_OPEN_SIGNUP=true; allowing %s with NO allowlist enforcement", github_username)
        event["response"]["autoConfirmUser"] = True
        return event

    elif mode == "platform":
        # Issue #4844. Parity branch — see the module docstring for why this copy
        # is not the live gate. Denies (by raising, this trigger's deny idiom) on
        # both "no membership" and "could not check": an unavailable membership
        # source must not fall through to a grant.
        if _check_platform_membership(username):
            logger.info("User %s holds a platform membership", github_username)
            event["response"]["autoConfirmUser"] = True
            return event
        logger.warning("User %s does not hold a platform membership", github_username)
        raise Exception(f"User {github_username} is not a member of any organization on this platform. Contact your administrator for access.")

    elif mode == "org":
        allowed = _check_org_membership(github_username)
        if allowed:
            logger.info(f"User {github_username} is a member of an allowed org")
            event["response"]["autoConfirmUser"] = True
            return event
        else:
            logger.warning(f"User {github_username} is NOT a member of any allowed org")
            raise Exception(f"User {github_username} is not a member of an allowed organization. Contact your administrator for access.")

    elif mode == "explicit":
        allowed = _check_explicit_allowlist(github_username, user_attributes)
        if allowed:
            logger.info(f"User {github_username} is on the explicit allowlist")
            event["response"]["autoConfirmUser"] = True
            return event
        else:
            logger.warning(f"User {github_username} is NOT on the explicit allowlist")
            raise Exception(f"User {github_username} is not on the allowlist. Contact your administrator for access.")

    else:
        logger.error(f"Unknown ALLOWLIST_MODE: {ALLOWLIST_MODE}; denying sign-up")
        raise Exception("Sign-up is currently disabled due to misconfiguration.")


def _extract_github_user_id(username: str) -> str:
    """Extract the numeric GitHub account id from the Cognito userName.

    For external providers the userName is ``<ProviderName>_<providerUserId>``,
    which for the GitHub IdP is ``GitHub_<numeric id>``. The id — not the login —
    is what the identity-index projection is keyed on, because logins are
    renameable. Deliberately does NOT fall back to the login/email the way
    ``_extract_github_username`` does: a login is not a valid key here, and
    guessing one would look up the wrong user rather than fail.
    """
    if "_" in username:
        candidate = username.split("_", 1)[1]
        if candidate.isdigit():
            return candidate
    return ""


def _check_platform_membership(username: str) -> bool:
    """Whether this identity holds at least one platform org membership (#4844).

    ``ALLOWLIST_MODE=platform``'s predicate: eligibility comes from the platform's
    own membership records, not from GitHub org membership. GitHub still proves
    *who* the user is; it no longer decides *whether they belong*.

    Keyed on the GitHub numeric **id** extracted from the Cognito userName, not
    the login: the projection is id-keyed because logins are renameable.

    The predicate is **row existence**, not ``is_active`` — that flag marks which
    single workspace a user currently has selected, so filtering on it would deny
    every member whose selection points at a different org, plus everyone with no
    selection at all. The projection is built without that filter for the same
    reason (``src/admin/memberships.py`` :: ``project_member_org_ids``).

    Fail-CLOSED in every failure mode: no numeric id, an unreadable projection, a
    missing shared module, or any exception all return False. Unlike
    :func:`_log_membership_eligibility_shadow`, this verdict is load-bearing, so
    swallowing an error into a grant would be fail-*open*.
    """
    try:
        from membership_eligibility import ELIGIBLE
        from membership_eligibility import check_platform_membership as _read

        github_id = _extract_github_user_id(username)
        if not github_id:
            logger.error("ALLOWLIST_MODE=platform: no numeric github id in userName=%r; denying", username)
            return False
        verdict = _read(github_id)
        if verdict == ELIGIBLE:
            return True
        logger.warning("ALLOWLIST_MODE=platform: github_id=%s verdict=%s; denying", github_id, verdict)
        return False
    except Exception as e:
        # Includes ImportError: if the shared reader is missing from the zip the
        # mode cannot be enforced, so it must not appear to pass. The packaging
        # that keeps it present is in infra/modules/cognito/pre_signup.tf.
        logger.exception("ALLOWLIST_MODE=platform: membership read raised for userName=%r; denying: %s", username, e)
        return False


def _log_membership_eligibility_shadow(username: str, github_username: str) -> None:
    """Log what the membership-eligibility read would decide (Issue #4849).

    Shadow only — never changes the sign-up outcome, and never raises: a fault in
    a read that has no opinion yet must not be able to deny a sign-up.

    Issue #4844: skipped under ``ALLOWLIST_MODE=platform``, where the same read is
    the live decision for this copy and logs its own real verdict. Shadowing an
    enforcing read would double the DynamoDB call inside Cognito's non-negotiable
    5s trigger budget.
    """
    if ALLOWLIST_MODE.strip().lower() == "platform":
        return
    try:
        from membership_eligibility import check_platform_membership

        github_id = _extract_github_user_id(username)
        if not github_id:
            logger.info(
                "membership-eligibility SHADOW: no numeric github id in userName=%r; skipping",
                username,
            )
            return
        verdict = check_platform_membership(github_id)
        logger.info(
            "membership-eligibility SHADOW: github_id=%s login=%s verdict=%s (mode=%s, outcome unaffected)",
            github_id,
            github_username,
            verdict,
            ALLOWLIST_MODE,
        )
    except Exception as e:
        logger.warning(f"membership-eligibility SHADOW: read raised (ignored): {e}")


def _extract_github_username(username: str, user_attributes: dict) -> str:
    """
    Extract the GitHub username from the Cognito event.

    The userName for external providers is typically "ProviderName_providerUserId".
    The actual GitHub username may be in preferred_username attribute.

    Args:
        username: Cognito userName field (e.g., "GitHub_12345")
        user_attributes: User attributes from the event

    Returns:
        GitHub username string
    """
    # preferred_username is typically set by the GitHub IdP mapping
    preferred = user_attributes.get("preferred_username", "")
    if preferred:
        return preferred

    # Fallback: use the email prefix
    email = user_attributes.get("email", "")
    if email and "@" in email:
        return email.split("@")[0]

    # Last resort: strip provider prefix from userName
    if "_" in username:
        return username.split("_", 1)[1]

    return username


def _check_org_membership(github_username: str) -> bool:
    """
    Check if the GitHub user is a member of any allowed organization.

    Uses the GitHub API: GET /orgs/{org}/members/{username}
    Returns 204 if member, 404 if not.

    Args:
        github_username: GitHub username to check

    Returns:
        True if the user is a member of at least one allowed org
    """
    if not ALLOWED_ORGS:
        logger.warning("ALLOWED_ORGS is empty; no orgs to check against")
        return False

    orgs = [org.strip() for org in ALLOWED_ORGS.split(",") if org.strip()]
    if not orgs:
        logger.warning("ALLOWED_ORGS parsed to empty list")
        return False

    token = _get_github_token()
    if not token:
        logger.error("No GitHub token available; cannot check org membership")
        return False

    for org in orgs:
        if _is_org_member(org, github_username, token):
            return True

    return False


def _is_org_member(org: str, username: str, token: str) -> bool:
    """
    Check membership of a single GitHub org.

    Args:
        org: GitHub organization name
        username: GitHub username
        token: GitHub API token

    Returns:
        True if the user is a member
    """
    url = f"https://api.github.com/orgs/{org}/members/{username}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            # 204 No Content = member
            return response.status == 204
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return True
        if e.code == 404:
            logger.debug(f"User {username} is not a member of org {org}")
            return False
        if e.code == 302:
            # 302 means requester is not an org member themselves;
            # cannot confirm membership
            logger.warning(f"GitHub returned 302 for org {org}; token may lack org:read scope")
            return False
        logger.error(f"GitHub API error for org {org}: {e.code} {e.reason}")
        return False
    except (urllib.error.URLError, OSError) as e:
        logger.error(f"Network error checking org {org} membership: {e}")
        return False


def _check_explicit_allowlist(github_username: str, user_attributes: dict) -> bool:
    """
    Check if the user is on the explicit DynamoDB allowlist.

    Checks by both GitHub username and email.

    Args:
        github_username: GitHub username
        user_attributes: User attributes from the event

    Returns:
        True if the user is found in the allowlist
    """
    if not ALLOWLIST_TABLE:
        logger.error("ALLOWLIST_TABLE not configured; cannot check allowlist")
        return False

    try:
        dynamodb = _get_dynamodb()
        table = dynamodb.Table(ALLOWLIST_TABLE)

        # Check by GitHub username
        response = table.get_item(Key={"username": github_username.lower()})
        if "Item" in response:
            item = response["Item"]
            # Check if the entry is active
            if item.get("active", True):
                return True

        # Also check by email as a fallback
        email = user_attributes.get("email", "")
        if email:
            response = table.get_item(Key={"username": email.lower()})
            if "Item" in response:
                item = response["Item"]
                if item.get("active", True):
                    return True

        return False

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceNotFoundException":
            logger.error(f"Allowlist table not found: {ALLOWLIST_TABLE}")
        else:
            logger.error(f"DynamoDB error checking allowlist: {e}")
        return False
    except Exception as e:
        logger.error(f"Error checking allowlist: {e}")
        return False
