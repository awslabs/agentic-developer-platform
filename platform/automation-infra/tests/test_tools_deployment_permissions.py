import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / "platform/automation-infra/tools-deployment.tf"
RBAC = ROOT / "platform/infra/tools-release-rbac.tf"
OVERLAY = ROOT / "environments/dev/automation-ci.tfvars.json"


class ToolsReleasePermissionsTests(unittest.TestCase):
    def test_tools_role_has_no_eks_edit_policy_association(self):
        self.assertNotIn('resource "aws_eks_access_policy_association" "tools_deployment"', POLICY.read_text())
        self.assertIn('kubernetes_groups = ["adp:tools-release-${var.environment}"]', POLICY.read_text())
        self.assertIn('name      = "adp:tools-release-${var.environment}"', RBAC.read_text())

    def test_existing_overlay_is_disabled_until_owner_reconciles_live_state(self):
        self.assertIs(json.loads(OVERLAY.read_text())["enable_tools_deployment"], False)


if __name__ == "__main__":
    unittest.main()
