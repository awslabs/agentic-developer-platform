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
            # Quota check for scope group 1 (shared: tid="", sub="")
            FakeResult(rows=[]),
            # Quota check for scope group 2 (tenant: tid="acme-corp", sub="")
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
            # Quota check (one scope group: tenant)
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
        mock_gateway_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=True,
        ) as mock_public:
            with patch("src.knowledge.accessibility.resolve_installation_for_repo") as mock_resolve:
                result = await validate_repo_accessibility(
                    "https://github.com/torvalds/linux",
                    "acme-corp",
                    db=mock_db,
                    gateway_db=mock_gateway_db,
                )

        assert result.allowed is True
        assert result.shared is True
        assert result.installation_id is None
        mock_public.assert_called_once_with("torvalds", "linux", http_client=None)
        mock_resolve.assert_not_called()

    @pytest.mark.anyio
    async def test_private_repo_owned_installation_accepts_with_id(self):
        """Private repo + owned installation → accept with worker installation_id."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

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
                    with patch(
                        "src.knowledge.accessibility.resolve_worker_installation_for_repo",
                        AsyncMock(return_value=98770),
                    ):
                        result = await validate_repo_accessibility(
                            "https://github.com/acme/private-svc",
                            "acme-corp",
                            db=mock_db,
                            gateway_db=mock_gateway_db,
                        )

        assert result.allowed is True
        assert result.shared is False
        # Issue #3529: stored installation_id is the WORKER's (ops-App), not the dev-App's
        assert result.installation_id == 98770

    @pytest.mark.anyio
    async def test_private_repo_other_tenant_rejects(self):
        """Private repo with installation belonging to another tenant → reject 403."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

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
                        gateway_db=mock_gateway_db,
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
        mock_gateway_db = AsyncMock()

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
                    gateway_db=mock_gateway_db,
                )

        assert result.allowed is False
        assert result.error_code == 422
        assert "Install the ADP App" in result.error_message

    @pytest.mark.anyio
    async def test_unparseable_source_ref_rejects(self):
        """Source ref that can't be parsed as GitHub URL → reject 400."""
        from src.knowledge.accessibility import validate_repo_accessibility

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

        result = await validate_repo_accessibility(
            "https://gitlab.com/acme/my-repo",
            "acme-corp",
            db=mock_db,
            gateway_db=mock_gateway_db,
        )

        assert result.allowed is False
        assert result.error_code == 400
        assert "Cannot parse" in result.error_message


# ---------------------------------------------------------------------------
# Issue #3266: Membership-based installation ownership fallback
# ---------------------------------------------------------------------------


class TestMembershipFallback:
    """Tests for org-member fallback when per-tenant ownership check fails."""

    @pytest.mark.anyio
    async def test_org_member_accepted_via_membership_fallback(self):
        """Org member + platform-App-covered private repo → accepted, scoped to org tenant."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=False,
        ):
            with patch(
                "src.knowledge.accessibility.resolve_installation_for_repo",
                return_value=124731,
            ):
                with patch(
                    "src.knowledge.accessibility.verify_installation_ownership",
                    return_value=False,
                ):
                    with patch(
                        "src.knowledge.accessibility.check_membership_for_installation",
                        return_value="aws-e",
                    ) as mock_membership:
                        with patch(
                            "src.knowledge.accessibility.resolve_worker_installation_for_repo",
                            AsyncMock(return_value=124731359),
                        ):
                            result = await validate_repo_accessibility(
                                "https://github.com/aws-e/adp",
                                "pranavsharma1000",
                                db=mock_db,
                                gateway_db=mock_gateway_db,
                                caller_user_id="cognito-sub-123",
                            )

        assert result.allowed is True
        assert result.shared is False
        # Issue #3529: stored installation_id is the WORKER's (ops-App)
        assert result.installation_id == 124731359
        assert result.tenant_id == "aws-e"
        mock_membership.assert_called_once_with("cognito-sub-123", 124731, db=mock_gateway_db)

    @pytest.mark.anyio
    async def test_non_member_rejected_after_fallback(self):
        """Non-member of the covering org → rejected 403."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

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
                    with patch(
                        "src.knowledge.accessibility.check_membership_for_installation",
                        return_value=None,
                    ):
                        result = await validate_repo_accessibility(
                            "https://github.com/evil-org/secret-repo",
                            "personal-tenant",
                            db=mock_db,
                            gateway_db=mock_gateway_db,
                            caller_user_id="cognito-sub-456",
                        )

        assert result.allowed is False
        assert result.error_code == 403
        assert "does not belong to your tenant" in result.error_message

    @pytest.mark.anyio
    async def test_per_tenant_ownership_still_wins(self):
        """Per-tenant secret path still wins — no membership fallback needed."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

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
                    with patch(
                        "src.knowledge.accessibility.check_membership_for_installation",
                    ) as mock_membership:
                        with patch(
                            "src.knowledge.accessibility.resolve_worker_installation_for_repo",
                            AsyncMock(return_value=98770),
                        ):
                            result = await validate_repo_accessibility(
                                "https://github.com/acme/private-svc",
                                "acme-corp",
                                db=mock_db,
                                gateway_db=mock_gateway_db,
                                caller_user_id="cognito-sub-789",
                            )

        assert result.allowed is True
        assert result.shared is False
        # Issue #3529: stored installation_id is the WORKER's (ops-App)
        assert result.installation_id == 98770
        assert result.tenant_id is None  # No override — per-tenant path won
        # Membership fallback should NOT have been called
        mock_membership.assert_not_called()

    @pytest.mark.anyio
    async def test_no_caller_user_id_skips_membership_fallback(self):
        """No caller_user_id → membership fallback skipped, rejects as before."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

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
                    with patch(
                        "src.knowledge.accessibility.check_membership_for_installation",
                    ) as mock_membership:
                        result = await validate_repo_accessibility(
                            "https://github.com/org/repo",
                            "my-tenant",
                            db=mock_db,
                            gateway_db=mock_gateway_db,
                            # No caller_user_id passed
                        )

        assert result.allowed is False
        assert result.error_code == 403
        mock_membership.assert_not_called()

    @pytest.mark.anyio
    async def test_public_repo_unchanged(self):
        """Public repo → shared scope path unchanged by membership feature."""
        from src.knowledge.accessibility import (
            clear_public_cache,
            validate_repo_accessibility,
        )

        clear_public_cache()

        mock_db = AsyncMock()
        mock_gateway_db = AsyncMock()

        with patch(
            "src.knowledge.accessibility.check_repo_public",
            return_value=True,
        ):
            with patch("src.knowledge.accessibility.resolve_installation_for_repo") as mock_resolve:
                result = await validate_repo_accessibility(
                    "https://github.com/torvalds/linux",
                    "acme-corp",
                    db=mock_db,
                    gateway_db=mock_gateway_db,
                    caller_user_id="cognito-sub-123",
                )

        assert result.allowed is True
        assert result.shared is True
        mock_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #3266: check_membership_for_installation unit tests
# ---------------------------------------------------------------------------


class TestCheckMembershipForInstallation:
    """Tests for the membership check helper in github_app_service."""

    @pytest.mark.anyio
    async def test_member_of_owning_org_returns_tenant_id(self):
        """User is member of org that owns installation → returns org tenant_id."""
        from src.knowledge.github_app_service import check_membership_for_installation
        from tests.knowledge.conftest import FakeAsyncSession, FakeResult

        db = FakeAsyncSession()
        # First query: resolve users.id from cognito_sub
        db.execute_results = [
            FakeResult(rows=[("pg-user-id-1",)]),
            # Second query: JOIN channel_tenant_map + tenant_memberships
            FakeResult(rows=[("aws-e",)]),
        ]

        result = await check_membership_for_installation(
            "cognito-sub-pranav",
            124731,
            db=db,
        )

        assert result == "aws-e"

    @pytest.mark.anyio
    async def test_non_member_returns_none(self):
        """User is not a member of any owning org → returns None."""
        from src.knowledge.github_app_service import check_membership_for_installation
        from tests.knowledge.conftest import FakeAsyncSession, FakeResult

        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[("pg-user-id-2",)]),
            # JOIN query returns no rows
            FakeResult(rows=[]),
        ]

        result = await check_membership_for_installation(
            "cognito-sub-stranger",
            99999,
            db=db,
        )

        assert result is None

    @pytest.mark.anyio
    async def test_unknown_user_returns_none(self):
        """Cognito sub not found in users table → returns None."""
        from src.knowledge.github_app_service import check_membership_for_installation
        from tests.knowledge.conftest import FakeAsyncSession, FakeResult

        db = FakeAsyncSession()
        db.execute_results = [
            # No user found
            FakeResult(rows=[]),
        ]

        result = await check_membership_for_installation(
            "cognito-sub-unknown",
            124731,
            db=db,
        )

        assert result is None


# ---------------------------------------------------------------------------
# Issue #3358: Route-level integration test — membership fallback end-to-end
# ---------------------------------------------------------------------------


class TestMembershipFallbackRouteIntegration:
    """Route-level test: exercises the full membership fallback path without
    patching check_membership_for_installation. Verifies the inserted row's
    tenant_id is the org tenant resolved via membership.
    """

    @pytest.mark.anyio
    async def test_register_asset_membership_fallback_scopes_to_org_tenant(self, make_client, fake_user):
        """Full route: org member registers private repo via membership fallback.

        Asserts the inserted row's tenant_id is the org tenant (aws-e), NOT the
        caller's personal tenant (acme-corp).
        """
        from src.knowledge.accessibility import validate_repo_accessibility

        # The agent_context db handles: quota, dup check, insert, fetch
        asset_row = FakeRow(tenant_id="aws-e", owner_sub=None)
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate (now checked after scope resolution)
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        # The gateway_db handles: verify_installation_ownership + check_membership_for_installation
        # - verify_installation_ownership: SELECT 1 FROM channel_tenant_map → NOT found (personal tenant doesn't own)
        # - check_membership_for_installation query 1: SELECT id FROM users WHERE cognito_sub → found
        # - check_membership_for_installation query 2: JOIN → returns owning org_id
        gateway_db = FakeAsyncSession()
        gateway_db.execute_results = [
            FakeResult(rows=[]),  # verify_installation_ownership → no match (personal tenant)
            FakeResult(rows=[("pg-user-id-1",)]),  # users lookup → found
            FakeResult(rows=[("aws-e",)]),  # membership JOIN → org tenant owns installation
        ]

        # Patch external HTTP calls but let DB queries flow through.
        # We override the autouse mock to use the REAL validate_repo_accessibility.
        with patch(
            "src.knowledge.routes.validate_repo_accessibility",
            side_effect=validate_repo_accessibility,
        ):
            with patch(
                "src.knowledge.accessibility.check_repo_public",
                return_value=False,
            ):
                with patch(
                    "src.knowledge.accessibility.resolve_installation_for_repo",
                    return_value=124731,
                ):
                    with patch(
                        "src.knowledge.accessibility.resolve_worker_installation_for_repo",
                        AsyncMock(return_value=124731359),
                    ):
                        with patch(
                            "src.knowledge.routes.dispatch_ingestion",
                            new_callable=AsyncMock,
                        ):
                            async with make_client(db, fake_user, gateway_db_session=gateway_db) as client:
                                resp = await client.post(
                                    "/api/agent-context/assets",
                                    json={
                                        "asset_type": "repo",
                                        "source_ref": "https://github.com/aws-e/adp",
                                        "scope": "personal",
                                    },
                                )

        assert resp.status_code == 201

        # Verify the INSERT used the org tenant (aws-e), not the caller's
        # personal tenant (acme-corp).
        # The insert is the 3rd execute call on the agent_context db.
        insert_params = db.executed_statements[2][1]
        assert insert_params["tenant_id"] == "aws-e"
        assert insert_params["owner_sub"] is None  # Org-scoped, not personal
        # Issue #3529: stored installation_id is the WORKER's (ops-App)
        assert insert_params["installation_id"] == 124731359

    @pytest.mark.anyio
    async def test_register_asset_gateway_db_used_for_ownership_check(self, make_client, fake_user):
        """Verify that ownership/membership queries hit gateway_db, not agent_context db.

        When ownership check passes on gateway_db, the route should accept
        without falling through to membership check.
        """
        from src.knowledge.accessibility import validate_repo_accessibility

        asset_row = FakeRow(tenant_id="acme-corp", owner_sub="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        # Gateway DB: verify_installation_ownership → FOUND (tenant owns it)
        gateway_db = FakeAsyncSession()
        gateway_db.execute_results = [
            FakeResult(rows=[(1,)]),  # verify_installation_ownership → match found
        ]

        with patch(
            "src.knowledge.routes.validate_repo_accessibility",
            side_effect=validate_repo_accessibility,
        ):
            with patch(
                "src.knowledge.accessibility.check_repo_public",
                return_value=False,
            ):
                with patch(
                    "src.knowledge.accessibility.resolve_installation_for_repo",
                    return_value=98765,
                ):
                    with patch(
                        "src.knowledge.accessibility.resolve_worker_installation_for_repo",
                        AsyncMock(return_value=98770),
                    ):
                        with patch(
                            "src.knowledge.routes.dispatch_ingestion",
                            new_callable=AsyncMock,
                        ):
                            async with make_client(db, fake_user, gateway_db_session=gateway_db) as client:
                                resp = await client.post(
                                    "/api/agent-context/assets",
                                    json={
                                        "asset_type": "repo",
                                        "source_ref": "https://github.com/acme/private-svc",
                                        "scope": "personal",
                                    },
                                )

        assert resp.status_code == 201

        # Gateway DB should have been queried (ownership check)
        assert len(gateway_db.executed_statements) == 1
        # The ownership query should target channel_tenant_map
        ownership_params = gateway_db.executed_statements[0][1]
        assert ownership_params["tenant_id"] == "acme-corp"
        assert ownership_params["installation_id"] == "98765"


# ---------------------------------------------------------------------------
# Issue #3358: Non-admin org-tenant registration policy pinning tests
# ---------------------------------------------------------------------------


class TestMembershipPolicyPinning:
    """Pinning tests: non-admin users can register via membership fallback,
    bypassing the scope='tenant' admin gate when the membership resolution
    provides the authorization.
    """

    @pytest.mark.anyio
    async def test_non_admin_scope_tenant_membership_fallback_accepted(self, make_client, fake_user):
        """Non-admin + scope='tenant' + membership fallback → 201 accepted.

        POLICY: The membership check IS the authorization. The admin gate only
        applies to explicit tenant-scope elevation without installation-membership.
        """
        asset_row = FakeRow(tenant_id="aws-e", owner_sub=None)
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            # Membership fallback resolved org tenant "aws-e"
            mock_validate.return_value = AccessibilityResult(
                allowed=True,
                shared=False,
                installation_id=124731,
                tenant_id="aws-e",
            )

            with patch("src.knowledge.routes.dispatch_ingestion", new_callable=AsyncMock):
                async with make_client(db, fake_user) as client:
                    resp = await client.post(
                        "/api/agent-context/assets",
                        json={
                            "asset_type": "repo",
                            "source_ref": "https://github.com/aws-e/adp",
                            "scope": "tenant",  # Non-admin requesting tenant scope
                        },
                    )

        # Non-admin is accepted because membership fallback fires BEFORE the admin gate
        assert resp.status_code == 201
        # Verify inserted with org tenant, not personal
        insert_params = db.executed_statements[2][1]
        assert insert_params["tenant_id"] == "aws-e"
        assert insert_params["owner_sub"] is None

    @pytest.mark.anyio
    async def test_non_admin_scope_tenant_no_membership_rejected(self, make_client, fake_user):
        """Non-admin + scope='tenant' + NO membership fallback → 403 admin gate.

        Without membership resolution, the explicit admin gate applies.
        """
        db = FakeAsyncSession()

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            # No tenant_id override → ownership passed normally
            mock_validate.return_value = AccessibilityResult(
                allowed=True,
                shared=False,
                installation_id=98765,
            )

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/private-svc",
                        "scope": "tenant",  # Non-admin requesting tenant scope
                    },
                )

        # Admin gate blocks: no membership resolution, non-admin
        assert resp.status_code == 403
        assert "admin privileges" in resp.json()["detail"]


class TestBulkCommitQuotaPerResolvedTenant:
    """Tests for per-resolved-tenant quota enforcement in bulk_commit (Issue #3358)."""

    @pytest.mark.anyio
    async def test_membership_resolved_item_rejected_at_org_tenant_quota(self, make_client, fake_user):
        """Membership-resolved item rejected because ORG tenant is at quota.

        The item is resolved to org tenant 'aws-e' via membership fallback.
        The quota check must count against 'aws-e', not the caller's session
        tenant. When 'aws-e' is at capacity → 429.
        """
        # Create a row object that simulates the quota query result
        # (has .asset_type and .cnt, which FakeRow's dataclass doesn't support)
        quota_row = type("QuotaRow", (), {"asset_type": "repo", "cnt": 200})()

        db = FakeAsyncSession()
        db.execute_results = [
            # Quota check for resolved scope group (tid="aws-e", sub="")
            # Returns existing count AT the limit (200 repos, limit is 200)
            FakeResult(rows=[quota_row]),
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            # Membership fallback resolves to org tenant "aws-e"
            mock_validate.return_value = AccessibilityResult(
                allowed=True,
                shared=False,
                installation_id=124731,
                tenant_id="aws-e",
            )

            async with make_client(db, fake_user) as client:
                resp = await client.post(
                    "/api/agent-context/assets/bulk/commit",
                    json={
                        "items": [
                            {
                                "asset_type": "repo",
                                "source_ref": "https://github.com/aws-e/adp",
                            },
                        ],
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 429
        data = resp.json()
        assert "Quota exceeded" in data["detail"]["message"]
        assert data["detail"]["quota"]["repo"]["used"] == 200
        assert data["detail"]["quota"]["repo"]["limit"] == 200

    @pytest.mark.anyio
    async def test_bulk_commit_membership_fallback_non_admin_accepted(self, make_client, fake_user):
        """Non-admin bulk_commit with membership-resolved items → accepted.

        POLICY pinning: membership fallback bypasses the admin gate in
        bulk_commit, same as register_asset.
        """
        asset_row = FakeRow(tenant_id="aws-e", owner_sub=None)
        db = FakeAsyncSession()
        db.execute_results = [
            # Quota check for scope group (tid="aws-e", sub="")
            FakeResult(rows=[]),
            # Dedup check
            FakeResult(rows=[]),
            # Insert
            FakeResult(),
            # Fetch
            FakeResult(rows=[asset_row]),
        ]

        with patch("src.knowledge.routes.validate_repo_accessibility") as mock_validate:
            from src.knowledge.accessibility import AccessibilityResult

            mock_validate.return_value = AccessibilityResult(
                allowed=True,
                shared=False,
                installation_id=124731,
                tenant_id="aws-e",
            )

            with patch("src.knowledge.routes.dispatch_ingestion", new_callable=AsyncMock):
                async with make_client(db, fake_user) as client:
                    resp = await client.post(
                        "/api/agent-context/assets/bulk/commit",
                        json={
                            "items": [
                                {
                                    "asset_type": "repo",
                                    "source_ref": "https://github.com/aws-e/adp",
                                },
                            ],
                            # Non-admin submitting at personal scope, but membership
                            # resolves to org — the admin gate should NOT fire.
                            "scope": "personal",
                        },
                    )

        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] == 1


# ---------------------------------------------------------------------------
# Issue #3266: Improved error messages in resolve_tenant_app_credentials
# ---------------------------------------------------------------------------


class TestResolveTenantAppCredentialsErrors:
    """Tests for distinct error messages from resolve_tenant_app_credentials."""

    @pytest.mark.anyio
    async def test_resource_not_found_error_message(self):
        """Missing secret → specific 'no connection configured' message."""
        from unittest.mock import MagicMock

        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception(
            "An error occurred (ResourceNotFoundException) when calling GetSecretValue: Secrets Manager can't find the specified secret."
        )

        with pytest.raises(ValueError, match="No GitHub App connection configured"):
            await resolve_tenant_app_credentials("my-tenant", sm_client=mock_sm)

    @pytest.mark.anyio
    async def test_access_denied_error_message(self):
        """IAM AccessDenied → specific 'platform configuration issue' message."""
        from unittest.mock import MagicMock

        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception(
            "An error occurred (AccessDeniedException) when calling GetSecretValue: no identity-based policy allows the action"
        )

        with pytest.raises(ValueError, match="Platform IAM policy does not permit"):
            await resolve_tenant_app_credentials("my-tenant", sm_client=mock_sm)

    @pytest.mark.anyio
    async def test_generic_error_message(self):
        """Other errors → generic 'try again later' message."""
        from unittest.mock import MagicMock

        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception("Connection timed out")

        with pytest.raises(ValueError, match="Failed to resolve GitHub App credentials"):
            await resolve_tenant_app_credentials("my-tenant", sm_client=mock_sm)

    @pytest.mark.anyio
    async def test_boto3_client_error_classified_by_response_code(self):
        """Proper boto3 ClientError uses response['Error']['Code'] (not string matching)."""
        from unittest.mock import MagicMock

        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        mock_sm = MagicMock()
        # Simulate a proper boto3 ClientError with .response attribute
        client_error = Exception("An error occurred")
        client_error.response = {"Error": {"Code": "ResourceNotFoundException", "Message": "Secret not found"}}
        mock_sm.get_secret_value.side_effect = client_error

        with pytest.raises(ValueError, match="No GitHub App connection configured"):
            await resolve_tenant_app_credentials("my-tenant", sm_client=mock_sm)


class TestClassifyBoto3Error:
    """Unit tests for _classify_boto3_error helper (Issue #3358)."""

    def test_client_error_with_response(self):
        """ClientError with .response → extracts Code."""
        from src.knowledge.github_app_service import _classify_boto3_error

        exc = Exception("boom")
        exc.response = {"Error": {"Code": "AccessDeniedException"}}
        assert _classify_boto3_error(exc) == "AccessDeniedException"

    def test_generic_exception_string_match(self):
        """Non-ClientError exception → falls back to string matching."""
        from src.knowledge.github_app_service import _classify_boto3_error

        exc = Exception("An error occurred (ResourceNotFoundException)")
        assert _classify_boto3_error(exc) == "ResourceNotFoundException"

    def test_unknown_error_returns_none(self):
        """Unrecognized error → returns None."""
        from src.knowledge.github_app_service import _classify_boto3_error

        exc = Exception("Connection timed out")
        assert _classify_boto3_error(exc) is None
