"""Unit tests for the personal-context synthesis pipeline.

Validates:
- With >=5 unsynthesized learnings for one user-persona, the pipeline produces
  >=1 synthesis entry written to that user's namespace only.
- Source learnings get synthesized=true; they are NOT deleted.
- A deliberately contradictory pair is flagged (contradicts relationship +
  contradiction_detected=true).
- Decay lowers decay_score for old unaccessed entries but never below 0.1
  and never deletes.
- Users with <5 unsynthesized entries are skipped (cost guard).
- Synthesis for user A never reads/writes user B's entries (isolation).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from personal_context.models import EntryType, PersonalContextEntry, Persona, Visibility
from personal_context.storage import PersonalContextStore, build_entry_path
from personal_context.synthesis import (
    SynthesisLLMClient,
    SynthesisPipeline,
    SynthesisResult,
)


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


class FakeLLMClient:
    """Fake LLM client that returns configurable synthesis results."""

    def __init__(self, result: SynthesisResult | None = None):
        self.result = result or SynthesisResult(
            insights=["Combined insight from multiple learnings"],
            contradictions=[],
            patterns=["Recurring pattern detected"],
        )
        self.call_count = 0
        self.last_learnings: list[dict[str, Any]] = []
        self.last_persona: str = ""
        self.model = "test-model"

    def synthesize(self, learnings: list[dict[str, Any]], persona: str) -> SynthesisResult:
        self.call_count += 1
        self.last_learnings = learnings
        self.last_persona = persona
        return self.result


def _create_learning(
    owner_sub: str,
    tenant_id: str,
    persona: str = "developer",
    content: str = "test learning",
    synthesized: bool = False,
    created_at: str | None = None,
    last_accessed_at: str | None = None,
    decay_score: float = 1.0,
    validated: bool = False,
    confidence: float = 0.7,
) -> PersonalContextEntry:
    """Create a test learning entry."""
    from ulid import ULID

    now = datetime.now(timezone.utc).isoformat()
    context: dict[str, Any] = {}
    if synthesized:
        context["synthesized"] = True

    return PersonalContextEntry(
        id=str(ULID()),
        type=EntryType.learning,
        owner_sub=owner_sub,
        tenant_id=tenant_id,
        visibility=Visibility.private,
        persona=Persona(persona),
        content=content,
        learning_type="test",
        context=context,
        confidence=confidence,
        validated=validated,
        created_at=created_at or now,
        last_accessed_at=last_accessed_at or now,
        decay_score=decay_score,
    )


def _seed_learnings(
    backend: FakeAGFSBackend,
    owner_sub: str,
    tenant_id: str,
    count: int = 5,
    persona: str = "developer",
    **kwargs: Any,
) -> list[PersonalContextEntry]:
    """Seed N learnings for a given owner in the backend."""
    entries = []
    for i in range(count):
        entry = _create_learning(
            owner_sub=owner_sub,
            tenant_id=tenant_id,
            persona=persona,
            content=f"Learning {i + 1} about topic {i + 1}",
            **kwargs,
        )
        path = build_entry_path(entry)
        backend.put(path, entry.model_dump())
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> FakeAGFSBackend:
    return FakeAGFSBackend()


@pytest.fixture
def store(backend: FakeAGFSBackend) -> PersonalContextStore:
    return PersonalContextStore(backend)


@pytest.fixture
def llm_client() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def pipeline(store: PersonalContextStore, llm_client: FakeLLMClient) -> SynthesisPipeline:
    return SynthesisPipeline(
        store=store,
        llm_client=llm_client,
        min_learnings=5,
        max_age_days=7,
        decay_idle_days=30,
        decay_step=0.1,
        decay_floor=0.1,
    )


# ---------------------------------------------------------------------------
# Test: Pipeline produces synthesis entries for qualifying users
# ---------------------------------------------------------------------------


class TestSynthesisProduction:
    """With >=5 unsynthesized learnings, pipeline produces synthesis entries."""

    def test_produces_synthesis_for_qualifying_user(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """5+ learnings for one user-persona produces >= 1 synthesis entry."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        metrics = pipeline.run()

        assert metrics.users_processed == 1
        assert metrics.syntheses_created >= 1

        # Verify synthesis entry written to owner's namespace
        syntheses = backend.list_prefix(f"/personal/{OWNER_A}/syntheses/")
        assert len(syntheses) >= 1
        for s in syntheses:
            assert s["owner_sub"] == OWNER_A
            assert s["type"] == "synthesis"
            assert s["tenant_id"] == TENANT_1

    def test_synthesis_written_only_to_owners_namespace(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Synthesis entries go to the correct owner's path, not another's."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=6)

        pipeline.run()

        # Owner A has syntheses
        syntheses_a = backend.list_prefix(f"/personal/{OWNER_A}/syntheses/")
        assert len(syntheses_a) >= 1

        # Owner B has nothing
        syntheses_b = backend.list_prefix(f"/personal/{OWNER_B}/syntheses/")
        assert len(syntheses_b) == 0

    def test_old_entries_trigger_synthesis_below_threshold(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Entries older than max_age_days trigger synthesis even with < 5."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        _seed_learnings(backend, OWNER_A, TENANT_1, count=3, created_at=old_date)

        metrics = pipeline.run()

        assert metrics.users_processed == 1
        assert metrics.syntheses_created >= 1


# ---------------------------------------------------------------------------
# Test: Source learnings marked synthesized but NOT deleted
# ---------------------------------------------------------------------------


class TestSourceLearningsPreserved:
    """Source learnings get synthesized=true; they are NOT deleted."""

    def test_source_learnings_marked_synthesized(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """After synthesis, all source learnings have context.synthesized=true."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        pipeline.run()

        # Verify ALL source learnings are marked synthesized
        learnings = backend.list_prefix(f"/personal/{OWNER_A}/learnings/")
        assert len(learnings) == 5  # Still exist (not deleted)
        for learning in learnings:
            assert learning["context"].get("synthesized") is True

    def test_source_learnings_never_deleted(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Source learnings still exist in backend after synthesis."""
        entries = _seed_learnings(backend, OWNER_A, TENANT_1, count=7)
        original_paths = [build_entry_path(e) for e in entries]

        pipeline.run()

        # All original learning entries still exist
        for path in original_paths:
            assert backend.get(path) is not None, f"Learning deleted: {path}"

    def test_already_synthesized_not_reprocessed(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
        llm_client: FakeLLMClient,
    ) -> None:
        """Learnings already marked synthesized=true are not re-processed."""
        # Seed 5 already-synthesized learnings
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5, synthesized=True)

        metrics = pipeline.run()

        # No LLM calls should be made (nothing to process)
        assert llm_client.call_count == 0
        assert metrics.users_processed == 0


# ---------------------------------------------------------------------------
# Test: Contradiction detection
# ---------------------------------------------------------------------------


class TestContradictionDetection:
    """Contradictory pairs are flagged with adjacency-list relationships."""

    def test_contradictions_marked_on_entries(
        self,
        backend: FakeAGFSBackend,
        store: PersonalContextStore,
    ) -> None:
        """Contradictions are written as adjacency-list in entry context."""
        # LLM that reports a contradiction between entries 1 and 2
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Insight"],
                contradictions=[
                    {
                        "entry_a": 1,
                        "entry_b": 2,
                        "description": "Entry 1 says X, entry 2 says not-X",
                    }
                ],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)

        entries = _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        metrics = pipeline.run()

        assert metrics.contradictions_found == 1

        # Verify both entries have contradiction_detected=true
        path_a = build_entry_path(entries[0])
        path_b = build_entry_path(entries[1])
        data_a = backend.get(path_a)
        data_b = backend.get(path_b)

        assert data_a is not None
        assert data_b is not None
        assert data_a["context"].get("contradiction_detected") is True
        assert data_b["context"].get("contradiction_detected") is True

        # Verify adjacency-list relationship
        assert len(data_a["context"]["contradicts"]) == 1
        assert data_a["context"]["contradicts"][0]["id"] == entries[1].id
        assert len(data_b["context"]["contradicts"]) == 1
        assert data_b["context"]["contradicts"][0]["id"] == entries[0].id


# ---------------------------------------------------------------------------
# Test: Confidence decay
# ---------------------------------------------------------------------------


class TestConfidenceDecay:
    """Decay lowers decay_score for old unaccessed entries."""

    def test_decay_reduces_score(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Entries idle > 30 days get decay_score reduced by 0.1."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        entries = _seed_learnings(
            backend,
            OWNER_A,
            TENANT_1,
            count=2,
            last_accessed_at=old_date,
            synthesized=True,  # Already synthesized so no LLM call
        )

        metrics = pipeline.run()

        assert metrics.entries_decayed == 2

        # Verify scores reduced
        for entry in entries:
            path = build_entry_path(entry)
            data = backend.get(path)
            assert data is not None
            assert data["decay_score"] == pytest.approx(0.9)

    def test_decay_never_below_floor(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """decay_score never goes below 0.1 (the floor)."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        # Start at 0.1 (already at floor)
        entries = _seed_learnings(
            backend,
            OWNER_A,
            TENANT_1,
            count=2,
            last_accessed_at=old_date,
            decay_score=0.1,
            synthesized=True,
        )

        metrics = pipeline.run()

        # Should NOT decay further (already at floor)
        assert metrics.entries_decayed == 0

        for entry in entries:
            path = build_entry_path(entry)
            data = backend.get(path)
            assert data is not None
            assert data["decay_score"] == pytest.approx(0.1)

    def test_decay_never_deletes_entries(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Decay NEVER deletes entries, even at minimum score."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        entries = _seed_learnings(
            backend,
            OWNER_A,
            TENANT_1,
            count=3,
            last_accessed_at=old_date,
            decay_score=0.2,
            synthesized=True,
        )

        pipeline.run()

        # All entries still exist
        for entry in entries:
            path = build_entry_path(entry)
            assert backend.get(path) is not None

    def test_recently_accessed_not_decayed(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Entries accessed recently (< 30 days) are not decayed."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        entries = _seed_learnings(
            backend,
            OWNER_A,
            TENANT_1,
            count=3,
            last_accessed_at=recent_date,
            synthesized=True,
        )

        metrics = pipeline.run()

        assert metrics.entries_decayed == 0
        for entry in entries:
            path = build_entry_path(entry)
            data = backend.get(path)
            assert data is not None
            assert data["decay_score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Test: Cost guard — users below threshold skipped
# ---------------------------------------------------------------------------


class TestCostGuard:
    """Users with <5 unsynthesized entries are skipped."""

    def test_below_threshold_skipped(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
        llm_client: FakeLLMClient,
    ) -> None:
        """Users with fewer than min_learnings recent entries are skipped."""
        # Seed only 4 learnings (below threshold of 5)
        _seed_learnings(backend, OWNER_A, TENANT_1, count=4)

        metrics = pipeline.run()

        assert metrics.users_processed == 0
        assert llm_client.call_count == 0

    def test_exactly_at_threshold_processed(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
        llm_client: FakeLLMClient,
    ) -> None:
        """Exactly min_learnings entries triggers synthesis."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)

        metrics = pipeline.run()

        assert metrics.users_processed == 1
        assert llm_client.call_count == 1


# ---------------------------------------------------------------------------
# Test: Cross-user isolation
# ---------------------------------------------------------------------------


class TestOwnerIsolation:
    """Synthesis for user A never reads/writes user B's entries."""

    def test_user_a_synthesis_ignores_user_b(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """User A's synthesis only processes user A's learnings."""
        # Seed learnings for both users
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)
        _seed_learnings(backend, OWNER_B, TENANT_1, count=5)

        pipeline.run()

        # Verify synthesis entries are owner-scoped
        syntheses_a = backend.list_prefix(f"/personal/{OWNER_A}/syntheses/")
        syntheses_b = backend.list_prefix(f"/personal/{OWNER_B}/syntheses/")

        # Both have their own syntheses
        assert len(syntheses_a) >= 1
        assert len(syntheses_b) >= 1

        # Verify owner isolation
        for s in syntheses_a:
            assert s["owner_sub"] == OWNER_A
        for s in syntheses_b:
            assert s["owner_sub"] == OWNER_B

    def test_synthesis_does_not_write_to_other_user(
        self,
        backend: FakeAGFSBackend,
        store: PersonalContextStore,
    ) -> None:
        """Synthesis for user A does not create entries under user B's path."""
        llm_client = FakeLLMClient()
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)

        # Only seed for user A
        _seed_learnings(backend, OWNER_A, TENANT_1, count=6)

        pipeline.run()

        # User B should have nothing
        all_b = backend.list_prefix(f"/personal/{OWNER_B}/")
        assert len(all_b) == 0

    def test_different_personas_processed_separately(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
        llm_client: FakeLLMClient,
    ) -> None:
        """Same user, different personas are processed as separate groups."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5, persona="developer")
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5, persona="architect")

        metrics = pipeline.run()

        # Two groups processed (developer + architect)
        assert metrics.users_processed == 2
        assert llm_client.call_count == 2


# ---------------------------------------------------------------------------
# Test: Supersession
# ---------------------------------------------------------------------------


class TestSupersession:
    """When a newer validated learning contradicts an older, supersede the older."""

    def test_validated_newer_supersedes_older(
        self,
        backend: FakeAGFSBackend,
        store: PersonalContextStore,
    ) -> None:
        """Validated newer entry supersedes the contradicting older entry."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        new_date = datetime.now(timezone.utc).isoformat()

        # Create an older entry
        old_entry = _create_learning(
            owner_sub=OWNER_A,
            tenant_id=TENANT_1,
            content="Old approach: always use synchronous calls",
            created_at=old_date,
        )
        # Create a newer validated entry that contradicts it
        new_entry = _create_learning(
            owner_sub=OWNER_A,
            tenant_id=TENANT_1,
            content="New approach: prefer async for external calls",
            created_at=new_date,
            validated=True,
        )

        # Seed both + 3 more to reach threshold
        backend.put(build_entry_path(old_entry), old_entry.model_dump())
        backend.put(build_entry_path(new_entry), new_entry.model_dump())
        for i in range(3):
            filler = _create_learning(owner_sub=OWNER_A, tenant_id=TENANT_1, content=f"Filler {i}")
            backend.put(build_entry_path(filler), filler.model_dump())

        # LLM reports contradiction between entry 1 (old) and entry 2 (new)
        llm_client = FakeLLMClient(
            result=SynthesisResult(
                insights=["Use async for external calls"],
                contradictions=[{"entry_a": 1, "entry_b": 2, "description": "sync vs async"}],
                patterns=[],
            )
        )
        pipeline = SynthesisPipeline(store=store, llm_client=llm_client, min_learnings=5)

        metrics = pipeline.run()

        assert metrics.entries_superseded == 1

        # Verify old entry is superseded
        data_old = backend.get(build_entry_path(old_entry))
        assert data_old is not None
        assert data_old["superseded_by"] == new_entry.id


# ---------------------------------------------------------------------------
# Test: LLM response parsing
# ---------------------------------------------------------------------------


class TestLLMResponseParsing:
    """Test the LLM response parsing logic."""

    def test_parse_valid_json(self) -> None:
        """Valid JSON response is parsed correctly."""
        content = '{"insights": ["A", "B"], "contradictions": [], "patterns": ["P"]}'
        result = SynthesisLLMClient._parse_response(content)
        assert result.insights == ["A", "B"]
        assert result.contradictions == []
        assert result.patterns == ["P"]

    def test_parse_json_with_code_fences(self) -> None:
        """JSON wrapped in markdown code fences is handled."""
        content = '```json\n{"insights": ["insight"], "contradictions": [], "patterns": []}\n```'
        result = SynthesisLLMClient._parse_response(content)
        assert result.insights == ["insight"]

    def test_parse_invalid_json_returns_empty(self) -> None:
        """Invalid JSON returns empty SynthesisResult."""
        content = "This is not JSON at all"
        result = SynthesisLLMClient._parse_response(content)
        assert result.insights == []
        assert result.contradictions == []
        assert result.patterns == []


# ---------------------------------------------------------------------------
# Test: Metrics tracking
# ---------------------------------------------------------------------------


class TestMetrics:
    """Verify metrics are accurately tracked."""

    def test_metrics_zero_when_nothing_to_process(
        self,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Empty backend produces zero metrics."""
        metrics = pipeline.run()
        assert metrics.users_processed == 0
        assert metrics.syntheses_created == 0
        assert metrics.contradictions_found == 0
        assert metrics.entries_decayed == 0
        assert metrics.entries_superseded == 0
        assert metrics.errors == 0

    def test_metrics_count_multiple_users(
        self,
        backend: FakeAGFSBackend,
        pipeline: SynthesisPipeline,
    ) -> None:
        """Metrics accurately count across multiple users."""
        _seed_learnings(backend, OWNER_A, TENANT_1, count=5)
        _seed_learnings(backend, OWNER_B, TENANT_2, count=5)

        metrics = pipeline.run()

        assert metrics.users_processed == 2
        # Default FakeLLMClient produces 1 insight per call
        assert metrics.syntheses_created == 2
