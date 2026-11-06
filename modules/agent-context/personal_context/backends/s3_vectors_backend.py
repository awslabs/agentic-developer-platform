"""Persistent embedding storage via Amazon S3 Vectors.

Replaces the in-memory ``dict[str, list[float]]`` in ExperienceTool with
durable per-user indexes in S3 Vectors. Each user gets their own index
(hard physical isolation — defense in depth).

Design reference: docs/design-1348-replace-openviking.md section 9.

Key characteristics:
- One index per user (``personal-{owner_sub}``) — created lazily on first save.
- Embeddings persist across pod restarts (fixes EPIC #1287 gap).
- Uses cosine distance with 1024-dim Titan Embed v2 vectors.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# S3 Vectors PutVectors batch limit
_MAX_BATCH_SIZE = 500
_MAX_ACTIVE_ATTEMPTS = 1024
_ATTEMPTS_PER_QUERY = 100


class S3VectorsEmbeddingStore:
    """Persistent embedding storage using one S3 Vectors index per user.

    Parameters
    ----------
    bucket_name:
        S3 Vectors bucket name (e.g. ``adp-dev-code-vectors-{account_id}``).
    region_name:
        AWS region where the S3 Vectors bucket resides. If None, uses the
        default from environment/instance profile.
    dimension:
        Vector dimension (default 1024 for Titan Embed v2).
    distance_metric:
        Distance metric (default ``cosine``).
    """

    def __init__(
        self,
        bucket_name: str,
        region_name: str | None = None,
        dimension: int = 1024,
        distance_metric: str = "cosine",
    ):
        self.bucket_name = bucket_name
        self.dimension = dimension
        self.distance_metric = distance_metric
        kwargs: dict[str, Any] = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._client = boto3.client("s3vectors", **kwargs)
        self._ensured_indexes: set[str] = set()

    def _index_name(self, owner_sub: str) -> str:
        """Derive the index name for a user (one index per user)."""
        return f"personal-{owner_sub}"

    def ensure_index(self, owner_sub: str) -> None:
        """Create the user's index if it doesn't exist (idempotent).

        Skips the API call if we've already ensured this index in the
        current process lifetime (cheap in-memory cache).
        """
        index_name = self._index_name(owner_sub)
        if index_name in self._ensured_indexes:
            return

        try:
            self._client.create_index(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                dimension=self.dimension,
                distanceMetric=self.distance_metric,
                dataType="float32",
            )
            logger.info("Created S3 Vectors index: %s", index_name)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            # Index already exists — treat as success
            if error_code in ("ConflictException", "ResourceAlreadyExistsException"):
                logger.debug("S3 Vectors index already exists: %s", index_name)
            else:
                raise

        self._ensured_indexes.add(index_name)

    def store(self, owner_sub: str, entry_id: str, embedding: list[float]) -> None:
        """Store an embedding for a personal-context entry.

        Creates the user's index on first call (lazy provisioning).

        Parameters
        ----------
        owner_sub:
            User's cognito sub (UUID). Determines which index to use.
        entry_id:
            Unique entry identifier (ULID). Used as the vector key.
        embedding:
            The float32 embedding vector (1024-dim).
        """
        self.ensure_index(owner_sub)
        index_name = self._index_name(owner_sub)

        self._client.put_vectors(
            vectorBucketName=self.bucket_name,
            indexName=index_name,
            vectors=[
                {
                    "key": entry_id,
                    "data": {"float32": embedding},
                    "metadata": {},
                }
            ],
        )

    def recall(
        self,
        owner_sub: str,
        query_embedding: list[float],
        top_k: int = 20,
    ) -> list[tuple[str, float]]:
        """Find similar entries by embedding. Returns (entry_id, distance) pairs.

        Parameters
        ----------
        owner_sub:
            User's cognito sub — queries their personal index.
        query_embedding:
            The query vector to find neighbors for.
        top_k:
            Maximum number of results to return (capped at 100 by S3 Vectors).

        Returns
        -------
        List of (entry_id, distance) tuples, sorted by ascending distance.
        Returns empty list if the index doesn't exist yet (new user, no saves).
        """
        index_name = self._index_name(owner_sub)

        try:
            response = self._client.query_vectors(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                queryVector={"float32": query_embedding},
                topK=min(top_k, 100),
                returnDistance=True,
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("NotFoundException", "ResourceNotFoundException"):
                # Index doesn't exist yet — user hasn't saved anything
                return []
            raise

        results: list[tuple[str, float]] = []
        for vector in response.get("vectors", []):
            key = vector.get("key", "")
            distance = vector.get("distance", 1.0)
            results.append((key, distance))

        return results

    def delete(self, owner_sub: str, entry_id: str) -> None:
        """Remove an embedding when a personal-context entry is deleted.

        Idempotent — does not error if the vector doesn't exist.
        """
        index_name = self._index_name(owner_sub)

        try:
            self._client.delete_vectors(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                keys=[entry_id],
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("NotFoundException", "ResourceNotFoundException"):
                return
            raise


class S3VectorsCodeStore:
    """Sharded code embedding store using hash-by-org to distribute writes.

    Used by the ingestion pipeline for code-intel semantic search.
    Scatter-gather queries across all shards at read time.

    Supports per-tenant index isolation (Story 5, #1774):
    - ``shared`` visibility → hash-sharded indexes (``code-shard-{N}``)
    - ``tenant`` visibility → ``tenant-{tenant_id}`` index
    - ``personal`` visibility → ``personal-{owner_sub}`` index

    Parameters
    ----------
    bucket_name:
        S3 Vectors bucket name.
    shard_count:
        Number of code-shard indexes (default 4).
    region_name:
        AWS region.
    """

    def __init__(
        self,
        bucket_name: str,
        shard_count: int = 4,
        region_name: str | None = None,
    ):
        self.bucket_name = bucket_name
        self.shard_count = shard_count
        kwargs: dict[str, Any] = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._client = boto3.client("s3vectors", **kwargs)
        self._ensured_indexes: set[str] = set()

    def get_shard_index(self, org_id: str) -> int:
        """Deterministic shard assignment by org_id hash."""
        h = hashlib.sha256(org_id.encode()).digest()
        return int.from_bytes(h[:4], "big") % self.shard_count

    def _index_name(self, shard: int) -> str:
        return f"code-shard-{shard}"

    def _ensure_index(self, index_name: str) -> None:
        """Create a named index if it doesn't exist (idempotent).

        Caches index names to avoid repeated create_index calls.
        """
        if index_name in self._ensured_indexes:
            return

        try:
            self._client.create_index(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                dimension=1024,
                distanceMetric="cosine",
                dataType="float32",
            )
            logger.info("Created S3 Vectors index: %s", index_name)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("ConflictException", "ResourceAlreadyExistsException"):
                logger.debug("S3 Vectors index already exists: %s", index_name)
            else:
                raise

        self._ensured_indexes.add(index_name)

    @staticmethod
    def resolve_index_name(
        visibility: str,
        org_id: str,
        tenant_id: str | None = None,
        owner_sub: str | None = None,
        shard_count: int = 4,
        product_id: str | None = None,
    ) -> str:
        """Determine target index name from scope visibility.

        Parameters
        ----------
        visibility:
            One of "shared", "tenant", "personal", "product".
        org_id:
            Organization ID (used for shard routing in shared mode).
        tenant_id:
            Tenant identifier (required for tenant and product visibility).
        owner_sub:
            User identifier (required when visibility is "personal").
        shard_count:
            Number of shared shards.
        product_id:
            Required for product visibility; Product data shares the tenant index.

        Returns
        -------
        Index name string for the target S3 Vectors index.

        Raises
        ------
        ValueError:
            If tenant_id/owner_sub missing for tenant/personal visibility.
        """
        if visibility == "product" and not product_id:
            raise ValueError("product_id required for product visibility")
        if visibility in {"tenant", "product"}:
            if not tenant_id:
                raise ValueError("tenant_id required for tenant visibility")
            return f"tenant-{tenant_id}"
        elif visibility == "personal":
            if not owner_sub:
                raise ValueError("owner_sub required for personal visibility")
            return f"personal-{owner_sub}"
        else:
            # Default: shared → hash-sharded
            h = hashlib.sha256(org_id.encode()).digest()
            shard = int.from_bytes(h[:4], "big") % shard_count
            return f"code-shard-{shard}"

    def put_vectors(self, vectors: list[dict[str, Any]], org_id: str) -> None:
        """Write code vectors to the appropriate shard.

        Automatically batches into groups of 500 (S3 Vectors limit).

        Parameters
        ----------
        vectors:
            List of dicts with keys: ``key``, ``embedding``, ``metadata``.
        org_id:
            Organization ID — determines which shard receives the write.
        """
        shard = self.get_shard_index(org_id)
        index_name = self._index_name(shard)

        # Batch into groups of 500
        for i in range(0, len(vectors), _MAX_BATCH_SIZE):
            batch = vectors[i : i + _MAX_BATCH_SIZE]
            s3v_vectors = [
                {
                    "key": v["key"],
                    "data": {"float32": v["embedding"]},
                    "metadata": v.get("metadata", {}),
                }
                for v in batch
            ]
            self._client.put_vectors(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                vectors=s3v_vectors,
            )

    def put_vectors_scoped(
        self,
        vectors: list[dict[str, Any]],
        org_id: str,
        visibility: str = "shared",
        tenant_id: str | None = None,
        owner_sub: str | None = None,
        product_id: str | None = None,
    ) -> None:
        """Write vectors to a scope-determined index.

        Routes vectors to the appropriate index based on visibility:
        - "shared" → code-shard-{N} (existing hash-sharded behavior)
        - "tenant" → tenant-{tenant_id}
        - "personal" → personal-{owner_sub}
        - "product" → tenant-{tenant_id}, with mandatory Product/asset metadata

        Creates the target index on first write (lazy provisioning).

        Parameters
        ----------
        vectors:
            List of dicts with keys: ``key``, ``embedding``, ``metadata``.
        org_id:
            Organization ID (used for shard routing in shared mode).
        visibility:
            Scope visibility ("shared", "tenant", "personal").
        tenant_id:
            Tenant identifier (required for "tenant" visibility).
        owner_sub:
            User identifier (required for "personal" visibility).
        """
        index_name = self.resolve_index_name(
            visibility=visibility,
            org_id=org_id,
            tenant_id=tenant_id,
            owner_sub=owner_sub,
            shard_count=self.shard_count,
            product_id=product_id,
        )

        if visibility == "product":
            if any(
                not isinstance(v.get("metadata", {}).get(field), str) or not v["metadata"][field]
                for v in vectors
                for field in ("asset_id", "ingestion_attempt_id")
            ):
                raise ValueError(
                    "asset_id and ingestion_attempt_id provenance required for Product vectors"
                )
            vectors = [
                {
                    **v,
                    "metadata": {
                        **v["metadata"],
                        "visibility": "product",
                        "product_id": product_id,
                        "tenant_id": tenant_id,
                    },
                }
                for v in vectors
            ]
        elif product_id or any(
            v.get("metadata", {}).get("product_id")
            or v.get("metadata", {}).get("visibility") == "product"
            for v in vectors
        ):
            raise ValueError("Product vectors require product visibility")

        # Ensure index exists (lazy creation for tenant/personal indexes)
        if visibility in ("tenant", "personal", "product"):
            self._ensure_index(index_name)

        # Batch into groups of 500
        for i in range(0, len(vectors), _MAX_BATCH_SIZE):
            batch = vectors[i : i + _MAX_BATCH_SIZE]
            s3v_vectors = [
                {
                    "key": v["key"],
                    "data": {"float32": v["embedding"]},
                    "metadata": v.get("metadata", {}),
                }
                for v in batch
            ]
            self._client.put_vectors(
                vectorBucketName=self.bucket_name,
                indexName=index_name,
                vectors=s3v_vectors,
            )

    def query(
        self,
        query_vector: list[float],
        org_id: str,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """Scatter-gather query across all shards, filtered by org_id.

        Parameters
        ----------
        query_vector:
            The query embedding.
        org_id:
            Organization ID for metadata filtering.
        top_k:
            Number of results to return.

        Returns
        -------
        Merged results sorted by distance (ascending), limited to top_k.
        """
        all_results: list[dict[str, Any]] = []

        for shard_idx in range(self.shard_count):
            index_name = self._index_name(shard_idx)
            try:
                response = self._client.query_vectors(
                    vectorBucketName=self.bucket_name,
                    indexName=index_name,
                    queryVector={"float32": query_vector},
                    topK=min(top_k * 2, 100),
                    filter={
                        "$and": [
                            {"org_id": {"$eq": org_id}},
                            {"product_id": {"$exists": False}},
                            {"visibility": {"$ne": "product"}},
                        ]
                    },
                    returnMetadata=True,
                    returnDistance=True,
                )
                all_results.extend(response.get("vectors", []))
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("NotFoundException", "ResourceNotFoundException"):
                    continue
                raise

        # Sort by distance (ascending = most similar first for cosine)
        all_results = [
            r
            for r in all_results
            if not r.get("metadata", {}).get("product_id")
            and r.get("metadata", {}).get("visibility") != "product"
        ]
        all_results.sort(key=lambda r: r.get("distance", float("inf")))
        return all_results[:top_k]

    def query_scoped(
        self,
        query_vector: list[float],
        org_id: str,
        tenant_id: str | None = None,
        owner_sub: str | None = None,
        top_k: int = 20,
        product_id: str | None = None,
        personal_only: bool = False,
        active_attempt_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query one Product, or union non-Product shared + tenant + personal results.

        Queries the indexes the caller is entitled to see:
        1. Shared shards (scatter-gather across code-shard-{0..N})
        2. Tenant index (tenant-{tenant_id}) if tenant_id provided
        3. Personal index (personal-{owner_sub}) if owner_sub provided

        Results from all indexes are merged and sorted by distance.

        Parameters
        ----------
        query_vector:
            The query embedding.
        org_id:
            Organization ID for metadata filtering on shared shards.
        tenant_id:
            Caller's tenant ID. If provided, also queries tenant-{tenant_id}.
        owner_sub:
            Caller's user ID. If provided, also queries personal-{owner_sub}.
        top_k:
            Number of results to return.
        product_id:
            When provided, search only this Product in the tenant index.

        Returns
        -------
        Merged results sorted by distance (ascending), limited to top_k.
        """
        attempts = sorted(set(active_attempt_ids or ()))
        if len(attempts) > _MAX_ACTIVE_ATTEMPTS:
            raise ValueError("Knowledge scope exceeds 1024 active ingestion attempts")
        batches = [
            attempts[i : i + _ATTEMPTS_PER_QUERY]
            for i in range(0, len(attempts), _ATTEMPTS_PER_QUERY)
        ]
        if product_id and personal_only:
            raise ValueError("Product and personal scopes cannot be combined")
        if product_id:
            if not tenant_id:
                raise ValueError("tenant_id required for Product query")
            results = []
            for batch in batches:
                results.extend(
                    self._query_single_index(
                        f"tenant-{tenant_id}",
                        query_vector,
                        top_k,
                        filter_metadata={
                            "$and": [
                                {"product_id": {"$eq": product_id}},
                                {"tenant_id": {"$eq": tenant_id}},
                                {"visibility": {"$eq": "product"}},
                                {"ingestion_attempt_id": {"$in": batch}},
                            ]
                        },
                    )
                )
            # Preserve the boundary even if a backend/cache ignores its filter.
            results = [
                r
                for r in results
                if r.get("metadata", {}).get("product_id") == product_id
                and r["metadata"].get("tenant_id") == tenant_id
                and r["metadata"].get("visibility") == "product"
                and r["metadata"].get("asset_id")
                and r["metadata"].get("ingestion_attempt_id") in attempts
            ]
            return sorted(results, key=lambda r: r.get("distance", float("inf")))[:top_k]

        non_product = {
            "$and": [{"product_id": {"$exists": False}}, {"visibility": {"$ne": "product"}}]
        }

        def personal_results():
            if not owner_sub:
                return []
            # Query legacy corpus once. Registered personal generations have a
            # separate filter so old duplicates cannot crowd out current chunks.
            results = self._query_single_index(
                f"personal-{owner_sub}",
                query_vector,
                top_k,
                filter_metadata={
                    "$and": [*non_product["$and"], {"visibility": {"$ne": "personal"}}]
                },
            )
            for batch in batches:
                results.extend(
                    self._query_single_index(
                        f"personal-{owner_sub}",
                        query_vector,
                        top_k,
                        filter_metadata={
                            "$and": [
                                *non_product["$and"],
                                {"visibility": {"$eq": "personal"}},
                                {"owner_sub": {"$eq": owner_sub}},
                                {"ingestion_attempt_id": {"$in": batch}},
                            ]
                        },
                    )
                )
            results = [
                r
                for r in results
                if not r.get("metadata", {}).get("product_id")
                and r.get("metadata", {}).get("visibility") != "product"
                and (
                    r.get("metadata", {}).get("visibility") != "personal"
                    or r.get("metadata", {}).get("ingestion_attempt_id") in attempts
                )
            ]
            # Keys are unique within a personal index, including all attempts.
            unique = {r.get("key", ""): r for r in results}
            return sorted(unique.values(), key=lambda r: r.get("distance", float("inf")))[:top_k]

        if personal_only:
            return personal_results()
        all_results: list[dict[str, Any]] = []

        # 1. Query shared shards (scatter-gather)
        for shard_idx in range(self.shard_count):
            index_name = self._index_name(shard_idx)
            results = self._query_single_index(
                index_name,
                query_vector,
                top_k,
                filter_metadata={"$and": [{"org_id": {"$eq": org_id}}, *non_product["$and"]]},
            )
            all_results.extend(results)

        # 2. Query tenant index (if caller has a tenant)
        if tenant_id:
            tenant_index = f"tenant-{tenant_id}"
            results = self._query_single_index(
                tenant_index, query_vector, top_k, filter_metadata=non_product
            )
            all_results.extend(results)

        # 3. Query personal index (if caller has an owner_sub)
        if owner_sub:
            results = personal_results()
            all_results.extend(results)

        # Merge: sort by distance ascending, limit to top_k
        all_results = [
            r
            for r in all_results
            if not r.get("metadata", {}).get("product_id")
            and r.get("metadata", {}).get("visibility") != "product"
        ]
        all_results.sort(key=lambda r: r.get("distance", float("inf")))
        return all_results[:top_k]

    def _query_single_index(
        self,
        index_name: str,
        query_vector: list[float],
        top_k: int,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query a single index, returning empty list on index-not-found.

        Parameters
        ----------
        index_name:
            Target index name.
        query_vector:
            The query embedding.
        top_k:
            Max results from this index.
        filter_metadata:
            Optional metadata filter (e.g., {"org_id": {"$eq": "..."}}).

        Returns
        -------
        List of vector result dicts from the index, or [] if index doesn't exist.
        """
        try:
            kwargs: dict[str, Any] = {
                "vectorBucketName": self.bucket_name,
                "indexName": index_name,
                "queryVector": {"float32": query_vector},
                "topK": min(top_k * 2, 100),
                "returnMetadata": True,
                "returnDistance": True,
            }
            if filter_metadata:
                kwargs["filter"] = filter_metadata
            response = self._client.query_vectors(**kwargs)
            return response.get("vectors", [])
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("NotFoundException", "ResourceNotFoundException"):
                return []
            raise
