"""Exercise the real shell gate with Terraform success and failure responses."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


class UpdateGateTests(unittest.TestCase):
    def test_real_terraform_retains_context_after_overlays_but_accepts_release_override(self):
        for module, context in (("webhook-ingress", "webhook-ingress"), ("gateway-worker-authority", "gateway")):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "main.tf").write_text('variable "integration" {}\nvariable "agent_image" {}\n')
                (root / "base.tfvars").write_text('integration="base"\nagent_image="base"\n')
                (root / "overlay.tfvars").write_text('integration="overlay"\nagent_image="overlay"\n')
                (root / f"{context}.tfvars.json").write_text(json.dumps({"integration": "retained", "agent_image": "old"}))
                (root / "integration-before.json").write_text(json.dumps({"account": "123456789012"}))
                prefix = '''set -euo pipefail
ok() { :; }
fail() { echo "$*" >&2; exit 1; }
terraform() {
  [ "$1" = plan ] || return 97
  shift
  local inputs=()
  for input in "$@"; do
    case "$input" in -var=*|-var-file=*) inputs+=("$input");; esac
  done
  echo 'jsonencode({integration=var.integration,image=var.agent_image})' | "$REAL_TERRAFORM" console -no-color "${inputs[@]}"
}
source "$1"
terraform_update_apply "$MODULE" base.tfvars -var-file=overlay.tfvars -var=agent_image=release
'''
                env = dict(os.environ, REAL_TERRAFORM=shutil.which("terraform"), MODULE=module,
                           UPGRADE_RUN_DIR=directory, ACCOUNT_ID="123456789012")
                result = subprocess.run(["bash", "-c", prefix, "test", str(SCRIPTS / "terraform-update.sh")],
                                        cwd=root, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(json.loads(result.stdout)), {"integration": "retained", "image": "release"})

    def run_gate(self, actions=None, plan_exit=2, show_exit=0, invalid=False, confirm=False, resource=None, check_only=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            mock = root / "terraform"
            mock.write_text('''#!/bin/bash
echo "$*" >> "$CALLS"
case "$1" in
  plan) exit "$PLAN_EXIT" ;;
  show) cat "$FIXTURE_JSON"; exit "$SHOW_EXIT" ;;
  apply) exit 0 ;;
esac
''')
            mock.chmod(0o755)
            plan = root / "fixture.json"
            plan.write_text("{}" if invalid else json.dumps({"resource_changes": [
                resource or {"address": "aws_example.live", "change": {"actions": actions or ["update"]}}
            ]}))
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", CALLS=str(calls),
                       FIXTURE_JSON=str(plan), PLAN_EXIT=str(plan_exit), SHOW_EXIT=str(show_exit),
                       TMPDIR=directory, CONFIRM_DESTRUCTIVE=str(confirm).lower(), UPGRADE_CHECK_ONLY=str(check_only).lower())
            command = '''set -euo pipefail
ok() { :; }
warn() { :; }
fail() { echo "$*" >&2; exit 1; }
source "$1"
terraform_update_apply test example.tfvars
'''
            result = subprocess.run(["/bin/bash", "-c", command, "test", str(SCRIPTS / "terraform-update.sh")],
                                    env=env, text=True, capture_output=True)
            self.assertTrue(calls.exists(), result.stderr)
            commands = calls.read_text().splitlines()
            return result, commands

    def test_safe_update_applies_exact_saved_plan(self):
        result, calls = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        plan_path = next(arg[5:] for arg in calls[0].split() if arg.startswith("-out="))
        self.assertEqual(calls[-1], f"apply {plan_path}")
        self.assertIn("-no-color", calls[0])

    def test_delete_and_both_replacement_orders_refuse(self):
        for actions in (["delete"], ["delete", "create"], ["create", "delete"], ["forget"]):
            with self.subTest(actions=actions):
                result, calls = self.run_gate(actions)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c.startswith("apply ") for c in calls))

    def test_no_change_skips_apply(self):
        result, calls = self.run_gate(plan_exit=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)

    def test_plan_and_inspection_failures_refuse(self):
        for options in ({"plan_exit": 1}, {"plan_exit": 137}, {"show_exit": 1},
                        {"invalid": True}, {"actions": ["unknown"]}):
            with self.subTest(options=options):
                result, calls = self.run_gate(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c.startswith("apply ") for c in calls))

    def test_explicit_confirmation_allows_saved_destructive_plan(self):
        result, calls = self.run_gate(["create", "delete"], confirm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(calls[-1].startswith("apply "))

    def test_final_convergence_check_never_applies_drift(self):
        result, calls = self.run_gate(check_only=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("apply ") for c in calls))

    def test_credential_reset_is_blocked_even_with_destructive_override(self):
        resource = {"address": "aws_secretsmanager_secret_version.github", "type": "aws_secretsmanager_secret_version",
                    "change": {"actions": ["update"], "before": {"secret_id": "existing", "secret_string": "existing-key"},
                               "after": {"secret_id": "existing", "secret_string": "PLACEHOLDER"}}}
        result, calls = self.run_gate(resource=resource, confirm=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("apply ") for c in calls))


class BackendTests(unittest.TestCase):
    def test_clean_and_previous_account_backends_are_rebound(self):
        spec = importlib.util.spec_from_file_location("backends", SCRIPTS / "prepare-backends.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean = root / "backend.tfvars"
            old = root / "gateway-backend.tfvars"
            other = root / "gateway.tfvars"
            clean.write_text('bucket = "adp-terraform-state-ACCOUNT_ID"\n')
            old.write_text('bucket = "adp-terraform-state-111111111111"\nkey = "dev/gateway"\n')
            other.write_text('trusted_account = "111111111111"\n')
            module.prepare(root, "222222222222")
            self.assertIn('adp-terraform-state-222222222222', clean.read_text())
            self.assertIn('adp-terraform-state-222222222222', old.read_text())
            self.assertIn('key = "dev/gateway"', old.read_text())
            self.assertIn('111111111111', other.read_text())
            with self.assertRaises(ValueError):
                module.prepare(root, "invalid")


if __name__ == "__main__":
    unittest.main()
