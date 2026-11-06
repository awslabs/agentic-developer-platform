"""Keep the W1 module handoff distinct from live acceptance."""

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class ArchitectureDocTest(unittest.TestCase):
    def test_architecture_and_manifest_identify_isolated_boundary(self):
        architecture = (ROOT / "docs/engineering-insights/architecture.md").read_text()
        manifest = (ROOT / "docs/adp-platform-deployment/deployment-manifest.md").read_text()
        self.assertIn("# Engineering Insights — delivery architecture", architecture)
        self.assertIn("## 5. Module and trust boundary", architecture)
        self.assertIn("## 6. Engineering Insights deployment boundary (EI-C01)", architecture)
        self.assertIn("modules/insights/", architecture)
        self.assertIn("insights-health-verification", architecture)
        self.assertIn("adp admin\nservice-account create", architecture)
        self.assertIn("runtime acceptance", architecture)
        self.assertIn("## Engineering Insights Module", manifest)
        self.assertIn("dev/modules/insights/terraform.tfstate", manifest)
        self.assertIn("--insights-only", manifest)

    def test_teardown_handoff_warns_about_evidence_deletion(self):
        for filename in ("docs/engineering-insights/architecture.md",
                         "docs/adp-platform-deployment/deployment-manifest.md"):
            with self.subTest(filename=filename):
                document = " ".join((ROOT / filename).read_text().split())
                self.assertIn("permanently deletes Insights evidence", document)
                self.assertIn("object versions and delete markers", document)
                self.assertIn("retention requirements", document)
                self.assertIn("verify a backup outside the selected resources", document)
                self.assertIn("force_destroy = false", document)
                self.assertNotIn("does not force-delete retained evidence", document)
                self.assertNotIn("bucket deletion refuses nonempty data", document)


if __name__ == "__main__":
    unittest.main()
