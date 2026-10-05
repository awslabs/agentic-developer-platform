"""Check issue #6998's published selectors against the scanned source revision."""

import importlib.util
import json
from collections import Counter
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "data/security/issue-6998-http-destinations.json"
BASELINE = "fa75f1c407ffeb075d43271eb8a18accaa6458b3"
PREFIX = "modules/agent-factory/"
SELECTORS = {
    "agent/src/clients/gitlab_client.ts": [50, 72, 97, 127],
    "agent/src/codex-persona-policy.ts": [133],
    "agent/src/complex-task-chat/recall-at-task-start.ts": [177],
    "agent/src/complex-task-chat/vault/gateway-client.ts": [139, 158],
    "agent/src/components/checkRunStreamer.ts": [606],
    "agent/src/github-comments.ts": [449],
    "agent/src/invocability-probe/capture-proxy.ts": [207],
    "agent/src/invocability-probe/gateway-client.ts": [105],
    "agent/src/lib/artifactGateway.ts": [32],
    "agent/src/lib/knowledgeBridge.ts": [66],
    "agent/src/utils/comment-authority.ts": [7],
    "agent/src/utils/installation.ts": [53],
    "codex-harness/src/github-entry.mjs": [157],
    "codex-reviewer/src/github.ts": [160],
}


class Issue6998MappingTest(unittest.TestCase):
    def test_all_published_selectors_point_to_scanned_fetches(self):
        catalog = json.loads(CATALOG.read_text())
        self.assertEqual(catalog["sourceRevision"], BASELINE)
        self.assertEqual(catalog["sourceReportSha256"],
                         "4b77d2237eb6864e2228eb433a6ec314a94aad858417f7ee094248c9ad6227bd")
        self.assertIn("pending", catalog["sourceVerification"])
        self.assertEqual(len(catalog["candidateRevision"]), 40)
        self.assertEqual(len(catalog["candidateRules"]["bundleSha256"]), 64)
        self.assertIn("not verified", catalog["candidateRules"]["source"])
        self.assertEqual(len(catalog["candidateRawSarifSha256"]), 64)
        self.assertEqual(catalog["rule"], "nodejs_scan.javascript-ssrf-rule-node_ssrf")
        actual = [(record["file"], record["line"]) for record in catalog["records"]]
        expected = [(path, line) for path, lines in SELECTORS.items() for line in lines]
        self.assertEqual(len(actual), 18)
        self.assertCountEqual(actual, expected)
        for record in catalog["records"]:
            self.assertEqual(record["disposition"], "unresolved")
            self.assertTrue(record["input"] and record["destination"])
            self.assertTrue(record["candidateEvidence"]["boundary"])
            hit = record["candidateEvidence"]["result"]
            self.assertEqual((hit["severity"], hit["severitySource"]), ("critical", "native"))
            self.assertGreater(hit["line"], 0)
            for check in record["candidateEvidence"]["checks"]:
                self.assertTrue((ROOT / PREFIX / check).is_file(), check)
        for path, lines in SELECTORS.items():
            source = subprocess.check_output(
                ["git", "show", f"{BASELINE}:{PREFIX}{path}"], cwd=ROOT, text=True
            ).splitlines()
            for line in lines:
                self.assertIn("fetch(", source[line - 1], (path, line))


    def test_candidate_coverage_and_unreviewed_suppressions(self):
        catalog = json.loads(CATALOG.read_text())
        self.assertEqual(Counter(record["candidateEvidence"]["result"]["suppression"]
                                 for record in catalog["records"]),
                         {"inSource-unaccepted": 13, "none": 5})
        spec = importlib.util.spec_from_file_location(
            "issue_6998_candidate", ROOT / ".github/scripts/issue_6998_candidate.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hits = {}
        for record in catalog["records"]:
            hits.setdefault(record["file"], []).append(record["candidateEvidence"]["result"])
        self.assertEqual(len(module.map_records(catalog, hits)), 18)
        for changed in (dict(list(hits.items())[1:]), {**hits, "extra.ts": [hits[next(iter(hits))][0]]}):
            with self.assertRaises(ValueError):
                module.map_records(catalog, changed)

    def test_native_severity_and_accepted_suppressions_remain_separate(self):
        spec = importlib.util.spec_from_file_location(
            "issue_6998_candidate", ROOT / ".github/scripts/issue_6998_candidate.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        catalog = json.loads(CATALOG.read_text())
        rule = "tmp.gitlab." + catalog["rule"]
        sarif = {"runs": [{"tool": {"driver": {"rules": [{"id": rule,
            "properties": {"security-severity": "7.8"}}]}}, "results": [
            {"ruleId": rule, "properties": {"issue_severity": "low"},
             "suppressions": [{"kind": "inSource"}], "locations": [{"physicalLocation": {
                 "artifactLocation": {"uri": PREFIX + "agent/src/utils/installation.ts"},
                 "region": {"startLine": 53}}}]},
            {"ruleId": rule, "properties": {"issue_severity": "low"},
             "suppressions": [{"kind": "inSource", "status": "accepted"}],
             "locations": [{"physicalLocation": {
                 "artifactLocation": {"uri": PREFIX + "agent/src/utils/comment-authority.ts"},
                 "region": {"startLine": 7}}}]},
        ]}]}
        hits = module.candidate_hits(sarif, catalog)
        self.assertEqual(hits["agent/src/utils/installation.ts"][0]["severity"], "low")
        self.assertEqual(hits["agent/src/utils/installation.ts"][0]["severitySource"], "native")
        self.assertEqual(hits["agent/src/utils/installation.ts"][0]["suppression"], "inSource-unaccepted")
        self.assertEqual(hits["agent/src/utils/comment-authority.ts"][0]["suppression"], "accepted")

    def test_handoff_keeps_missing_evidence_and_failed_suites_visible(self):
        catalog = json.loads(CATALOG.read_text())
        handoff = catalog["integrationHandoff"]
        subprocess.check_call(["git", "cat-file", "-e", handoff["testedRevision"] + "^{commit}"], cwd=ROOT)
        self.assertEqual(set(handoff["acceptance"]), {"AC-01", "AC-02", "AC-03", "AC-04"})
        self.assertIn("blocked", handoff["acceptance"]["AC-01"])
        self.assertIn("blocked", handoff["acceptance"]["AC-03"])
        self.assertIn("pending", handoff["acceptance"]["AC-04"])
        self.assertIn("140/141", handoff["localChecks"]["agent"])
        self.assertIn("196/198", handoff["localChecks"]["codexReviewer"])
        self.assertIsNone(catalog["candidateImageDigest"])
        self.assertTrue(all(record["disposition"] == "unresolved" for record in catalog["records"]))

if __name__ == "__main__":
    unittest.main()
