"""Unit tests for github_app_service — resolve_installation_for_repo,
resolve_worker_installation_for_repo & verify_installation_ownership.

Issue #2086: GitHub accessibility helpers.
Issue #3529: Dual-resolver design — global App for ownership check,
             ops App for the worker's installation_id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.knowledge.github_app_service import (
    _get_ops_app_credentials,
    resolve_installation_for_repo,
    resolve_worker_installation_for_repo,
    verify_installation_ownership,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_global_creds():
    """Patch _get_global_app_credentials to return configured credentials."""
    with patch(
        "src.knowledge.github_app_service._get_global_app_credentials",
        return_value=(
            "3410773",
            "-----BEGIN RSA PRIVATE KEY-----\nfake-dev-key\n-----END RSA PRIVATE KEY-----",
        ),
    ) as mock:
        yield mock


@pytest.fixture
def mock_ops_creds():
    """Patch _get_ops_app_credentials to return ops App credentials."""
    coro = AsyncMock(
        return_value=(
            "3410864",
            "-----BEGIN RSA PRIVATE KEY-----\nfake-ops-key\n-----END RSA PRIVATE KEY-----",
        ),
    )
    with patch(
        "src.knowledge.github_app_service._get_ops_app_credentials",
        coro,
    ) as mock:
        yield mock


@pytest.fixture
def mock_jwt():
    """Patch _mint_app_jwt to return a predictable token."""
    with patch(
        "src.knowledge.github_app_service._mint_app_jwt",
        return_value="fake-jwt-token",
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# resolve_installation_for_repo tests (global/dev App — for ownership check)
# ---------------------------------------------------------------------------


class TestResolveInstallationForRepo:
    """Tests for resolve_installation_for_repo (global App)."""

    @pytest.mark.asyncio
    async def test_returns_installation_id_on_200(self, mock_global_creds, mock_jwt):
        """200 response → returns the installation_id from the response body."""
        mock_response = httpx.Response(
            200,
            json={"id": 124731131, "app_id": 3410773, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_installation_for_repo("aws-e", "adp", http_client=mock_client)

        assert result == 124731131
        mock_client.get.assert_called_once_with(
            "/repos/aws-e/adp/installation",
            headers={"Authorization": "Bearer fake-jwt-token"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, mock_global_creds, mock_jwt):
        """404 response (App not installed on repo) → returns None."""
        mock_response = httpx.Response(
            404,
            json={"message": "Not Found"},
            request=httpx.Request(
                "GET",
                "https://api.github.com/repos/acme/unknown-repo/installation",
            ),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_installation_for_repo("acme", "unknown-repo", http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_other_errors(self, mock_global_creds, mock_jwt):
        """Non-200/non-404 (e.g., 403) → raises HTTPStatusError."""
        mock_response = httpx.Response(
            403,
            json={"message": "Forbidden"},
            request=httpx.Request(
                "GET",
                "https://api.github.com/repos/acme/secret-repo/installation",
            ),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await resolve_installation_for_repo("acme", "secret-repo", http_client=mock_client)

    @pytest.mark.asyncio
    async def test_raises_when_no_credentials_available(self):
        """Missing global App credentials → raises ValueError."""
        with patch(
            "src.knowledge.github_app_service._get_global_app_credentials",
            return_value=("", ""),
        ):
            with pytest.raises(ValueError, match="No GitHub App credentials available"):
                await resolve_installation_for_repo("acme", "my-repo")

    @pytest.mark.asyncio
    async def test_uses_global_app_credentials_not_ops(self, mock_jwt):
        """resolve_installation_for_repo uses the GLOBAL App (for ownership check)."""
        mock_response = httpx.Response(
            200,
            json={"id": 124731131, "app_id": 3410773, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch(
            "src.knowledge.github_app_service._get_global_app_credentials",
            return_value=("3410773", "-----BEGIN RSA PRIVATE KEY-----\ndev-key\n-----END RSA PRIVATE KEY-----"),
        ) as global_mock:
            result = await resolve_installation_for_repo("aws-e", "adp", http_client=mock_client)

        # Global creds were used
        global_mock.assert_called_once()
        # JWT was minted with global/dev App ID
        mock_jwt.assert_called_once_with(
            "3410773",
            "-----BEGIN RSA PRIVATE KEY-----\ndev-key\n-----END RSA PRIVATE KEY-----",
        )
        assert result == 124731131


# ---------------------------------------------------------------------------
# resolve_worker_installation_for_repo tests (ops App — for the worker)
# ---------------------------------------------------------------------------


class TestResolveWorkerInstallationForRepo:
    """Tests for resolve_worker_installation_for_repo (ops App)."""

    @pytest.mark.asyncio
    async def test_returns_ops_installation_id_on_200(self, mock_ops_creds, mock_jwt):
        """200 response → returns the ops-App installation_id."""
        mock_response = httpx.Response(
            200,
            json={"id": 124731359, "app_id": 3410864, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_worker_installation_for_repo("aws-e", "adp", http_client=mock_client)

        assert result == 124731359
        mock_client.get.assert_called_once_with(
            "/repos/aws-e/adp/installation",
            headers={"Authorization": "Bearer fake-jwt-token"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, mock_ops_creds, mock_jwt):
        """Ops App not installed on repo (404) → returns None."""
        mock_response = httpx.Response(
            404,
            json={"message": "Not Found"},
            request=httpx.Request(
                "GET",
                "https://api.github.com/repos/acme/no-ops-app/installation",
            ),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_worker_installation_for_repo("acme", "no-ops-app", http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_ops_app_credentials(self, mock_jwt):
        """resolve_worker_installation_for_repo uses ops-App credentials (not global)."""
        mock_response = httpx.Response(
            200,
            json={"id": 124731359, "app_id": 3410864, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        ops_mock = AsyncMock(
            return_value=("3410864", "-----BEGIN RSA PRIVATE KEY-----\nops-key\n-----END RSA PRIVATE KEY-----"),
        )
        with patch(
            "src.knowledge.github_app_service._get_ops_app_credentials",
            ops_mock,
        ):
            result = await resolve_worker_installation_for_repo("aws-e", "adp", http_client=mock_client)

        ops_mock.assert_called_once()
        # JWT was minted with ops App ID
        mock_jwt.assert_called_once_with(
            "3410864",
            "-----BEGIN RSA PRIVATE KEY-----\nops-key\n-----END RSA PRIVATE KEY-----",
        )
        assert result == 124731359

    @pytest.mark.asyncio
    async def test_no_silent_fallback_to_global(self):
        """Issue #3529: NO fallback to global App — raises ValueError if ops creds unreadable."""
        with patch(
            "src.knowledge.github_app_service._get_ops_app_credentials",
            AsyncMock(side_effect=ValueError("AccessDeniedException: IAM policy")),
        ):
            with pytest.raises(ValueError, match="AccessDeniedException"):
                await resolve_worker_installation_for_repo("acme", "repo")

    @pytest.mark.asyncio
    async def test_two_apps_different_installation_ids(self, mock_jwt):
        """Issue #3529 integration: two Apps yield DIFFERENT installation_ids for the same org.

        Full contract test:
        - resolve_installation_for_repo (global App) returns dev-App id 124731131
          → used for ownership check against channel_tenant_map
        - resolve_worker_installation_for_repo (ops App) returns ops-App id 124731359
          → stored on the asset for the worker to mint with

        These MUST be different IDs. A single-App fixture cannot catch this bug.
        """
        # Dev-App response (for ownership check)
        dev_response = httpx.Response(
            200,
            json={"id": 124731131, "app_id": 3410773, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )
        # Ops-App response (for worker installation)
        ops_response = httpx.Response(
            200,
            json={"id": 124731359, "app_id": 3410864, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/aws-e/adp/installation"),
        )

        dev_client = AsyncMock(spec=httpx.AsyncClient)
        dev_client.get = AsyncMock(return_value=dev_response)

        ops_client = AsyncMock(spec=httpx.AsyncClient)
        ops_client.get = AsyncMock(return_value=ops_response)

        # Step 1: Resolve with global App (for ownership check)
        with patch(
            "src.knowledge.github_app_service._get_global_app_credentials",
            return_value=("3410773", "-----BEGIN RSA PRIVATE KEY-----\ndev\n-----END RSA PRIVATE KEY-----"),
        ):
            ownership_id = await resolve_installation_for_repo("aws-e", "adp", http_client=dev_client)

        # Step 2: Resolve with ops App (for worker)
        with patch(
            "src.knowledge.github_app_service._get_ops_app_credentials",
            AsyncMock(return_value=("3410864", "-----BEGIN RSA PRIVATE KEY-----\nops\n-----END RSA PRIVATE KEY-----")),
        ):
            worker_id = await resolve_worker_installation_for_repo("aws-e", "adp", http_client=ops_client)

        # The two IDs MUST be different — this is the exact production scenario
        assert ownership_id == 124731131  # dev-App installation (channel_tenant_map has this)
        assert worker_id == 124731359  # ops-App installation (worker mints with this)
        assert ownership_id != worker_id  # Explicit: the bug was storing ownership_id for the worker


# ---------------------------------------------------------------------------
# verify_installation_ownership tests
# ---------------------------------------------------------------------------


class TestVerifyInstallationOwnership:
    """Tests for verify_installation_ownership (delegate as of #4070 ·A0).

    Issue #4070: these tests used to drive an ``AsyncMock`` db and assert on the
    generated SQL, e.g. ``assert "metadata->>'installation_id'" in query_text``.
    That is the #4046 plumbing-assertion antipattern sub-EPIC #4068 exists to
    reject: it passes with the authorization check deleted, so it could never
    have caught the cross-tenant hole it appeared to cover. It also pinned the
    production query to Postgres-only ``->>``, which is what made this function
    untestable against the SQLite suite and forced the mocking in the first place.

    They now seed a real database and assert the ACCESS DECISION.
    """

    @staticmethod
    async def _seed(db, *, org_id, installation_id, github_org_id="1111", extra_orgs=()):
        from src.shared.models.organization import Organization
        from src.shared.models.vault import ChannelTenantMap

        db.add(Organization(id=org_id, name=org_id, github_org_id=github_org_id, github_installation_ids=[]))
        for other_id, other_gh in extra_orgs:
            db.add(Organization(id=other_id, name=other_id, github_org_id=other_gh, github_installation_ids=[]))
        db.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=github_org_id,
                installation_id=str(installation_id),
                org_id=org_id,
            )
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_returns_true_for_matching_tenant(self, db_session):
        """Installation belonging to the caller's tenant → True."""
        await self._seed(db_session, org_id="acme-corp", installation_id=98765)

        assert await verify_installation_ownership("acme-corp", 98765, db=db_session) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_different_tenant(self, db_session):
        """Installation belonging to ANOTHER tenant → False (cross-tenant guard)."""
        await self._seed(db_session, org_id="acme-corp", installation_id=98765, extra_orgs=[("evil-corp", "9999")])

        assert await verify_installation_ownership("evil-corp", 98765, db=db_session) is False

    @pytest.mark.asyncio
    async def test_returns_true_for_personal_install(self, db_session):
        """Personal installs resolve too — they are recorded in channel_tenant_map
        only, never in organizations.github_installation_ids."""
        await self._seed(
            db_session,
            org_id="personal-user-tenant",
            installation_id=55555,
            github_org_id="personal:4242:user-1",
        )

        assert await verify_installation_ownership("personal-user-tenant", 55555, db=db_session) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_installation(self, db_session):
        """An installation no tenant claims is denied, not granted by default."""
        await self._seed(db_session, org_id="acme-corp", installation_id=98765)

        assert await verify_installation_ownership("acme-corp", 12345, db=db_session) is False

    @pytest.mark.asyncio
    async def test_installation_id_type_does_not_change_the_decision(self, db_session):
        """The int/str boundary must not be load-bearing for authorization.

        The old test asserted the parameter was cast to ``str`` because the
        Postgres ``->>`` comparison is textual. That is an implementation detail;
        what matters is that the same installation resolves the same way however
        the id is spelled.
        """
        await self._seed(db_session, org_id="acme-corp", installation_id=98765)

        assert await verify_installation_ownership("acme-corp", 98765, db=db_session) is True
        assert await verify_installation_ownership("acme-corp", int("98765"), db=db_session) is True


# ---------------------------------------------------------------------------
# _get_ops_app_credentials tests (Issue #3529)
# ---------------------------------------------------------------------------


class TestGetOpsAppCredentials:
    """Tests for _get_ops_app_credentials — reading ops App secrets from SM."""

    @pytest.mark.asyncio
    async def test_reads_ops_secrets_from_secrets_manager(self):
        """Reads app_id and private_key from the configured secret names."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "3410864"},
            {"SecretString": "-----BEGIN RSA PRIVATE KEY-----\nops-key\n-----END RSA PRIVATE KEY-----"},
        ]

        app_id, key = await _get_ops_app_credentials(sm_client=mock_sm)

        assert app_id == "3410864"
        assert "ops-key" in key
        # Verify it read both secrets
        assert mock_sm.get_secret_value.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_access_denied(self):
        """AccessDeniedException → ValueError mentioning IAM pattern."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetSecretValue",
        )

        with pytest.raises(ValueError, match="IAM policy"):
            await _get_ops_app_credentials(sm_client=mock_sm)

    @pytest.mark.asyncio
    async def test_raises_on_not_found(self):
        """ResourceNotFoundException → ValueError."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetSecretValue",
        )

        with pytest.raises(ValueError, match="Failed to read ops App credentials"):
            await _get_ops_app_credentials(sm_client=mock_sm)

    @pytest.mark.asyncio
    async def test_uses_env_var_overrides(self):
        """Respects OPS_GITHUB_APP_ID_SECRET / OPS_GITHUB_APP_KEY_SECRET env vars."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "9999"},
            {"SecretString": "custom-key-pem"},
        ]

        with patch.dict(
            "os.environ",
            {
                "OPS_GITHUB_APP_ID_SECRET": "custom/secret/id",
                "OPS_GITHUB_APP_KEY_SECRET": "custom/secret/key",
            },
        ):
            app_id, key = await _get_ops_app_credentials(sm_client=mock_sm)

        assert app_id == "9999"
        # Verify the custom secret names were used
        calls = mock_sm.get_secret_value.call_args_list
        assert calls[0][1]["SecretId"] == "custom/secret/id"
        assert calls[1][1]["SecretId"] == "custom/secret/key"
