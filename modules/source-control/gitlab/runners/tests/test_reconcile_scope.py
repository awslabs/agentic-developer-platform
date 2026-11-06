import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_scope.py"
spec = importlib.util.spec_from_file_location("reconcile_scope", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class ReconcileScopeTests(unittest.TestCase):
    def test_approved_numeric_groups(self):
        self.assertEqual(scope.group_ids("42,201"), "42,201")

    def test_rejects_unapproved_or_ambiguous_scope(self):
        for candidate in ("", "0", "042", "42,042", "42,42", "42,", "42 201", "42;rm -rf /",
                          "42\n201", ",42", ",".join(str(value) for value in range(1, 34))):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                scope.group_ids(candidate)

    def test_scheduled_workflow_is_scoped_and_bounded(self):
        workflow = (Path(__file__).resolve().parents[5] / ".github/workflows/gitlab-runners-reconcile.yml").read_text()
        for required in ("'*/5 * * * *'", "timeout-minutes: 8", "refs/heads/main",
                         "adp-deploy-dev", "reconcile_scope.py", "reconcile_instances.py",
                         "deployment_guard.py identity", "adp-dev-gitlab-runners-reconciler",
                         "timeout 420s", "cancel-in-progress: false", "unset-current-credentials: true"):
            self.assertIn(required, workflow)
        for forbidden in ("terraform destroy", "aws ec2 terminate-instances", "upload-artifact", "set -x"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
