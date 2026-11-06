"""Check the offline runner/instance Terraform ownership boundary after S1."""

import re
import subprocess
import unittest
from pathlib import Path


RUNNERS = Path(__file__).resolve().parents[1]
GITLAB = RUNNERS.parent
REPO = next(parent for parent in RUNNERS.parents if (parent / ".git").exists())
S1_BASELINE = "0b693faef2786d32950a74ff6ce31102499eaade"


def blocks(source):
    return set(re.findall(r'^\s*(resource|data|module)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{',
                          source, flags=re.MULTILINE))


class StateBoundaryTests(unittest.TestCase):
    def test_runners_have_their_own_s3_backend(self):
        versions = (RUNNERS / "versions.tf").read_text()
        backend = (REPO / "environments/dev/modules/gitlab-runners-backend.tfvars.example").read_text()
        instance_backend = (REPO / "environments/dev/modules/gitlab-backend.tfvars").read_text()
        self.assertEqual(versions.count('backend "s3" {}'), 1)
        self.assertEqual(re.findall(r'^key\s*=\s*"([^"]+)"', backend, re.MULTILINE),
                         ["dev/modules/gitlab-runners/terraform.tfstate"])
        self.assertIn('encrypt = true', backend)
        self.assertNotIn('key = ', versions)
        self.assertNotIn('dev/modules/gitlab-runners/terraform.tfstate', instance_backend)
        self.assertIn('dev/modules/gitlab/terraform.tfstate', instance_backend)

    def test_cross_stack_discovery_is_read_only_and_group_bindings_are_inputs(self):
        sources = "\n".join(path.read_text() for path in sorted(RUNNERS.glob("*.tf")))
        declarations = blocks(sources)
        self.assertFalse(any(kind == "module" for kind, _, _ in declarations))
        self.assertEqual({(name, identifier) for kind, name, identifier in declarations
                          if kind == "data" and name == "terraform_remote_state"},
                         {("terraform_remote_state", "platform")})
        self.assertFalse(any(kind == "resource" and name in (
            "aws_instance", "aws_lb", "aws_secretsmanager_secret", "aws_secretsmanager_secret_version")
            for kind, name, _ in declarations))
        self.assertEqual({identifier for kind, name, identifier in declarations
                          if kind == "resource" and name == "aws_ssm_parameter"},
                         {"manager_alive", "registration_receipt"})
        self.assertEqual({identifier for kind, name, identifier in declarations
                          if kind == "data" and name == "aws_ssm_parameter"}, {"runner_url", "ca_secret_arn"})
        main = (RUNNERS / "main.tf").read_text()
        self.assertIn('for group_id, group in var.groups', main)
        self.assertIn('backend = "s3"', main)
        self.assertIn('key    = var.platform_state.key', main)
        self.assertEqual(main.count('with_decryption = false'), 2)
        variables = (RUNNERS / "variables.tf").read_text()
        self.assertIn('var.platform_state.key == "${var.environment}/platform/terraform.tfstate"', variables)

    def test_instance_source_has_not_changed_since_s1(self):
        instance = str((GITLAB / "infra").relative_to(REPO))
        diff = subprocess.run(["git", "diff", "--name-only", S1_BASELINE, "--", instance],
                              cwd=REPO, capture_output=True, text=True, check=True)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", instance],
                                   cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(diff.stdout.strip(), "", "GitLab instance Terraform changed since S1")
        self.assertEqual(untracked.stdout.strip(), "", "Untracked instance Terraform sources found")


if __name__ == "__main__":
    unittest.main()
