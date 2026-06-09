"""Personal-context experiential synthesis pipeline ("dream cycle").

Periodically distills a user's raw learnings into higher-order insights,
detects contradictions, decays stale confidence, and marks superseded entries.

Pipeline stages:
1. Enumerate users with >= MIN_LEARNINGS_THRESHOLD unsynthesized learnings
   (or any older than MAX_UNSYNTHESIZED_AGE_DAYS), grouped by owner_sub.
2. Per user-persona: retrieve unsynthesized learnings -> call Claude Sonnet via
   LiteLLM -> write synthesis entries -> mark source learnings synthesized=true.
3. Confidence/decay: entries with last_accessed_at > 30 days -> decay_score
   reduced by 0.1 (floor 0.1). Entries < 0.3 flagged for archival (NOT deleted).
4. Supersession: when a newer learning clearly contradicts + is validated, set
   superseded_by on the older entry.
5. Metrics: log counts.

SAFETY: Never deletes source learnings. Never crosses owner namespaces.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from ulid import ULID

from .identity import CallerIdentity
from .models import EntryType, PersonalContextEntry
from .storage import PersonalContextStore, build_entry_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (environment variables with sane defaults)
# ---------------------------------------------------------------------------

SYNTHESIS_MODEL = os.environ.get("SYNTHESIS_MODEL", "bedrock/global.anthropic.claude-sonnet-4-6")
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"
)
OV_URL = os.environ.get("OV_URL", "http://openviking.agent-context.svc.cluster.local:1933")
MIN_LEARNINGS_THRESHOLD = int(os.environ.get("MIN_LEARNINGS_THRESHOLD", "5"))
MAX_UNSYNTHESIZED_AGE_DAYS = int(os.environ.get("MAX_UNSYNTHESIZED_AGE_DAYS", "7"))
DECAY_IDLE_DAYS = int(os.environ.get("DECAY_IDLE_DAYS", "30"))
DECAY_STEP = float(os.environ.get("DECAY_STEP", "0.1"))
DECAY_FLOOR = float(os.environ.get("DECAY_FLOOR", "0.1"))
ARCHIVAL_THRESHOLD = float(os.environ.get("ARCHIVAL_THRESHOLD", "0.3"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SynthesisMetrics:
    """Aggregate metrics for one synthesis run."""

    users_processed: int = 0
    syntheses_created: int = 0
    contradictions_found: int = 0
    entries_decayed: int = 0
    entries_superseded: int = 0
    errors: int = 0


@dataclass
class SynthesisResult:
    """Output of LLM synthesis for one user-persona batch."""

    insights: list[str] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------


class SynthesisLLMClient:
    """Call Claude Sonnet via LiteLLM for synthesis."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or SYNTHESIS_MODEL
        self.timeout = timeout

    def synthesize(
        self,
        learnings: list[dict[str, Any]],
        persona: str,
    ) -> SynthesisResult:
        """Call LLM to synthesize learnings into insights.

        Parameters
        ----------
        learnings:
            List of learning entry dicts (content, context, confidence, etc.)
        persona:
            The agent persona these learnings belong to.

        Returns
        -------
        SynthesisResult with insights, contradictions, and patterns.
        """
        prompt = self._build_prompt(learnings, persona)

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a synthesis engine. Analyze the provided learnings "
                            "and produce structured output. Respond ONLY with valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return self._parse_response(content)

    @staticmethod
    def _build_prompt(learnings: list[dict[str, Any]], persona: str) -> str:
        """Build the synthesis prompt from learnings."""
        learnings_text = "\n".join(
            f"- [{i + 1}] (id={entry.get('id', 'unknown')}, "
            f"confidence={entry.get('confidence', 0.7)}): "
            f"{entry.get('content', '')}"
            for i, entry in enumerate(learnings)
        )

        return f"""Given these {len(learnings)} learnings for a {persona} agent, analyze them and produce:

1. **Synthesized Insights** (1-3): Higher-order conclusions that emerge from combining multiple learnings.
2. **Contradictions**: Pairs of entries that conflict with each other (reference by entry number).
3. **Recurring Patterns**: Themes that appear across multiple learnings.

## Learnings:
{learnings_text}

## Required JSON output format:
{{
  "insights": ["insight 1", "insight 2"],
  "contradictions": [
    {{"entry_a": 1, "entry_b": 3, "description": "why they conflict"}}
  ],
  "patterns": ["pattern 1", "pattern 2"]
}}

Respond ONLY with valid JSON. Do not include markdown code fences."""

    @staticmethod
    def _parse_response(content: str) -> SynthesisResult:
        """Parse LLM JSON response into SynthesisResult."""
        # Strip any markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first and last lines (code fences)
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM synthesis response as JSON")
            return SynthesisResult()

        return SynthesisResult(
            insights=parsed.get("insights", []),
            contradictions=parsed.get("contradictions", []),
            patterns=parsed.get("patterns", []),
        )


# ---------------------------------------------------------------------------
# Synthesis Pipeline
# ---------------------------------------------------------------------------


class SynthesisPipeline:
    """Orchestrates the full synthesis pipeline.

    SAFETY INVARIANTS:
    - Never deletes source learnings (only marks synthesized=true).
    - Never reads/writes across owner namespaces.
    - Decay only lowers decay_score, never deletes.
    """

    def __init__(
        self,
        store: PersonalContextStore,
        llm_client: SynthesisLLMClient | None = None,
        embedding_client: Any | None = None,
        min_learnings: int = MIN_LEARNINGS_THRESHOLD,
        max_age_days: int = MAX_UNSYNTHESIZED_AGE_DAYS,
        decay_idle_days: int = DECAY_IDLE_DAYS,
        decay_step: float = DECAY_STEP,
        decay_floor: float = DECAY_FLOOR,
    ):
        self.store = store
        self.llm_client = llm_client or SynthesisLLMClient()
        self.embedding_client = embedding_client
        self.min_learnings = min_learnings
        self.max_age_days = max_age_days
        self.decay_idle_days = decay_idle_days
        self.decay_step = decay_step
        self.decay_floor = decay_floor

    def run(self) -> SynthesisMetrics:
        """Execute the full synthesis pipeline.

        Returns
        -------
        SynthesisMetrics with counts of what was processed.
        """
        metrics = SynthesisMetrics()
        now = datetime.now(timezone.utc)

        # Stage 1: Enumerate users with unsynthesized learnings
        user_learnings = self._enumerate_users()
        logger.info("Synthesis: found %d user-persona groups to process", len(user_learnings))

        # Stage 2: Per user-persona synthesis
        for (owner_sub, persona), learnings in user_learnings.items():
            if not self._should_synthesize(learnings, now):
                continue
            try:
                result = self._synthesize_user_persona(owner_sub, persona, learnings, metrics)
                metrics.users_processed += 1
                if result:
                    metrics.syntheses_created += len(result.insights)
                    metrics.contradictions_found += len(result.contradictions)
            except Exception:
                logger.exception(
                    "Error synthesizing for owner=%s persona=%s",
                    owner_sub[:8],
                    persona,
                )
                metrics.errors += 1

        # Stage 3: Confidence decay (across ALL learnings)
        metrics.entries_decayed = self._apply_decay(now)

        logger.info(
            "Synthesis complete: users=%d, syntheses=%d, contradictions=%d, "
            "decayed=%d, superseded=%d, errors=%d",
            metrics.users_processed,
            metrics.syntheses_created,
            metrics.contradictions_found,
            metrics.entries_decayed,
            metrics.entries_superseded,
            metrics.errors,
        )
        return metrics

    def _enumerate_users(self) -> dict[tuple[str, str], list[PersonalContextEntry]]:
        """Enumerate all user-persona groups with unsynthesized learnings.

        Scans private learning paths and groups by (owner_sub, persona).
        Only returns groups meeting the minimum threshold.
        """
        all_learnings = self.store.backend.list_prefix("/personal/")
        groups: dict[tuple[str, str], list[PersonalContextEntry]] = {}

        for data in all_learnings:
            try:
                entry = PersonalContextEntry(**data)
            except Exception:
                continue

            # Only process unsynthesized learnings
            if entry.type != EntryType.learning:
                continue
            if entry.context.get("synthesized"):
                continue

            key = (entry.owner_sub, entry.persona.value)
            groups.setdefault(key, []).append(entry)

        return groups

    def _should_synthesize(self, learnings: list[PersonalContextEntry], now: datetime) -> bool:
        """Determine if a group of learnings qualifies for synthesis.

        Qualifies if:
        - Count >= min_learnings, OR
        - Any learning is older than max_age_days
        """
        if len(learnings) >= self.min_learnings:
            return True

        # Check if any are old enough to force synthesis
        for entry in learnings:
            created = datetime.fromisoformat(entry.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (now - created).days
            if age_days >= self.max_age_days:
                return True

        return False

    def _synthesize_user_persona(
        self,
        owner_sub: str,
        persona: str,
        learnings: list[PersonalContextEntry],
        metrics: SynthesisMetrics,
    ) -> SynthesisResult | None:
        """Run synthesis for one user-persona group.

        Steps:
        1. Call LLM with learnings
        2. Write synthesis entries to user's namespace
        3. Mark contradictions (adjacency-list)
        4. Mark source learnings as synthesized
        5. Handle supersession
        """
        # Prepare learnings for LLM
        learning_dicts = [entry.model_dump() for entry in learnings]
        result = self.llm_client.synthesize(learning_dicts, persona)

        if not result.insights and not result.contradictions:
            return result

        # Determine tenant_id from the learnings (all same owner = same tenant)
        tenant_id = learnings[0].tenant_id
        identity = CallerIdentity(owner_sub=owner_sub, tenant_id=tenant_id)

        # Write synthesis entries
        for insight in result.insights:
            synthesis_entry = {
                "id": str(ULID()),
                "type": "synthesis",
                "owner_sub": owner_sub,
                "tenant_id": tenant_id,
                "visibility": "private",
                "persona": persona,
                "content": insight,
                "learning_type": "synthesized_insight",
                "context": {
                    "source_ids": [e.id for e in learnings],
                    "synthesis_model": self.llm_client.model,
                    "synthesized_at": datetime.now(timezone.utc).isoformat(),
                },
                "confidence": 0.7,
            }
            self.store.write_entry(identity, synthesis_entry)

        # Mark contradictions via adjacency-list in context
        for contradiction in result.contradictions:
            entry_a_idx = contradiction.get("entry_a", 0) - 1  # 1-indexed
            entry_b_idx = contradiction.get("entry_b", 0) - 1
            description = contradiction.get("description", "")

            if 0 <= entry_a_idx < len(learnings) and 0 <= entry_b_idx < len(learnings):
                entry_a = learnings[entry_a_idx]
                entry_b = learnings[entry_b_idx]
                self._mark_contradiction(entry_a, entry_b, description)

                # Stage 4: Supersession — if one is validated and newer, supersede
                self._check_supersession(entry_a, entry_b, metrics)

        # Mark source learnings as synthesized (NEVER delete)
        for entry in learnings:
            self._mark_synthesized(entry)

        return result

    def _mark_contradiction(
        self,
        entry_a: PersonalContextEntry,
        entry_b: PersonalContextEntry,
        description: str,
    ) -> None:
        """Mark two entries as contradicting each other (adjacency-list).

        Writes the relationship into each entry's context dict.
        """
        # Update entry_a
        contradicts_a = entry_a.context.get("contradicts", [])
        contradicts_a.append({"id": entry_b.id, "description": description})
        entry_a.context["contradicts"] = contradicts_a
        entry_a.context["contradiction_detected"] = True
        path_a = build_entry_path(entry_a)
        self.store.backend.put(path_a, entry_a.model_dump())

        # Update entry_b
        contradicts_b = entry_b.context.get("contradicts", [])
        contradicts_b.append({"id": entry_a.id, "description": description})
        entry_b.context["contradicts"] = contradicts_b
        entry_b.context["contradiction_detected"] = True
        path_b = build_entry_path(entry_b)
        self.store.backend.put(path_b, entry_b.model_dump())

    def _check_supersession(
        self,
        entry_a: PersonalContextEntry,
        entry_b: PersonalContextEntry,
        metrics: SynthesisMetrics,
    ) -> None:
        """If one contradicting entry is validated+newer, supersede the older.

        Does NOT delete — only sets superseded_by on the older entry.
        """
        # Only supersede if one is validated
        if not entry_a.validated and not entry_b.validated:
            return

        # Determine which is newer
        created_a = datetime.fromisoformat(entry_a.created_at)
        created_b = datetime.fromisoformat(entry_b.created_at)
        if created_a.tzinfo is None:
            created_a = created_a.replace(tzinfo=timezone.utc)
        if created_b.tzinfo is None:
            created_b = created_b.replace(tzinfo=timezone.utc)

        if entry_b.validated and created_b > created_a and not entry_a.superseded_by:
            # B is newer + validated → supersede A
            entry_a.superseded_by = entry_b.id
            entry_a.confidence = max(self.decay_floor, entry_a.confidence - 0.2)
            path_a = build_entry_path(entry_a)
            self.store.backend.put(path_a, entry_a.model_dump())
            metrics.entries_superseded += 1
        elif entry_a.validated and created_a > created_b and not entry_b.superseded_by:
            # A is newer + validated → supersede B
            entry_b.superseded_by = entry_a.id
            entry_b.confidence = max(self.decay_floor, entry_b.confidence - 0.2)
            path_b = build_entry_path(entry_b)
            self.store.backend.put(path_b, entry_b.model_dump())
            metrics.entries_superseded += 1

    def _mark_synthesized(self, entry: PersonalContextEntry) -> None:
        """Mark a source learning as synthesized (NOT deleted).

        Sets context.synthesized=true so it won't be re-processed.
        """
        entry.context["synthesized"] = True
        path = build_entry_path(entry)
        self.store.backend.put(path, entry.model_dump())

    def _apply_decay(self, now: datetime) -> int:
        """Apply confidence decay to idle entries.

        Entries with last_accessed_at > decay_idle_days get decay_score reduced
        by decay_step (floor decay_floor). NEVER deletes entries.

        Returns count of decayed entries.
        """
        decayed_count = 0
        all_entries = self.store.backend.list_prefix("/personal/")

        for data in all_entries:
            try:
                entry = PersonalContextEntry(**data)
            except Exception:
                continue

            # Only decay learnings and syntheses (not patterns)
            if entry.type not in (EntryType.learning, EntryType.synthesis):
                continue

            last_accessed = datetime.fromisoformat(entry.last_accessed_at)
            if last_accessed.tzinfo is None:
                last_accessed = last_accessed.replace(tzinfo=timezone.utc)

            idle_days = (now - last_accessed).days
            if idle_days < self.decay_idle_days:
                continue

            # Apply decay (floor at decay_floor, never delete)
            new_score = max(self.decay_floor, entry.decay_score - self.decay_step)
            if new_score < entry.decay_score:
                entry.decay_score = new_score
                path = build_entry_path(entry)
                self.store.backend.put(path, entry.model_dump())
                decayed_count += 1

                # Flag for archival if below threshold (informational only)
                if new_score < ARCHIVAL_THRESHOLD:
                    logger.debug(
                        "Entry %s flagged for archival (decay_score=%.2f)",
                        entry.id,
                        new_score,
                    )

        return decayed_count


# ---------------------------------------------------------------------------
# CLI entrypoint (for CronJob execution)
# ---------------------------------------------------------------------------


def _create_agfs_backend() -> Any:
    """Create an AGFS backend connected to OpenViking.

    Returns a backend object with put/get/delete/list_prefix methods.
    """

    class OpenVikingAGFSBackend:
        """AGFS backend using OpenViking HTTP API."""

        def __init__(self, base_url: str, root_key: str | None = None):
            self.base_url = base_url.rstrip("/")
            self.root_key = root_key
            self._headers: dict[str, str] = {}
            if root_key:
                self._headers["Authorization"] = f"Bearer {root_key}"

        def put(self, path: str, data: dict[str, Any]) -> None:
            response = httpx.put(
                f"{self.base_url}/api/v1/files{path}",
                json=data,
                headers=self._headers,
                timeout=30.0,
            )
            response.raise_for_status()

        def get(self, path: str) -> dict[str, Any] | None:
            response = httpx.get(
                f"{self.base_url}/api/v1/files{path}",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

        def delete(self, path: str) -> None:
            response = httpx.delete(
                f"{self.base_url}/api/v1/files{path}",
                headers=self._headers,
                timeout=30.0,
            )
            response.raise_for_status()

        def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
            response = httpx.get(
                f"{self.base_url}/api/v1/files",
                params={"prefix": prefix},
                headers=self._headers,
                timeout=60.0,
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
            result = response.json()
            if isinstance(result, list):
                return result
            return result.get("items", [])

    ov_url = os.environ.get("OV_URL", OV_URL)
    root_key = os.environ.get("OPENVIKING_ROOT_KEY", "")
    return OpenVikingAGFSBackend(ov_url, root_key or None)


def main() -> None:
    """CLI entrypoint for the synthesis CronJob."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    logger.info("=== Personal Context Synthesis Pipeline ===")
    logger.info("Model: %s", SYNTHESIS_MODEL)
    logger.info("Min learnings threshold: %d", MIN_LEARNINGS_THRESHOLD)
    logger.info("Max unsynthesized age: %d days", MAX_UNSYNTHESIZED_AGE_DAYS)

    try:
        backend = _create_agfs_backend()
        store = PersonalContextStore(backend)
        pipeline = SynthesisPipeline(store=store)
        metrics = pipeline.run()

        logger.info("=== Synthesis Pipeline Complete ===")
        logger.info(
            "Results: users=%d syntheses=%d contradictions=%d decayed=%d superseded=%d errors=%d",
            metrics.users_processed,
            metrics.syntheses_created,
            metrics.contradictions_found,
            metrics.entries_decayed,
            metrics.entries_superseded,
            metrics.errors,
        )

        if metrics.errors > 0:
            logger.warning("Completed with %d errors", metrics.errors)
            sys.exit(1)

    except Exception:
        logger.exception("Synthesis pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
