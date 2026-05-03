"""DDB identity-index write-through for identity operations.

Issue #387: Wraps the existing IdentityIndexClient with audit logging.
Called post-commit — failures are logged but don't roll back Postgres.
"""

import logging

from src.admin.identity_index import IdentityIndexClient

logger = logging.getLogger(__name__)


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
