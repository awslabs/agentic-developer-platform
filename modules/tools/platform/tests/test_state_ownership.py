import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/tools/platform/infra"
BOOTSTRAP = ROOT / "modules/tools/platform/bootstrap"


class StateOwnershipTests(unittest.TestCase):
    def test_release_root_contains_only_agentcore_resources(self):
        sources = "\n".join(path.read_text() for path in INFRA.glob("*.tf"))
        managed = re.findall(r'^resource "([^"]+)" "([^"]+)"', sources, re.MULTILINE)
        self.assertTrue(managed)
        self.assertTrue(all(kind.startswith("aws_bedrockagentcore_") for kind, _ in managed), managed)
        self.assertIn('backend "s3" {}', sources)

    def test_bootstrap_root_owns_foundation_not_agentcore(self):
        sources = "\n".join(path.read_text() for path in BOOTSTRAP.glob("*.tf"))
        managed = re.findall(r'^resource "([^"]+)" "([^"]+)"', sources, re.MULTILINE)
        self.assertTrue(managed)
        self.assertFalse(any(kind.startswith("aws_bedrockagentcore_") for kind, _ in managed), managed)
        self.assertTrue({"aws_iam_role", "aws_kms_key", "aws_ecr_repository", "aws_dynamodb_table"}.issubset({kind for kind, _ in managed}))


if __name__ == "__main__":
    unittest.main()
