"""Synthetic issue #6999 report tests; the actual SARIF stays in private storage."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from reconcile_6999 import ASSIGNED, reconcile


def synthetic_report():
    results = []
    rules = []
    for rule, path, line in ASSIGNED:
        rule_id = "tmp.gitlab." + rule
        rating = "high" if rule.endswith("eval_nodejs") else "critical"
        results.append({"ruleId": rule_id, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": path}, "region": {"startLine": line}}}]})
        if not any(item["id"] == rule_id for item in rules):
            rules.append({"id": rule_id, "properties": {"security-severity": rating}})
    return {"runs": [{"tool": {"driver": {"rules": rules}}, "results": results}]}


class ReconcileTest(unittest.TestCase):
    def test_preserves_eleven_distinct_records_and_native_ratings(self):
        records = reconcile(synthetic_report())
        self.assertEqual([record["result_index"] for record in records], list(range(11)))
        self.assertEqual(sum(record["rating"] == "critical" for record in records), 9)
        self.assertTrue(all(record["rating_source"] == "native" for record in records))

    def test_missing_and_duplicate_fail_closed(self):
        report = synthetic_report()
        report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"] = 24
        with self.assertRaisesRegex(ValueError, "app.js:23=0"):
            reconcile(report)
        report = synthetic_report()
        report["runs"][0]["results"].append(report["runs"][0]["results"][0])
        with self.assertRaisesRegex(ValueError, "app.js:23=2"):
            reconcile(report)

    def test_sarif_error_level_is_not_native_severity(self):
        report = synthetic_report()
        report["runs"][0]["tool"]["driver"]["rules"][0]["properties"] = {}
        report["runs"][0]["results"][0]["level"] = "error"
        with self.assertRaisesRegex(ValueError, "native critical"):
            reconcile(report)

    def test_accepted_suppression_stays_separate_from_rating(self):
        report = synthetic_report()
        report["runs"][0]["results"][0]["suppressions"] = [{"status": "accepted"}]
        records = reconcile(report)
        self.assertTrue(records[0]["accepted_suppression"])
        self.assertEqual(records[0]["rating"], "critical")


if __name__ == "__main__":
    unittest.main()
