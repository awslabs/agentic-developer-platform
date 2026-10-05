import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("reconcile.py")
SPEC = importlib.util.spec_from_file_location("runner_scan_reconcile", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RESOLVER = MODULE.load_resolver(Path(__file__).resolve().parents[4] / ".github/scripts/diff_security_findings.py")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "scan.sarif"

    def report(self, severity="High", extra_results=None, location="/usr/local/bin/tool"):
        results = [{
            "ruleId": "CVE-example",
            "message": {"text": "package 1.0"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": location}}}],
        }]
        results.extend(extra_results or [])
        self.path.write_text(json.dumps({"runs": [{
            "tool": {"driver": {"rules": [{
                "id": "CVE-example", "help": {"text": f"Severity: {severity}"},
            }]}}, "results": results,
        }]}))
        return {"sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(), "critical": 0, "high": 1}

    def test_preserves_path_and_unresolved_disposition(self):
        expected = self.report()
        record = MODULE.inventory(self.path, "platform-arc-runner", RESOLVER, expected)
        self.assertEqual(record["counts"], {"critical": 0, "high": 1})
        self.assertEqual(record["findings"][0]["installed_paths"], ["/usr/local/bin/tool"])
        self.assertEqual(record["findings"][0]["disposition"], "unresolved")
        self.assertEqual(record["findings"][0]["severity_source"], "native")

    def test_rejects_tampered_report(self):
        expected = self.report()
        self.path.write_text(self.path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            MODULE.inventory(self.path, "platform-arc-runner", RESOLVER, expected)

    def test_rejects_dropped_or_double_counted_result(self):
        result = {"ruleId": "CVE-example", "locations": [{
            "physicalLocation": {"artifactLocation": {"uri": "/usr/bin/other"}},
        }]}
        expected = self.report(extra_results=[result])
        with self.assertRaisesRegex(ValueError, "occurrence counts"):
            MODULE.inventory(self.path, "platform-arc-runner", RESOLVER, expected)

    def test_rejects_missing_provenance(self):
        expected = self.report(location="")
        with self.assertRaisesRegex(ValueError, "lacks an installed path"):
            MODULE.inventory(self.path, "platform-arc-runner", RESOLVER, expected)

    def test_sarif_error_level_alone_is_not_high(self):
        expected = self.report(severity="Unknown")
        expected["high"] = 0
        self.assertEqual(MODULE.inventory(self.path, "platform-arc-runner", RESOLVER, expected)["findings"], [])


if __name__ == "__main__":
    unittest.main()
