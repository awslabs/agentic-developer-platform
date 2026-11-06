"""Source-code embedding sink for the ingestion pipeline (embed_vectors stage, #2297).

Chunks a cloned repo's source files, embeds each chunk via the LiteLLM proxy
(Titan Embed v2, 1024-dim), and writes the vectors to Amazon S3 Vectors so the
Door's semantic search returns code-level hits for self-serve repos.

This is DISTINCT from wiki embedding (which lives in the deepwiki stage via
wiki_store.store_wiki): this embeds actual source files, not the DeepWiki summary.

Scope isolation reuses wiki_store._resolve_vector_index() so code vectors land in
the same shared/tenant/personal indexes the Door already reads (query_scoped in
door/server.py). Shared vectors carry an org_id metadata field so the Door's
shard-level metadata filter surfaces them.

Fail-open: the S3 Vectors bucket is provisioned separately (#2486). Until it
exists (or when embed_vectors is disabled) this module reports a clean skip
status instead of raising, so the rest of the pipeline is unaffected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests

from wiki_store import _resolve_vector_index

log = logging.getLogger("code-embedder")

# Titan Embed v2 dimension (must match the Door read-side + personal_context store).
EMBEDDING_DIMENSION = 1024

# S3 Vectors PutVectors batch limit.
_MAX_BATCH_SIZE = 500

# Error codes that mean "the vector bucket/index isn't there yet" (#2486 pending).
_BUCKET_MISSING_CODES = frozenset(
    {
        "NotFoundException",
        "ResourceNotFoundException",
        "NoSuchBucket",
    }
)

# Source-file extensions worth embedding for code search. Mirrors the language
# set the basic code-index builder recognizes (ingest-repo._build_basic_code_index).
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".kt",
        ".swift",
        ".scala",
        ".php",
        ".md",
    }
)

# Directories never worth embedding (vendored / generated / VCS).
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".terraform",
        ".mypy_cache",
        ".pytest_cache",
        "target",
    }
)


class LiteLLMEmbeddingClient:
    """Generate embeddings via the LiteLLM proxy's OpenAI-compatible endpoint.

    Uses ``POST {base_url}/embeddings`` with the configured model. The proxy
    routes this to AWS Bedrock Titan Embed v2. Kept thin (requests-based) so the
    ingestion image doesn't need to package personal_context.
    """

    def __init__(self, base_url: str, model: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``. Raises on empty input / HTTP error."""
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        resp = requests.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": text},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def discover_source_files(clone_path: str, max_files: int = 500) -> list[Path]:
    """Walk the clone and return source files worth embedding (capped).

    Skips vendored/generated directories and non-source extensions. Caps the
    file count to keep embedding cost bounded per repo (large repos are
    truncated, matching the code-index builder's 500-file cap).
    """
    clone = Path(clone_path)
    found: list[Path] = []
    for root, dirs, files in os.walk(clone):
        # Prune skip dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if Path(fname).suffix.lower() in _SOURCE_EXTENSIONS:
                found.append(Path(root) / fname)
                if len(found) >= max_files:
                    return found
    return found


def chunk_file_text(text: str, max_chunk_chars: int = 4000) -> list[str]:
    """Split file text into char-bounded chunks on line boundaries.

    Keeps whole lines together and caps each chunk at ``max_chunk_chars`` so
    embedding inputs stay within the model's context. Empty/whitespace-only
    files produce no chunks.
    """
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        # A single oversized line: emit current, then hard-split the line.
        if len(line) > max_chunk_chars:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            for i in range(0, len(line), max_chunk_chars):
                chunks.append(line[i : i + max_chunk_chars])
            continue
        if current_len + len(line) > max_chunk_chars and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))

    # Drop chunks that are only whitespace.
    return [c for c in chunks if c.strip()]


class S3VectorsCodeWriter:
    """Minimal S3 Vectors writer for code chunks (scope-aware).

    Mirrors personal_context.backends.S3VectorsCodeStore.put_vectors_scoped but
    is self-contained so the ingestion image needn't package personal_context.
    Routes to the scope-resolved index and lazily creates tenant/personal indexes.
    Read-back verification queries the index for vectors tagged with the repo.
    """

    def __new__(cls, *args, **kwargs):
        from job_client import enabled, VectorWriter
        if enabled():
            return VectorWriter()
        return super().__new__(cls)

    def __init__(self, bucket_name: str, region_name: str | None = None):
        import boto3

        self.bucket_name = bucket_name
        kwargs: dict[str, Any] = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._client = boto3.client("s3vectors", **kwargs)
        self._ensured: set[str] = set()

    def _ensure_index(self, index_name: str) -> None:
        """Create the named index if absent (idempotent, cached)."""
        if index_name in self._ensured:
            return
        from botocore.exceptions import ClientError

        try:
            self._client.create_index(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                dimension=EMBEDDING_DIMENSION,
                distanceMetric="cosine",
                dataType="float32",
            )
            log.info("Created S3 Vectors index: %s", index_name)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("ConflictException", "ResourceAlreadyExistsException"):
                raise
        self._ensured.add(index_name)

    def put_vectors(self, index_name: str, vectors: list[dict[str, Any]]) -> None:
        """Write vectors (batched at the 500 S3 Vectors limit) to ``index_name``."""
        self._ensure_index(index_name)
        for i in range(0, len(vectors), _MAX_BATCH_SIZE):
            batch = vectors[i : i + _MAX_BATCH_SIZE]
            self._client.put_vectors(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                vectors=[
                    {
                        "key": v["key"],
                        "data": {"float32": v["embedding"]},
                        "metadata": v.get("metadata", {}),
                    }
                    for v in batch
                ],
            )

    def count_repo_vectors(self, index_name: str, query_vector: list[float], org_repo: str) -> int:
        """Query ``index_name`` for vectors tagged repo=org_repo (read-back verify)."""
        response = self._client.query_vectors(
            vectorBucketName=self.bucket_name,
            indexName=index_name,
            queryVector={"float32": query_vector},
            topK=1,
            filter={"repo": {"$eq": org_repo}},
        )
        return len(response.get("vectors", []))

    def clear_asset_vectors(self, index_name: str, asset_id: str, ingestion_attempt_id: str) -> None:
        """Remove retry leftovers for exactly one registry asset and ingestion attempt.

        Other attempts remain untouched, so a late worker cannot delete newer results.
        Collect keys before deleting so pagination is not changed mid-scan. No
        caller-supplied prefix is accepted; colon is prohibited in registry IDs.
        """
        import re
        if not isinstance(asset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", asset_id):
            raise ValueError("Invalid registry asset ID")
        from scope import validate_ingestion_attempt
        attempt = validate_ingestion_attempt(ingestion_attempt_id)
        prefix = f"{asset_id}:{attempt}:"
        self._ensure_index(index_name)
        keys = []
        token = None
        while True:
            response = self._client.list_vectors(
                vectorBucketName=self.bucket_name, indexName=index_name,
                returnData=False, returnMetadata=False,
                **({"nextToken": token} if token else {}),
            )
            keys.extend(item["key"] for item in response.get("vectors", [])
                        if item.get("key", "").startswith(prefix))
            token = response.get("nextToken")
            if not token:
                break
        for offset in range(0, len(keys), 500):
            self._client.delete_vectors(vectorBucketName=self.bucket_name, indexName=index_name,
                                        keys=keys[offset:offset + 500])

    def count_asset_vectors(self, index_name, query_vector, tenant_id, product_id, asset_id, ingestion_attempt_id, owner_sub=None) -> int:
        """Verify the current Product registration, never another copy of the source."""
        response = self._client.query_vectors(
            vectorBucketName=self.bucket_name, indexName=index_name,
            queryVector={"float32": query_vector}, topK=1,
            filter={"$and": [{key: {"$eq": value}} for key, value in {
                "visibility": "product" if product_id else "personal", "asset_id": asset_id,
                "ingestion_attempt_id": ingestion_attempt_id,
                **({"tenant_id": tenant_id} if tenant_id else {}),
                **({"product_id": product_id} if product_id else {"owner_sub": owner_sub}),
            }.items()]},
        )
        return len(response.get("vectors", []))


def embed_code_repo(
    clone_path: str,
    org_repo: str,
    *,
    bucket_name: str,
    embedding_client: Any,
    vector_writer: Any | None = None,
    region_name: str | None = None,
    visibility: str = "shared",
    tenant_id: str | None = None,
    owner_sub: str | None = None,
    product_id: str | None = None,
    registry_asset_id: str | None = None,
    ingestion_attempt_id: str | None = None,
    shard_count: int = 4,
    max_files: int = 500,
) -> dict[str, Any]:
    """Embed a repo's source files into S3 Vectors. Fail-open on a missing bucket.

    Returns a dict with keys: ``status`` (complete | bucket_not_configured |
    bucket_missing | no_source | embed_failed | verify_failed), ``vectors``,
    ``files``, ``index``, ``error``.

    The vector_writer is injectable for tests; in production an S3VectorsCodeWriter
    is created lazily. A missing bucket (name unset, or S3 Vectors NotFound because
    #2486 hasn't provisioned it) yields a clean skip status — never an exception.
    """
    from botocore.exceptions import ClientError

    from scope import parse_scope, asset_storage_prefix, validate_ingestion_attempt
    scope = parse_scope({"visibility": visibility, "tenant_id": tenant_id, "owner_sub": owner_sub, "product_id": product_id})
    if scope.is_product or registry_asset_id:
        asset_storage_prefix(scope, "content", registry_asset_id)
        ingestion_attempt_id = validate_ingestion_attempt(ingestion_attempt_id)
    if not bucket_name:
        log.info(
            "embed_vectors: S3 Vectors bucket not configured — skipping %s (see #2486)",
            org_repo,
        )
        return {"status": "bucket_not_configured", "vectors": 0, "files": 0}

    org_id = org_repo.split("/")[0]
    index_name = _resolve_vector_index(
        visibility=visibility,
        org_id=org_id,
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        shard_count=shard_count,
        product_id=product_id,
    )

    if scope.is_product or (scope.is_personal and registry_asset_id):
        try:
            vector_writer = vector_writer or S3VectorsCodeWriter(bucket_name, region_name=region_name)
            vector_writer.clear_asset_vectors(index_name, registry_asset_id, ingestion_attempt_id)
        except Exception as exc:
            return {"status": "replace_failed", "error": str(exc), "vectors": 0, "files": 0}

    # Discover + chunk source files.
    source_files = discover_source_files(clone_path, max_files=max_files)
    if not source_files:
        log.info("embed_vectors: no embeddable source files in %s", org_repo)
        return {"status": "no_source", "vectors": 0, "files": 0, "index": index_name}

    clone = Path(clone_path)
    vectors: list[dict[str, Any]] = []
    first_embedding: list[float] | None = None
    embedded_files = 0
    try:
        for fpath in source_files:
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            rel = str(fpath.relative_to(clone))
            chunks = chunk_file_text(text)
            if not chunks:
                continue
            embedded_files += 1
            for idx, chunk in enumerate(chunks):
                embedding = embedding_client.embed(chunk)
                if first_embedding is None:
                    first_embedding = embedding
                vectors.append(
                    {
                        "key": f"{registry_asset_id}:{ingestion_attempt_id}:code:{org_repo}:{rel}:{idx}" if registry_asset_id else f"code:{org_repo}:{rel}:{idx}",
                        "embedding": embedding,
                        "metadata": {
                            **({"visibility": visibility} if registry_asset_id else {}),
                            **({"tenant_id": tenant_id} if tenant_id else {}),
                            **({"owner_sub": owner_sub} if owner_sub and registry_asset_id else {}),
                            **({"product_id": product_id} if product_id else {}),
                            **({"asset_id": registry_asset_id, "ingestion_attempt_id": ingestion_attempt_id} if registry_asset_id else {}),
                            "repo": org_repo,
                            "org_id": org_id,
                            "source_type": "code",
                            "file_path": rel,
                            "chunk_idx": idx,
                            "chunk_text": chunk[:500],
                        },
                    }
                )
    except Exception as e:  # embedding endpoint error
        log.warning("embed_vectors: embedding failed for %s: %s", org_repo, e)
        return {"status": "embed_failed", "error": str(e), "vectors": 0, "files": 0}

    if not vectors:
        return {"status": "no_source", "vectors": 0, "files": 0, "index": index_name}

    # Write to S3 Vectors — treat a missing bucket/index as a clean skip.
    writer = vector_writer
    try:
        if writer is None:
            writer = S3VectorsCodeWriter(bucket_name, region_name=region_name)
        writer.put_vectors(index_name, vectors)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _BUCKET_MISSING_CODES:
            log.info(
                "embed_vectors: S3 Vectors bucket/index missing for %s (%s) — "
                "skipping until #2486 provisions it",
                org_repo,
                code,
            )
            return {"status": "bucket_missing", "error": code, "vectors": 0, "files": 0}
        log.warning("embed_vectors: put_vectors failed for %s: %s", org_repo, e)
        return {"status": "embed_failed", "error": str(e), "vectors": 0, "files": 0}

    # Read-back verify: query for ≥1 vector tagged with this repo.
    verified = False
    try:
        if first_embedding is not None:
            if scope.is_product:
                verified = writer.count_asset_vectors(index_name, first_embedding, tenant_id, product_id, registry_asset_id, ingestion_attempt_id) > 0
            elif scope.is_personal and registry_asset_id:
                verified = writer.count_asset_vectors(index_name, first_embedding, tenant_id, None,
                                                     registry_asset_id, ingestion_attempt_id, owner_sub=owner_sub) > 0
            else:
                verified = writer.count_repo_vectors(index_name, first_embedding, org_repo) > 0
    except ClientError as e:
        log.warning("embed_vectors: verification query failed for %s: %s", org_repo, e)
        verified = False

    if not verified:
        return {
            "status": "verify_failed",
            "vectors": len(vectors),
            "files": embedded_files,
            "index": index_name,
            "error": "read-back query returned no vectors for repo",
        }

    log.info(
        "embed_vectors: %d vectors from %d files -> %s (repo=%s)",
        len(vectors),
        embedded_files,
        index_name,
        org_repo,
    )
    return {
        "status": "complete",
        "vectors": len(vectors),
        "files": embedded_files,
        "index": index_name,
    }


def embed_registered_documents(
    documents: list[dict[str, str]], scope, registry_asset_id: str,
    *, embedding_client=None, vector_writer=None, ingestion_attempt_id=None,
) -> dict[str, Any]:
    """Embed an owned URL crawl or converted document with registration provenance."""
    import hashlib
    from config import settings
    from scope import asset_storage_prefix, parse_scope, validate_ingestion_attempt

    scope = parse_scope(scope.to_dict())
    if not (scope.is_product or scope.is_personal):
        raise ValueError("Registered document embeddings require Product or personal scope")
    asset_storage_prefix(scope, "content", registry_asset_id)
    ingestion_attempt_id = validate_ingestion_attempt(ingestion_attempt_id)
    bucket = settings.s3_vectors_bucket
    if not bucket:
        return {"status": "failed", "error": "S3 Vectors bucket is not configured", "vectors": 0}
    index = _resolve_vector_index(scope.visibility, "", scope.tenant_id, scope.owner_sub, 4, scope.product_id)
    try:
        writer = vector_writer or S3VectorsCodeWriter(bucket, settings.effective_s3_vectors_region)
        client = embedding_client or LiteLLMEmbeddingClient(settings.llm_base_url, settings.embed_model)
        # Clear leftovers for this attempt; other attempts are rejected by registry checks.
        writer.clear_asset_vectors(index, registry_asset_id, ingestion_attempt_id)
        vectors = []
        for document in documents:
            source = document["source"]
            source_hash = hashlib.sha256(source.encode()).hexdigest()[:24]
            for idx, chunk in enumerate(chunk_file_text(document["text"])):
                vectors.append({
                    "key": f"{registry_asset_id}:{ingestion_attempt_id}:content:{source_hash}:{idx}",
                    "embedding": client.embed(chunk),
                    "metadata": {
                        "visibility": scope.visibility, "asset_id": registry_asset_id,
                        **({"tenant_id": scope.tenant_id} if scope.tenant_id else {}),
                        **({"product_id": scope.product_id} if scope.is_product else {"owner_sub": scope.owner_sub}),
                        "ingestion_attempt_id": ingestion_attempt_id,
                        "source_type": document["source_type"], "source_ref": source,
                        "source": source, "content_s3_key": document["s3_key"],
                        "chunk_idx": idx, "chunk_text": chunk[:500],
                    },
                })
        if not vectors:
            return {"status": "failed", "error": "No document text was indexed", "vectors": 0}
        writer.put_vectors(index, vectors)
        if not writer.count_asset_vectors(index, vectors[0]["embedding"], scope.tenant_id,
                                          scope.product_id, registry_asset_id, ingestion_attempt_id, owner_sub=scope.owner_sub):
            return {"status": "failed", "error": "Embedding read-back verification failed", "vectors": 0}
        return {"status": "complete", "vectors": len(vectors), "index": index}
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "vectors": 0}
