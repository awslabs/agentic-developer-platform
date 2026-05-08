"""
Cognito user provisioning — idempotent create + admin_initiate_auth.

Issue #520: Lambda broker for GitHub sign-in.
"""

import logging
import secrets
import string
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CognitoTokens(TypedDict):
    id_token: str
    access_token: str
    refresh_token: str
    expires_in: int


def _generate_random_password(length: int = 32) -> str:
    """Generate a cryptographically random password meeting Cognito requirements."""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    # Ensure at least one of each required type
    password = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()"),
    ]
    password += [secrets.choice(alphabet) for _ in range(length - 4)]
    # Shuffle to avoid predictable positions
    shuffled = list(password)
    secrets.SystemRandom().shuffle(shuffled)
    return "".join(shuffled)


def provision_and_authenticate(
    user_pool_id: str,
    client_id: str,
    github_id: int,
    github_login: str,
    email: str,
    name: str,
    avatar_url: str,
) -> CognitoTokens:
    """
    Ensure a Cognito user exists for the GitHub identity and return tokens.

    Username convention: GitHub_<numeric-id> (immutable, unique).
    If the user already exists, skip creation. Then authenticate via
    ADMIN_USER_PASSWORD_AUTH to get Cognito tokens.

    Args:
        user_pool_id: Cognito User Pool ID.
        client_id: Cognito App Client ID.
        github_id: GitHub numeric user ID.
        github_login: GitHub login/username.
        email: User's email.
        name: User's display name.
        avatar_url: User's avatar URL.

    Returns:
        CognitoTokens with id_token, access_token, refresh_token, expires_in.
    """
    client = boto3.client("cognito-idp")
    username = f"GitHub_{github_id}"
    password = _generate_random_password()

    # Try to create the user (idempotent — skip if exists)
    user_exists = _ensure_user_exists(
        client=client,
        user_pool_id=user_pool_id,
        username=username,
        password=password,
        email=email,
        name=name,
        github_login=github_login,
        avatar_url=avatar_url,
    )

    if not user_exists:
        # User was just created with `password`. Set it as permanent.
        _set_permanent_password(client, user_pool_id, username, password)
    else:
        # User already exists — reset password so we can authenticate
        password = _generate_random_password()
        _set_permanent_password(client, user_pool_id, username, password)

    # Authenticate to get tokens
    return _admin_initiate_auth(client, user_pool_id, client_id, username, password)


def _ensure_user_exists(
    client,
    user_pool_id: str,
    username: str,
    password: str,
    email: str,
    name: str,
    github_login: str,
    avatar_url: str,
) -> bool:
    """
    Create Cognito user if not exists. Returns True if user already existed.
    """
    try:
        client.admin_get_user(UserPoolId=user_pool_id, Username=username)
        logger.info("User %s already exists in Cognito", username)
        # Update attributes in case they changed
        _update_user_attributes(client, user_pool_id, username, email, name, github_login, avatar_url)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise

    # User doesn't exist — create
    logger.info("Creating Cognito user: %s", username)
    user_attributes = [
        {"Name": "name", "Value": name},
    ]
    # Only set email + email_verified together. Cognito rejects email_verified=true
    # without an email. GitHub users who keep their email private AND whose
    # OAuth App lacks user:email scope land here with email="" — we skip both
    # attributes and let the user fill them in later via the admin UI.
    if email:
        user_attributes.append({"Name": "email", "Value": email})
        user_attributes.append({"Name": "email_verified", "Value": "true"})
    if github_login:
        user_attributes.append({"Name": "custom:github_username", "Value": github_login})

    client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        TemporaryPassword=password,
        UserAttributes=user_attributes,
        MessageAction="SUPPRESS",  # Don't send welcome email
    )
    return False


def _update_user_attributes(
    client,
    user_pool_id: str,
    username: str,
    email: str,
    name: str,
    github_login: str,
    avatar_url: str,
) -> None:
    """Update mutable attributes for an existing user."""
    attributes = [
        {"Name": "name", "Value": name},
    ]
    if email:
        attributes.append({"Name": "email", "Value": email})
        attributes.append({"Name": "email_verified", "Value": "true"})
    if github_login:
        attributes.append({"Name": "custom:github_username", "Value": github_login})

    try:
        client.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=attributes,
        )
    except ClientError as e:
        # Non-fatal: log and continue
        logger.warning("Failed to update user attributes for %s: %s", username, e)


def _set_permanent_password(client, user_pool_id: str, username: str, password: str) -> None:
    """Set a permanent password for the user (removes FORCE_CHANGE_PASSWORD state)."""
    client.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def _admin_initiate_auth(
    client,
    user_pool_id: str,
    client_id: str,
    username: str,
    password: str,
) -> CognitoTokens:
    """Authenticate user and return Cognito tokens."""
    response = client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
        },
    )

    result = response.get("AuthenticationResult", {})
    if not result:
        challenge = response.get("ChallengeName", "UNKNOWN")
        raise ValueError(f"Unexpected auth challenge: {challenge}")

    return CognitoTokens(
        id_token=result["IdToken"],
        access_token=result["AccessToken"],
        refresh_token=result.get("RefreshToken", ""),
        expires_in=result.get("ExpiresIn", 3600),
    )
