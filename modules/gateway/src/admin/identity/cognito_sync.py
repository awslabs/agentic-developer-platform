"""Cognito side-effects for identity operations.

Issue #387: Idempotent Cognito group creation + user invitation.
User creation failures are explicit; group creation remains idempotent. The
calling lifecycle service owns database commits and recovery.
"""

import asyncio
import logging

from src.admin.cognito_service import CognitoService, CognitoServiceError
from src.shared.exceptions import ConflictError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


class CognitoSyncService:
    """Handles Cognito side-effects with retry and idempotency."""

    def __init__(self, cognito_service: CognitoService | None = None):
        self._cognito = cognito_service or CognitoService()

    async def ensure_org_group(self, org_id: str) -> bool:
        """Create Cognito group org-<tenant_id> idempotently.

        Returns True if group exists (created or already existed), False on failure.
        """
        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(self._cognito.create_org_group, org_id)
                return True
            except CognitoServiceError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "Cognito group creation failed (attempt %d/%d) for org %s: %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    org_id,
                    str(e),
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error("Cognito group creation exhausted retries for org %s", org_id)
        return False

    async def create_user_and_invite(
        self,
        email: str,
        org_id: str,
        dept_id: str,
        team_id: str,
        name: str | None = None,
        role: str = "member",
        send_invite: bool = True,
        github_username: str | None = None,
    ) -> dict:
        """Create Cognito user and optionally send invite.

        Return the actual Cognito identity or raise. Existing usernames are a
        conflict, never success: reconciliation requires an expected subject.
        """
        for attempt in range(MAX_RETRIES):
            try:
                result = await asyncio.to_thread(
                    self._cognito.create_user,
                    email=email,
                    org_id=org_id,
                    dept_id=dept_id,
                    team_id=team_id,
                    name=name,
                    role=role,
                    github_username=github_username,
                    suppress_invitation=not send_invite,
                )
                return result
            except CognitoServiceError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "Cognito user creation failed (attempt %d/%d) for %s: %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    email,
                    str(e),
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error("Cognito user creation exhausted retries for %s", email)
        raise CognitoServiceError("Cognito user creation failed; retry provisioning the existing ADP user")

    async def verified_user(self, username: str, expected_sub: str) -> dict:
        """Read the configured pool and compare immutable subjects, not emails."""
        result = await asyncio.to_thread(self._cognito.get_user, username)
        if not result:
            raise ConflictError("Cognito user does not exist in the configured pool")
        subject, _ = cognito_identity(result)
        if subject != expected_sub:
            raise ConflictError("Cognito subject does not match expected_sub")
        if result.get("Enabled") is False:
            raise ConflictError("Cannot link a disabled Cognito user")
        return result

    async def ensure_user_group(self, username: str, org_id: str) -> None:
        if not await self.ensure_org_group(org_id):
            raise CognitoServiceError("Cognito organization group could not be provisioned")
        await asyncio.to_thread(self._cognito.add_user_to_group, username=username, group_name=f"org-{org_id}")

    async def delete_user(self, email: str) -> bool:
        """Delete a user from Cognito. Best-effort."""
        try:
            await asyncio.to_thread(self._cognito.delete_user, username=email)
            return True
        except Exception as e:
            logger.warning("Failed to delete Cognito user %s: %s", email, e)
            return False


def cognito_identity(result: dict) -> tuple[str, str]:
    """AdminCreateUser and AdminGetUser name their attribute lists differently."""
    attributes = {item["Name"]: item["Value"] for item in result.get("Attributes", result.get("UserAttributes", []))}
    subject, username = attributes.get("sub"), result.get("Username")
    if not subject or not username:
        raise CognitoServiceError("Cognito response did not include an immutable subject and username")
    return subject, username
