"""
Unit tests for the wiki dual-sink module (wiki_store.py).

Tests wiki chunking, S3 write, embedding, and catalog update logic.
Validates the design in docs/design-notes/1382-deepwiki-s3-dual-sink.md.

Issue #1382.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add ingestion source to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))

from wiki_store import (  # noqa: E402
    chunk_wiki_by_sections,
    get_shard_index,
    store_code_index_to_s3,
    store_wiki,
)

from .conftest import FakeVectorStore  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

SAMPLE_WIKI = """\
# Repository Wiki

This is the preamble with overview info.

## Architecture

The system uses a layered architecture with clear separation of concerns.
The API layer handles HTTP requests and delegates to service classes.

### Components

- **Gateway**: Routes incoming requests
- **Service Layer**: Business logic
- **Data Layer**: Database access

## Authentication

Authentication uses JWT tokens issued by the auth service.
The token contains the user's principal ID and org membership.

## Rate Limiting

This module implements token-bucket throttling to prevent abuse.
Each tenant gets a configurable token bucket with refill rate.

## Deployment

Deployed via EKS with Helm charts. CI/CD uses GitHub Actions.
"""

SHORT_WIKI = "Just a short note."

NO_HEADINGS_WIKI = """\
This is a wiki without any markdown headings.
It just has paragraphs of text.

Another paragraph here with some content.

And a third paragraph for good measure.
"""

LONG_SECTION_WIKI = """\
## Very Long Section

""" + "\n\n".join([f"Paragraph {i}: " + "x" * 500 for i in range(20)])


@dataclass
class FakeS3Writer:
    """In-memory S3 writer for testing."""

    _objects: dict[str, str] = field(default_factory=dict)
    _fail: bool = False

    def put_object(self, bucket: str, key: str, body: str) -> bool:
        if self._fail:
            return False
        self._objects[f"{bucket}/{key}"] = body
        return True


@dataclass
class FakeEmbeddingClient:
    """Deterministic embedding client for testing.

    Produces a fixed-length vector based on text hash for reproducibility.
    """

    dimension: int = 4
    _calls: list[str] = field(default_factory=list)

    def embed(self, text: str) -> list[float]:
        self._calls.append(text)
        # Deterministic "embedding" from text hash
        import hashlib

        h = hashlib.md5(text.encode()).digest()
        vec = [b / 255.0 for b in h[: self.dimension]]
        # Normalize
        mag = sum(x * x for x in vec) ** 0.5
        return [x / mag for x in vec] if mag > 0 else vec


@dataclass
class FakeCatalogClient:
    """In-memory catalog client for testing."""

    _updates: list[dict[str, Any]] = field(default_factory=list)

    def update_wiki_status(self, repo_name: str, wiki_status: str, wiki_s3_key: str | None) -> bool:
        self._updates.append(
            {"repo_name": repo_name, "wiki_status": wiki_status, "wiki_s3_key": wiki_s3_key}
        )
        return True


# ---------------------------------------------------------------------------
# Chunking tests
# ---------------------------------------------------------------------------


class TestChunkWikiByHeadings:
    """Wiki chunking splits on ## / ### headings correctly."""

    def test_splits_on_headings(self):
        """Sections are split on ## / ### headings."""
        chunks = chunk_wiki_by_sections(SAMPLE_WIKI)

        # Should have: preamble + Architecture + Components + Authentication +
        #              Rate Limiting + Deployment
        # But Components is under Architecture (###), so it's a separate chunk
        headings = [c.heading for c in chunks]

        # First chunk is preamble (heading is "" or "# Repository Wiki")
        assert chunks[0].heading == ""  # Preamble
        assert "## Architecture" in headings
        assert "### Components" in headings
        assert "## Authentication" in headings
        assert "## Rate Limiting" in headings
        assert "## Deployment" in headings

    def test_preserves_heading_in_text(self):
        """Each chunk includes its heading text for embedding context."""
        chunks = chunk_wiki_by_sections(SAMPLE_WIKI)

        # Find the Architecture chunk
        arch_chunks = [c for c in chunks if c.heading == "## Architecture"]
        assert len(arch_chunks) == 1
        assert "## Architecture" in arch_chunks[0].text
        assert "layered architecture" in arch_chunks[0].text

    def test_long_section_splits_on_paragraphs(self):
        """Sections exceeding max_chunk_chars are sub-split on paragraph breaks."""
        chunks = chunk_wiki_by_sections(LONG_SECTION_WIKI, max_chunk_chars=3000)

        # Should produce multiple chunks for the long section
        assert len(chunks) > 1
        # All sub-chunks inherit the same heading
        assert all(c.heading == "## Very Long Section" for c in chunks)

    def test_short_section_merged(self):
        """Tiny sections (<min_chunk_chars) merge with the next section."""
        tiny_wiki = "## A\n\nHi\n\n## B\n\nThis is the real content of section B with details."
        chunks = chunk_wiki_by_sections(tiny_wiki, min_chunk_chars=50)

        # "Hi" (3 chars) is below min_chunk_chars, so it merges with B
        assert len(chunks) == 1
        assert "Hi" in chunks[0].text
        assert "real content" in chunks[0].text

    def test_no_headings_single_chunk(self):
        """Flat markdown without headings produces a single chunk."""
        chunks = chunk_wiki_by_sections(NO_HEADINGS_WIKI)

        assert len(chunks) == 1
        assert chunks[0].heading == ""
        assert "without any markdown headings" in chunks[0].text

    def test_empty_text_returns_empty(self):
        """Empty or whitespace-only text returns no chunks."""
        assert chunk_wiki_by_sections("") == []
        assert chunk_wiki_by_sections("   \n  ") == []

    def test_chunk_indices_sequential(self):
        """Chunk indices are sequential starting from 0."""
        chunks = chunk_wiki_by_sections(SAMPLE_WIKI)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_idx == i

    def test_code_blocks_not_split(self):
        """Code blocks within a section are kept intact."""
        wiki_with_code = """\
## Example

Here is some code:

```python
def hello():
    print("world")
    return 42
```

And more text after the code block.
"""
        chunks = chunk_wiki_by_sections(wiki_with_code)

        # The code block should be in one chunk
        code_chunk = [c for c in chunks if "```python" in c.text]
        assert len(code_chunk) == 1
        assert 'print("world")' in code_chunk[0].text
        assert "```" in code_chunk[0].text


# ---------------------------------------------------------------------------
# Shard assignment tests
# ---------------------------------------------------------------------------


class TestShardAssignment:
    """Shard assignment is deterministic and well-distributed."""

    def test_deterministic(self):
        """Same org_id always maps to same shard."""
        assert get_shard_index("org-123") == get_shard_index("org-123")
        assert get_shard_index("org-456") == get_shard_index("org-456")

    def test_distributes_across_shards(self):
        """Different org_ids distribute across available shards."""
        shards_used = set()
        for i in range(100):
            shard = get_shard_index(f"org-{i}", shard_count=4)
            shards_used.add(shard)
            assert 0 <= shard < 4

        # With 100 orgs and 4 shards, all shards should be used
        assert len(shards_used) == 4

    def test_respects_shard_count(self):
        """Shard index is always within [0, shard_count)."""
        for i in range(50):
            assert 0 <= get_shard_index(f"org-{i}", shard_count=8) < 8


# ---------------------------------------------------------------------------
# Store wiki tests (integration of all components)
# ---------------------------------------------------------------------------


class TestStoreWiki:
    """store_wiki() writes to S3 and embeds to vector store."""

    def test_writes_s3_object(self):
        """S3 PutObject is called with correct key and body."""
        s3 = FakeS3Writer()
        result = store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=["team:backend"],
            s3_writer=s3,
            s3_bucket="test-bucket",
        )

        assert result.s3_success is True
        assert result.s3_key == "content/wikis/aws-e-adp-wiki.md"
        assert "test-bucket/content/wikis/aws-e-adp-wiki.md" in s3._objects
        assert s3._objects["test-bucket/content/wikis/aws-e-adp-wiki.md"] == SAMPLE_WIKI

    def test_s3_write_failure_reported(self):
        """S3 write failure is captured in result."""
        s3 = FakeS3Writer(_fail=True)
        result = store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=[],
            s3_writer=s3,
            s3_bucket="test-bucket",
        )

        assert result.s3_success is False
        assert result.s3_key is None

    def test_embeds_chunks_to_vector_store(self):
        """Each wiki chunk is embedded and written to vector store."""
        vs = FakeVectorStore()
        emb = FakeEmbeddingClient()
        result = store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=["team:backend"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=emb,
            s3_bucket="test-bucket",
        )

        assert result.vectors_success is True
        assert result.vectors_written > 0

        # Check that embedding client was called for each chunk
        chunks = chunk_wiki_by_sections(SAMPLE_WIKI)
        assert len(emb._calls) == len(chunks)

    def test_metadata_correct_on_vectors(self):
        """Vector metadata has repo, org_id, source_type='wiki'."""
        vs = FakeVectorStore()
        emb = FakeEmbeddingClient()
        store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=["team:backend"],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=emb,
            s3_bucket="test-bucket",
        )

        # Get vectors from the shard
        shard_idx = get_shard_index("org-aws-e")
        index_name = f"code-shard-{shard_idx}"
        vectors = vs._indexes.get(index_name, [])

        assert len(vectors) > 0
        for v in vectors:
            meta = v["metadata"]
            assert meta["repo"] == "aws-e/adp"
            assert meta["org_id"] == "org-aws-e"
            assert meta["source_type"] == "wiki"
            assert meta["language"] == "en"
            assert "wiki_s3_key" in meta

    def test_catalog_updated_on_success(self):
        """Catalog update records wiki_status and s3_key."""
        catalog = FakeCatalogClient()
        result = store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=[],
            s3_writer=FakeS3Writer(),
            catalog_client=catalog,
            s3_bucket="test-bucket",
        )

        assert result.catalog_updated is True
        assert len(catalog._updates) == 1
        assert catalog._updates[0]["repo_name"] == "aws-e/adp"
        assert catalog._updates[0]["wiki_status"] == "complete"
        assert catalog._updates[0]["wiki_s3_key"] == "content/wikis/aws-e-adp-wiki.md"

    def test_no_s3_writer_skips_gracefully(self):
        """Without S3 writer, wiki store skips S3 write without error."""
        result = store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=[],
        )

        assert result.s3_success is False
        assert result.error is None  # Not an error — just skipped

    def test_vector_key_format(self):
        """Vector keys follow the wiki:{repo}:{heading}:{idx} format."""
        vs = FakeVectorStore()
        emb = FakeEmbeddingClient()
        store_wiki(
            wiki_text=SAMPLE_WIKI,
            org_repo="aws-e/adp",
            org_id="org-aws-e",
            allowed_principals=[],
            s3_writer=FakeS3Writer(),
            vector_store=vs,
            embedding_client=emb,
            s3_bucket="test-bucket",
        )

        shard_idx = get_shard_index("org-aws-e")
        index_name = f"code-shard-{shard_idx}"
        vectors = vs._indexes.get(index_name, [])

        for v in vectors:
            assert v["key"].startswith("wiki:aws-e/adp:")


# ---------------------------------------------------------------------------
# Code-index S3 store tests
# ---------------------------------------------------------------------------


class TestStoreCodeIndexToS3:
    """store_code_index_to_s3() writes markdown summary to S3."""

    def test_writes_code_index_md(self):
        """Code-index markdown is written to the correct S3 key."""
        s3 = FakeS3Writer()
        key = store_code_index_to_s3(
            code_index_md="# Code Index\n\n- func1\n- func2",
            org_repo="aws-e/adp",
            s3_writer=s3,
            s3_bucket="test-bucket",
        )

        assert key == "content/code-indexes/aws-e-adp-code-index.md"
        assert "test-bucket/content/code-indexes/aws-e-adp-code-index.md" in s3._objects

    def test_returns_none_on_failure(self):
        """Returns None when S3 write fails."""
        s3 = FakeS3Writer(_fail=True)
        key = store_code_index_to_s3(
            code_index_md="# Code Index",
            org_repo="aws-e/adp",
            s3_writer=s3,
            s3_bucket="test-bucket",
        )

        assert key is None

    def test_no_writer_returns_none(self):
        """Returns None without an S3 writer."""
        key = store_code_index_to_s3(
            code_index_md="# Code Index",
            org_repo="aws-e/adp",
        )

        assert key is None
