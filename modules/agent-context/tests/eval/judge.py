"""LLM-as-judge for relevance & helpfulness scoring.

Scores answer quality on a 0-3 scale:
  3 = Fully helpful: accurate, complete, actionable
  2 = Useful with gaps: mostly correct, missing minor detail
  1 = On-topic but unhelpful: relevant subject, wrong/incomplete content
  0 = Wrong/irrelevant/hallucinated

Uses the LiteLLM proxy (same httpx pattern as personal_context/synthesis.py).
Temperature fixed at 0 for determinism.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_JUDGE_MODEL = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_LLM_BASE_URL = "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
LLM_BASE_URL = os.environ.get("EVAL_LLM_BASE_URL", DEFAULT_LLM_BASE_URL)

# Temperature MUST be 0 for deterministic judging. Not configurable.
_JUDGE_TEMPERATURE = 0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class JudgeResult:
    """Result of one LLM judge evaluation."""

    score: int  # 0-3
    justification: str  # Why this score (cites grounding)
    precision: float | None = None  # For search/impact only
    recall: float | None = None  # For search/impact only
    error: str = ""  # Non-empty if judge call failed


@dataclass
class CalibrationItem:
    """One calibration entry with expected score."""

    id: str
    verb: str
    query: str
    response: Any  # Simulated Door response
    expected_substance: str
    grounding_snippet: str
    expected_score: int  # 0-3
    category: str = ""  # "good", "bad", or "echo"


@dataclass
class CalibrationResult:
    """Result of running the calibration set."""

    passed: bool
    details: list[dict] = field(default_factory=list)
    echo_guard_passed: bool = True  # All echo items scored <= 1


# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = """\
You are a grading judge for a code intelligence system. Your job is to evaluate \
whether the system's response is relevant, accurate, and helpful — grounded \
against the real source code, not surface plausibility.

Score the response on a 0-3 scale:
- 3: Fully helpful — covers all key points in expected_substance, accurately \
reflects the real source code, actionable for an agent.
- 2: Useful with gaps — covers most points, minor gaps or imprecision, but \
still directionally correct and useful.
- 1: On-topic but unhelpful — relevant subject area but substantively \
incomplete, misleading, or adds nothing beyond what the query itself says.
- 0: Wrong, irrelevant, hallucinated, or merely echoes the query/metadata.

ANTI-GAMING RULES (mandatory):
- An answer that echoes the query, target name, or metadata WITHOUT adding \
substantive derived content MUST score <= 1, regardless of how on-topic it sounds.
- "Plausible-sounding" is NOT evidence. You must verify claims against the \
grounding_snippet when provided. If the response contradicts the grounding, score 0.
- Generic descriptions that could apply to any code ("this module handles X") \
without citing specific structure/behavior from the real code score <= 1.

For search/impact verbs, also evaluate:
- precision: (relevant items in response) / (total items in response). Items \
that are plausibly real callers/references (verifiable from grounding) are NOT \
precision penalties even if not in expected_substance.
- recall: (expected items found in response) / (total expected items). Missing \
a real expected item is a recall failure.

Respond ONLY with valid JSON (no markdown fences):
{"score": N, "justification": "...", "precision": 0.X_or_null, "recall": 0.X_or_null}
"""


def _build_judge_user_prompt(
    *,
    query: str,
    verb: str,
    response: Any,
    expected_substance: str,
    grounding_snippet: str,
) -> str:
    """Build the user message for the judge."""
    response_text = (
        json.dumps(response, indent=2, default=str) if not isinstance(response, str) else response
    )

    # Truncate very long responses to avoid token waste
    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n... [truncated]"

    parts = [
        f"## QUERY\n{query}",
        f"\n## VERB TYPE\n{verb}",
        f"\n## SYSTEM RESPONSE\n{response_text}",
        f"\n## EXPECTED SUBSTANCE (what a good answer must convey)\n{expected_substance}",
    ]

    if grounding_snippet:
        snippet = grounding_snippet
        if len(snippet) > 3000:
            snippet = snippet[:3000] + "\n... [truncated]"
        parts.append(f"\n## REAL SOURCE CODE (ground truth)\n{snippet}")

    parts.append(
        "\n## YOUR TASK\n"
        "Score 0-3 per the rubric. Cite which parts of the grounding support "
        "or contradict the response. For search/impact verbs, include precision "
        "and recall. Respond with JSON only."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Judge client
# ---------------------------------------------------------------------------


class RelevanceJudge:
    """LLM-as-judge for answer relevance and helpfulness.

    Calls the LiteLLM proxy with temperature=0 for deterministic grading.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or JUDGE_MODEL
        self.timeout = timeout

    def grade(
        self,
        *,
        query: str,
        verb: str,
        response: Any,
        expected_substance: str,
        grounding_snippet: str = "",
    ) -> JudgeResult:
        """Grade a single response.

        Parameters
        ----------
        query:
            The original query sent to the Door.
        verb:
            MCP verb type (search_exact, search_semantic, understand, impact, browse).
        response:
            The Door's actual response (dict or string).
        expected_substance:
            Human-written description of what a good answer must convey.
        grounding_snippet:
            Frozen source code the answer should derive from (optional).

        Returns
        -------
        JudgeResult with score 0-3, justification, and optional precision/recall.
        """
        user_prompt = _build_judge_user_prompt(
            query=query,
            verb=verb,
            response=response,
            expected_substance=expected_substance,
            grounding_snippet=grounding_snippet,
        )

        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": _JUDGE_TEMPERATURE,
                    "max_tokens": 512,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as e:
            log.error("Judge LLM call failed: %s", e)
            return JudgeResult(score=0, justification="", error=str(e))

        return self._parse_response(resp.json())

    def run_calibration(self, calibration_set: list[CalibrationItem]) -> CalibrationResult:
        """Run the calibration set and verify judge trustworthiness.

        The eval run is INVALID if calibration fails.

        Rules:
        - Each item's actual score must be within +/- 1 of expected_score.
        - All "echo" category items must score <= 1.
        """
        details: list[dict] = []
        all_within_tolerance = True
        echo_guard_passed = True

        for item in calibration_set:
            result = self.grade(
                query=item.query,
                verb=item.verb,
                response=item.response,
                expected_substance=item.expected_substance,
                grounding_snippet=item.grounding_snippet,
            )

            within_tolerance = abs(result.score - item.expected_score) <= 1
            echo_ok = True
            if item.category == "echo" and result.score > 1:
                echo_ok = False
                echo_guard_passed = False

            if not within_tolerance:
                all_within_tolerance = False

            details.append(
                {
                    "id": item.id,
                    "category": item.category,
                    "expected_score": item.expected_score,
                    "actual_score": result.score,
                    "within_tolerance": within_tolerance,
                    "echo_ok": echo_ok,
                    "justification": result.justification,
                }
            )

            log.info(
                "Calibration [%s] (%s): expected=%d, actual=%d, ok=%s",
                item.id,
                item.category,
                item.expected_score,
                result.score,
                within_tolerance and echo_ok,
            )

        passed = all_within_tolerance and echo_guard_passed

        return CalibrationResult(
            passed=passed,
            details=details,
            echo_guard_passed=echo_guard_passed,
        )

    @staticmethod
    def _parse_response(data: dict) -> JudgeResult:
        """Parse LLM JSON response into JudgeResult."""
        content = data["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            log.warning("Judge returned non-JSON: %s", content[:200])
            return JudgeResult(
                score=0,
                justification=f"Judge returned non-JSON: {content[:200]}",
                error="json_parse_error",
            )

        score = int(parsed.get("score", 0))
        # Clamp to valid range
        score = max(0, min(3, score))

        return JudgeResult(
            score=score,
            justification=parsed.get("justification", ""),
            precision=parsed.get("precision"),
            recall=parsed.get("recall"),
        )
