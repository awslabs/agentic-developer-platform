"""DDB identity-index write-through for identity operations.

Issue #387: Wraps the existing IdentityIndexClient with audit logging.
Issue #401: Extended with channel_user write-through for user identities.
Issue #537: Sequential dual-write to old table + new user-identity-index table,
            gated by USER_IDENTITY_INDEX_V2_WRITE feature flag.

Called post-commit — failures are logged but don't roll back Postgres.
"""

import logging
import os

from src.admin.identity_index import IdentityIndexClient

from .user_identity_index import UserIdentityIndexClient

logger = logging.getLogger(__name__)

# identity_type value for user-level channel identity rows.
# Provider-in-key convention matches Phase A.1's `github_installation_id`
# (declared in src/admin/identity_index.py::IdentityType). The webhook
# Lambda's identity_resolver reads rows keyed as `github_user` — writer and
# reader must agree.
GITHUB_USER_TYPE = "github_user"


def _v2_write_enabled() -> bool:
    """Check if dual-write to user-identity-index is enabled."""
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


class IdentityIndexWriter:
    """Write-through to DDB identity-index with audit logging."""

    def __init__(
        self,
        client: IdentityIndexClient | None = None,
        user_identity_client: UserIdentityIndexClient | None = None,
    ):
        self._client = client or IdentityIndexClient()
        self._user_identity_client = user_identity_client or UserIdentityIndexClient()

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
    # channel_user write-through (Issue #401, extended by #537)
    # ------------------------------------------------------------------

    async def put_user_identity(
        self,
        provider_user_id: str,
        user_id: str,
        org_id: str,
        provider: str = "github",
        provider_username: str | None = None,
        member_org_ids: list[str] | None = None,
    ) -> bool:
        """Write a channel_user entry to DDB for a single identity.

        Sequential dual-write (Issue #537):
          1. Write to OLD table (identity_type=github_user) — backward compat.
             Failure of this write is propagated to the caller.
          2. If OLD write succeeded AND USER_IDENTITY_INDEX_V2_WRITE=true,
             write to NEW table (PK=provider, SK=provider_user_id).
             Failure of the NEW write is logged but NOT propagated.

        Issue #3134: Optional member_org_ids param writes the list of org_ids
        where the user has TenantMembership. Used by the webhook Lambda for
        cross-tenant trigger policy enforcement.

        Issue #3134 fix: When member_org_ids is NOT provided, uses UpdateItem
        (SET semantics) to avoid wiping a previously-written member_org_ids attr.
        When member_org_ids IS provided, uses PutItem (full overwrite) to set
        the complete state including memberships.

        Returns True if the OLD-table write succeeded, False if exhausted retries.
        """
        logger.info(
            "identity-index put channel_user: provider=%s provider_user_id=%s user_id=%s org_id=%s",
            provider,
            provider_user_id,
            user_id,
            org_id,
        )

        if member_org_ids is not None:
            # Full PutItem — caller owns member_org_ids and wants to set it explicitly
            extra_attrs: dict[str, str | None] = {
                "user_id": user_id,
                "provider_username": provider_username,
            }
            old_success = await self._client.put_identity(
                identity_type=GITHUB_USER_TYPE,
                identity_value=provider_user_id,
                org_id=org_id,
                extra_attrs=extra_attrs,
                member_org_ids=member_org_ids,
            )
        else:
            # UpdateItem — preserve existing member_org_ids
            old_success = await self._client.update_user_identity_core(
                identity_value=provider_user_id,
                user_id=user_id,
                org_id=org_id,
                provider_username=provider_username,
            )

        if not old_success:
            return False

        # Step 2: Write to NEW table (feature-flag gated)
        if _v2_write_enabled():
            try:
                if member_org_ids is not None:
                    new_success = await self._user_identity_client.put_user_identity(
                        provider=provider,
                        provider_user_id=provider_user_id,
                        user_id=user_id,
                        org_id=org_id,
                        provider_username=provider_username,
                        member_org_ids=member_org_ids,
                    )
                else:
                    new_success = await self._user_identity_client.update_user_core_attrs(
                        provider=provider,
                        provider_user_id=provider_user_id,
                        user_id=user_id,
                        org_id=org_id,
                        provider_username=provider_username,
                    )
                if not new_success:
                    logger.warning(
                        "user-identity-index v2 write failed (non-fatal): provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
            except Exception:
                logger.exception(
                    "user-identity-index v2 write exception (non-fatal): provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )

        return True

    async def update_user_membership_orgs(
        self,
        provider_user_id: str,
        member_org_ids: list[str],
        provider: str = "github",
    ) -> bool:
        """Update only the member_org_ids attribute on a user's DDB rows.

        Issue #3134: Targeted update for membership-change events — avoids
        needing the full identity context (user_id, org_id, etc.) just to
        update membership. Dual-write to both old + new tables.

        Returns True if the OLD-table update succeeded, False otherwise.
        """
        logger.info(
            "identity-index update_user_membership_orgs: provider=%s provider_user_id=%s member_org_ids=%s",
            provider,
            provider_user_id,
            member_org_ids,
        )

        # Update OLD table
        old_success = await self._client.update_membership_orgs(
            identity_type=GITHUB_USER_TYPE,
            identity_value=provider_user_id,
            member_org_ids=member_org_ids,
        )

        if not old_success:
            return False

        # Update NEW table (feature-flag gated)
        if _v2_write_enabled():
            try:
                new_success = await self._user_identity_client.update_membership_orgs(
                    provider=provider,
                    provider_user_id=provider_user_id,
                    member_org_ids=member_org_ids,
                )
                if not new_success:
                    logger.warning(
                        "user-identity-index v2 update_membership_orgs failed (non-fatal): provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
            except Exception:
                logger.exception(
                    "user-identity-index v2 update_membership_orgs exception (non-fatal): provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )

        return True

    async def delete_user_identity(self, provider_user_id: str, provider: str = "github") -> bool:
        """Delete a single channel_user entry from DDB.

        Sequential dual-delete: OLD table first, then NEW table (flag-gated).
        Returns True if OLD-table delete succeeded, False if all retries exhausted.
        """
        logger.info(
            "identity-index delete channel_user: provider=%s provider_user_id=%s",
            provider,
            provider_user_id,
        )

        # Step 1: Delete from OLD table
        old_success = await self._client.delete_identity(
            identity_type=GITHUB_USER_TYPE,
            identity_value=provider_user_id,
        )

        if not old_success:
            return False

        # Step 2: Delete from NEW table (feature-flag gated)
        if _v2_write_enabled():
            try:
                new_success = await self._user_identity_client.delete_user_identity(
                    provider=provider,
                    provider_user_id=provider_user_id,
                )
                if not new_success:
                    logger.warning(
                        "user-identity-index v2 delete failed (non-fatal): provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
            except Exception:
                logger.exception(
                    "user-identity-index v2 delete exception (non-fatal): provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )

        return True

    async def sync_user_identities(
        self,
        user_id: str,
        org_id: str,
        identities: list[dict],
    ) -> None:
        """Write channel_user entries for all identities of a user.

        Each identity dict must have: provider_user_id, and optionally provider_username, provider.
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
                provider=ident.get("provider", "github"),
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
