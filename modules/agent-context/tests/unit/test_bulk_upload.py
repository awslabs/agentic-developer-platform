"""Unit tests for the bulk-upload endpoints (preview + commit).

Issue #1792 (Story C of E10 #1736).

Tests cover:
- File parsing: comments, empty lines, extended format, type inference
- Preview: valid file returns items, rejected lines, duplicates, quota
- Preview: file too large / too many lines returns 413
- Preview: non-admin cannot use tenant scope (403)
- Preview: no DB writes occur
- Commit: writes rows, skips duplicates, returns created assets
- Commit: quota exceeded returns 429
- Commit: non-admin cannot use tenant scope (403)
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_context.api.assets_router import (
    get_assets_db,
    get_current_user_from_state,
    router,
)
from agent_context.api.bulk_parser import (
    MAX_FILE_SIZE_BYTES,
    MAX_LINES,
    infer_asset_type,
    parse_bulk_file,
)


# ---------------------------------------------------------------------------
# Fixtures — reuse the same fake pattern from test_assets_router.py
# ---------------------------------------------------------------------------


@dataclass
class FakeTokenContext:
    """Minimal TokenContext stub for tests."""

    user_id: str = "user-alice"
    org_id: str = "acme-corp"
    is_admin: bool = False


@dataclass
class FakeRow:
    """Simulates a DB row returned by fetchone/fetchall."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    asset_type: str = "repo"
    source_ref: str = "https://github.com/acme/my-service"
    display_name: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = "acme-corp"
    owner_sub: str | None = None
    project_id: uuid.UUID | None = None
    status: str = "registered"
    last_error: str | None = None
    retry_count: int = 0
    registered_by: str | None = "admin-bob"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeResult:
    """Simulates SQLAlchemy result from execute()."""

    def __init__(self, rows: list[Any] | None = None, scalar_val: Any = None):
        self._rows = rows or []
        self._scalar_val = scalar_val

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def scalar(self) -> Any:
        return self._scalar_val


class FakeAsyncSession:
    """Async mock for SQLAlchemy AsyncSession."""

    def __init__(self):
        self.execute_results: list[FakeResult] = []
        self._call_index = 0
        self.committed = False
        self.executed_statements: list[Any] = []

    async def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        self.executed_statements.append((stmt, params))
        if self._call_index < len(self.execute_results):
            result = self.execute_results[self._call_index]
            self._call_index += 1
            return result
        return FakeResult()

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def admin_user() -> FakeTokenContext:
    return FakeTokenContext(user_id="admin-bob", is_admin=True)


@pytest.fixture
def regular_user() -> FakeTokenContext:
    return FakeTokenContext(user_id="user-alice", is_admin=False)


@pytest.fixture
def app() -> FastAPI:
    """Create a FastAPI app with the assets router mounted."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def make_client(app: FastAPI):
    """Factory to create a test client with configured dependencies."""

    def _make(db_session: FakeAsyncSession, user: FakeTokenContext | None = None):
        if user is None:
            user = FakeTokenContext(is_admin=True)

        async def override_db() -> AsyncGenerator:
            yield db_session

        async def override_user() -> FakeTokenContext:
            return user

        app.dependency_overrides[get_assets_db] = override_db
        app.dependency_overrides[get_current_user_from_state] = override_user
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )

    return _make


# ---------------------------------------------------------------------------
# Unit tests for bulk_parser.py
# ---------------------------------------------------------------------------


class TestInferAssetType:
    """Tests for infer_asset_type()."""

    def test_github_https(self):
        assert infer_asset_type("https://github.com/acme/repo") == "repo"

    def test_github_ssh(self):
        assert infer_asset_type("git@github.com:acme/repo.git") == "repo"

    def test_s3_path(self):
        assert infer_asset_type("s3://my-bucket/docs/file.pdf") == "doc"

    def test_http_url(self):
        assert infer_asset_type("https://docs.aws.amazon.com/bedrock/") == "url"

    def test_unsupported_protocol(self):
        assert infer_asset_type("ftp://old-server/file") is None

    def test_bare_string(self):
        assert infer_asset_type("not-a-url") is None


class TestParseBulkFile:
    """Tests for parse_bulk_file()."""

    def test_simple_file(self):
        content = "https://github.com/acme/repo1\nhttps://github.com/acme/repo2\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 2
        assert len(rejected) == 0
        assert total == 2
        assert skipped == 0
        assert valid[0].source_ref == "https://github.com/acme/repo1"
        assert valid[0].asset_type == "repo"
        assert valid[0].line == 1

    def test_comments_and_empty_lines(self):
        content = "# Comment\n\nhttps://github.com/acme/repo\n# Another comment\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert total == 4
        assert skipped == 3  # 2 comments + 1 empty

    def test_extended_format_with_display_name_and_tags(self):
        content = "https://github.com/acme/repo | My Repo | team:platform, priority:high\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert valid[0].display_name == "My Repo"
        assert valid[0].tags == {"team": "platform", "priority": "high"}

    def test_extended_format_display_name_only(self):
        content = "https://docs.aws.amazon.com/bedrock/ | Bedrock Docs\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert valid[0].display_name == "Bedrock Docs"
        assert valid[0].tags == {}
        assert valid[0].asset_type == "url"

    def test_rejected_unsupported_protocol(self):
        content = "ftp://old-server/file\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 0
        assert len(rejected) == 1
        assert rejected[0].reason == "Cannot infer asset_type from source_ref"
        assert rejected[0].line == 1

    def test_mixed_file(self):
        content = (
            "# Assets\n"
            "https://github.com/acme/repo1\n"
            "s3://bucket/doc.pdf | Architecture Doc | category:design\n"
            "not-a-url\n"
            "https://docs.example.com/ | Docs\n"
        )
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 3
        assert len(rejected) == 1
        assert total == 5
        assert skipped == 1
        # Check types
        assert valid[0].asset_type == "repo"
        assert valid[1].asset_type == "doc"
        assert valid[2].asset_type == "url"


# ---------------------------------------------------------------------------
# Preview endpoint tests
# ---------------------------------------------------------------------------


class TestBulkPreview:
    """Tests for POST /api/agent-context/assets/bulk."""

    @pytest.mark.anyio
    async def test_preview_valid_file(self, make_client, admin_user):
        """Happy path: valid file returns parsed items."""
        db = FakeAsyncSession()
        # For each valid item, the endpoint checks for duplicates (2 items → 2 queries)
        # then does a quota count query
        db.execute_results = [
            FakeResult(rows=[]),  # no dup for item 1
            FakeResult(rows=[]),  # no dup for item 2
            FakeResult(rows=[]),  # existing counts (none)
        ]

        file_content = b"https://github.com/acme/repo1\nhttps://github.com/acme/repo2\n"

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 2
        assert data["parsed"] == 2
        assert len(data["valid"]) == 2
        assert len(data["rejected"]) == 0
        assert len(data["duplicates"]) == 0
        assert data["quota_ok"] is True
        # No DB writes should have occurred
        assert db.committed is False

    @pytest.mark.anyio
    async def test_preview_detects_duplicates(self, make_client, admin_user):
        """Existing assets in the same scope are flagged as duplicates."""
        existing_id = uuid.uuid4()
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[FakeRow(id=existing_id)]),  # dup found for item 1
            FakeResult(rows=[]),  # no dup for item 2
            FakeResult(rows=[]),  # existing counts
        ]

        file_content = b"https://github.com/acme/existing-repo\nhttps://github.com/acme/new-repo\n"

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["duplicates"]) == 1
        assert data["duplicates"][0]["existing_id"] == str(existing_id)
        assert len(data["valid"]) == 1
        assert db.committed is False

    @pytest.mark.anyio
    async def test_preview_quota_exceeded(self, make_client, admin_user):
        """Preview shows quota_ok=False when new items would exceed limit."""
        db = FakeAsyncSession()
        # No duplicates for the item
        db.execute_results = [
            FakeResult(rows=[]),  # no dup
            # Existing count: 199 repos (limit is 200 for tenant)
            FakeResult(rows=[FakeRow(asset_type="repo", source_ref="count")]),
        ]
        # Patch: the count query returns asset_type + cnt
        # Need a row with .asset_type and .cnt attributes

        @dataclass
        class CountRow:
            asset_type: str = "repo"
            cnt: int = 200  # At limit already

        db.execute_results = [
            FakeResult(rows=[]),  # no dup
            FakeResult(rows=[CountRow()]),  # existing counts: 200 repos
        ]

        file_content = b"https://github.com/acme/one-more-repo\n"

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["quota_ok"] is False

    @pytest.mark.anyio
    async def test_preview_file_too_large(self, make_client, admin_user):
        """File exceeding 1MB returns 413."""
        db = FakeAsyncSession()
        # Create content larger than 1 MB
        large_content = b"https://github.com/acme/repo\n" * (MAX_FILE_SIZE_BYTES // 28 + 100)

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(large_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 413

    @pytest.mark.anyio
    async def test_preview_too_many_lines(self, make_client, admin_user):
        """File exceeding 500 lines returns 413."""
        db = FakeAsyncSession()
        # 501 short lines (under 1MB)
        content = b"https://github.com/a/b\n" * (MAX_LINES + 1)

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 413

    @pytest.mark.anyio
    async def test_preview_non_admin_tenant_scope_forbidden(self, make_client, regular_user):
        """Non-admin cannot upload at tenant scope."""
        db = FakeAsyncSession()
        file_content = b"https://github.com/acme/repo\n"

        async with make_client(db, regular_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_preview_personal_scope_allowed(self, make_client, regular_user):
        """Non-admin can upload at personal scope."""
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[]),  # no dup
            FakeResult(rows=[]),  # existing counts
        ]

        file_content = b"https://github.com/acme/my-repo\n"

        async with make_client(db, regular_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "personal"},
            )

        assert resp.status_code == 200
        assert db.committed is False

    @pytest.mark.anyio
    async def test_preview_with_rejected_lines(self, make_client, admin_user):
        """Invalid lines are returned in rejected list."""
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[]),  # no dup for valid item
            FakeResult(rows=[]),  # existing counts
        ]

        file_content = b"https://github.com/acme/repo\nftp://bad-protocol\nnot-a-url\n"

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk",
                files={"file": ("assets.txt", BytesIO(file_content), "text/plain")},
                data={"scope": "tenant"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["valid"]) == 1
        assert len(data["rejected"]) == 2


# ---------------------------------------------------------------------------
# Commit endpoint tests
# ---------------------------------------------------------------------------


class TestBulkCommit:
    """Tests for POST /api/agent-context/assets/bulk/commit."""

    @pytest.mark.anyio
    async def test_commit_creates_assets(self, make_client, admin_user):
        """Happy path: commit writes rows and returns created assets."""
        created_row = FakeRow(
            source_ref="https://github.com/acme/repo1",
            tenant_id="acme-corp",
            owner_sub=None,
        )
        db = FakeAsyncSession()
        # Sequence: 1) quota count, 2) dup check, 3) insert, 4) fetch created
        db.execute_results = [
            FakeResult(rows=[]),  # existing counts (empty)
            FakeResult(rows=[]),  # no dup for item
            FakeResult(),  # insert
            FakeResult(rows=[created_row]),  # fetch created row
        ]

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "items": [
                        {
                            "source_ref": "https://github.com/acme/repo1",
                            "asset_type": "repo",
                            "display_name": None,
                            "tags": {},
                        }
                    ],
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] == 1
        assert data["skipped_duplicates"] == 0
        assert len(data["assets"]) == 1
        assert db.committed is True

    @pytest.mark.anyio
    async def test_commit_skips_duplicates(self, make_client, admin_user):
        """Commit skips items that already exist in the scope."""
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[]),  # existing counts
            FakeResult(rows=[FakeRow()]),  # dup found for item 1
        ]

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "items": [
                        {
                            "source_ref": "https://github.com/acme/existing",
                            "asset_type": "repo",
                            "display_name": None,
                            "tags": {},
                        }
                    ],
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] == 0
        assert data["skipped_duplicates"] == 1

    @pytest.mark.anyio
    async def test_commit_quota_exceeded(self, make_client, admin_user):
        """Commit rejects entire batch if quota would be exceeded."""

        @dataclass
        class CountRow:
            asset_type: str = "repo"
            cnt: int = 200  # Already at tenant limit

        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[CountRow()]),  # existing counts: at limit
        ]

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "items": [
                        {
                            "source_ref": "https://github.com/acme/new-repo",
                            "asset_type": "repo",
                            "display_name": None,
                            "tags": {},
                        }
                    ],
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 429
        data = resp.json()
        assert "Quota exceeded" in data["detail"]["message"]

    @pytest.mark.anyio
    async def test_commit_non_admin_tenant_scope_forbidden(self, make_client, regular_user):
        """Non-admin cannot commit at tenant scope."""
        db = FakeAsyncSession()

        async with make_client(db, regular_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "items": [
                        {
                            "source_ref": "https://github.com/acme/repo",
                            "asset_type": "repo",
                            "display_name": None,
                            "tags": {},
                        }
                    ],
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_commit_empty_items_rejected(self, make_client, admin_user):
        """Empty items list returns 400."""
        db = FakeAsyncSession()

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={"items": [], "scope": "tenant"},
            )

        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_commit_personal_scope_allowed(self, make_client, regular_user):
        """Non-admin can commit at personal scope."""
        created_row = FakeRow(
            source_ref="https://github.com/acme/my-repo",
            tenant_id="acme-corp",
            owner_sub="user-alice",
        )
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[]),  # existing counts
            FakeResult(rows=[]),  # no dup
            FakeResult(),  # insert
            FakeResult(rows=[created_row]),  # fetch
        ]

        async with make_client(db, regular_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "items": [
                        {
                            "source_ref": "https://github.com/acme/my-repo",
                            "asset_type": "repo",
                            "display_name": None,
                            "tags": {},
                        }
                    ],
                    "scope": "personal",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] == 1
