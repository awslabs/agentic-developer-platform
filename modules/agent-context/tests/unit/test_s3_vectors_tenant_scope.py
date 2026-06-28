"""Unit tests for S3 Vectors per-tenant index isolation (Story 5, #1774).

Validates:
- Index selection by scope: shared → code-shard-{N}, tenant → tenant-{id},
  personal → personal-{sub}.
- put_vectors_scoped routes vectors to the correct scope-determined index.
- query_scoped unions shared + tenant + personal index results.
- Cross-tenant isolation: tenant A's vectors not visible to tenant B's query.
- Backward compatibility: unscoped put_vectors still writes to shared shards.
- Wiki store scope-aware routing via visibility parameter.
"""

from __future__ import annotations

import hashlib
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion source to path for wiki_store import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))

from personal_context.backends.s3_vectors_backend import S3VectorsCodeStore  # noqa: E402
from wiki_store import _resolve_vector_index, store_wiki  # noqa: E402

from .conftest import FakeVectorStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedding(seed: int = 0, dim: int = 8) -> list[float]:
    """Create a deterministic unit-norm embedding vector."""
    raw = [(seed + i) * 0.1 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


# ---------------------------------------------------------------------------
# resolve_index_name tests
# ---------------------------------------------------------------------------


class TestResolveIndexName:
    """S3VectorsCodeStore.resolve_index_name routes by visibility."""

    def test_shared_uses_hash_shard(self) -> None:
        """Shared visibility maps to code-shard-{N} based on org_id hash."""
        org_id = "org-acme"
        h = hashlib.sha256(org_id.encode()).digest()
        expected_shard = int.from_bytes(h[:4], "big") % 4

        name = S3VectorsCodeStore.resolve_index_name(
            visibility="shared", org_id=org_id, shard_count=4
        )
        assert name == f"code-shard-{expected_shard}"

    def test_tenant_uses_tenant_prefix(self) -> None:
        """Tenant visibility maps to tenant-{tenant_id}."""
        name = S3VectorsCodeStore.resolve_index_name(
            visibility="tenant", org_id="org-acme", tenant_id="acme-corp"
        )
        assert name == "tenant-acme-corp"

    def test_personal_uses_personal_prefix(self) -> None:
        """Personal visibility maps to personal-{owner_sub}."""
        name = S3VectorsCodeStore.resolve_index_name(
            visibility="personal", org_id="org-acme", owner_sub="user-abc-123"
        )
        assert name == "personal-user-abc-123"

    def test_tenant_without_tenant_id_raises(self) -> None:
        """Tenant visibility without tenant_id raises ValueError."""
        with pytest.raises(ValueError, match="tenant_id required"):
            S3VectorsCodeStore.resolve_index_name(
                visibility="tenant", org_id="org-acme", tenant_id=None
            )

    def test_personal_without_owner_sub_raises(self) -> None:
        """Personal visibility without owner_sub raises ValueError."""
        with pytest.raises(ValueError, match="owner_sub required"):
            S3VectorsCodeStore.resolve_index_name(
                visibility="personal", org_id="org-acme", owner_sub=None
            )

    def test_unknown_visibility_defaults_to_shared(self) -> None:
        """Unknown visibility value defaults to shared shard logic."""
        name = S3VectorsCodeStore.resolve_index_name(
            visibility="unknown", org_id="org-acme", shard_count=4
        )
        assert name.startswith("code-shard-")


# ---------------------------------------------------------------------------
# _resolve_vector_index (wiki_store helper) tests
# ---------------------------------------------------------------------------


class TestResolveVectorIndex:
    """wiki_store._resolve_vector_index routes by visibility with fallback."""

    def test_shared_routes_to_shard(self) -> None:
        """Shared visibility routes to code-shard-{N}."""
        index = _resolve_vector_index(
            visibility="shared",
            org_id="org-test",
            tenant_id=None,
            owner_sub=None,
            shard_count=4,
        )
        assert index.startswith("code-shard-")

    def test_tenant_routes_to_tenant_index(self) -> None:
        """Tenant visibility routes to tenant-{id}."""
        index = _resolve_vector_index(
            visibility="tenant",
            org_id="org-test",
            tenant_id="my-tenant",
            owner_sub=None,
            shard_count=4,
        )
        assert index == "tenant-my-tenant"

    def test_personal_routes_to_personal_index(self) -> None:
        """Personal visibility routes to personal-{sub}."""
        index = _resolve_vector_index(
            visibility="personal",
            org_id="org-test",
            tenant_id=None,
            owner_sub="user-xyz",
            shard_count=4,
        )
        assert index == "personal-user-xyz"

    def test_tenant_without_id_falls_back_to_shared(self) -> None:
        """Tenant visibility without tenant_id falls back to shared shard."""
        index = _resolve_vector_index(
            visibility="tenant",
            org_id="org-test",
            tenant_id=None,
            owner_sub=None,
            shard_count=4,
        )
        assert index.startswith("code-shard-")

    def test_personal_without_sub_falls_back_to_shared(self) -> None:
        """Personal visibility without owner_sub falls back to shared shard."""
        index = _resolve_vector_index(
            visibility="personal",
            org_id="org-test",
            tenant_id=None,
            owner_sub=None,
            shard_count=4,
        )
        assert index.startswith("code-shard-")


# ---------------------------------------------------------------------------
# put_vectors_scoped tests (mocked boto3)
# ---------------------------------------------------------------------------


class TestPutVectorsScoped:
    """put_vectors_scoped routes vectors to the correct scope-determined index."""

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def code_store(self, mock_s3v) -> S3VectorsCodeStore:
        return S3VectorsCodeStore(bucket_name="test-bucket", shard_count=4)

    def test_shared_writes_to_shard(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Shared visibility writes to code-shard-{N}."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(vectors, org_id="org-a", visibility="shared")

        call_kwargs = mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["indexName"].startswith("code-shard-")

    def test_tenant_writes_to_tenant_index(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Tenant visibility writes to tenant-{tenant_id}."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(
            vectors, org_id="org-a", visibility="tenant", tenant_id="acme"
        )

        call_kwargs = mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["indexName"] == "tenant-acme"

    def test_personal_writes_to_personal_index(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Personal visibility writes to personal-{owner_sub}."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(
            vectors, org_id="org-a", visibility="personal", owner_sub="user-123"
        )

        call_kwargs = mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["indexName"] == "personal-user-123"

    def test_tenant_creates_index_lazily(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Tenant/personal indexes are created on first write."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(
            vectors, org_id="org-a", visibility="tenant", tenant_id="new-tenant"
        )

        # Should have called create_index for the tenant index
        mock_s3v.create_index.assert_called_once()
        create_kwargs = mock_s3v.create_index.call_args[1]
        assert create_kwargs["indexName"] == "tenant-new-tenant"

    def test_shared_does_not_create_index(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Shared visibility does not call create_index (pre-provisioned)."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(vectors, org_id="org-a", visibility="shared")

        mock_s3v.create_index.assert_not_called()

    def test_tenant_index_creation_cached(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Second write to same tenant index skips create_index."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors_scoped(
            vectors, org_id="org-a", visibility="tenant", tenant_id="acme"
        )
        code_store.put_vectors_scoped(
            vectors, org_id="org-a", visibility="tenant", tenant_id="acme"
        )

        # create_index called only once (cached)
        assert mock_s3v.create_index.call_count == 1


# ---------------------------------------------------------------------------
# query_scoped tests (mocked boto3)
# ---------------------------------------------------------------------------


class TestQueryScoped:
    """query_scoped unions results from shared + tenant + personal indexes."""

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def code_store(self, mock_s3v) -> S3VectorsCodeStore:
        return S3VectorsCodeStore(bucket_name="test-bucket", shard_count=2)

    def test_queries_shared_shards_only_when_no_tenant(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Without tenant_id/owner_sub, only shared shards are queried."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id=None,
            owner_sub=None,
            top_k=10,
        )

        # 2 shards queried (shard_count=2)
        assert mock_s3v.query_vectors.call_count == 2

    def test_queries_shared_plus_tenant(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """With tenant_id, queries shared shards + tenant index."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="acme",
            owner_sub=None,
            top_k=10,
        )

        # 2 shared shards + 1 tenant index = 3
        assert mock_s3v.query_vectors.call_count == 3
        # Last call should be to tenant-acme
        calls = mock_s3v.query_vectors.call_args_list
        assert calls[2][1]["indexName"] == "tenant-acme"

    def test_queries_shared_plus_tenant_plus_personal(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """With both tenant_id and owner_sub, queries all three index types."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="acme",
            owner_sub="user-xyz",
            top_k=10,
        )

        # 2 shared + 1 tenant + 1 personal = 4
        assert mock_s3v.query_vectors.call_count == 4
        calls = mock_s3v.query_vectors.call_args_list
        assert calls[2][1]["indexName"] == "tenant-acme"
        assert calls[3][1]["indexName"] == "personal-user-xyz"

    def test_merges_results_sorted_by_distance(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Results from multiple indexes are merged and sorted by distance."""
        # Shared shard returns one result
        shared_result = {"vectors": [{"key": "shared-1", "distance": 0.5}]}
        # Tenant index returns a closer result
        tenant_result = {"vectors": [{"key": "tenant-1", "distance": 0.1}]}
        # Personal index returns a mid-distance result
        personal_result = {"vectors": [{"key": "personal-1", "distance": 0.3}]}

        mock_s3v.query_vectors.side_effect = [
            shared_result,  # shard 0
            {"vectors": []},  # shard 1
            tenant_result,  # tenant-acme
            personal_result,  # personal-user-xyz
        ]

        results = code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="acme",
            owner_sub="user-xyz",
            top_k=10,
        )

        assert len(results) == 3
        # Sorted by distance ascending
        assert results[0]["key"] == "tenant-1"
        assert results[0]["distance"] == 0.1
        assert results[1]["key"] == "personal-1"
        assert results[1]["distance"] == 0.3
        assert results[2]["key"] == "shared-1"
        assert results[2]["distance"] == 0.5

    def test_missing_tenant_index_returns_empty(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Non-existent tenant index returns empty (not an error)."""
        from botocore.exceptions import ClientError

        def side_effect(**kwargs):
            if kwargs.get("indexName") == "tenant-new-tenant":
                raise ClientError(
                    {"Error": {"Code": "NotFoundException", "Message": "Not found"}},
                    "QueryVectors",
                )
            return {"vectors": [{"key": "shared-hit", "distance": 0.2}]}

        mock_s3v.query_vectors.side_effect = side_effect

        results = code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="new-tenant",
            owner_sub=None,
            top_k=10,
        )

        # Still gets shared results despite tenant index missing
        assert any(r["key"] == "shared-hit" for r in results)

    def test_top_k_limits_merged_results(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """top_k limits the final merged result set."""
        # Return many results from shared shards
        many_results = {"vectors": [{"key": f"r-{i}", "distance": i * 0.1} for i in range(10)]}
        mock_s3v.query_vectors.return_value = many_results

        results = code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id=None,
            owner_sub=None,
            top_k=5,
        )

        assert len(results) == 5

    def test_shared_shard_filter_uses_org_id(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Shared shard queries include org_id metadata filter."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-specific",
            tenant_id=None,
            owner_sub=None,
            top_k=10,
        )

        # Check first shard query has filter
        first_call = mock_s3v.query_vectors.call_args_list[0]
        assert first_call[1]["filter"] == {"org_id": {"$eq": "org-specific"}}

    def test_tenant_query_has_no_org_filter(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Tenant index queries do NOT include org_id filter (physical isolation)."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="acme",
            owner_sub=None,
            top_k=10,
        )

        # Tenant call (index 2 after 2 shards) should NOT have filter
        tenant_call = mock_s3v.query_vectors.call_args_list[2]
        assert "filter" not in tenant_call[1]


# ---------------------------------------------------------------------------
# Cross-tenant isolation tests
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Vectors in tenant-A's index are NOT visible to tenant-B's queries."""

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def code_store(self, mock_s3v) -> S3VectorsCodeStore:
        return S3VectorsCodeStore(bucket_name="test-bucket", shard_count=2)

    def test_tenant_b_cannot_see_tenant_a_vectors(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """Querying with tenant_id=B never queries tenant-A's index."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="tenant-B",
            owner_sub=None,
            top_k=10,
        )

        # Should query: shard-0, shard-1, tenant-tenant-B (NOT tenant-tenant-A)
        queried_indexes = [call[1]["indexName"] for call in mock_s3v.query_vectors.call_args_list]
        assert "tenant-tenant-B" in queried_indexes
        assert "tenant-tenant-A" not in queried_indexes

    def test_personal_isolation_between_users(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """User A's personal index is not queried when user B queries."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query_scoped(
            query_vector=_make_embedding(1),
            org_id="org-a",
            tenant_id="acme",
            owner_sub="user-B",
            top_k=10,
        )

        queried_indexes = [call[1]["indexName"] for call in mock_s3v.query_vectors.call_args_list]
        assert "personal-user-B" in queried_indexes
        assert "personal-user-A" not in queried_indexes


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Existing unscoped put_vectors continues to work unchanged."""

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def code_store(self, mock_s3v) -> S3VectorsCodeStore:
        return S3VectorsCodeStore(bucket_name="test-bucket", shard_count=4)

    def test_unscoped_put_vectors_unchanged(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Legacy put_vectors still routes to code-shard-{N}."""
        vectors = [{"key": "v1", "embedding": _make_embedding(1), "metadata": {}}]
        code_store.put_vectors(vectors, org_id="org-legacy")

        call_kwargs = mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["indexName"].startswith("code-shard-")

    def test_unscoped_query_unchanged(self, code_store: S3VectorsCodeStore, mock_s3v) -> None:
        """Legacy query() still scatter-gathers across shared shards only."""
        mock_s3v.query_vectors.return_value = {"vectors": []}

        code_store.query(_make_embedding(1), org_id="org-legacy", top_k=10)

        # Should query all 4 shards
        assert mock_s3v.query_vectors.call_count == 4
        queried_indexes = [call[1]["indexName"] for call in mock_s3v.query_vectors.call_args_list]
        assert all(idx.startswith("code-shard-") for idx in queried_indexes)


# ---------------------------------------------------------------------------
# Wiki store scope-aware routing tests
# ---------------------------------------------------------------------------


@dataclass
class FakeS3Writer:
    """In-memory S3 writer for testing."""

    _objects: dict[str, str] = field(default_factory=dict)

    def put_object(self, bucket: str, key: str, body: str) -> bool:
        self._objects[f"{bucket}/{key}"] = body
        return True


@dataclass
class FakeEmbeddingClient:
    """Deterministic embedding client for testing."""

    dimension: int = 4

    def embed(self, text: str) -> list[float]:
        import hashlib

        h = hashlib.md5(text.encode(), usedforsecurity=False).digest()  # nosec B324
        vec = [b / 255.0 for b in h[: self.dimension]]
        mag = sum(x * x for x in vec) ** 0.5
        return [x / mag for x in vec] if mag > 0 else vec


class TestWikiStoreScoped:
    """store_wiki routes vectors to scope-appropriate index."""

    def test_shared_visibility_routes_to_shard(self) -> None:
        """Default (shared) visibility routes to code-shard-{N}."""
        vs = FakeVectorStore()
        result = store_wiki(
            wiki_text="## Test\n\nContent here.",
            org_repo="org/repo",
            org_id="org-test",
            allowed_principals=["*"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=FakeEmbeddingClient(),
            s3_bucket="bucket",
            visibility="shared",
        )

        assert result.vectors_success is True
        # Vectors should be in a code-shard-* index
        index_names = vs.list_indexes()
        assert len(index_names) == 1
        assert index_names[0].startswith("code-shard-")

    def test_tenant_visibility_routes_to_tenant_index(self) -> None:
        """Tenant visibility routes vectors to tenant-{id} index."""
        vs = FakeVectorStore()
        result = store_wiki(
            wiki_text="## Tenant Wiki\n\nTenant-specific content.",
            org_repo="org/repo",
            org_id="org-test",
            allowed_principals=["team:backend"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=FakeEmbeddingClient(),
            s3_bucket="bucket",
            visibility="tenant",
            tenant_id="acme-corp",
        )

        assert result.vectors_success is True
        index_names = vs.list_indexes()
        assert "tenant-acme-corp" in index_names

    def test_personal_visibility_routes_to_personal_index(self) -> None:
        """Personal visibility routes vectors to personal-{sub} index."""
        vs = FakeVectorStore()
        result = store_wiki(
            wiki_text="## My Notes\n\nPersonal wiki content.",
            org_repo="org/repo",
            org_id="org-test",
            allowed_principals=[],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=FakeEmbeddingClient(),
            s3_bucket="bucket",
            visibility="personal",
            owner_sub="user-abc-123",
        )

        assert result.vectors_success is True
        index_names = vs.list_indexes()
        assert "personal-user-abc-123" in index_names

    def test_tenant_without_id_falls_back_to_shared(self) -> None:
        """Tenant visibility without tenant_id falls back to shared shard."""
        vs = FakeVectorStore()
        result = store_wiki(
            wiki_text="## Fallback\n\nShould go to shared.",
            org_repo="org/repo",
            org_id="org-test",
            allowed_principals=["*"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=FakeEmbeddingClient(),
            s3_bucket="bucket",
            visibility="tenant",
            tenant_id=None,  # Missing → fallback
        )

        assert result.vectors_success is True
        index_names = vs.list_indexes()
        assert len(index_names) == 1
        assert index_names[0].startswith("code-shard-")

    def test_existing_tests_unaffected_by_default(self) -> None:
        """store_wiki without visibility param defaults to shared (backward compat)."""
        vs = FakeVectorStore()
        result = store_wiki(
            wiki_text="## Legacy\n\nExisting behavior preserved.",
            org_repo="org/legacy",
            org_id="org-legacy",
            allowed_principals=["*"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=FakeEmbeddingClient(),
            s3_bucket="bucket",
        )

        assert result.vectors_success is True
        index_names = vs.list_indexes()
        assert all(name.startswith("code-shard-") for name in index_names)
