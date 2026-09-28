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
from src.shared.identity.providers import SUPPORTED_PROVIDERS

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
        user_kind: str | None = None,
        bot_kind: str | None = None,
        verification_method: str | None = None,
    ) -> bool:
        """Write a channel_user entry to DDB for a single identity.

        GitHub sequential dual-write (Issue #537):
          1. Write to OLD table (identity_type=github_user) — backward compat.
             Failure of this write is propagated to the caller.
          2. If OLD write succeeded AND USER_IDENTITY_INDEX_V2_WRITE=true,
             write to NEW table (PK=provider, SK=provider_user_id).
             Failure of the NEW write is logged but NOT propagated.

        Other providers have no legacy key. They write only their provider-keyed
        NEW row when the flag is enabled, returning that write's result. With the
        flag disabled they are a successful no-op; they must never occupy a
        legacy github_user key, even when their external ID matches a GitHub ID.

        Issue #3134: Optional member_org_ids param writes the list of org_ids
        where the user has TenantMembership. Used by the webhook Lambda for
        cross-tenant trigger policy enforcement.

        Issue #3134 fix: When member_org_ids is NOT provided, uses UpdateItem
        (SET semantics) to avoid wiping a previously-written member_org_ids attr.
        When member_org_ids IS provided, uses PutItem (full overwrite) to set
        the complete state including memberships.

        Issue #780: Optional user_kind/bot_kind mark this identity as a known
        bot (e.g. the platform GitHub App's own bot user) rather than a human.
        Read by the webhook Lambda's identity_resolver to route bot senders
        through the loop guards instead of the default human path.

        Issue #5664 (A10): Optional verification_method projects the provenance of
        the Postgres `user_identities` row this entry mirrors. The webhook resolver
        reads it to decide whether resolving a sender also entitles a caller to act
        as them; without it every resolution looked equally trustworthy and the
        authority gate could only be advisory. Callers that hold the ORM row should
        always pass it — omitting it leaves the row's provenance unknown, which is
        treated as NOT proof downstream.

        GitHub returns the OLD-table result; other providers return the v2 result
        when enabled. A failed projection never rolls back the SQL commit.
        """
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider!r}")
        logger.info(
            "identity-index put channel_user: provider=%s provider_user_id=%s user_id=%s org_id=%s",
            provider,
            provider_user_id,
            user_id,
            org_id,
        )

        if provider != "github":
            # Only GitHub identities can be represented by the legacy schema.
            old_success = True
        elif member_org_ids is not None:
            # Full PutItem — caller owns member_org_ids and wants to set it explicitly
            extra_attrs: dict[str, str | None] = {
                "user_id": user_id,
                "provider_username": provider_username,
                "user_kind": user_kind,
                "bot_kind": bot_kind,
                "verification_method": verification_method,
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
                user_kind=user_kind,
                bot_kind=bot_kind,
                verification_method=verification_method,
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
                        user_kind=user_kind,
                        bot_kind=bot_kind,
                        verification_method=verification_method,
                    )
                else:
                    new_success = await self._user_identity_client.update_user_core_attrs(
                        provider=provider,
                        provider_user_id=provider_user_id,
                        user_id=user_id,
                        org_id=org_id,
                        provider_username=provider_username,
                        user_kind=user_kind,
                        bot_kind=bot_kind,
                        verification_method=verification_method,
                    )
                if not new_success:
                    logger.warning(
                        "user-identity-index v2 write failed: provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
                if provider != "github":
                    return new_success
            except Exception:
                logger.exception(
                    "user-identity-index v2 write exception: provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )
                if provider != "github":
                    return False

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
        update membership. Only GitHub updates the old table; all providers
        update their own new-table key when v2 writes are enabled.

        Uses the same result and feature-flag semantics as put_user_identity.
        """
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider!r}")
        logger.info(
            "identity-index update_user_membership_orgs: provider=%s provider_user_id=%s member_org_ids=%s",
            provider,
            provider_user_id,
            member_org_ids,
        )

        if provider == "github":
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
                        "user-identity-index v2 update_membership_orgs failed: provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
                if provider != "github":
                    return new_success
            except Exception:
                logger.exception(
                    "user-identity-index v2 update_membership_orgs exception: provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )
                if provider != "github":
                    return False

        return True

    async def delete_user_identity(self, provider_user_id: str, provider: str = "github") -> bool:
        """Delete a single channel_user entry from DDB.

        GitHub deletes OLD first, then NEW (flag-gated); other providers delete
        only their own NEW key. Uses the result semantics of put_user_identity.
        """
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider!r}")
        logger.info(
            "identity-index delete channel_user: provider=%s provider_user_id=%s",
            provider,
            provider_user_id,
        )

        if provider == "github":
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
                        "user-identity-index v2 delete failed: provider=%s provider_user_id=%s",
                        provider,
                        provider_user_id,
                    )
                if provider != "github":
                    return new_success
            except Exception:
                logger.exception(
                    "user-identity-index v2 delete exception: provider=%s provider_user_id=%s",
                    provider,
                    provider_user_id,
                )
                if provider != "github":
                    return False

        return True

    async def sync_user_identities(
        self,
        user_id: str,
        org_id: str,
        identities: list[dict],
    ) -> None:
        """Write channel_user entries for all identities of a user.

        Each identity dict must have: provider_user_id, and optionally
        provider_username, provider, verification_method.
        Best-effort — failures are logged but don't propagate.

        Issue #5664 (A10): verification_method is forwarded when the caller supplies
        it. This helper previously kept only provider_user_id and provider_username,
        so identities created through it reached DDB with no provenance and could
        not authorize anything once the authority gate started requiring proof.
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
                verification_method=ident.get("verification_method"),
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

    async def delete_all_user_identities(self, provider_user_ids: list[str], provider: str = "github") -> None:
        """Delete a user's channel identities for one provider (on user deletion).

        Defaults to GitHub for legacy callers. Call separately for each provider
        so matching external IDs never delete another provider's identity.
        Best-effort — failures are logged but don't propagate.
        """
        import asyncio

        if not provider_user_ids:
            return

        tasks = [self.delete_user_identity(pid, provider=provider) for pid in provider_user_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = sum(1 for r in results if r is False or isinstance(r, Exception))
        if failures:
            logger.warning(
                "identity-index delete_all_user_identities: %d/%d deletes failed",
                failures,
                len(tasks),
            )
