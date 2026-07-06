"""Unit tests for the embed_vectors stage in the ingestion pipeline (#2297).

Covers code_embedder.py — source-code embedding into S3 Vectors:
- Config: embed_vectors_enabled default + env-var bool parsing; embed_model default
- discover_source_files: extension filtering + skip-dir pruning + cap
- chunk_file_text: line-boundary chunking, oversized-line split, empty input
- embed_code_repo: happy path (vectors written + verified), tenant routing,
  fail-open on unconfigured bucket, fail-open on S3 Vectors NotFound (#2486
  pending), verify failure, no-source

All infra (boto3 s3vectors, LiteLLM HTTP) is mocked — no live infra required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
_INGESTION_PATH = str(Path(__file__).parent.parent.parent / "images" / "ingestion")
sys.path.insert(0, _INGESTION_PATH)

import code_embedder  # noqa: E402
from code_embedder import (  # noqa: E402
    chunk_file_text,
    discover_source_files,
    embed_code_repo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeEmbeddingClient:
    """Deterministic embedding client — returns a fixed 1024-dim vector."""

    def __init__(self):
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1] * code_embedder.EMBEDDING_DIMENSION


class _FakeVectorWriter:
    """In-memory S3 Vectors writer capturing put_vectors + verifying reads."""

    def __init__(self, verify_count: int = 1):
        self.puts: list[tuple[str, list[dict]]] = []
        self._verify_count = verify_count

    def put_vectors(self, index_name: str, vectors: list[dict]) -> None:
        self.puts.append((index_name, vectors))

    def count_repo_vectors(self, index_name, query_vector, org_repo) -> int:
        return self._verify_count


@pytest.fixture
def repo_tree(tmp_path):
    """A small clone with source files, a vendored dir, and a binary/unknown file."""
    root = tmp_path / "org" / "repo"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "main.py").write_text("def hello():\n    return 'world'\n")
    (root / "util.ts").write_text("export const x = 1;\n")
    (root / "README.md").write_text("# Title\n\nSome docs.\n")
    (root / "data.bin").write_text("not-source")
    node = root / "node_modules" / "pkg"
    node.mkdir(parents=True)
    (node / "index.js").write_text("module.exports = {};\n")
    return str(root)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestEmbedVectorsConfig:
    def test_default_enabled(self):
        from config import Settings

        with patch.dict(os.environ, {}, clear=False):
            assert Settings().embed_vectors_enabled is True

    def test_bool_string_false(self):
        from config import Settings

        with patch.dict(os.environ, {"EMBED_VECTORS_ENABLED": "false"}):
            assert Settings().embed_vectors_enabled is False

    def test_bool_string_zero(self):
        from config import Settings

        with patch.dict(os.environ, {"EMBED_VECTORS_ENABLED": "0"}):
            assert Settings().embed_vectors_enabled is False

    def test_default_model(self):
        from config import Settings

        assert Settings().embed_model == "bedrock/amazon.titan-embed-text-v2:0"


# ---------------------------------------------------------------------------
# discover_source_files
# ---------------------------------------------------------------------------


class TestDiscoverSourceFiles:
    def test_finds_source_skips_vendor_and_binary(self, repo_tree):
        found = {p.name for p in discover_source_files(repo_tree)}
        assert "main.py" in found
        assert "util.ts" in found
        assert "README.md" in found
        # node_modules pruned, unknown extension skipped
        assert "index.js" not in found
        assert "data.bin" not in found

    def test_respects_max_files_cap(self, tmp_path):
        root = tmp_path / "big"
        root.mkdir()
        for i in range(10):
            (root / f"f{i}.py").write_text("x = 1\n")
        found = discover_source_files(str(root), max_files=3)
        assert len(found) == 3


# ---------------------------------------------------------------------------
# chunk_file_text
# ---------------------------------------------------------------------------


class TestChunkFileText:
    def test_empty(self):
        assert chunk_file_text("") == []
        assert chunk_file_text("   \n  ") == []

    def test_small_file_single_chunk(self):
        chunks = chunk_file_text("line1\nline2\n")
        assert chunks == ["line1\nline2\n"]

    def test_splits_on_char_budget(self):
        text = "\n".join("x" * 100 for _ in range(100))  # ~10k chars
        chunks = chunk_file_text(text, max_chunk_chars=1000)
        assert len(chunks) > 1
        assert all(len(c) <= 1000 for c in chunks)

    def test_oversized_single_line_is_hard_split(self):
        text = "a" * 5000
        chunks = chunk_file_text(text, max_chunk_chars=1000)
        assert len(chunks) == 5
        assert all(len(c) <= 1000 for c in chunks)


# ---------------------------------------------------------------------------
# embed_code_repo
# ---------------------------------------------------------------------------


class TestEmbedCodeRepo:
    def test_bucket_not_configured_skips(self, repo_tree):
        """Empty bucket name → clean skip, no embedding attempted."""
        client = _FakeEmbeddingClient()
        result = embed_code_repo(repo_tree, "org/repo", bucket_name="", embedding_client=client)
        assert result["status"] == "bucket_not_configured"
        assert result["vectors"] == 0
        assert client.calls == []  # never embedded

    def test_no_source_files(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = embed_code_repo(
            str(empty),
            "org/repo",
            bucket_name="b",
            embedding_client=_FakeEmbeddingClient(),
            vector_writer=_FakeVectorWriter(),
        )
        assert result["status"] == "no_source"

    def test_happy_path_writes_and_verifies(self, repo_tree):
        client = _FakeEmbeddingClient()
        writer = _FakeVectorWriter(verify_count=1)
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="b",
            embedding_client=client,
            vector_writer=writer,
        )
        assert result["status"] == "complete"
        assert result["vectors"] > 0
        assert result["files"] == 3  # main.py, util.ts, README.md
        # Shared visibility → hash-sharded index
        assert result["index"].startswith("code-shard-")
        assert len(writer.puts) == 1
        index_name, vectors = writer.puts[0]
        # Vector key + metadata shape
        v = vectors[0]
        assert v["key"].startswith("code:org/repo:")
        assert v["metadata"]["repo"] == "org/repo"
        assert v["metadata"]["org_id"] == "org"
        assert v["metadata"]["source_type"] == "code"

    def test_tenant_routing(self, repo_tree):
        writer = _FakeVectorWriter(verify_count=1)
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="b",
            embedding_client=_FakeEmbeddingClient(),
            vector_writer=writer,
            visibility="tenant",
            tenant_id="acme",
        )
        assert result["status"] == "complete"
        assert result["index"] == "tenant-acme"

    def test_verify_failure(self, repo_tree):
        """Write succeeds but read-back finds nothing → verify_failed."""
        writer = _FakeVectorWriter(verify_count=0)
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="b",
            embedding_client=_FakeEmbeddingClient(),
            vector_writer=writer,
        )
        assert result["status"] == "verify_failed"
        assert result["vectors"] > 0

    def test_bucket_missing_notfound_skips(self, repo_tree):
        """S3 Vectors NotFoundException (bucket pending #2486) → clean skip."""
        from botocore.exceptions import ClientError

        writer = MagicMock()
        writer.put_vectors.side_effect = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "no bucket"}},
            "PutVectors",
        )
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="adp-dev-code-vectors-123",
            embedding_client=_FakeEmbeddingClient(),
            vector_writer=writer,
        )
        assert result["status"] == "bucket_missing"
        assert result["vectors"] == 0

    def test_other_clienterror_is_embed_failed(self, repo_tree):
        from botocore.exceptions import ClientError

        writer = MagicMock()
        writer.put_vectors.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "PutVectors",
        )
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="b",
            embedding_client=_FakeEmbeddingClient(),
            vector_writer=writer,
        )
        assert result["status"] == "embed_failed"

    def test_embedding_endpoint_failure(self, repo_tree):
        client = MagicMock()
        client.embed.side_effect = RuntimeError("proxy 503")
        result = embed_code_repo(
            repo_tree,
            "org/repo",
            bucket_name="b",
            embedding_client=client,
            vector_writer=_FakeVectorWriter(),
        )
        assert result["status"] == "embed_failed"
        assert "proxy 503" in result["error"]
