"""Offline checks for the unexecuted #7033 Opus qualification handoff."""

import json
import re
import runpy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = Path(__file__).with_name("README.md")


class QualificationHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        section = DOCUMENT.read_text().split("## Opus live-qualification handoff (#7033 AC-03)", 1)[1]
        sample = re.search(r"### Protected qualification receipt shape.*?```json\n(.*?)\n```", section, flags=re.S)
        if sample is None:
            raise AssertionError("Missing Opus qualification receipt template")
        cls.section = section
        cls.receipt = json.loads(sample.group(1))

    def test_stage_schedule_requires_reached_200_plateau(self):
        stages = self.receipt["stages"]
        self.assertEqual([stage["name"] for stage in stages], [f"ramp-{sessions}" for sessions in (10, 60, 100, 150, 200)])
        self.assertEqual([stage["sessions"] for stage in stages], [10, 60, 100, 150, 200])
        self.assertEqual([stage["min_dwell_seconds"] for stage in stages], [60, 120, 120, 120, 300])
        self.assertTrue(all(stage["status"] == "not_run" for stage in stages))
        self.assertTrue(all(stage["actual_dwell_seconds"] is None for stage in stages))
        self.assertTrue(all(stage["actual_sessions"] is None for stage in stages))
        self.assertTrue(all(stage["reached_at_ref"] is None for stage in stages))
        self.assertTrue(all(stage["attempts"] is None and stage["valid_completed"] is None for stage in stages))
        self.assertIn("after the target sessions are reached", self.section)
        self.assertIn("≥99% valid completed streams", self.section)

    def test_receipt_cannot_stand_in_for_live_evidence(self):
        receipt = self.receipt
        self.assertEqual(receipt["criterion"], "AC-03")
        self.assertTrue(receipt["example_only"])
        self.assertEqual(receipt["scenario"], "MOD-047-F001-S003")
        self.assertEqual(receipt["status"], "not_run")
        self.assertIsNone(receipt["authorization_ref"])
        self.assertIsNone(receipt["deployed_revision_ref"])
        self.assertIsNone(receipt["protected_receipt_ref"])
        self.assertTrue(all(value is None for value in receipt["limits"].values()))
        self.assertTrue(all(value is None for value in receipt["totals"].values()))
        for category in (
            "physical_attempts", "complete_streams", "failed_before_content",
            "failed_after_content", "provider_terminal_failed_or_incomplete",
            "protocol_errors", "admission_or_quota_rejections",
            "lost_worker_attempts", "http_200_missing_terminal", "unknown",
            "reserved_ambiguous_bound", "total_transition_seconds",
        ):
            self.assertIn(category, receipt["totals"])
        self.assertEqual(receipt["health"]["status"], "not_run")
        self.assertEqual(receipt["cleanup"]["status"], "not_run")
        self.assertEqual(receipt["human_acceptance"]["status"], "pending")
        for requirement in (
            "12,000", "1,440", "240", "predeclared", "physical", "no restarts/OOM",
            "unknown", "cleanup", "not extrapolated", "numeric",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.lower(), self.section.lower())

    def test_public_handoff_contains_no_private_target(self):
        findings = runpy.run_path(str(ROOT / "scripts/check-public-docs.py"))["findings"]
        self.assertEqual(list(findings(self.section)), [])


if __name__ == "__main__":
    unittest.main()
