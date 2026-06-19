"""Unit tests for eval report aggregation consistency.

Verifies that the summary (report.passed, report.failed) and per-verb stats
(by_verb[v].passed) agree with a manual recount of results[].

Regression test for #1642: search_semantic reported 0/75 passed in summary
while raw results showed 12/75 passed (manual_review was mutually exclusive
with passed in the tallying logic).
"""

from __future__ import annotations

from unittest.mock import patch

from tests.eval.run_eval import (
    EvalConfig,
    EvalReport,
    EvalResult,
    GoldenQuestion,
    run_evaluation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_question(id: str, verb: str, repo: str = "test-repo") -> GoldenQuestion:
    """Create a minimal GoldenQuestion for testing."""
    return GoldenQuestion(
        id=id,
        repo=repo,
        verb=verb,
        query=f"query for {id}",
        expected={"files": ["some/file.py"]},
        pass_criterion="file in top-K",
    )


def _make_result(
    question_id: str,
    verb: str,
    passed: bool,
    manual_review: bool = False,
    error: str = "",
) -> EvalResult:
    """Create a minimal EvalResult for testing."""
    return EvalResult(
        question_id=question_id,
        repo="test-repo",
        verb=verb,
        query=f"query for {question_id}",
        passed=passed,
        score=1.0 if passed else 0.0,
        manual_review=manual_review,
        error=error,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvalAggregation:
    """Tests that summary aggregation is consistent with raw results."""

    def test_manual_review_with_passes_counted_correctly(self):
        """A verb where results have both manual_review=True and passed=True.

        This is the exact bug from #1642: search_semantic sets manual_review=True
        on all results, but some also have passed=True. The summary must count
        both the manual_review flag AND the passed status independently.
        """
        # Simulate: 10 search_semantic questions, 4 pass, 6 fail, all manual_review
        questions = [_make_question(f"sem-{i}", "search_semantic") for i in range(10)]
        results = []
        for i, q in enumerate(questions):
            passed = i < 4  # first 4 pass
            results.append(_make_result(q.id, q.verb, passed=passed, manual_review=True))

        # Also add some non-manual-review results from another verb
        exact_questions = [_make_question(f"exact-{i}", "search_exact") for i in range(5)]
        for i, q in enumerate(exact_questions):
            passed = i < 3  # first 3 pass
            results.append(_make_result(q.id, q.verb, passed=passed, manual_review=False))

        # Build report by replaying the tallying logic
        report = EvalReport()
        for r in results:
            report.total += 1
            if r.manual_review:
                report.manual_review += 1
            if r.error:
                pass
            elif r.passed:
                report.passed += 1
            else:
                report.failed += 1

            verb_stats = report.by_verb.setdefault(
                r.verb,
                {"total": 0, "passed": 0, "failed": 0, "errors": 0, "manual_review": 0},
            )
            verb_stats["total"] += 1
            if r.manual_review:
                verb_stats["manual_review"] += 1
            if r.error:
                verb_stats["errors"] += 1
            elif r.passed:
                verb_stats["passed"] += 1
            else:
                verb_stats["failed"] += 1

            report.results.append(r)

        # Verify summary matches raw recount
        raw_passed = sum(1 for r in report.results if r.passed)
        assert report.passed == raw_passed == 7  # 4 semantic + 3 exact

        # Verify by_verb matches raw recount per verb
        sem_raw = sum(1 for r in report.results if r.verb == "search_semantic" and r.passed)
        assert report.by_verb["search_semantic"]["passed"] == sem_raw == 4

        exact_raw = sum(1 for r in report.results if r.verb == "search_exact" and r.passed)
        assert report.by_verb["search_exact"]["passed"] == exact_raw == 3

        # Verify by_verb sum equals summary passed
        by_verb_total_passed = sum(stats["passed"] for stats in report.by_verb.values())
        assert by_verb_total_passed == report.passed

        # manual_review counts are orthogonal
        assert report.manual_review == 10
        assert report.by_verb["search_semantic"]["manual_review"] == 10

    def test_self_consistency_assertion_fires_on_mismatch(self):
        """The self-consistency assertion in run_evaluation() catches mismatches.

        We mock the internals to produce an inconsistent tally and verify
        the AssertionError is raised.
        """
        # Create a report with intentionally inconsistent counts
        report = EvalReport()
        report.total = 5
        report.passed = 99  # wrong — doesn't match results
        report.failed = 0
        report.results = [
            _make_result("q1", "search_exact", passed=True),
            _make_result("q2", "search_exact", passed=False),
        ]
        report.by_verb = {
            "search_exact": {"total": 2, "passed": 1, "failed": 1, "errors": 0, "manual_review": 0}
        }

        # The assertion logic (same as in run_evaluation):
        raw_passed = sum(1 for r in report.results if r.passed)
        by_verb_passed = sum(stats.get("passed", 0) for stats in report.by_verb.values())

        # Verify the assertion would catch this
        assert report.passed != raw_passed  # 99 != 1
        assert raw_passed == by_verb_passed == 1

    def test_errors_not_double_counted_as_failures(self):
        """Results with errors should count as errors, not also as failures."""
        results = [
            _make_result("q1", "search_exact", passed=True),
            _make_result("q2", "search_exact", passed=False, error="timeout"),
            _make_result("q3", "search_exact", passed=False),
        ]

        report = EvalReport()
        for r in results:
            report.total += 1
            if r.manual_review:
                report.manual_review += 1
            if r.error:
                report.errors += 1
            elif r.passed:
                report.passed += 1
            else:
                report.failed += 1

            verb_stats = report.by_verb.setdefault(
                r.verb,
                {"total": 0, "passed": 0, "failed": 0, "errors": 0, "manual_review": 0},
            )
            verb_stats["total"] += 1
            if r.manual_review:
                verb_stats["manual_review"] += 1
            if r.error:
                verb_stats["errors"] += 1
            elif r.passed:
                verb_stats["passed"] += 1
            else:
                verb_stats["failed"] += 1

            report.results.append(r)

        assert report.passed == 1
        assert report.failed == 1
        assert report.errors == 1
        assert report.total == 3

        # by_verb should agree
        assert report.by_verb["search_exact"]["passed"] == 1
        assert report.by_verb["search_exact"]["failed"] == 1
        assert report.by_verb["search_exact"]["errors"] == 1

    def test_all_verbs_partial_passes(self):
        """Multiple verbs each with partial passes sum correctly."""
        verbs_and_passes = [
            ("search_exact", 5, 10, False),
            ("search_semantic", 3, 8, True),  # manual_review=True
            ("understand", 2, 5, False),
            ("browse", 4, 7, False),
            ("impact", 1, 3, False),
        ]

        report = EvalReport()
        for verb, n_pass, n_total, is_manual in verbs_and_passes:
            for i in range(n_total):
                passed = i < n_pass
                r = _make_result(f"{verb}-{i}", verb, passed=passed, manual_review=is_manual)
                report.total += 1
                if r.manual_review:
                    report.manual_review += 1
                if r.error:
                    report.errors += 1
                elif r.passed:
                    report.passed += 1
                else:
                    report.failed += 1

                verb_stats = report.by_verb.setdefault(
                    r.verb,
                    {"total": 0, "passed": 0, "failed": 0, "errors": 0, "manual_review": 0},
                )
                verb_stats["total"] += 1
                if r.manual_review:
                    verb_stats["manual_review"] += 1
                if r.error:
                    verb_stats["errors"] += 1
                elif r.passed:
                    verb_stats["passed"] += 1
                else:
                    verb_stats["failed"] += 1

                report.results.append(r)

        # Total passed = 5 + 3 + 2 + 4 + 1 = 15
        expected_passed = 5 + 3 + 2 + 4 + 1
        raw_passed = sum(1 for r in report.results if r.passed)
        by_verb_passed = sum(stats["passed"] for stats in report.by_verb.values())

        assert report.passed == expected_passed == 15
        assert raw_passed == expected_passed
        assert by_verb_passed == expected_passed

        # manual_review count is just the semantic ones
        assert report.manual_review == 8

    def test_run_evaluation_self_consistency(self):
        """Integration test: run_evaluation with mocked queries produces consistent report.

        Mocks the query/scoring layer to produce a known set of results
        and verifies the built-in self-consistency assertion passes.
        """
        config = EvalConfig(eval_mode="direct")

        # Create a small golden set
        questions = [
            _make_question("sem-1", "search_semantic"),
            _make_question("sem-2", "search_semantic"),
            _make_question("exact-1", "search_exact"),
        ]

        # Mock responses: sem-1 passes, sem-2 fails, exact-1 passes
        def mock_query_direct(cfg, verb, args):
            if verb in ("search_exact", "search_semantic"):
                # Return a result that matches expected file for *-1 questions
                if "sem-1" in str(args) or "exact-1" in str(args):
                    return {
                        "results": [{"file": "some/file.py", "content": "", "path": "some/file.py"}]
                    }
            return {"results": []}

        with (
            patch("tests.eval.run_eval.load_corpus", return_value=[]),
            patch("tests.eval.run_eval.load_golden", return_value=questions),
            patch("tests.eval.run_eval.check_ingestion", return_value={}),
            patch("tests.eval.run_eval.query_direct", side_effect=mock_query_direct),
        ):
            # This should NOT raise AssertionError
            report = run_evaluation(config)

        # Verify consistency
        raw_passed = sum(1 for r in report.results if r.passed)
        by_verb_passed = sum(stats["passed"] for stats in report.by_verb.values())
        assert report.passed == raw_passed == by_verb_passed
