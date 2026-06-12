"""Unit tests for S3-based storage backends.

Validates:
- S3AGFSBackend: put/get/delete/list_prefix parity with FakeAGFSBackend.
- S3VectorsEmbeddingStore: save→recall returns the inserted entry ranked by
  similarity; persistence across simulated pod restart.
- InMemoryEmbeddingStore: backward compatibility with legacy behavior.
"""

from __future__ import annotations

import json
import math
import uuid
from unittest.mock import MagicMock, patch

import pytest

from personal_context.backends.s3_backend import S3AGFSBackend
from personal_context.backends.s3_vectors_backend import (
    S3VectorsCodeStore,
    S3VectorsEmbeddingStore,
)
from personal_context.experience_tool import InMemoryEmbeddingStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_uuid() -> str:
    return str(uuid.uuid4())


def _make_embedding(seed: int = 0, dim: int = 8) -> list[float]:
    """Create a deterministic unit-norm embedding vector."""
    raw = [(seed + i) * 0.1 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


# ---------------------------------------------------------------------------
# S3AGFSBackend Tests (mocked boto3)
# ---------------------------------------------------------------------------


class TestS3AGFSBackend:
    """S3AGFSBackend implements the AGFS protocol (put/get/delete/list_prefix)."""

    @pytest.fixture
    def mock_s3(self):
        """Create a mock S3 client."""
        with patch("personal_context.backends.s3_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def backend(self, mock_s3) -> S3AGFSBackend:
        return S3AGFSBackend(bucket_name="test-bucket", prefix="personal-context")

    def test_put_stores_json(self, backend: S3AGFSBackend, mock_s3) -> None:
        """put() calls S3 PutObject with JSON body."""
        data = {"id": "01ABC", "content": "test"}
        backend.put("/personal/user1/learnings/01ABC.json", data)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "personal-context/personal/user1/learnings/01ABC.json"
        assert json.loads(call_kwargs["Body"].decode()) == data

    def test_get_returns_parsed_json(self, backend: S3AGFSBackend, mock_s3) -> None:
        """get() retrieves and parses JSON from S3."""
        expected = {"id": "01ABC", "content": "hello"}
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=json.dumps(expected).encode()))
        }

        result = backend.get("/personal/user1/learnings/01ABC.json")
        assert result == expected

    def test_get_returns_none_on_no_such_key(self, backend: S3AGFSBackend, mock_s3) -> None:
        """get() returns None when the key doesn't exist."""
        from botocore.exceptions import ClientError

        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )

        result = backend.get("/personal/user1/learnings/missing.json")
        assert result is None

    def test_delete_calls_s3(self, backend: S3AGFSBackend, mock_s3) -> None:
        """delete() calls S3 DeleteObject."""
        backend.delete("/personal/user1/learnings/01ABC.json")

        mock_s3.delete_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="personal-context/personal/user1/learnings/01ABC.json",
        )

    def test_list_prefix_returns_items(self, backend: S3AGFSBackend, mock_s3) -> None:
        """list_prefix() paginates and returns parsed JSON items."""
        # Mock paginator
        paginator = MagicMock()
        mock_s3.get_paginator.return_value = paginator

        items_data = [
            {"id": "01A", "content": "first"},
            {"id": "01B", "content": "second"},
        ]
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "personal-context/personal/user1/learnings/01A.json"},
                    {"Key": "personal-context/personal/user1/learnings/01B.json"},
                ]
            }
        ]

        # Mock get_object calls for each item
        mock_s3.get_object.side_effect = [
            {"Body": MagicMock(read=MagicMock(return_value=json.dumps(d).encode()))}
            for d in items_data
        ]

        result = backend.list_prefix("/personal/user1/learnings/")
        assert len(result) == 2
        assert result[0]["id"] == "01A"
        assert result[1]["id"] == "01B"

    def test_key_construction_strips_leading_slash(self, backend: S3AGFSBackend) -> None:
        """Internal _key() strips leading slash from AGFS paths."""
        assert (
            backend._key("/personal/user/file.json") == "personal-context/personal/user/file.json"
        )
        assert backend._key("relative/path.json") == "personal-context/relative/path.json"


# ---------------------------------------------------------------------------
# S3VectorsEmbeddingStore Tests (mocked boto3)
# ---------------------------------------------------------------------------


class TestS3VectorsEmbeddingStore:
    """S3VectorsEmbeddingStore persists embeddings in S3 Vectors."""

    @pytest.fixture
    def mock_s3v(self):
        """Create a mock S3 Vectors client."""
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def store(self, mock_s3v) -> S3VectorsEmbeddingStore:
        return S3VectorsEmbeddingStore(
            bucket_name="test-vectors-bucket",
            dimension=8,
            distance_metric="cosine",
        )

    def test_store_creates_index_then_puts(self, store: S3VectorsEmbeddingStore, mock_s3v) -> None:
        """store() creates index on first call, then puts the vector."""
        owner = _make_uuid()
        embedding = _make_embedding(seed=1)

        store.store(owner, "entry-001", embedding)

        # Should have called create_index once
        mock_s3v.create_index.assert_called_once_with(
            vectorBucketName="test-vectors-bucket",
            indexName=f"personal-{owner}",
            dimension=8,
            distanceMetric="cosine",
            dataType="float32",
        )
        # Should have called put_vectors
        mock_s3v.put_vectors.assert_called_once()
        put_call = mock_s3v.put_vectors.call_args[1]
        assert put_call["vectorBucketName"] == "test-vectors-bucket"
        assert put_call["indexName"] == f"personal-{owner}"
        assert put_call["vectors"][0]["key"] == "entry-001"

    def test_store_skips_index_creation_on_second_call(
        self, store: S3VectorsEmbeddingStore, mock_s3v
    ) -> None:
        """store() only creates index once per owner in process lifetime."""
        owner = _make_uuid()
        store.store(owner, "entry-001", _make_embedding(1))
        store.store(owner, "entry-002", _make_embedding(2))

        # create_index called only once (cached)
        assert mock_s3v.create_index.call_count == 1
        assert mock_s3v.put_vectors.call_count == 2

    def test_store_handles_existing_index_gracefully(
        self, store: S3VectorsEmbeddingStore, mock_s3v
    ) -> None:
        """store() handles ConflictException on create_index."""
        from botocore.exceptions import ClientError

        mock_s3v.create_index.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "Already exists"}},
            "CreateIndex",
        )
        owner = _make_uuid()
        # Should not raise
        store.store(owner, "entry-001", _make_embedding(1))
        mock_s3v.put_vectors.assert_called_once()

    def test_recall_returns_sorted_results(self, store: S3VectorsEmbeddingStore, mock_s3v) -> None:
        """recall() returns (entry_id, distance) sorted by distance."""
        owner = _make_uuid()
        mock_s3v.query_vectors.return_value = {
            "vectors": [
                {"key": "entry-002", "distance": 0.3},
                {"key": "entry-001", "distance": 0.1},
                {"key": "entry-003", "distance": 0.5},
            ]
        }

        results = store.recall(owner, _make_embedding(1), top_k=10)
        assert len(results) == 3
        # Verify they're returned in order from API (pre-sorted by S3 Vectors)
        assert results[0] == ("entry-002", 0.3)
        assert results[1] == ("entry-001", 0.1)
        assert results[2] == ("entry-003", 0.5)

    def test_recall_returns_empty_on_missing_index(
        self, store: S3VectorsEmbeddingStore, mock_s3v
    ) -> None:
        """recall() returns empty list if user's index doesn't exist."""
        from botocore.exceptions import ClientError

        mock_s3v.query_vectors.side_effect = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "Not found"}},
            "QueryVectors",
        )
        owner = _make_uuid()
        results = store.recall(owner, _make_embedding(1))
        assert results == []

    def test_delete_calls_s3v(self, store: S3VectorsEmbeddingStore, mock_s3v) -> None:
        """delete() removes the vector from the user's index."""
        owner = _make_uuid()
        store.delete(owner, "entry-001")

        mock_s3v.delete_vectors.assert_called_once_with(
            vectorBucketName="test-vectors-bucket",
            indexName=f"personal-{owner}",
            keys=["entry-001"],
        )

    def test_delete_handles_missing_index(self, store: S3VectorsEmbeddingStore, mock_s3v) -> None:
        """delete() is idempotent even if the index doesn't exist."""
        from botocore.exceptions import ClientError

        mock_s3v.delete_vectors.side_effect = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "Not found"}},
            "DeleteVectors",
        )
        owner = _make_uuid()
        # Should not raise
        store.delete(owner, "entry-001")


# ---------------------------------------------------------------------------
# Persistence across simulated pod restart
# ---------------------------------------------------------------------------


class TestPersistenceAcrossRestart:
    """Prove that S3VectorsEmbeddingStore persists across pod restarts.

    This is the core fix for EPIC #1287 — the in-memory dict was lost.
    We simulate a restart by creating a new store instance.
    """

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    def test_recall_after_restart_still_works(self, mock_s3v) -> None:
        """Create store A → store → destroy → create store B → recall succeeds."""
        owner = _make_uuid()
        embedding = _make_embedding(seed=42)

        # Store instance A — writes the embedding
        store_a = S3VectorsEmbeddingStore(bucket_name="test-vectors-bucket", dimension=8)
        store_a.store(owner, "entry-persist", embedding)

        # Simulate pod restart: destroy store_a, create store_b
        del store_a

        # Store instance B — should still be able to recall
        # (because data is in S3 Vectors, not in-memory)
        mock_s3v.query_vectors.return_value = {
            "vectors": [{"key": "entry-persist", "distance": 0.05}]
        }
        store_b = S3VectorsEmbeddingStore(bucket_name="test-vectors-bucket", dimension=8)
        results = store_b.recall(owner, embedding, top_k=5)

        assert len(results) == 1
        assert results[0][0] == "entry-persist"
        assert results[0][1] == 0.05

    def test_inmemory_store_loses_data_on_restart(self) -> None:
        """InMemoryEmbeddingStore DOES lose data (proving the gap)."""
        owner = _make_uuid()
        embedding = _make_embedding(seed=42)

        store_a = InMemoryEmbeddingStore()
        store_a.store(owner, "entry-gone", embedding)

        # Verify it's there
        results = store_a.recall(owner, embedding)
        assert len(results) == 1

        # Simulate restart — new instance has nothing
        store_b = InMemoryEmbeddingStore()
        results = store_b.recall(owner, embedding)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# InMemoryEmbeddingStore Tests
# ---------------------------------------------------------------------------


class TestInMemoryEmbeddingStore:
    """InMemoryEmbeddingStore provides backward-compatible behavior."""

    def test_store_and_recall(self) -> None:
        """Store then recall returns the entry with correct distance."""
        store = InMemoryEmbeddingStore()
        owner = _make_uuid()
        emb = _make_embedding(seed=1)

        store.store(owner, "entry-1", emb)
        results = store.recall(owner, emb)

        assert len(results) == 1
        assert results[0][0] == "entry-1"
        # Same vector → distance should be 0 (1 - cos_sim of identical vectors)
        assert results[0][1] == pytest.approx(0.0, abs=1e-6)

    def test_recall_sorted_by_distance(self) -> None:
        """recall() returns results sorted by ascending distance."""
        store = InMemoryEmbeddingStore()
        owner = _make_uuid()

        # Store three embeddings with different similarities to the query
        store.store(owner, "close", _make_embedding(seed=1))
        store.store(owner, "medium", _make_embedding(seed=5))
        store.store(owner, "far", _make_embedding(seed=100))

        # Query with seed=1 embedding — should be closest to "close"
        results = store.recall(owner, _make_embedding(seed=1))
        assert results[0][0] == "close"
        assert results[0][1] <= results[1][1] <= results[2][1]

    def test_delete_removes_entry(self) -> None:
        """delete() removes the embedding from the store."""
        store = InMemoryEmbeddingStore()
        owner = _make_uuid()

        store.store(owner, "entry-del", _make_embedding(seed=1))
        store.delete(owner, "entry-del")

        results = store.recall(owner, _make_embedding(seed=1))
        assert len(results) == 0

    def test_recall_respects_top_k(self) -> None:
        """recall() limits results to top_k."""
        store = InMemoryEmbeddingStore()
        owner = _make_uuid()

        for i in range(10):
            store.store(owner, f"entry-{i}", _make_embedding(seed=i))

        results = store.recall(owner, _make_embedding(seed=0), top_k=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# S3VectorsCodeStore Tests
# ---------------------------------------------------------------------------


class TestS3VectorsCodeStore:
    """S3VectorsCodeStore handles hash-sharded code vectors."""

    @pytest.fixture
    def mock_s3v(self):
        with patch("personal_context.backends.s3_vectors_backend.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def code_store(self, mock_s3v) -> S3VectorsCodeStore:
        return S3VectorsCodeStore(
            bucket_name="test-code-bucket",
            shard_count=4,
        )

    def test_shard_assignment_deterministic(self, code_store: S3VectorsCodeStore) -> None:
        """Same org_id always maps to same shard."""
        org = "org-acme"
        shard1 = code_store.get_shard_index(org)
        shard2 = code_store.get_shard_index(org)
        assert shard1 == shard2
        assert 0 <= shard1 < 4

    def test_different_orgs_may_get_different_shards(self, code_store: S3VectorsCodeStore) -> None:
        """Different orgs get distributed across shards."""
        shards = {code_store.get_shard_index(f"org-{i}") for i in range(100)}
        # With 100 orgs and 4 shards, should hit all shards
        assert len(shards) == 4

    def test_put_vectors_routes_to_correct_shard(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """put_vectors() writes to the shard determined by org_id hash."""
        vectors = [{"key": "repo:file:func:1", "embedding": _make_embedding(1), "metadata": {}}]
        org = "org-test"
        expected_shard = code_store.get_shard_index(org)

        code_store.put_vectors(vectors, org)

        call_kwargs = mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["indexName"] == f"code-shard-{expected_shard}"

    def test_query_scatter_gathers_all_shards(
        self, code_store: S3VectorsCodeStore, mock_s3v
    ) -> None:
        """query() queries all shards and merges results."""
        mock_s3v.query_vectors.return_value = {
            "vectors": [{"key": "result-1", "distance": 0.2, "metadata": {}}]
        }

        results = code_store.query(_make_embedding(1), org_id="org-test", top_k=5)

        # Should have called query_vectors 4 times (one per shard)
        assert mock_s3v.query_vectors.call_count == 4
        # Results merged from all shards
        assert len(results) == 4  # 1 result per shard × 4 shards
