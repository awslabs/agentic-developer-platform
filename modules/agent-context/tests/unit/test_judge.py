"""Unit tests for the relevance judge module.

Tests judge logic (prompt building, response parsing, calibration) using
mocked HTTP responses. Does NOT require a live LLM endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tests.eval.judge import (
    CalibrationItem,
    RelevanceJudge,
    _build_judge_user_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_llm_response(score: int, justification: str, precision=None, recall=None) -> dict:
    """Build a mock LiteLLM chat/completions response."""
    content = {"score": score, "justification": justification}
    if precision is not None:
        content["precision"] = precision
    if recall is not None:
        content["recall"] = recall
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


class FakeHTTPResponse:
    """Minimal mock for httpx.Response."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# Tests: _build_judge_user_prompt
# ---------------------------------------------------------------------------


class TestBuildJudgeUserPrompt:
    """Test prompt construction logic."""

    def test_includes_all_sections(self):
        prompt = _build_judge_user_prompt(
            query="test query",
            verb="understand",
            response={"definitions": [{"symbol": "foo"}]},
            expected_substance="Must explain foo",
            grounding_snippet="def foo(): pass",
        )
        assert "## QUERY" in prompt
        assert "test query" in prompt
        assert "## VERB TYPE" in prompt
        assert "understand" in prompt
        assert "## SYSTEM RESPONSE" in prompt
        assert "## EXPECTED SUBSTANCE" in prompt
        assert "Must explain foo" in prompt
        assert "## REAL SOURCE CODE" in prompt
        assert "def foo(): pass" in prompt

    def test_omits_grounding_when_empty(self):
        prompt = _build_judge_user_prompt(
            query="q",
            verb="browse",
            response={"entries": []},
            expected_substance="Must list files",
            grounding_snippet="",
        )
        assert "## REAL SOURCE CODE" not in prompt

    def test_truncates_long_response(self):
        long_response = {"data": "x" * 5000}
        prompt = _build_judge_user_prompt(
            query="q",
            verb="search_exact",
            response=long_response,
            expected_substance="substance",
            grounding_snippet="",
        )
        assert "... [truncated]" in prompt

    def test_truncates_long_grounding(self):
        prompt = _build_judge_user_prompt(
            query="q",
            verb="impact",
            response={},
            expected_substance="substance",
            grounding_snippet="x" * 4000,
        )
        assert "... [truncated]" in prompt


# ---------------------------------------------------------------------------
# Tests: RelevanceJudge._parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parses_valid_json(self):
        data = _make_llm_response(3, "Great answer", precision=0.9, recall=1.0)
        result = RelevanceJudge._parse_response(data)
        assert result.score == 3
        assert result.justification == "Great answer"
        assert result.precision == 0.9
        assert result.recall == 1.0
        assert result.error == ""

    def test_parses_without_precision_recall(self):
        data = _make_llm_response(2, "Decent")
        result = RelevanceJudge._parse_response(data)
        assert result.score == 2
        assert result.precision is None
        assert result.recall is None

    def test_clamps_score_above_3(self):
        data = {"choices": [{"message": {"content": '{"score": 5, "justification": "x"}'}}]}
        result = RelevanceJudge._parse_response(data)
        assert result.score == 3

    def test_clamps_score_below_0(self):
        data = {"choices": [{"message": {"content": '{"score": -1, "justification": "x"}'}}]}
        result = RelevanceJudge._parse_response(data)
        assert result.score == 0

    def test_handles_markdown_fenced_json(self):
        content = '```json\n{"score": 2, "justification": "fenced"}\n```'
        data = {"choices": [{"message": {"content": content}}]}
        result = RelevanceJudge._parse_response(data)
        assert result.score == 2
        assert result.justification == "fenced"

    def test_handles_non_json(self):
        data = {"choices": [{"message": {"content": "I cannot evaluate this."}}]}
        result = RelevanceJudge._parse_response(data)
        assert result.score == 0
        assert result.error == "json_parse_error"


# ---------------------------------------------------------------------------
# Tests: RelevanceJudge.grade (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGrade:
    """Test the grade method with mocked HTTP calls."""

    @patch("tests.eval.judge.httpx.post")
    def test_successful_grade(self, mock_post):
        mock_post.return_value = FakeHTTPResponse(_make_llm_response(2, "Mostly correct"))
        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        result = judge.grade(
            query="test",
            verb="understand",
            response={"definitions": []},
            expected_substance="Must explain X",
            grounding_snippet="code here",
        )
        assert result.score == 2
        assert result.justification == "Mostly correct"
        assert result.error == ""

        # Verify temperature=0 was used
        call_args = mock_post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert body["temperature"] == 0

    @patch("tests.eval.judge.httpx.post")
    def test_http_failure_returns_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        result = judge.grade(
            query="test",
            verb="browse",
            response={},
            expected_substance="Must list files",
        )
        assert result.score == 0
        assert "Connection refused" in result.error


# ---------------------------------------------------------------------------
# Tests: RelevanceJudge.run_calibration
# ---------------------------------------------------------------------------


class TestCalibration:
    """Test calibration logic."""

    @patch("tests.eval.judge.httpx.post")
    def test_calibration_passes_when_scores_within_tolerance(self, mock_post):
        """Judge scores within +/- 1 of expected → calibration passes."""
        # Return scores that are within tolerance for each call
        mock_post.side_effect = [
            FakeHTTPResponse(_make_llm_response(3, "good")),  # expected 3 → actual 3
            FakeHTTPResponse(_make_llm_response(0, "bad")),  # expected 0 → actual 0
            FakeHTTPResponse(_make_llm_response(0, "echo")),  # echo, expected 0 → actual 0
        ]

        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        items = [
            CalibrationItem(
                id="cal-good",
                verb="understand",
                query="q",
                response={},
                expected_substance="s",
                grounding_snippet="g",
                expected_score=3,
                category="good",
            ),
            CalibrationItem(
                id="cal-bad",
                verb="understand",
                query="q",
                response={},
                expected_substance="s",
                grounding_snippet="g",
                expected_score=0,
                category="bad",
            ),
            CalibrationItem(
                id="cal-echo",
                verb="understand",
                query="q",
                response={},
                expected_substance="s",
                grounding_snippet="g",
                expected_score=0,
                category="echo",
            ),
        ]
        result = judge.run_calibration(items)
        assert result.passed is True
        assert result.echo_guard_passed is True
        assert len(result.details) == 3

    @patch("tests.eval.judge.httpx.post")
    def test_calibration_fails_when_score_out_of_tolerance(self, mock_post):
        """Score diff > 1 from expected → calibration fails."""
        mock_post.return_value = FakeHTTPResponse(
            _make_llm_response(0, "wrong")  # expected 3, actual 0 → diff 3 > 1
        )

        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        items = [
            CalibrationItem(
                id="cal-good",
                verb="understand",
                query="q",
                response={},
                expected_substance="s",
                grounding_snippet="g",
                expected_score=3,
                category="good",
            ),
        ]
        result = judge.run_calibration(items)
        assert result.passed is False

    @patch("tests.eval.judge.httpx.post")
    def test_echo_guard_fails_when_echo_scores_above_1(self, mock_post):
        """Echo answer scored > 1 → echo guard fails → calibration fails."""
        mock_post.return_value = FakeHTTPResponse(
            _make_llm_response(2, "seems good")  # echo item, should be <= 1
        )

        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        items = [
            CalibrationItem(
                id="cal-echo",
                verb="understand",
                query="q",
                response={},
                expected_substance="s",
                grounding_snippet="g",
                expected_score=0,
                category="echo",
            ),
        ]
        result = judge.run_calibration(items)
        assert result.passed is False
        assert result.echo_guard_passed is False

    @patch("tests.eval.judge.httpx.post")
    def test_echo_answer_scores_at_most_1_regression(self, mock_post):
        """Anti-gaming regression: an answer that echoes query/metadata must score <= 1.

        This test verifies the judge prompt's anti-gaming instruction works.
        The echo answer just repeats the target name with no substance.
        """
        # Simulate a well-calibrated judge that correctly identifies the echo
        mock_post.return_value = FakeHTTPResponse(
            _make_llm_response(1, "Answer merely echoes the target name")
        )

        judge = RelevanceJudge(base_url="http://localhost:4000/v1")
        result = judge.grade(
            query="agent-skills/plugin.json",
            verb="understand",
            response={
                "definitions": [
                    {
                        "symbol": "plugin.json",
                        "file": "plugin.json",
                        "kind": "file",
                        "content": "plugin.json - agent-skills/plugin.json",
                    }
                ]
            },
            expected_substance="Must explain plugin metadata, commands/, skills/",
            grounding_snippet='{"name": "agent-skills", "version": "1.0.0"}',
        )
        # The judge should score this <= 1 (echo content)
        assert result.score <= 1
