import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/devlake-poc.yml"


class OptionalPublicationWorkflowTest(unittest.TestCase):
    def setUp(self):
        workflow = WORKFLOW.read_text()
        self.triggers, self.jobs = workflow.split("jobs:\n", 1)
        self.verify, self.publish = self.jobs.split("\n  publish:\n", 1)

    def test_verify_covers_source_repairs_shared_workflows_and_completion_reports(self):
        for path in ("modules/connectors/devlake/**", "modules/insights/**",
                     "modules/gateway/**", "docs/engineering-insights/**",
                     ".github/workflows/**"):
            with self.subTest(path=path):
                self.assertIn(f"- '{path}'", self.triggers)
        self.assertIn("name: DevLake POC / verify", self.verify)
        self.assertIn("DEVLAKE_SOURCE_INTEGRATION: '1'", self.verify)
        self.assertIn("python3 -m unittest discover -s modules/insights/tests/devlake_poc", self.verify)
        self.assertIn("go-version: '1.26.0'", self.verify)
        self.assertNotIn("--publish", self.verify)

    def test_shared_cli_preflight_precedes_checks_with_read_only_github_access(self):
        self.assertIn("checks: read", self.verify)
        self.assertIn("pull-requests: read", self.verify)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.verify)
        for field in ("ADP_DEVLAKE_PR_BASE_SHA", "ADP_DEVLAKE_PR_HEAD_SHA", "ADP_DEVLAKE_PR_NUMBER"):
            self.assertIn(field, self.verify)
        self.assertLess(self.verify.index("verify_poc.py --preflight-only"),
                        self.verify.index("python3 -m unittest discover"))
        self.assertNotIn("--preflight-only", self.publish)

    def test_terminal_publish_failure_uses_separate_protected_finalizer(self):
        self.assertIn("finalize-on-publish-failure:", self.jobs)
        finalizer = self.jobs.split("  finalize-on-publish-failure:\n", 1)[1]
        self.assertIn("needs.publish.result == 'failure'", finalizer)
        self.assertIn("github.event_name == 'workflow_dispatch'", finalizer)
        self.assertIn("inputs.publish == true", finalizer)
        self.assertIn("environment: adp-devlake-finalizer", finalizer)
        self.assertIn("ref: ${{ vars.ADP_DEVLAKE_FINALIZER_REVISION }}", finalizer)
        self.assertIn("--mode manual --action failure", finalizer)
        self.assertNotIn("--mode manual --action failure", self.verify)

    def test_publishing_needs_positive_manual_selection_and_protected_identity(self):
        self.assertRegex(self.triggers, r"(?s)workflow_dispatch:\n\s+inputs:\n\s+publish:\n")
        self.assertIn("default: false", self.triggers)
        self.assertIn("options: [api, worker]", self.triggers)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.publish == true", self.publish)
        self.assertIn("github.event.repository.default_branch", self.publish)
        self.assertIn("needs: verify", self.publish)
        self.assertIn("environment: adp-devlake-publisher", self.publish)
        for tool in ("github.com/anchore/syft/cmd/syft@v1.42.0",
                     "github.com/zricethezav/gitleaks/v8@v8.30.0"):
            self.assertIn(tool, self.publish)
        self.assertLess(self.publish.index("scripts/checks.py"), self.publish.index("publish.py --publish"))
        self.assertIn('--output "$RUNNER_TEMP/devlake/checks.json"', self.publish)
        self.assertIn('test -s "$checks"', self.publish)
        self.assertIn('--expected-digest "$digest"', self.publish)
        self.assertIn('--checks "$checks"', self.publish)
        self.assertIn('--receipt "$RUNNER_TEMP/devlake/publication-receipt.json"', self.publish)
        self.assertIn("secrets.ADP_DEVLAKE_CONNECTION_LABEL", self.publish)
        self.assertNotIn("id-token: write", self.triggers)
        shared = (ROOT / ".github/workflows/insights-deploy.yml").read_text()
        self.assertNotIn("devlake", shared.split("jobs:\n", 1)[1].lower())


if __name__ == "__main__":
    unittest.main()
