"""Cognito service — admin user management via AdminCreateUser API.

This module handles invite-only user creation (no self-signup).
Cognito is configured with allow_admin_create_user_only = true.
"""

import asyncio
import logging

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.config import settings

logger = logging.getLogger(__name__)


def _get_cognito_client():
    """Create a Cognito Identity Provider client."""
    return boto3.client("cognito-idp", region_name=settings.aws_region)


async def admin_create_user(email: str, org_id: str, role: str) -> str:
    """Invite a user via Cognito AdminCreateUser API.

    Sets custom attributes (org_id, role) and sends invite email with
    temporary password.

    Args:
        email: User email address
        org_id: Organization UUID string
        role: RBAC role (developer, workspace-admin, org-admin)

    Returns:
        Cognito user sub (unique ID)

    Raises:
        HTTPException on failure
    """
    if not settings.cognito_user_pool_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cognito is not configured",
        )

    client = _get_cognito_client()
    try:
        response = await asyncio.to_thread(
            client.admin_create_user,
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": org_id},
                {"Name": "custom:role", "Value": role},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )

        # Extract the sub attribute from the response
        cognito_sub = ""
        for attr in response["User"]["Attributes"]:
            if attr["Name"] == "sub":
                cognito_sub = attr["Value"]
                break

        logger.info(
            "Cognito user invited: sub=%s email=%s role=%s org_id=%s",
            cognito_sub,
            email,
            role,
            org_id,
        )
        return cognito_sub

    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error("Cognito AdminCreateUser failed: %s — %s", error_code, error_msg)

        if error_code == "UsernameExistsException":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists in Cognito",
            ) from exc
        if error_code == "InvalidParameterException":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid parameter: {error_msg}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cognito error: {error_code}",
        ) from exc


async def admin_disable_user(email: str) -> None:
    """Disable a user in Cognito.

    Args:
        email: User email (used as username)

    Raises:
        HTTPException on failure
    """
    if not settings.cognito_user_pool_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cognito is not configured",
        )

    client = _get_cognito_client()
    try:
        await asyncio.to_thread(
            client.admin_disable_user,
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
        )
        logger.info("Cognito user disabled: email=%s", email)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error("Cognito AdminDisableUser failed: %s — %s", error_code, error_msg)
        if error_code == "UserNotFoundException":
            logger.warning("Cognito user not found for disable: %s (continuing)", email)
            return
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cognito error: {error_code}",
        ) from exc


async def admin_update_user_role(email: str, role: str) -> None:
    """Update a user's role custom attribute in Cognito.

    Args:
        email: User email (used as username)
        role: New RBAC role

    Raises:
        HTTPException on failure
    """
    if not settings.cognito_user_pool_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cognito is not configured",
        )

    client = _get_cognito_client()
    try:
        await asyncio.to_thread(
            client.admin_update_user_attributes,
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "custom:role", "Value": role},
            ],
        )
        logger.info("Cognito user role updated: email=%s role=%s", email, role)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error(
            "Cognito AdminUpdateUserAttributes failed: %s — %s", error_code, error_msg
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cognito error: {error_code}",
        ) from exc
