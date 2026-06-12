"""Experience MCP tool — save, recall, and list_syntheses for personal context.

This is the 6th tool on the Context MCP Server, enabling users and agents to
persist experiential knowledge scoped per-user and per-persona, with
decay-weighted semantic recall.

All recall operations go through the #1.1 owner read-filter (PersonalContextStore)
to enforce cross-user isolation.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Protocol

from ulid import ULID

from .embeddings import EmbeddingClient
from .identity import CallerIdentity, require_identity
from .models import EntryType, Persona, PersonalContextEntry, Visibility
from .storage import PersonalContextStore
from . import graph as personal_graph


class ExperienceToolError(Exception):
    """Raised when the experience tool encounters a validation error."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class EmbeddingStore(Protocol):
    """Protocol for persistent embedding storage backends."""

    def store(self, owner_sub: str, entry_id: str, embedding: list[float]) -> None: ...
    def recall(
        self, owner_sub: str, query_embedding: list[float], top_k: int = 20
    ) -> list[tuple[str, float]]: ...
    def delete(self, owner_sub: str, entry_id: str) -> None: ...


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class InMemoryEmbeddingStore:
    """In-memory embedding store (legacy fallback, lost on pod restart).

    Used when STORAGE_BACKEND=openviking or when no S3 Vectors backend
    is configured. Exists only for backward compatibility during migration.
    """

    def __init__(self) -> None:
        self._embeddings: dict[str, list[float]] = {}

    def store(self, owner_sub: str, entry_id: str, embedding: list[float]) -> None:
        self._embeddings[entry_id] = embedding

    def recall(
        self, owner_sub: str, query_embedding: list[float], top_k: int = 20
    ) -> list[tuple[str, float]]:
        """Return all stored embeddings as (entry_id, distance) pairs.

        Distance is computed as ``1.0 - cosine_similarity`` to match
        S3 Vectors cosine-distance semantics.
        """
        results: list[tuple[str, float]] = []
        for entry_id, embedding in self._embeddings.items():
            similarity = _cosine_similarity(query_embedding, embedding)
            distance = 1.0 - similarity
            results.append((entry_id, distance))
        results.sort(key=lambda x: x[1])
        return results[:top_k]

    def delete(self, owner_sub: str, entry_id: str) -> None:
        self._embeddings.pop(entry_id, None)


class ExperienceTool:
    """Handler for the ``experience`` MCP tool.

    Parameters
    ----------
    store:
        PersonalContextStore instance (provides owner-scoped CRUD).
    embedding_client:
        Client implementing the EmbeddingClient protocol.
    embedding_store:
        Persistent embedding storage backend. If None, falls back to
        InMemoryEmbeddingStore (legacy behavior, lost on restart).
    """

    def __init__(
        self,
        store: PersonalContextStore,
        embedding_client: EmbeddingClient,
        embedding_store: EmbeddingStore | None = None,
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.embedding_store: EmbeddingStore = embedding_store or InMemoryEmbeddingStore()

    def handle(self, arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """Route an experience tool call to the appropriate action.

        Parameters
        ----------
        arguments:
            Tool call arguments (action, persona, content, query, etc.).
        headers:
            Request headers containing identity (X-Owner-Sub, X-Tenant-Id).

        Returns
        -------
        Tool result dict.

        Raises
        ------
        ExperienceToolError
            On validation failures (missing required fields, invalid action).
        """
        action = arguments.get("action")
        if action not in ("save", "recall", "list_syntheses"):
            raise ExperienceToolError(
                f"Invalid action: {action!r}. Must be one of: save, recall, list_syntheses"
            )

        identity = require_identity(headers)

        if action == "save":
            return self._save(arguments, identity)
        elif action == "recall":
            return self._recall(arguments, identity)
        else:
            return self._list_syntheses(arguments, identity)

    def _save(self, arguments: dict[str, Any], identity: CallerIdentity) -> dict[str, Any]:
        """Save an experiential learning entry.

        Required: content, persona.
        Optional: learning_type, context, visibility.
        """
        content = arguments.get("content", "").strip()
        if not content:
            raise ExperienceToolError("'content' is required for save action")

        persona_str = arguments.get("persona", "developer")
        try:
            persona = Persona(persona_str)
        except ValueError:
            raise ExperienceToolError(
                f"Invalid persona: {persona_str!r}. Must be one of: {[p.value for p in Persona]}"
            )

        visibility_str = arguments.get("visibility", "private")
        try:
            visibility = Visibility(visibility_str)
        except ValueError:
            raise ExperienceToolError(
                f"Invalid visibility: {visibility_str!r}. Must be 'private' or 'shared'"
            )

        # Generate embedding for the content
        embedding = self.embedding_client.embed(content)

        # Build entry data
        entry_id = str(ULID())
        entry_data: dict[str, Any] = {
            "id": entry_id,
            "type": EntryType.learning.value,
            "owner_sub": "",  # Will be force-stamped
            "tenant_id": "",  # Will be force-stamped
            "visibility": visibility.value,
            "persona": persona.value,
            "learning_type": arguments.get("learning_type", ""),
            "content": content,
            "context": arguments.get("context", {}),
            "confidence": 0.7,
            "validated": False,
            "synthesized": False,
            "decay_score": 1.0,
        }

        # Write via store (force-stamps owner_sub/tenant_id from identity)
        entry = self.store.write_entry(identity, entry_data)

        # Persist embedding in the embedding store (survives pod restart)
        self.embedding_store.store(identity.owner_sub, entry.id, embedding)

        return {
            "status": "saved",
            "id": entry.id,
            "persona": entry.persona.value,
            "visibility": entry.visibility.value,
        }

    def _recall(self, arguments: dict[str, Any], identity: CallerIdentity) -> dict[str, Any]:
        """Recall entries via semantic search, ranked by similarity x decay_score.

        Required: query, persona.
        Optional: limit, cross_persona, visibility, graph_expand.
        """
        query = arguments.get("query", "").strip()
        if not query:
            raise ExperienceToolError("'query' is required for recall action")

        persona_str = arguments.get("persona", "developer")
        try:
            persona = Persona(persona_str)
        except ValueError:
            raise ExperienceToolError(
                f"Invalid persona: {persona_str!r}. Must be one of: {[p.value for p in Persona]}"
            )

        limit = arguments.get("limit", 5)
        cross_persona = arguments.get("cross_persona", False)
        graph_expand = arguments.get("graph_expand", False)

        # Generate query embedding
        query_embedding = self.embedding_client.embed(query)

        # Get all visible entries (private + shared within tenant) via #1.1 filter
        entries = self.store.list_entries(identity, entry_type=EntryType.learning)

        # Filter by persona (unless cross_persona=true)
        if not cross_persona:
            entries = [e for e in entries if e.persona == persona]

        # Get embedding similarity via the embedding store
        # Recall from the owner's index to get (entry_id, distance) pairs
        recall_results = self.embedding_store.recall(
            identity.owner_sub, query_embedding, top_k=limit * 4
        )
        # Build a map of entry_id -> similarity (1 - distance for cosine)
        similarity_map: dict[str, float] = {
            entry_id: 1.0 - distance for entry_id, distance in recall_results
        }

        # Score entries: similarity x decay_score
        scored: list[tuple[float, PersonalContextEntry]] = []
        for entry in entries:
            similarity = similarity_map.get(entry.id)
            if similarity is None:
                continue
            combined_score = similarity * entry.decay_score
            scored.append((combined_score, entry))

        # Sort by combined score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Take top-k
        top_entries = scored[:limit]

        # Update last_accessed_at on returned entries (resets decay clock)
        now = datetime.now(timezone.utc).isoformat()
        results = []
        for score, entry in top_entries:
            entry.last_accessed_at = now
            # Persist the updated last_accessed_at via store
            from .storage import build_entry_path

            path = build_entry_path(entry)
            self.store.backend.put(path, entry.model_dump())

            result_item: dict[str, Any] = {
                "id": entry.id,
                "content": entry.content,
                "persona": entry.persona.value,
                "learning_type": entry.learning_type,
                "confidence": entry.confidence,
                "decay_score": entry.decay_score,
                "score": round(score, 4),
                "visibility": entry.visibility.value,
                "created_at": entry.created_at,
            }

            # Optionally expand 1-hop graph neighborhood (when flag is on)
            if graph_expand and personal_graph.is_graph_enabled():
                neighbors = personal_graph.get_neighbors(entry.id, identity)
                if neighbors:
                    result_item["graph_neighbors"] = neighbors

            results.append(result_item)

        return {
            "status": "ok",
            "query": query,
            "results": results,
            "total": len(results),
            "graph_expanded": graph_expand and personal_graph.is_graph_enabled(),
        }

    def _list_syntheses(
        self, arguments: dict[str, Any], identity: CallerIdentity
    ) -> dict[str, Any]:
        """List synthesis summaries for the caller's persona.

        Until #3.1 (synthesis job) is implemented, this returns an empty list.
        """
        persona_str = arguments.get("persona", "developer")
        try:
            persona = Persona(persona_str)
        except ValueError:
            raise ExperienceToolError(
                f"Invalid persona: {persona_str!r}. Must be one of: {[p.value for p in Persona]}"
            )

        # List synthesis entries via the owner-scoped store
        entries = self.store.list_entries(identity, entry_type=EntryType.synthesis)

        # Filter by persona
        entries = [e for e in entries if e.persona == persona]

        results = [
            {
                "id": entry.id,
                "content": entry.content,
                "persona": entry.persona.value,
                "learning_type": entry.learning_type,
                "confidence": entry.confidence,
                "created_at": entry.created_at,
            }
            for entry in entries
        ]

        return {
            "status": "ok",
            "persona": persona.value,
            "syntheses": results,
            "total": len(results),
        }
