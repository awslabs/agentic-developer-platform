"""DDB identity-index write-through for identity operations.

Issue #387: Wraps the existing IdentityIndexClient with audit logging.
Issue #401: Extended with channel_user write-through for user identities.

Called post-commit — failures are logged but don't roll back Postgres.
"""

import logging

from src.admin.identity_index import IdentityIndexClient

logger = logging.getLogger(__name__)

# identity_type value for user-level channel identity rows
CHANNEL_USER_TYPE = "channel_user"


class IdentityIndexWriter:
    """Write-through to DDB identity-index with audit logging."""

    def __init__(self, client: IdentityIndexClient | None = None):
        self._client = client or IdentityIndexClient()

    async def sync_org_channels(
        self,
        org_id: str,
        github_installation_ids: list[str],
        cognito_client_ids: list[str],
        old_github_installation_ids: list[str] | None = None,
        old_cognito_client_ids: list[str] | None = None,
    ) -> None:
        """Write-through channel identities to DDB after Postgres commit.

        Best-effort with retry (handled by underlying client).
        """
        logger.info(
            "identity-index sync: org=%s github_ids=%d cognito_ids=%d",
            org_id,
            len(github_installation_ids),
            len(cognito_client_ids),
        )
        await self._client.sync_identities_for_org(
            org_id=org_id,
            github_installation_ids=github_installation_ids,
            cognito_client_ids=cognito_client_ids,
            old_github_installation_ids=old_github_installation_ids,
            old_cognito_client_ids=old_cognito_client_ids,
        )

    async def delete_org_identities(
        self,
        github_installation_ids: list[str],
        cognito_client_ids: list[str],
    ) -> None:
        """Remove all identity-index entries for an org (on soft-delete/archive)."""
        await self._client.delete_all_for_org(
            github_installation_ids=github_installation_ids,
            cognito_client_ids=cognito_client_ids,
        )

    # ------------------------------------------------------------------
    # channel_user write-through (Issue #401)
    # ------------------------------------------------------------------

    async def put_user_identity(
        self,
        provider_user_id: str,
        user_id: str,
        org_id: str,
        provider_username: str | None = None,
    ) -> bool:
        """Write a channel_user entry to DDB for a single identity.

        Uses upsert semantics (PutItem overwrites) so re-creating the same
        user does not cause DDB write errors.

        Returns True if write succeeded, False if all retries exhausted.
        """
        logger.info(
            "identity-index put channel_user: provider_user_id=%s user_id=%s org_id=%s",
            provider_user_id,
            user_id,
            org_id,
        )
        return await self._client.put_identity(
            identity_type=CHANNEL_USER_TYPE,
            identity_value=provider_user_id,
            org_id=org_id,
            extra_attrs={
                "user_id": user_id,
                "provider_username": provider_username,
            },
        )

    async def delete_user_identity(self, provider_user_id: str) -> bool:
        """Delete a single channel_user entry from DDB.

        Returns True if delete succeeded, False if all retries exhausted.
        """
        logger.info(
            "identity-index delete channel_user: provider_user_id=%s",
            provider_user_id,
        )
        return await self._client.delete_identity(
            identity_type=CHANNEL_USER_TYPE,
            identity_value=provider_user_id,
        )

    async def sync_user_identities(
        self,
        user_id: str,
        org_id: str,
        identities: list[dict],
    ) -> None:
        """Write channel_user entries for all identities of a user.

        Each identity dict must have: provider_user_id, and optionally provider_username.
        Best-effort — failures are logged but don't propagate.
        """
        import asyncio

        if not identities:
            return

        tasks = [
            self.put_user_identity(
                provider_user_id=ident["provider_user_id"],
                user_id=user_id,
                org_id=org_id,
                provider_username=ident.get("provider_username"),
            )
            for ident in identities
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = sum(1 for r in results if r is False or isinstance(r, Exception))
        if failures:
            logger.warning(
                "identity-index sync_user_identities: user_id=%s %d/%d writes failed",
                user_id,
                failures,
                len(tasks),
            )

    async def delete_all_user_identities(self, provider_user_ids: list[str]) -> None:
        """Delete all channel_user entries for a user (on user deletion).

        Best-effort — failures are logged but don't propagate.
        """
        import asyncio

        if not provider_user_ids:
            return

        tasks = [self.delete_user_identity(pid) for pid in provider_user_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = sum(1 for r in results if r is False or isinstance(r, Exception))
        if failures:
            logger.warning(
                "identity-index delete_all_user_identities: %d/%d deletes failed",
                failures,
                len(tasks),
            )
