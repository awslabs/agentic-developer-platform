"""Offline checks for the public, unexecuted #7033 diagnosis contract."""

import json
import re
import runpy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = Path(__file__).with_name("README.md")


class DiagnosisReportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCUMENT.read_text()
        section = cls.text.split("## Matched stream-failure diagnosis (#7033)", 1)[1]
        sample = re.search(r"### Protected report format.*?```json\n(.*?)\n```", section, flags=re.S)
        if sample is None:
            raise AssertionError("Missing #7033 JSON report template")
        cls.section = section
        cls.report = json.loads(sample.group(1))

    def test_template_cannot_claim_live_attribution(self):
        report = self.report
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["example_only"])
        self.assertEqual(report["status"], "not_run")
        self.assertEqual(report["attribution"]["classification"], "not_run")
        self.assertEqual(report["cleanup"]["state"], "not_run")
        self.assertEqual(report["attempts"], [])
        self.assertIsNone(report["protected_receipt_ref"])
        self.assertTrue(all(value is None for value in report["limits"].values()))
        self.assertTrue(all(value is None for value in report["totals"].values()))
        self.assertTrue(all(value is None for key, value in report["profile"].items() if key != "pairing_seed"))

    def test_each_attempt_requires_physical_evidence_and_closure(self):
        section = self.section
        for cohort in ("cold", "reused", "idle", "concurrent_1", "concurrent_5", "concurrent_10"):
            with self.subTest(cohort=cohort):
                self.assertIn(cohort, self.report["cohorts"])
        attempt_fields = section.split("Each protected `attempts` row carries", 1)[1]
        for key in (
            "pair_id", "arm", "cohort", "physical_attempt_id", "sdk_before_send",
            "socket_submission", "provider_acknowledgement", "valid_terminal",
            "reader_closed", "client_closed", "usage_ref", "outcome",
        ):
            with self.subTest(field=key):
                self.assertIn(f"`{key}`", attempt_fields)
        self.assertIn("300 physical attempts per model", section)
        self.assertIn("120 seconds", section)
        self.assertIn("direct success", section.lower())

    def test_procedure_does_not_publish_private_target_evidence(self):
        findings = runpy.run_path(str(ROOT / "scripts/check-public-docs.py"))["findings"]
        self.assertEqual(list(findings(self.section)), [])


if __name__ == "__main__":
    unittest.main()
