"""Allowlist coverage report tests — Issue #5420 (PMM-03).

§5, C2: The report-only allowlist migration stage.  Tests that the report
correctly identifies which patterns match canonical IDs and which match nothing.
"""

from __future__ import annotations

from src.admin.persona_models.catalogue import PLATFORM_MODEL_CATALOGUE
from src.admin.persona_models.catalogue_service import report_allowlist_coverage


class TestAllowlistReport:
    """§5 allowlist coverage report."""

    def test_anthropic_wildcard_matches(self):
        """'global.anthropic.claude-*' matches catalogue entries."""
        report = report_allowlist_coverage(["global.anthropic.claude-*"])
        assert len(report) == 1
        assert report[0]["empty"] is False
        assert len(report[0]["matched_ids"]) > 0

    def test_friendly_name_matches_nothing(self):
        """C2: 'claude-sonnet' matches NO canonical ID (the broken seeded data)."""
        report = report_allowlist_coverage(["claude-sonnet"])
        assert len(report) == 1
        assert report[0]["empty"] is True
        assert report[0]["matched_ids"] == []

    def test_claude_haiku_matches_nothing(self):
        """C2: 'claude-haiku' matches NO canonical ID."""
        report = report_allowlist_coverage(["claude-haiku"])
        assert len(report) == 1
        assert report[0]["empty"] is True

    def test_wildcard_matches_all(self):
        """'*' matches everything."""
        report = report_allowlist_coverage(["*"])
        assert len(report) == 1
        assert report[0]["empty"] is False
        assert set(report[0]["matched_ids"]) == {model.canonical_model_id for model in PLATFORM_MODEL_CATALOGUE}

    def test_inert_seeded_patterns_reported(self):
        """The actual seeded patterns from lambda-authorizer are all empty."""
        # These are the actual values from infra/modules/lambda-authorizer/main.tf
        seeded = ["claude-sonnet", "claude-haiku"]
        report = report_allowlist_coverage(seeded)
        for entry in report:
            assert entry["empty"] is True, f"Pattern '{entry['pattern']}' unexpectedly matched: {entry['matched_ids']}"

    def test_multiple_patterns_reported_individually(self):
        """Each pattern gets its own report entry."""
        patterns = ["global.anthropic.claude-opus-*", "claude-sonnet", "us.anthropic.claude-*"]
        report = report_allowlist_coverage(patterns)
        assert len(report) == 3
        # First should match (opus family)
        assert report[0]["empty"] is False
        # Second matches nothing
        assert report[1]["empty"] is True
        # Third should match (us. prefix)
        assert report[2]["empty"] is False
