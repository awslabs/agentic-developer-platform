"""Unit tests for Neptune graph edges in personal-context relationships.

Validates:
- Vertex upsert always sets owner_sub/tenant_id; traversal filters on them
  (a different owner's vertices are unreachable).
- Each edge type (contradicts, derived_from, supports, exemplifies, cross_persona)
  writes + reads back.
- Flag off -> no Neptune calls; #3.1 adjacency-lists used; everything still works.
- Neptune unreachable while flag on -> graceful fallback (logged), recall still
  returns flat results, no crash.
- Cross-tenant traversal returns nothing.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from personal_context import graph as personal_graph
from personal_context.experience_tool import ExperienceTool
from personal_context.identity import CallerIdentity
from personal_context.models import EntryType, Persona, PersonalContextEntry, Visibility
from personal_context.storage import PersonalContextStore, build_entry_path
from personal_context.synthesis import SynthesisPipeline, SynthesisResult


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


class FakeNeptuneGraph:
    """In-memory Neptune graph simulator for testing.

    Tracks vertices and edges to validate graph operations without
    a real Neptune cluster.
    """

    def __init__(self) -> None:
        self.vertices: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.query_log: list[str] = []
        self.fail_next: bool = False

    def execute(self, query: str) -> dict[str, Any] | None:
        """Simulate Gremlin query execution."""
        self.query_log.append(query)
        if self.fail_next:
            return None

        # Parse vertex upsert
        if "addV('personal_context')" in query or "coalesce(unfold()" in query:
            entry_id = self._extract_property(query, "entry_id")
            if entry_id:
                props = {
                    "entry_id": entry_id,
                    "owner_sub": self._extract_property(query, "owner_sub") or "",
                    "tenant_id": self._extract_property(query, "tenant_id") or "",
                    "type": self._extract_property(query, "type") or "",
                    "persona": self._extract_property(query, "persona") or "",
                    "visibility": self._extract_property(query, "visibility") or "",
                }
                self.vertices[entry_id] = props
            return {"status": {"code": 200}}

        # Parse edge add
        if "addE(" in query:
            from_id = self._extract_has_entry_id(query, 0)
            to_id = self._extract_has_entry_id(query, 1)
            edge_type = self._extract_edge_type(query)
            if from_id and to_id and edge_type:
                self.edges.append(
                    {
                        "from": from_id,
                        "to": to_id,
                        "type": edge_type,
                    }
                )
            return {"status": {"code": 200}}

        # Parse neighbor query (bothE)
        if "bothE()" in query:
            center_id = self._extract_has_entry_id(query, 0)
            owner_filter = self._extract_property(query, "owner_sub")
            tenant_filter = self._extract_property(query, "tenant_id")

            neighbors = []
            for edge in self.edges:
                neighbor_id = None
                direction = None
                if edge["from"] == center_id:
                    neighbor_id = edge["to"]
                    direction = "outgoing"
                elif edge["to"] == center_id:
                    neighbor_id = edge["from"]
                    direction = "incoming"

                if neighbor_id and neighbor_id in self.vertices:
                    v = self.vertices[neighbor_id]
                    # Apply isolation filter
                    if v["owner_sub"] == owner_filter or (
                        v["visibility"] == "shared" and v["tenant_id"] == tenant_filter
                    ):
                        neighbors.append(
                            {
                                "entry_id": neighbor_id,
                                "type": v["type"],
                                "persona": v["persona"],
                                "edge_type": edge["type"],
                                "direction": direction,
                            }
                        )

            return {
                "result": {
                    "data": {
                        "@value": neighbors,
                    }
                }
            }

        # Parse drop query
        if ".drop()" in query:
            entry_id = self._extract_has_entry_id(query, 0)
            if entry_id:
                self.vertices.pop(entry_id, None)
                self.edges = [
                    e for e in self.edges if e["from"] != entry_id and e["to"] != entry_id
                ]
            return {"status": {"code": 200}}

        return {"status": {"code": 200}}

    @staticmethod
    def _extract_property(query: str, prop_name: str) -> str | None:
        """Extract a property value from a Gremlin query string."""
        import re

        pattern = rf"\.property\('{prop_name}', '([^']*)'\)"
        match = re.search(pattern, query)
        if match:
            return match.group(1)
        # Also try has() form (with or without leading dot — inside or() has no dot)
        pattern = rf"has\('{prop_name}', '([^']*)'\)"
        match = re.search(pattern, query)
        return match.group(1) if match else None

    @staticmethod
    def _extract_has_entry_id(query: str, index: int) -> str | None:
        """Extract the nth entry_id from has() clauses in the query."""
        import re

        matches = re.findall(r"has\('entry_id', '([^']*)'\)", query)
        if index < len(matches):
            return matches[index]
        return None

    @staticmethod
    def _extract_edge_type(query: str) -> str | None:
        """Extract edge type from addE() or outE() in the query."""
        import re

        match = re.search(r"addE\('([^']*)'\)", query)
        return match.group(1) if match else None


class FakeLLMClient:
    """Fake LLM client for synthesis tests."""

    def __init__(self, result: SynthesisResult | None = None):
        self.result = result or SynthesisResult(
            insights=["Combined insight"],
            contradictions=[],
            patterns=["Pattern"],
        )
        self.call_count = 0
        self.model = "test-model"

    def synthesize(self, learnings: list[dict[str, Any]], persona: str) -> SynthesisResult:
        self.call_count += 1
        return self.result


class FakeEmbeddingClient:
    """Deterministic fake embedding client."""

    def __init__(self, dimension: int = 64):
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        import math

        vector = [0.0] * self.dimension
        for char in text.lower():
            idx = ord(char) % self.dimension
            vector[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 0:
            vector = [x / norm for x in vector]
        return vector


def _create_learning(
    owner_sub: str,
    tenant_id: str,
    persona: str = "developer",
    content: str = "test learning",
    visibility: str = "private",
) -> PersonalContextEntry:
    """Create a test learning entry."""
    from ulid import ULID

    return PersonalContextEntry(
        id=str(ULID()),
        type=EntryType.learning,
        owner_sub=owner_sub,
        tenant_id=tenant_id,
        visibility=Visibility(visibility),
        persona=Persona(persona),
        content=content,
        learning_type="test",
        context={},
        confidence=0.7,
    )


def _seed_learnings(
    backend: FakeAGFSBackend,
    owner_sub: str,
    tenant_id: str,
    count: int = 5,
    persona: str = "developer",
) -> list[PersonalContextEntry]:
    """Seed N learnings for a given owner."""
    entries = []
    for i in range(count):
        entry = _create_learning(
            owner_sub=owner_sub,
            tenant_id=tenant_id,
            persona=persona,
            content=f"Learning {i + 1} about topic {i + 1}",
        )
        path = build_entry_path(entry)
        backend.put(path, entry.model_dump())
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_neptune() -> FakeNeptuneGraph:
    return FakeNeptuneGraph()


@pytest.fixture
def backend() -> FakeAGFSBackend:
    return FakeAGFSBackend()


@pytest.fixture
def store(backend: FakeAGFSBackend) -> PersonalContextStore:
    return PersonalContextStore(backend)


# ---------------------------------------------------------------------------
# Test: Vertex upsert always sets owner_sub/tenant_id
# ---------------------------------------------------------------------------


class TestVertexUpsert:
    """Vertex upsert always sets owner_sub/tenant_id; traversal filters on them."""

    def test_upsert_sets_mandatory_properties(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Every vertex carries owner_sub and tenant_id."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.upsert_vertex(
                entry_id="01ABC123",
                owner_sub=OWNER_A,
                tenant_id=TENANT_1,
                entry_type="learning",
                persona="developer",
                visibility="private",
            )

        assert result is True
        vertex = fake_neptune.vertices.get("01ABC123")
        assert vertex is not None
        assert vertex["owner_sub"] == OWNER_A
        assert vertex["tenant_id"] == TENANT_1
        assert vertex["type"] == "learning"
        assert vertex["persona"] == "developer"
        assert vertex["visibility"] == "private"

    def test_upsert_refuses_without_owner_sub(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Vertex upsert fails if owner_sub is empty."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.upsert_vertex(
                entry_id="01ABC456",
                owner_sub="",
                tenant_id=TENANT_1,
                entry_type="learning",
                persona="developer",
                visibility="private",
            )

        assert result is False
        assert "01ABC456" not in fake_neptune.vertices

    def test_upsert_refuses_without_tenant_id(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Vertex upsert fails if tenant_id is empty."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.upsert_vertex(
                entry_id="01ABC789",
                owner_sub=OWNER_A,
                tenant_id="",
                entry_type="learning",
                persona="developer",
                visibility="private",
            )

        assert result is False
        assert "01ABC789" not in fake_neptune.vertices

    def test_traversal_filters_by_owner(self, fake_neptune: FakeNeptuneGraph) -> None:
        """A different owner's vertices are unreachable in traversal."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            # Create vertices for two owners
            personal_graph.upsert_vertex(
                "entry-a1", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            personal_graph.upsert_vertex(
                "entry-b1", OWNER_B, TENANT_1, "learning", "developer", "private"
            )
            # Connect them
            personal_graph.add_edge("entry-a1", "entry-b1", "supports")

            # Owner A should NOT see Owner B's private vertex
            identity_a = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            neighbors = personal_graph.get_neighbors("entry-a1", identity_a)

        # Owner B's private vertex is not visible to Owner A
        assert all(n["entry_id"] != "entry-b1" for n in neighbors)


# ---------------------------------------------------------------------------
# Test: Each edge type writes + reads back
# ---------------------------------------------------------------------------


class TestEdgeTypes:
    """Each edge type (contradicts, derived_from, etc.) writes + reads back."""

    @pytest.mark.parametrize(
        "edge_type",
        [
            "derived_from",
            "contradicts",
            "supports",
            "exemplifies",
            "cross_persona",
        ],
    )
    def test_edge_type_roundtrip(self, fake_neptune: FakeNeptuneGraph, edge_type: str) -> None:
        """Edge of given type is written and readable via get_neighbors."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            # Create two vertices owned by the same user
            personal_graph.upsert_vertex(
                "src-1", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            personal_graph.upsert_vertex(
                "dst-1", OWNER_A, TENANT_1, "synthesis", "developer", "private"
            )

            # Add edge
            result = personal_graph.add_edge("src-1", "dst-1", edge_type)
            assert result is True

            # Verify edge exists in graph
            assert len(fake_neptune.edges) == 1
            assert fake_neptune.edges[0]["from"] == "src-1"
            assert fake_neptune.edges[0]["to"] == "dst-1"
            assert fake_neptune.edges[0]["type"] == edge_type

            # Verify traversal returns the neighbor
            identity = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            neighbors = personal_graph.get_neighbors("src-1", identity)

        assert len(neighbors) == 1
        assert neighbors[0]["entry_id"] == "dst-1"
        assert neighbors[0]["edge_type"] == edge_type
        assert neighbors[0]["direction"] == "outgoing"

    def test_invalid_edge_type_rejected(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Invalid edge types are rejected."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.add_edge("src", "dst", "invalid_type")

        assert result is False
        assert len(fake_neptune.edges) == 0

    def test_cross_persona_edge_with_properties(self, fake_neptune: FakeNeptuneGraph) -> None:
        """cross_persona edge can carry transfer_context property."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            personal_graph.upsert_vertex(
                "cp-1", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            personal_graph.upsert_vertex(
                "cp-2", OWNER_A, TENANT_1, "learning", "architect", "private"
            )
            result = personal_graph.add_edge(
                "cp-1",
                "cp-2",
                "cross_persona",
                properties={"transfer_context": "applies to system design too"},
            )

        assert result is True
        # Verify the query included the property
        edge_query = [q for q in fake_neptune.query_log if "addE" in q]
        assert len(edge_query) == 1
        assert "transfer_context" in edge_query[0]


# ---------------------------------------------------------------------------
# Test: Flag off -> no Neptune calls
# ---------------------------------------------------------------------------


class TestFlagOff:
    """Flag off -> no Neptune calls; #3.1 adjacency-lists used; everything still works."""

    def test_upsert_noop_when_disabled(self) -> None:
        """upsert_vertex returns False and makes no calls when flag is off."""
        with patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False):
            result = personal_graph.upsert_vertex(
                "test-id", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
        assert result is False

    def test_add_edge_noop_when_disabled(self) -> None:
        """add_edge returns False when flag is off."""
        with patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False):
            result = personal_graph.add_edge("src", "dst", "contradicts")
        assert result is False

    def test_get_neighbors_empty_when_disabled(self) -> None:
        """get_neighbors returns empty list when flag is off."""
        with patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False):
            identity = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            result = personal_graph.get_neighbors("test-id", identity)
        assert result == []

    def test_synthesis_uses_adjacency_lists_only_when_disabled(
        self, backend: FakeAGFSBackend, store: PersonalContextStore
    ) -> None:
        """When flag is off, synthesis writes adjacency-lists but no Neptune calls."""
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Insight"],
                contradictions=[{"entry_a": 1, "entry_b": 2, "description": "conflict"}],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)
        entries = _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False),
            patch.object(personal_graph, "_execute_gremlin") as mock_exec,
        ):
            pipeline.run()

        # No Neptune calls made
        mock_exec.assert_not_called()

        # But adjacency-lists are still written
        path_a = build_entry_path(entries[0])
        data_a = backend.get(path_a)
        assert data_a is not None
        assert data_a["context"].get("contradiction_detected") is True

    def test_recall_works_without_graph(
        self, backend: FakeAGFSBackend, store: PersonalContextStore
    ) -> None:
        """Recall works normally when graph is disabled (no graph_neighbors key)."""
        embedding_client = FakeEmbeddingClient()
        tool = ExperienceTool(store=store, embedding_client=embedding_client)
        headers = {"X-Owner-Sub": OWNER_A, "X-Tenant-Id": TENANT_1}

        tool.handle(
            {"action": "save", "persona": "developer", "content": "test learning content"},
            headers,
        )

        with patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False):
            result = tool.handle(
                {
                    "action": "recall",
                    "persona": "developer",
                    "query": "test learning",
                    "graph_expand": True,
                },
                headers,
            )

        assert result["status"] == "ok"
        assert result["graph_expanded"] is False
        # No graph_neighbors key when graph is off
        for r in result["results"]:
            assert "graph_neighbors" not in r


# ---------------------------------------------------------------------------
# Test: Neptune unreachable -> graceful fallback
# ---------------------------------------------------------------------------


class TestNeptuneUnreachable:
    """Neptune unreachable while flag on -> graceful fallback, no crash."""

    def test_upsert_graceful_on_failure(self, fake_neptune: FakeNeptuneGraph) -> None:
        """upsert_vertex returns False when Neptune is unreachable."""
        fake_neptune.fail_next = True
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.upsert_vertex(
                "test-id", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
        assert result is False

    def test_add_edge_graceful_on_failure(self, fake_neptune: FakeNeptuneGraph) -> None:
        """add_edge returns False when Neptune is unreachable."""
        fake_neptune.fail_next = True
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = personal_graph.add_edge("src", "dst", "contradicts")
        assert result is False

    def test_get_neighbors_empty_on_failure(self, fake_neptune: FakeNeptuneGraph) -> None:
        """get_neighbors returns empty list when Neptune is unreachable."""
        fake_neptune.fail_next = True
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            identity = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            result = personal_graph.get_neighbors("test-id", identity)
        assert result == []

    def test_recall_returns_flat_results_on_neptune_failure(
        self, backend: FakeAGFSBackend, store: PersonalContextStore
    ) -> None:
        """Recall still returns flat results when Neptune fails (no crash)."""
        embedding_client = FakeEmbeddingClient()
        tool = ExperienceTool(store=store, embedding_client=embedding_client)
        headers = {"X-Owner-Sub": OWNER_A, "X-Tenant-Id": TENANT_1}

        tool.handle(
            {"action": "save", "persona": "developer", "content": "important learning"},
            headers,
        )

        def failing_execute(query: str) -> None:
            return None

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", failing_execute),
        ):
            result = tool.handle(
                {
                    "action": "recall",
                    "persona": "developer",
                    "query": "important learning",
                    "graph_expand": True,
                },
                headers,
            )

        # Recall still works — results returned without graph_neighbors
        assert result["status"] == "ok"
        assert result["total"] >= 1
        assert result["graph_expanded"] is True
        # No graph_neighbors because Neptune failed
        for r in result["results"]:
            assert "graph_neighbors" not in r

    def test_synthesis_continues_on_neptune_failure(
        self, backend: FakeAGFSBackend, store: PersonalContextStore
    ) -> None:
        """Synthesis completes successfully even when Neptune writes fail."""
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Insight from synthesis"],
                contradictions=[],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        def failing_execute(query: str) -> None:
            return None

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", failing_execute),
        ):
            metrics = pipeline.run()

        # Synthesis still completes
        assert metrics.users_processed == 1
        assert metrics.syntheses_created >= 1
        # AGFS entries still written
        syntheses = backend.list_prefix(f"/personal/{OWNER_A}/syntheses/")
        assert len(syntheses) >= 1


# ---------------------------------------------------------------------------
# Test: Cross-tenant traversal returns nothing
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Cross-tenant traversal returns nothing."""

    def test_different_tenant_private_vertex_invisible(
        self, fake_neptune: FakeNeptuneGraph
    ) -> None:
        """Private vertices in a different tenant are invisible."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            # Owner A in Tenant 1
            personal_graph.upsert_vertex(
                "t1-entry", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            # Owner B in Tenant 2
            personal_graph.upsert_vertex(
                "t2-entry", OWNER_B, TENANT_2, "learning", "developer", "private"
            )
            # Connect them
            personal_graph.add_edge("t1-entry", "t2-entry", "supports")

            # Owner A (Tenant 1) traverses — should NOT see Tenant 2's private vertex
            identity_a = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            neighbors = personal_graph.get_neighbors("t1-entry", identity_a)

        assert len(neighbors) == 0

    def test_shared_visible_within_same_tenant(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Shared vertices within the same tenant ARE visible."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            # Owner A in Tenant 1
            personal_graph.upsert_vertex(
                "own-entry", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            # Owner B in Tenant 1 (same tenant), shared visibility
            personal_graph.upsert_vertex(
                "shared-entry", OWNER_B, TENANT_1, "learning", "developer", "shared"
            )
            # Connect them
            personal_graph.add_edge("own-entry", "shared-entry", "supports")

            # Owner A (Tenant 1) traverses — should see the shared entry
            identity_a = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            neighbors = personal_graph.get_neighbors("own-entry", identity_a)

        assert len(neighbors) == 1
        assert neighbors[0]["entry_id"] == "shared-entry"

    def test_shared_not_visible_across_tenants(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Shared vertices in a different tenant are NOT visible."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            # Owner A in Tenant 1
            personal_graph.upsert_vertex(
                "my-entry", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            # Owner B in Tenant 2, shared visibility (different tenant!)
            personal_graph.upsert_vertex(
                "other-shared", OWNER_B, TENANT_2, "learning", "developer", "shared"
            )
            # Connect them
            personal_graph.add_edge("my-entry", "other-shared", "supports")

            # Owner A (Tenant 1) traverses — should NOT see Tenant 2's shared vertex
            identity_a = CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)
            neighbors = personal_graph.get_neighbors("my-entry", identity_a)

        assert len(neighbors) == 0


# ---------------------------------------------------------------------------
# Test: Synthesis writes graph edges when flag is on
# ---------------------------------------------------------------------------


class TestSynthesisGraphIntegration:
    """Synthesis writes Neptune edges alongside adjacency-lists when flag is on."""

    def test_synthesis_writes_derived_from_edges(
        self, backend: FakeAGFSBackend, store: PersonalContextStore, fake_neptune: FakeNeptuneGraph
    ) -> None:
        """Synthesis creates derived_from edges from synthesis to source learnings."""
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Synthesized insight"],
                contradictions=[],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)
        entries = _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            metrics = pipeline.run()

        assert metrics.syntheses_created == 1

        # Verify vertices were created (synthesis + 5 source learnings = 6)
        assert len(fake_neptune.vertices) == 6

        # Verify derived_from edges (one per source learning)
        derived_edges = [e for e in fake_neptune.edges if e["type"] == "derived_from"]
        assert len(derived_edges) == 5

        # Each derived_from edge goes FROM synthesis TO a source learning
        source_ids = {e.id for e in entries}
        for edge in derived_edges:
            assert edge["to"] in source_ids

    def test_synthesis_writes_contradicts_edges(
        self, backend: FakeAGFSBackend, store: PersonalContextStore, fake_neptune: FakeNeptuneGraph
    ) -> None:
        """Synthesis creates contradicts edges when contradictions found."""
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Insight"],
                contradictions=[{"entry_a": 1, "entry_b": 2, "description": "conflict"}],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)
        entries = _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            metrics = pipeline.run()

        assert metrics.contradictions_found == 1

        # Verify contradicts edges (bidirectional = 2 edges)
        contradicts_edges = [e for e in fake_neptune.edges if e["type"] == "contradicts"]
        assert len(contradicts_edges) == 2
        # One direction: entry_a -> entry_b
        assert any(
            e["from"] == entries[0].id and e["to"] == entries[1].id for e in contradicts_edges
        )
        # Other direction: entry_b -> entry_a
        assert any(
            e["from"] == entries[1].id and e["to"] == entries[0].id for e in contradicts_edges
        )


# ---------------------------------------------------------------------------
# Test: Graph-expanded recall
# ---------------------------------------------------------------------------


class TestGraphExpandedRecall:
    """Recall with graph_expand returns 1-hop neighborhood."""

    def test_recall_with_graph_expand(
        self, backend: FakeAGFSBackend, store: PersonalContextStore, fake_neptune: FakeNeptuneGraph
    ) -> None:
        """Recall with graph_expand=True includes graph_neighbors in results."""
        embedding_client = FakeEmbeddingClient()
        tool = ExperienceTool(store=store, embedding_client=embedding_client)
        headers = {"X-Owner-Sub": OWNER_A, "X-Tenant-Id": TENANT_1}

        # Save and recall
        save_result = tool.handle(
            {"action": "save", "persona": "developer", "content": "graph test learning"},
            headers,
        )
        entry_id = save_result["id"]

        # Set up graph with a neighbor
        fake_neptune.vertices[entry_id] = {
            "entry_id": entry_id,
            "owner_sub": OWNER_A,
            "tenant_id": TENANT_1,
            "type": "learning",
            "persona": "developer",
            "visibility": "private",
        }
        fake_neptune.vertices["synth-1"] = {
            "entry_id": "synth-1",
            "owner_sub": OWNER_A,
            "tenant_id": TENANT_1,
            "type": "synthesis",
            "persona": "developer",
            "visibility": "private",
        }
        fake_neptune.edges.append(
            {
                "from": "synth-1",
                "to": entry_id,
                "type": "derived_from",
            }
        )

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            result = tool.handle(
                {
                    "action": "recall",
                    "persona": "developer",
                    "query": "graph test learning",
                    "graph_expand": True,
                },
                headers,
            )

        assert result["status"] == "ok"
        assert result["graph_expanded"] is True
        assert result["total"] >= 1
        # The first result should have graph_neighbors
        first_result = result["results"][0]
        assert "graph_neighbors" in first_result
        assert len(first_result["graph_neighbors"]) == 1
        assert first_result["graph_neighbors"][0]["entry_id"] == "synth-1"
        assert first_result["graph_neighbors"][0]["edge_type"] == "derived_from"

    def test_recall_without_graph_expand_omits_neighbors(
        self, backend: FakeAGFSBackend, store: PersonalContextStore
    ) -> None:
        """Recall without graph_expand=True does NOT include graph_neighbors."""
        embedding_client = FakeEmbeddingClient()
        tool = ExperienceTool(store=store, embedding_client=embedding_client)
        headers = {"X-Owner-Sub": OWNER_A, "X-Tenant-Id": TENANT_1}

        tool.handle(
            {"action": "save", "persona": "developer", "content": "plain recall test"},
            headers,
        )

        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
        ):
            result = tool.handle(
                {
                    "action": "recall",
                    "persona": "developer",
                    "query": "plain recall test",
                },
                headers,
            )

        assert result["status"] == "ok"
        # No graph_neighbors when graph_expand is not requested
        for r in result["results"]:
            assert "graph_neighbors" not in r


# ---------------------------------------------------------------------------
# Test: remove_vertex
# ---------------------------------------------------------------------------


class TestRemoveVertex:
    """remove_vertex removes a vertex and all edges."""

    def test_remove_vertex_cleans_up(self, fake_neptune: FakeNeptuneGraph) -> None:
        """Removing a vertex also removes all connected edges."""
        with (
            patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True),
            patch.object(personal_graph, "NEPTUNE_ENDPOINT", "test.neptune.amazonaws.com"),
            patch.object(personal_graph, "_execute_gremlin", fake_neptune.execute),
        ):
            personal_graph.upsert_vertex(
                "rm-1", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            personal_graph.upsert_vertex(
                "rm-2", OWNER_A, TENANT_1, "learning", "developer", "private"
            )
            personal_graph.add_edge("rm-1", "rm-2", "supports")

            assert "rm-1" in fake_neptune.vertices
            assert len(fake_neptune.edges) == 1

            result = personal_graph.remove_vertex("rm-1")

        assert result is True
        assert "rm-1" not in fake_neptune.vertices
        assert len(fake_neptune.edges) == 0

    def test_remove_vertex_noop_when_disabled(self) -> None:
        """remove_vertex is a no-op when graph is disabled."""
        with patch.object(personal_graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", False):
            result = personal_graph.remove_vertex("any-id")
        assert result is False
