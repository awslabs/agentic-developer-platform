"""Wiki dual-sink: S3 (human browse) + S3 Vectors (semantic embeddings).

Replaces the OpenViking upload path for DeepWiki output and code-index markdown.
Two sinks:
  1. S3 PutObject — wiki markdown as a browsable artifact
  2. S3 Vectors — section-aware chunks embedded via LiteLLM/Titan for semantic search

Design: docs/design-notes/1382-deepwiki-s3-dual-sink.md
Depends on: #1348 (S3 Vectors), #1354 (Mountpoint), #1355 (catalog), #1356 (ACL)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("wiki-store")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WikiChunk:
    """A section-aware chunk of wiki text for embedding."""

    heading: str  # e.g., "## Architecture"
    text: str  # Full chunk text (includes heading)
    chunk_idx: int  # Sequential index within the wiki
    char_start: int  # Start position in original wiki text
    char_end: int  # End position in original wiki text


@dataclass
class WikiStoreResult:
    """Result of storing a wiki to S3 + S3 Vectors."""

    s3_key: str | None = None
    s3_success: bool = False
    vectors_written: int = 0
    vectors_success: bool = False
    catalog_updated: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Protocols (for dependency injection in tests)
# ---------------------------------------------------------------------------


class S3Writer(Protocol):
    """Protocol for S3 object writes."""

    def put_object(self, bucket: str, key: str, body: str) -> bool: ...


class EmbeddingClient(Protocol):
    """Protocol for text embedding generation."""

    def embed(self, text: str) -> list[float]: ...


class VectorStore(Protocol):
    """Protocol for vector storage (S3 Vectors or FakeVectorStore)."""

    def put_vectors(self, index_name: str, vectors: list[dict[str, Any]]) -> None: ...


class CatalogClient(Protocol):
    """Protocol for catalog updates (Postgres repositories table)."""

    def update_wiki_status(
        self, repo_name: str, wiki_status: str, wiki_s3_key: str | None
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Wiki chunking (section-aware)
# ---------------------------------------------------------------------------

# Regex matching markdown headings (##, ###, ####)
_HEADING_RE = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)


def chunk_wiki_by_sections(
    wiki_text: str,
    max_chunk_chars: int = 6000,
    min_chunk_chars: int = 50,
) -> list[WikiChunk]:
    """Split wiki markdown into section-aware chunks for embedding.

    Rules:
    1. Split on headings (##, ###, ####). Each heading starts a new chunk.
    2. Include the heading in its chunk (for embedding context).
    3. If a section exceeds max_chunk_chars, sub-split on paragraph breaks.
    4. Content before the first heading is chunk 0 ("preamble").
    5. Very short sections (<min_chunk_chars) are merged with the next section.

    Returns a list of WikiChunk ordered by position in the original text.
    """
    if not wiki_text or not wiki_text.strip():
        return []

    # Find all heading positions
    headings = list(_HEADING_RE.finditer(wiki_text))

    # Build raw sections (heading + body until next heading)
    raw_sections: list[tuple[str, str, int, int]] = []  # (heading, body, start, end)

    if not headings:
        # No headings — treat entire text as one section
        raw_sections.append(("", wiki_text.strip(), 0, len(wiki_text)))
    else:
        # Preamble (content before first heading)
        if headings[0].start() > 0:
            preamble = wiki_text[: headings[0].start()].strip()
            if preamble:
                raw_sections.append(("", preamble, 0, headings[0].start()))

        # Each heading + its body
        for i, match in enumerate(headings):
            heading_text = match.group(0)  # e.g., "## Architecture"
            start = match.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(wiki_text)
            body = wiki_text[start:end].strip()
            raw_sections.append((heading_text, body, start, end))

    # Merge short sections with the next one
    merged_sections: list[tuple[str, str, int, int]] = []
    i = 0
    while i < len(raw_sections):
        heading, body, start, end = raw_sections[i]
        # Merge forward if too short (but not the last section)
        while len(body) < min_chunk_chars and i + 1 < len(raw_sections):
            i += 1
            _, next_body, _, next_end = raw_sections[i]
            body = body + "\n\n" + next_body
            end = next_end
        merged_sections.append((heading, body, start, end))
        i += 1

    # Sub-split long sections on paragraph breaks
    chunks: list[WikiChunk] = []
    chunk_idx = 0

    for heading, body, start, end in merged_sections:
        if len(body) <= max_chunk_chars:
            chunks.append(
                WikiChunk(
                    heading=heading,
                    text=body,
                    chunk_idx=chunk_idx,
                    char_start=start,
                    char_end=end,
                )
            )
            chunk_idx += 1
        else:
            # Sub-split on paragraph breaks (\n\n)
            paragraphs = body.split("\n\n")
            current_text = ""
            current_start = start

            for para in paragraphs:
                if current_text and len(current_text) + len(para) + 2 > max_chunk_chars:
                    # Emit current chunk
                    chunks.append(
                        WikiChunk(
                            heading=heading,
                            text=current_text.strip(),
                            chunk_idx=chunk_idx,
                            char_start=current_start,
                            char_end=current_start + len(current_text),
                        )
                    )
                    chunk_idx += 1
                    current_text = para
                    current_start = current_start + len(current_text)
                else:
                    if current_text:
                        current_text += "\n\n" + para
                    else:
                        current_text = para

            # Emit final sub-chunk
            if current_text.strip():
                chunks.append(
                    WikiChunk(
                        heading=heading,
                        text=current_text.strip(),
                        chunk_idx=chunk_idx,
                        char_start=current_start,
                        char_end=end,
                    )
                )
                chunk_idx += 1

    return chunks


# ---------------------------------------------------------------------------
# Shard assignment (matches design-1348 §8.2)
# ---------------------------------------------------------------------------


def get_shard_index(org_id: str, shard_count: int = 4) -> int:
    """Deterministic shard assignment by org_id hash."""
    h = hashlib.sha256(org_id.encode()).digest()
    return int.from_bytes(h[:4], "big") % shard_count


# ---------------------------------------------------------------------------
# Store wiki to S3 + S3 Vectors
# ---------------------------------------------------------------------------


def _resolve_vector_index(
    visibility: str,
    org_id: str,
    tenant_id: str | None,
    owner_sub: str | None,
    shard_count: int,
) -> str:
    """Resolve the target vector index name based on scope visibility.

    Per-tenant isolation (Story 5, #1774):
    - "shared" → code-shard-{N} (hash-sharded by org_id)
    - "tenant" → tenant-{tenant_id}
    - "personal" → personal-{owner_sub}

    Falls back to shared if tenant/personal visibility lacks required IDs.
    """
    if visibility == "tenant" and tenant_id:
        return f"tenant-{tenant_id}"
    elif visibility == "personal" and owner_sub:
        return f"personal-{owner_sub}"
    else:
        # Default: shared → hash-sharded
        shard_idx = get_shard_index(org_id, shard_count)
        return f"code-shard-{shard_idx}"


def store_wiki(
    wiki_text: str,
    org_repo: str,
    org_id: str,
    allowed_principals: list[str],
    *,
    s3_writer: S3Writer | None = None,
    vector_store: VectorStore | None = None,
    embedding_client: EmbeddingClient | None = None,
    catalog_client: CatalogClient | None = None,
    s3_bucket: str = "",
    wiki_s3_prefix: str = "content/wikis",
    shard_count: int = 4,
    visibility: str = "shared",
    tenant_id: str | None = None,
    owner_sub: str | None = None,
) -> WikiStoreResult:
    """Store wiki to S3 (human browse) and S3 Vectors (semantic search).

    This is the dual-sink replacement for upload_to_openviking() for wiki content.

    Args:
        wiki_text: The generated wiki markdown.
        org_repo: Repository identifier (e.g., "aws-e/adp").
        org_id: Organization identifier (for shard routing).
        allowed_principals: List of principals allowed to access this repo.
        s3_writer: S3 client for object writes (injectable for testing).
        vector_store: Vector store client (injectable for testing).
        embedding_client: Embedding generation client (injectable for testing).
        catalog_client: Catalog update client (injectable for testing).
        s3_bucket: Target S3 bucket name.
        wiki_s3_prefix: S3 key prefix for wiki objects.
        shard_count: Number of S3 Vectors shards.
        visibility: Scope visibility ("shared", "tenant", "personal").
        tenant_id: Tenant identifier (required for "tenant" visibility).
        owner_sub: User identifier (required for "personal" visibility).

    Returns:
        WikiStoreResult with status for each sink.
    """
    result = WikiStoreResult()
    safe_name = org_repo.replace("/", "-")
    s3_key = f"{wiki_s3_prefix}/{safe_name}-wiki.md"

    # --- Sink 1: Write wiki to S3 ---
    if s3_writer:
        try:
            success = s3_writer.put_object(s3_bucket, s3_key, wiki_text)
            result.s3_key = s3_key if success else None
            result.s3_success = success
            if success:
                log.info("Wiki written to S3: s3://%s/%s", s3_bucket, s3_key)
            else:
                log.warning("S3 write returned failure for %s", s3_key)
        except Exception as e:
            log.error("S3 write failed for %s: %s", s3_key, e)
            result.error = f"S3 write failed: {e}"
    else:
        log.warning("No S3 writer provided — skipping S3 wiki write")

    # --- Sink 2: Chunk + Embed + Write to S3 Vectors ---
    if vector_store and embedding_client:
        try:
            chunks = chunk_wiki_by_sections(wiki_text)
            if not chunks:
                log.warning("Wiki chunking produced 0 chunks for %s", org_repo)
                result.vectors_success = True  # Not a failure — just empty
                result.vectors_written = 0
            else:
                index_name = _resolve_vector_index(
                    visibility=visibility,
                    org_id=org_id,
                    tenant_id=tenant_id,
                    owner_sub=owner_sub,
                    shard_count=shard_count,
                )

                vectors: list[dict[str, Any]] = []
                for chunk in chunks:
                    embedding = embedding_client.embed(chunk.text)
                    vector_key = f"wiki:{org_repo}:{chunk.heading or 'preamble'}:{chunk.chunk_idx}"
                    vectors.append(
                        {
                            "key": vector_key,
                            "embedding": embedding,
                            "metadata": {
                                "repo": org_repo,
                                "org_id": org_id,
                                "source_type": "wiki",
                                "language": "en",
                                "section_heading": chunk.heading or "preamble",
                                "chunk_text": chunk.text[:500],
                                "wiki_s3_key": s3_key,
                            },
                        }
                    )

                vector_store.put_vectors(index_name, vectors)
                result.vectors_written = len(vectors)
                result.vectors_success = True
                log.info(
                    "Wiki embedded: %d chunks -> %s (visibility=%s)",
                    len(vectors),
                    index_name,
                    visibility,
                )
        except Exception as e:
            log.error("Wiki embedding failed for %s: %s", org_repo, e)
            result.error = (result.error or "") + f" Embedding failed: {e}"
    else:
        log.warning("No vector_store/embedding_client — skipping wiki embedding")

    # --- Catalog update ---
    if catalog_client:
        try:
            wiki_status = "complete" if result.s3_success else "failed"
            updated = catalog_client.update_wiki_status(org_repo, wiki_status, result.s3_key)
            result.catalog_updated = updated
        except Exception as e:
            log.error("Catalog update failed for %s: %s", org_repo, e)

    return result


def store_code_index_to_s3(
    code_index_md: str,
    org_repo: str,
    *,
    s3_writer: S3Writer | None = None,
    s3_bucket: str = "",
    code_index_s3_prefix: str = "content/code-indexes",
) -> str | None:
    """Store code-index markdown summary to S3.

    This is the S3 equivalent of uploading the code-index markdown to OpenViking.
    The JSON code-index is already written to filesystem — this stores the
    human-readable markdown summary for browsing.

    Returns the S3 key on success, None on failure.
    """
    if not s3_writer:
        log.warning("No S3 writer — skipping code-index S3 write")
        return None

    safe_name = org_repo.replace("/", "-")
    s3_key = f"{code_index_s3_prefix}/{safe_name}-code-index.md"

    try:
        success = s3_writer.put_object(s3_bucket, s3_key, code_index_md)
        if success:
            log.info("Code-index written to S3: s3://%s/%s", s3_bucket, s3_key)
            return s3_key
        else:
            log.warning("S3 write returned failure for code-index %s", s3_key)
            return None
    except Exception as e:
        log.error("Code-index S3 write failed for %s: %s", s3_key, e)
        return None
