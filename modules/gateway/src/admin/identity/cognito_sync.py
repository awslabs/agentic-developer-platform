"""Cognito side-effects for identity operations.

Issue #387: Idempotent Cognito group creation + user invitation.
Called post-commit — failures are logged and retried but don't roll back Postgres.
"""

import asyncio
import logging

from src.admin.cognito_service import CognitoService, CognitoServiceError

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
    ) -> dict | None:
        """Create Cognito user and optionally send invite.

        Returns Cognito user dict on success, None on failure.
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
                # Add user to org group
                try:
                    await asyncio.to_thread(
                        self._cognito.add_user_to_group,
                        username=email,
                        group_name=f"org-{org_id}",
                    )
                except Exception as e:
                    logger.warning("Failed to add user %s to org group: %s", email, e)

                return result
            except CognitoServiceError as e:
                # UserAlreadyExists is not retryable — it's a success case
                if "already exists" in str(e).lower():
                    logger.info("User %s already exists in Cognito, treating as success", email)
                    return {}
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
        return None

    async def delete_user(self, email: str) -> bool:
        """Delete a user from Cognito. Best-effort."""
        try:
            await asyncio.to_thread(self._cognito.delete_user, username=email)
            return True
        except Exception as e:
            logger.warning("Failed to delete Cognito user %s: %s", email, e)
            return False
