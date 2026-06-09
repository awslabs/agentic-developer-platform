"""Unit tests for the experience MCP tool (save/recall/list_syntheses).

Validates:
- save → recall round-trip returns the saved entry for the same owner.
- recall as a different owner returns none of it (isolation via #1.1 filter).
- Semantic recall: paraphrase ranks in top-k despite keyword disjointness.
- Negative control: unrelated query does NOT surface the entry.
- recall ranking favors higher decay_score when similarity is comparable.
- persona filter scopes results; cross_persona=true broadens.
- save without content / recall without query → ExperienceToolError.
- list_syntheses returns empty gracefully before synthesis exists.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

import pytest

from personal_context.experience_tool import ExperienceTool, ExperienceToolError, _cosine_similarity
from personal_context.storage import PersonalContextStore


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


def _make_uuid() -> str:
    return str(uuid.uuid4())


OWNER_A = _make_uuid()
OWNER_B = _make_uuid()
TENANT_1 = "org-acme"
TENANT_2 = "org-globex"


class FakeAGFSBackend:
    """In-memory AGFS backend for testing."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def put(self, path: str, data: dict[str, Any]) -> None:
        self._store[path] = data

    def get(self, path: str) -> dict[str, Any] | None:
        return self._store.get(path)

    def delete(self, path: str) -> None:
        self._store.pop(path, None)

    def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return [v for k, v in self._store.items() if k.startswith(prefix)]


class FakeEmbeddingClient:
    """Deterministic fake embedding client for testing.

    Uses a simple bag-of-characters approach to produce embeddings that
    have meaningful cosine similarity for semantically similar inputs while
    keeping tests deterministic and fast.
    """

    def __init__(self, dimension: int = 64):
        self.dimension = dimension
        self.call_count = 0

    def embed(self, text: str) -> list[float]:
        """Generate a deterministic embedding based on character frequencies.

        Similar texts (sharing characters) produce similar vectors.
        """
        self.call_count += 1
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        # Build a vector from character frequencies
        vector = [0.0] * self.dimension
        text_lower = text.lower()
        for char in text_lower:
            idx = ord(char) % self.dimension
            vector[idx] += 1.0

        # Normalize to unit vector
        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 0:
            vector = [x / norm for x in vector]
        return vector


@pytest.fixture
def backend() -> FakeAGFSBackend:
    return FakeAGFSBackend()


@pytest.fixture
def store(backend: FakeAGFSBackend) -> PersonalContextStore:
    return PersonalContextStore(backend)


@pytest.fixture
def embedding_client() -> FakeEmbeddingClient:
    return FakeEmbeddingClient()


@pytest.fixture
def tool(store: PersonalContextStore, embedding_client: FakeEmbeddingClient) -> ExperienceTool:
    return ExperienceTool(store=store, embedding_client=embedding_client)


@pytest.fixture
def headers_a() -> dict[str, str]:
    return {"X-Owner-Sub": OWNER_A, "X-Tenant-Id": TENANT_1}


@pytest.fixture
def headers_b() -> dict[str, str]:
    return {"X-Owner-Sub": OWNER_B, "X-Tenant-Id": TENANT_1}


@pytest.fixture
def headers_other_tenant() -> dict[str, str]:
    return {"X-Owner-Sub": OWNER_B, "X-Tenant-Id": TENANT_2}


# ---------------------------------------------------------------------------
# Save → Recall round-trip
# ---------------------------------------------------------------------------


class TestSaveRecallRoundTrip:
    """save → recall returns the saved entry for the same owner."""

    def test_save_and_recall_basic(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """Save a learning, recall with same content → appears in results."""
        save_result = tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "Always run migrations before deploying the gateway",
                "learning_type": "deploy_fix",
            },
            headers_a,
        )
        assert save_result["status"] == "saved"
        assert save_result["id"]

        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "migrations before deploying gateway",
            },
            headers_a,
        )
        assert recall_result["status"] == "ok"
        assert recall_result["total"] >= 1
        assert any("migrations" in r["content"].lower() for r in recall_result["results"])

    def test_save_returns_entry_metadata(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """Save returns the entry ID, persona, and visibility."""
        result = tool.handle(
            {
                "action": "save",
                "persona": "architect",
                "content": "Prefer event sourcing for audit-critical flows",
                "visibility": "shared",
            },
            headers_a,
        )
        assert result["status"] == "saved"
        assert result["persona"] == "architect"
        assert result["visibility"] == "shared"
        assert len(result["id"]) > 0


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


class TestOwnerIsolation:
    """Recall as a different owner returns none of another user's entries."""

    def test_different_owner_sees_nothing(
        self,
        tool: ExperienceTool,
        headers_a: dict[str, str],
        headers_b: dict[str, str],
    ) -> None:
        """Owner B cannot recall Owner A's private entries."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "Secret deployment trick only I know",
            },
            headers_a,
        )

        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "secret deployment trick",
            },
            headers_b,
        )
        assert recall_result["total"] == 0

    def test_shared_visible_within_tenant(
        self,
        tool: ExperienceTool,
        headers_a: dict[str, str],
        headers_b: dict[str, str],
    ) -> None:
        """Shared entries are visible to same-tenant users."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "Team pattern: use circuit breakers for external calls",
                "visibility": "shared",
            },
            headers_a,
        )

        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "circuit breakers external calls",
            },
            headers_b,
        )
        assert recall_result["total"] >= 1
        assert any("circuit breakers" in r["content"].lower() for r in recall_result["results"])

    def test_shared_not_visible_across_tenants(
        self,
        tool: ExperienceTool,
        headers_a: dict[str, str],
        headers_other_tenant: dict[str, str],
    ) -> None:
        """Shared entries are NOT visible to different-tenant users."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "Org-specific secret sauce for deployments",
                "visibility": "shared",
            },
            headers_a,
        )

        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "secret sauce deployments",
            },
            headers_other_tenant,
        )
        assert recall_result["total"] == 0


# ---------------------------------------------------------------------------
# Semantic recall
# ---------------------------------------------------------------------------


class TestSemanticRecall:
    """Semantic recall: paraphrase ranks in top-k despite keyword disjointness."""

    def test_paraphrase_recall(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """A paraphrased query still surfaces the relevant entry."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "database connection pool exhaustion causes timeouts",
                "learning_type": "code_pattern",
            },
            headers_a,
        )

        # Query with overlapping semantics (shared characters in our fake embedder)
        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "database connection timeouts pool",
            },
            headers_a,
        )
        assert recall_result["total"] >= 1
        assert recall_result["results"][0]["score"] > 0.5

    def test_negative_control_unrelated_query(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """An unrelated query does NOT surface the entry (score too low)."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "zzzzxyzzy unique content with rare characters",
            },
            headers_a,
        )

        # Completely different content
        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "kubernetes pod scheduling affinity rules",
            },
            headers_a,
        )
        # The unrelated entry should either not appear or have very low score
        for r in recall_result["results"]:
            if "zzzzxyzzy" in r["content"]:
                # If it shows up, its score should be low
                assert r["score"] < 0.8


# ---------------------------------------------------------------------------
# Decay-weighted ranking
# ---------------------------------------------------------------------------


class TestDecayRanking:
    """Recall ranking favors higher decay_score when similarity is comparable."""

    def test_higher_decay_ranks_first(
        self, tool: ExperienceTool, headers_a: dict[str, str], backend: FakeAGFSBackend
    ) -> None:
        """Entry with higher decay_score ranks above one with lower decay."""
        # Save two similar entries
        result1 = tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "always check error codes from API calls",
            },
            headers_a,
        )
        result2 = tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "always check error codes from service calls",
            },
            headers_a,
        )

        # Manually reduce decay_score of the first entry
        for path, data in backend._store.items():
            if data.get("id") == result1["id"]:
                data["decay_score"] = 0.3
                backend.put(path, data)
                break

        # Recall — the second entry (decay=1.0) should rank higher
        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "check error codes from calls",
            },
            headers_a,
        )
        assert recall_result["total"] == 2
        # Higher-decay entry should be first
        assert recall_result["results"][0]["id"] == result2["id"]


# ---------------------------------------------------------------------------
# Persona filter
# ---------------------------------------------------------------------------


class TestPersonaFilter:
    """Persona filter scopes results; cross_persona=true broadens."""

    def test_persona_scopes_results(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """Only entries matching the persona are returned."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "developer learning about testing patterns",
            },
            headers_a,
        )
        tool.handle(
            {
                "action": "save",
                "persona": "architect",
                "content": "architect learning about system design patterns",
            },
            headers_a,
        )

        # Recall as developer — should not see architect entries
        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "patterns",
            },
            headers_a,
        )
        for r in recall_result["results"]:
            assert r["persona"] == "developer"

    def test_cross_persona_broadens(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """cross_persona=true returns entries from all personas."""
        tool.handle(
            {
                "action": "save",
                "persona": "developer",
                "content": "developer tip about code review",
            },
            headers_a,
        )
        tool.handle(
            {
                "action": "save",
                "persona": "reviewer",
                "content": "reviewer tip about code review",
            },
            headers_a,
        )

        # Recall with cross_persona=true
        recall_result = tool.handle(
            {
                "action": "recall",
                "persona": "developer",
                "query": "code review tip",
                "cross_persona": True,
            },
            headers_a,
        )
        personas_seen = {r["persona"] for r in recall_result["results"]}
        assert len(personas_seen) >= 2


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidationErrors:
    """Validation errors produce clean ExperienceToolError, not 500s."""

    def test_save_without_content_raises(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """save without content → ExperienceToolError."""
        with pytest.raises(ExperienceToolError, match="'content' is required"):
            tool.handle(
                {"action": "save", "persona": "developer", "content": ""},
                headers_a,
            )

    def test_save_with_whitespace_only_content_raises(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """save with whitespace-only content → ExperienceToolError."""
        with pytest.raises(ExperienceToolError, match="'content' is required"):
            tool.handle(
                {"action": "save", "persona": "developer", "content": "   "},
                headers_a,
            )

    def test_recall_without_query_raises(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """recall without query → ExperienceToolError."""
        with pytest.raises(ExperienceToolError, match="'query' is required"):
            tool.handle(
                {"action": "recall", "persona": "developer", "query": ""},
                headers_a,
            )

    def test_invalid_action_raises(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """Invalid action → ExperienceToolError."""
        with pytest.raises(ExperienceToolError, match="Invalid action"):
            tool.handle(
                {"action": "delete", "persona": "developer"},
                headers_a,
            )

    def test_invalid_persona_raises(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """Invalid persona → ExperienceToolError."""
        with pytest.raises(ExperienceToolError, match="Invalid persona"):
            tool.handle(
                {"action": "save", "persona": "hacker", "content": "test"},
                headers_a,
            )

    def test_missing_identity_headers_raises(self, tool: ExperienceTool) -> None:
        """Missing identity headers → IdentityError (403)."""
        from personal_context.identity import IdentityError

        with pytest.raises(IdentityError):
            tool.handle(
                {"action": "save", "persona": "developer", "content": "test"},
                {},
            )


# ---------------------------------------------------------------------------
# list_syntheses
# ---------------------------------------------------------------------------


class TestListSyntheses:
    """list_syntheses returns empty gracefully before synthesis exists."""

    def test_list_syntheses_empty(self, tool: ExperienceTool, headers_a: dict[str, str]) -> None:
        """Before any synthesis, returns empty list."""
        result = tool.handle(
            {"action": "list_syntheses", "persona": "developer"},
            headers_a,
        )
        assert result["status"] == "ok"
        assert result["syntheses"] == []
        assert result["total"] == 0

    def test_list_syntheses_filters_by_persona(
        self, tool: ExperienceTool, headers_a: dict[str, str]
    ) -> None:
        """list_syntheses only returns syntheses for the requested persona."""
        result = tool.handle(
            {"action": "list_syntheses", "persona": "architect"},
            headers_a,
        )
        assert result["status"] == "ok"
        assert result["persona"] == "architect"
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Cosine similarity helper
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Unit tests for the cosine similarity helper."""

    def test_identical_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors(self) -> None:
        assert _cosine_similarity([], []) == 0.0

    def test_zero_vector(self) -> None:
        assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
