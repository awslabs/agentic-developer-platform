"""
Pre Sign-Up Lambda Trigger for AWS Cognito.

Controls which GitHub users can create accounts via the "Sign in with GitHub" flow.
Supports three modes:
- open: Any GitHub user can sign in
- org: Only members of specified GitHub orgs can sign in
- explicit: Only users in the DynamoDB allowlist table can sign in

Issue #314: GitHub-based authentication across ADP web UIs
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

    mode = ALLOWLIST_MODE.lower()

    if mode == "open":
        logger.info("Allowlist mode is 'open'; allowing all users")
        event["response"]["autoConfirmUser"] = True
        return event

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
