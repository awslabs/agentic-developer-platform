"""Unit tests for register-time accessibility validation in POST /assets + bulk/commit.

Issue #2087 (#2082 Phase-1 story 4): Tests the accessibility validation flow:
  - Public repo → ACCEPT shared, NO installation call
  - Private repo the tenant's App is installed on → ACCEPT tenant-scoped + installation_id
  - Private repo whose installation belongs to ANOTHER tenant → REJECT (cross-tenant)
  - Typo / nonexistent repo → REJECT with actionable message
  - Bulk: one bad item → whole batch rejected (validate-all-before-insert)
  - Bulk: public checks use cache (one fetch for same owner/repo)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.knowledge.conftest import (
    FakeAsyncSession,
    FakeResult,
    FakeRow,
)

# ---------------------------------------------------------------------------
# POST /api/agent-context/assets — accessibility validation
# ---------------------------------------------------------------------------


class TestRegisterAssetAccessibility:
    """Tests for register-time accessibility validation in POST /assets."""

    @pytest.mark.anyio
    async def test_public_repo_accepted_as_shared(self, make_client, fake_user):
        """Public repo → ACCEPT shared scope, tenant_id=NULL, no installation call."""
        asset_row = FakeRow(tenant_id=None, owner_sub=None)
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.return_value = AccessibilityResult(allowed=True, shared=True)

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/torvalds/linux",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 201
        # Verify validate was called
        mock_validate.assert_called_once()
        # The source_ref and tenant from the call
        call_args = mock_validate.call_args
        assert call_args[0][0] == "https://github.com/torvalds/linux"

    @pytest.mark.anyio
    async def test_private_repo_with_owned_installation_accepted(self, make_client, fake_user):
        """Private repo with tenant's installation → ACCEPT tenant-scoped + installation_id stored."""
        asset_row = FakeRow()
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.return_value = AccessibilityResult(allowed=True, shared=False, installation_id=98765)

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/private-repo",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 201
        # Verify the insert was called with installation_id
        # The 3rd execute call is the INSERT
        insert_stmt = db.executed_statements[2]
        insert_params = insert_stmt[1]
        assert insert_params["installation_id"] == 98765

    @pytest.mark.anyio
    async def test_private_repo_cross_tenant_rejected(self, make_client, fake_user):
        """Private repo whose installation belongs to ANOTHER tenant → REJECT 403."""
        db = FakeAsyncSession()

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.return_value = AccessibilityResult(
                allowed=False,
                error_message="The GitHub App installation for evil-org/secret-repo does not belong to your tenant.",
                error_code=403,
            )

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/evil-org/secret-repo",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 403
        assert "does not belong to your tenant" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_nonexistent_repo_rejected_with_actionable_message(self, make_client, fake_user):
        """Typo / nonexistent repo → REJECT 422 with actionable message."""
        db = FakeAsyncSession()

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.return_value = AccessibilityResult(
                allowed=False,
                error_message="We don't have access to acme/nonexistent-typo. Install the ADP App on it via Settings → Connections.",
                error_code=422,
            )

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/nonexistent-typo",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 422
        assert "Install the ADP App" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_non_repo_asset_skips_accessibility_validation(self, make_client, fake_user):
        """Non-repo asset types (url, doc) bypass accessibility validation entirely."""
        asset_row = FakeRow(asset_type="url", source_ref="https://example.com/docs")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "url",
                        "source_ref": "https://example.com/docs",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 201
        # validate_repo_accessibility should NOT have been called for url type
        mock_validate.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/agent-context/assets/bulk/commit — accessibility validation
# ---------------------------------------------------------------------------


class TestBulkCommitAccessibility:
    """Tests for register-time accessibility validation in bulk/commit."""

    @pytest.mark.anyio
    async def test_bulk_one_bad_item_rejects_entire_batch(self, make_client, admin_user):
        """One inaccessible repo in a batch → whole batch rejected, zero rows inserted."""
        db = FakeAsyncSession()

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            # First item passes, second fails
            mock_validate.side_effect = [
                AccessibilityResult(allowed=True, shared=True),
                AccessibilityResult(
                    allowed=False,
                    error_message="We don't have access to evil-org/secret. Install the ADP App on it via Settings → Connections.",
                    error_code=422,
                ),
            ]

            async with make_client(db, admin_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets/bulk/commit",
                    json={
                        "items": [
                            {
                                "asset_type": "repo",
                                "source_ref": "https://github.com/torvalds/linux",
                            },
                            {
                                "asset_type": "repo",
                                "source_ref": "https://github.com/evil-org/secret",
                            },
                        ],
                        "scope": "tenant",
                    },
                )

        assert resp.status_code == 422
        data = resp.json()
        assert "evil-org/secret" in data["detail"]["source_ref"]
        # Verify no DB inserts happened (no commit)
        assert db.committed is False

    @pytest.mark.anyio
    async def test_bulk_all_valid_items_accepted(self, make_client, admin_user):
        """All items pass accessibility → batch proceeds to insert."""
        asset_row1 = FakeRow(tenant_id=None, owner_sub=None)
        asset_row2 = FakeRow(tenant_id="acme-corp", owner_sub=None)
        db = FakeAsyncSession()
        db.execute_results = [
            # Quota check
            FakeResult(rows=[]),
            # Dedup check for item 1
            FakeResult(rows=[]),
            # Insert 1
            FakeResult(),
            # Fetch 1
            FakeResult(rows=[asset_row1]),
            # Dedup check for item 2
            FakeResult(rows=[]),
            # Insert 2
            FakeResult(),
            # Fetch 2
            FakeResult(rows=[asset_row2]),
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.side_effect = [
                AccessibilityResult(allowed=True, shared=True),
                AccessibilityResult(allowed=True, shared=False, installation_id=12345),
            ]

            # Patch dispatch to avoid SQS dependency
            with patch("src.knowledge.routes.dispatch_ingestion", new_callable=AsyncMock):
                async with make_client(db, admin_user) as client:
                    resp = await client.post(
                        "/api/agent-context/assets/bulk/commit",
                        json={
                            "items": [
                                {
                                    "asset_type": "repo",
                                    "source_ref": "https://github.com/torvalds/linux",
                                },
                                {
                                    "asset_type": "repo",
                                    "source_ref": "https://github.com/acme/private-svc",
                                },
                            ],
                            "scope": "tenant",
                        },
                    )

        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] == 2

    @pytest.mark.anyio
    async def test_bulk_non_repo_items_skip_validation(self, make_client, admin_user):
        """Non-repo items in bulk commit bypass accessibility validation."""
        asset_row = FakeRow(asset_type="url", source_ref="https://docs.example.com")
        db = FakeAsyncSession()
        db.execute_results = [
            # Quota check
            FakeResult(rows=[]),
            # Dedup check
            FakeResult(rows=[]),
            # Insert
            FakeResult(),
            # Fetch
            FakeResult(rows=[asset_row]),
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            with patch("src.knowledge.routes.dispatch_ingestion", new_callable=AsyncMock):
                async with make_client(db, admin_user) as client:
                    resp = await client.post(
                        "/api/agent-context/assets/bulk/commit",
                        json={
                            "items": [
                                {
                                    "asset_type": "url",
                                    "source_ref": "https://docs.example.com",
                                },
                            ],
                            "scope": "tenant",
                        },
                    )

        assert resp.status_code == 201
        # No repo items → validate_repo_accessibility never called
        mock_validate.assert_not_called()


# ---------------------------------------------------------------------------
# accessibility.py unit tests — check_repo_public + validate_repo_accessibility
# ---------------------------------------------------------------------------


class TestCheckRepoPublic:
    """Tests for check_repo_public helper."""

    @pytest.mark.anyio
    async def test_public_repo_returns_true(self):
        """200 + private=false → True."""
        import httpx

        from src.knowledge.accessibility import check_repo_public, clear_public_cache

        clear_public_cache()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = httpx.Response(
            200,
            json={"full_name": "torvalds/linux", "private": False},
            request=httpx.Request("GET", "https://api.github.com/repos/torvalds/linux"),
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_repo_public("torvalds", "linux", http_client=mock_client)
        assert result is True

    @pytest.mark.anyio
    async def test_private_repo_returns_false(self):
        """200 + private=true → False."""
        import httpx

        from src.knowledge.accessibility import check_repo_public, clear_public_cache

        clear_public_cache()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = httpx.Response(
            200,
            json={"full_name": "acme/secret", "private": True},
            request=httpx.Request("GET", "https://api.github.com/repos/acme/secret"),
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_repo_public("acme", "secret", http_client=mock_client)
        assert result is False

    @pytest.mark.anyio
    async def test_nonexistent_repo_returns_false(self):
        """404 → False (repo doesn't exist or is private and unauthenticated)."""
        import httpx

        from src.knowledge.accessibility import check_repo_public, clear_public_cache

        clear_public_cache()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = httpx.Response(
            404,
            json={"message": "Not Found"},
            request=httpx.Request("GET", "https://api.github.com/repos/acme/typo"),
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_repo_public("acme", "typo", http_client=mock_client)
        assert result is False

    @pytest.mark.anyio
    async def test_cache_avoids_repeated_calls(self):
        """Second call for same owner/repo uses cache — no extra HTTP call."""
        import httpx

        from src.knowledge.accessibility import check_repo_public, clear_public_cache

        clear_public_cache()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = httpx.Response(
            200,
            json={"full_name": "torvalds/linux", "private": False},
            request=httpx.Request("GET", "https://api.github.com/repos/torvalds/linux"),
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        # First call
        result1 = await check_repo_public("torvalds", "linux", http_client=mock_client)
        # Second call — should use cache
        result2 = await check_repo_public("torvalds", "linux", http_client=mock_client)

        assert result1 is True
        assert result2 is True
        # HTTP client should only be called once
        assert mock_client.get.call_count == 1


class TestParseGitHubOwnerRepo:
    """Tests for parse_github_owner_repo helper."""

    def test_https_url(self):
        from src.knowledge.accessibility import parse_github_owner_repo

        assert parse_github_owner_repo("https://github.com/acme/my-repo") == (
            "acme",
            "my-repo",
        )

    def test_https_url_with_git_suffix(self):
        from src.knowledge.accessibility import parse_github_owner_repo

        assert parse_github_owner_repo("https://github.com/acme/my-repo.git") == (
            "acme",
            "my-repo",
        )

    def test_ssh_url(self):
        from src.knowledge.accessibility import parse_github_owner_repo

        assert parse_github_owner_repo("git@github.com:acme/my-repo.git") == (
            "acme",
            "my-repo",
        )

    def test_non_github_url_returns_none(self):
        from src.knowledge.accessibility import parse_github_owner_repo

        assert parse_github_owner_repo("https://gitlab.com/acme/my-repo") is None

    def test_incomplete_path_returns_none(self):
        from src.knowledge.accessibility import parse_github_owner_repo

        assert parse_github_owner_repo("https://github.com/acme") is None


class TestValidateRepoAccessibility:
    """Tests for the full validate_repo_accessibility orchestrator."""

    @pytest.mark.anyio
    async def test_public_repo_accepts_shared_no_installation_call(self):
        """Public repo → shared, resolve_installation_for_repo never called."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=True,
        ) as mock_public:
            with patch("src.knowledge.accessibility.resolve_installation_for_repo") as mock_resolve:
                result = await validate_repo_accessibility(
                    "https://github.com/torvalds/linux",
                    "acme-corp",
                    db=mock_db,
                )

        assert result.allowed is True
        assert result.shared is True
        assert result.installation_id is None
        mock_public.assert_called_once_with("torvalds", "linux", http_client=None)
        mock_resolve.assert_not_called()

    @pytest.mark.anyio
    async def test_private_repo_owned_installation_accepts_with_id(self):
        """Private repo + owned installation → accept with installation_id."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=False,
        ):
            with patch(
                "src.knowledge.accessibility.resolve_installation_for_repo",
                return_value=98765,
            ):
                with patch(
                    "src.knowledge.accessibility.verify_installation_ownership",
                    return_value=True,
                ):
                    result = await validate_repo_accessibility(
                        "https://github.com/acme/private-svc",
                        "acme-corp",
                        db=mock_db,
                    )

        assert result.allowed is True
        assert result.shared is False
        assert result.installation_id == 98765

    @pytest.mark.anyio
    async def test_private_repo_other_tenant_rejects(self):
        """Private repo with installation belonging to another tenant → reject 403."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=False,
        ):
            with patch(
                "src.knowledge.accessibility.resolve_installation_for_repo",
                return_value=99999,
            ):
                with patch(
                    "src.knowledge.accessibility.verify_installation_ownership",
                    return_value=False,
                ):
                    result = await validate_repo_accessibility(
                        "https://github.com/evil-org/secret-repo",
                        "acme-corp",
                        db=mock_db,
                    )

        assert result.allowed is False
        assert result.error_code == 403
        assert "does not belong to your tenant" in result.error_message

    @pytest.mark.anyio
    async def test_no_installation_rejects_with_actionable_message(self):
        """App not installed on repo → reject 422 with install instructions."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=False,
        ):
            with patch(
                "src.knowledge.accessibility.resolve_installation_for_repo",
                return_value=None,
            ):
                result = await validate_repo_accessibility(
                    "https://github.com/acme/no-install",
                    "acme-corp",
                    db=mock_db,
                )

        assert result.allowed is False
        assert result.error_code == 422
        assert "Install the ADP App" in result.error_message

    @pytest.mark.anyio
    async def test_unparseable_source_ref_rejects(self):
        """Source ref that can't be parsed as GitHub URL → reject 400."""
        from src.knowledge.accessibility import validate_repo_accessibility

        mock_db = AsyncMock()

        result = await validate_repo_accessibility(
            "https://gitlab.com/acme/my-repo",
            "acme-corp",
            db=mock_db,
        )

        assert result.allowed is False
        assert result.error_code == 400
        assert "Cannot parse" in result.error_message
