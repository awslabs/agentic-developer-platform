"""Check issue #6998's published selectors against the scanned source revision."""

import json
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
        self.assertEqual(catalog["rule"], "nodejs_scan.javascript-ssrf-rule-node_ssrf")
        actual = [(record["file"], record["line"]) for record in catalog["records"]]
        expected = [(path, line) for path, lines in SELECTORS.items() for line in lines]
        self.assertEqual(len(actual), 18)
        self.assertCountEqual(actual, expected)
        for record in catalog["records"]:
            self.assertEqual(record["disposition"], "unresolved")
            self.assertTrue(record["input"] and record["destination"])
        for path, lines in SELECTORS.items():
            source = subprocess.check_output(
                ["git", "show", f"{BASELINE}:{PREFIX}{path}"], cwd=ROOT, text=True
            ).splitlines()
            for line in lines:
                self.assertIn("fetch(", source[line - 1], (path, line))


if __name__ == "__main__":
    unittest.main()
