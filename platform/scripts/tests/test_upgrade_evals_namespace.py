"""The #7651 ownership handoff must pass the real gate without enabling deletes."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evals_policy", SCRIPTS / "upgrade-plan-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
ACCOUNT = "123456789012"


def fixtures():
    namespace = {"id": "adp-gateway-evals", "metadata": [{"name": "adp-gateway-evals", "uid": "existing-namespace-uid"}]}
    change = {"address": "kubernetes_namespace.gateway_evals", "mode": "managed", "type": "kubernetes_namespace",
              "change": {"actions": ["forget"], "before": namespace, "after": None}}
    plan = {"variables": {"environment": {"value": "dev"}, "aws_region": {"value": "us-east-1"}},
            "resource_changes": [change]}
    state = {"resources": [
        {"mode": "managed", "module": "module.eks", "type": "aws_eks_cluster", "name": "main", "instances": [
            {"attributes": {"name": "adp-dev-eks-cluster", "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/adp-dev-eks-cluster"}}]},
        {"mode": "managed", "module": "module.eks", "type": "kubernetes_namespace", "name": "gateway_evals",
         "instances": [{"attributes": copy.deepcopy(namespace)}]},
    ]}
    return plan, state


class EvalsNamespaceMigrationTests(unittest.TestCase):
    def test_only_verified_duplicate_owner_can_be_forgotten(self):
        plan, state = fixtures()
        result = policy.evaluate(plan, "agent-factory", ACCOUNT, platform_state=state)
        self.assertEqual(result, {"routine": ["kubernetes_namespace.gateway_evals"], "blocked": [], "protected": []})
        self.assertTrue(policy.evaluate(plan, "agent-factory", ACCOUNT)["blocked"])
        self.assertTrue(policy.evaluate(plan, "platform", ACCOUNT, platform_state=state)["blocked"])

    def test_namespace_deletion_replacement_and_other_resources_stay_blocked(self):
        plan, state = fixtures()
        cases = [("actions", ["delete"]), ("actions", ["delete", "create"]), ("actions", ["create", "delete"]),
                 ("address", "kubernetes_namespace.other"), ("type", "kubernetes_namespace_v1"),
                 ("mode", "data"), ("deposed", "old-instance"), ("id", "other-namespace"),
                 ("name", "other-namespace"), ("uid", "")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                modified = copy.deepcopy(plan)
                resource = modified["resource_changes"][0]
                if field == "actions":
                    resource["change"][field] = value
                elif field == "id":
                    resource["change"]["before"][field] = value
                elif field in ("name", "uid"):
                    resource["change"]["before"]["metadata"][0][field] = value
                else:
                    resource[field] = value
                self.assertTrue(policy.evaluate(modified, "agent-factory", ACCOUNT, platform_state=state)["blocked"])

    def test_missing_stale_or_foreign_platform_ownership_is_rejected(self):
        plan, state = fixtures()
        cases = []
        for field, value in (("uid", "different-namespace-uid"), ("name", "other-namespace")):
            changed = copy.deepcopy(state)
            changed["resources"][1]["instances"][0]["attributes"]["metadata"][0][field] = value
            cases.append(changed)
        for arn in (f"arn:aws:eks:eu-west-1:{ACCOUNT}:cluster/adp-dev-eks-cluster",
                    "arn:aws:eks:us-east-1:999999999999:cluster/adp-dev-eks-cluster",
                    f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/adp-prod-eks-cluster"):
            changed = copy.deepcopy(state)
            changed["resources"][0]["instances"][0]["attributes"]["arn"] = arn
            cases.append(changed)
        changed = copy.deepcopy(state)
        changed["resources"][1]["instances"][0]["deposed"] = "old"
        cases.extend([changed, {}, {"resources": state["resources"][:1]}, {"resources": state["resources"] * 2}])
        for index, changed in enumerate(cases):
            with self.subTest(case=index):
                self.assertTrue(policy.evaluate(plan, "agent-factory", ACCOUNT, platform_state=changed)["blocked"])

    def test_state_read_is_target_scoped_and_fail_closed(self):
        plan, state = fixtures()
        with patch.object(policy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(state), "")) as run:
            self.assertEqual(policy.read_namespace_owner(plan, ACCOUNT), state)
            self.assertEqual(run.call_args.args[0], ["aws", "s3", "cp",
                f"s3://adp-terraform-state-{ACCOUNT}/dev/platform/terraform.tfstate", "-", "--region", "us-east-1", "--only-show-errors"])
        with patch.object(policy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "private diagnostics")):
            with self.assertRaisesRegex(ValueError, "Cannot read platform namespace ownership"):
                policy.read_namespace_owner(plan, ACCOUNT)
        with patch.object(policy.subprocess, "run") as run:
            plan["variables"]["environment"]["value"] = "../other"
            with self.assertRaises(ValueError):
                policy.read_namespace_owner(plan, ACCOUNT)
            run.assert_not_called()

    def test_shell_gate_applies_only_when_platform_owns_same_namespace(self):
        for scenario in ("owned", "wrong_uid", "unreadable", "malformed", "delete", "fresh"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, state = fixtures()
                if scenario == "wrong_uid":
                    state["resources"][1]["instances"][0]["attributes"]["metadata"][0]["uid"] = "different"
                if scenario == "delete":
                    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
                if scenario == "fresh":
                    plan["resource_changes"] = []
                (root / "plan.json").write_text(json.dumps(plan))
                (root / "owner.json").write_text("invalid" if scenario == "malformed" else json.dumps(state))
                for name, body in {
                    "terraform": '''echo "$*" >> "$REVIEW_ROOT/terraform.calls"
case "$1" in
  plan) exit 2;;
  show) cat "$REVIEW_ROOT/plan.json";;
  apply) exit 0;;
esac
''',
                    "aws": '''echo "$*" >> "$REVIEW_ROOT/aws.calls"
[ "$REVIEW_SCENARIO" != unreadable ] || exit 1
cat "$REVIEW_ROOT/owner.json"
''',
                }.items():
                    file = root / name
                    file.write_text("#!/bin/bash\n" + body)
                    file.chmod(0o755)
                env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", REVIEW_ROOT=directory,
                           REVIEW_SCENARIO=scenario, ACCOUNT_ID=ACCOUNT, TMPDIR=directory, CONFIRM_DESTRUCTIVE="false")
                for key in ("UPGRADE_RUN_DIR", "UPGRADE_CHECK_ONLY", "ADP_CHAT_RETIREMENT_DIR", "ADP_WORKER_MIGRATION_EVIDENCE"):
                    env.pop(key, None)
                command = '''set -euo pipefail
ok() { :; }
fail() { echo "$*" >&2; exit 1; }
source "$1"
terraform_update_apply agent-factory /dev/null
'''
                result = subprocess.run(["bash", "-c", command, "test", str(SCRIPTS / "terraform-update.sh")],
                                        env=env, text=True, capture_output=True)
                allowed = scenario in ("owned", "fresh")
                self.assertEqual(result.returncode == 0, allowed, result.stdout + result.stderr)
                calls = (root / "terraform.calls").read_text().splitlines()
                self.assertEqual(any(c.startswith("apply ") for c in calls), allowed)
                if allowed:
                    saved_plan = next(arg[5:] for arg in calls[0].split() if arg.startswith("-out="))
                    self.assertEqual(calls[-1], "apply " + saved_plan)
                if scenario in ("delete", "fresh"):
                    self.assertFalse((root / "aws.calls").exists())


if __name__ == "__main__":
    unittest.main()
