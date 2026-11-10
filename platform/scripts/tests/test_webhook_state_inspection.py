"""Webhook adoption helpers must treat a module without state as managing nothing.

Before the module's first apply `terraform state list` exits 1 ("No state file
was found!"), which previously stopped every fresh installation.
"""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (
    ROOT / "modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh"
)

EMPTY_S3_STATE = json.dumps(
    {
        "version": 4,
        "serial": 0,
        "lineage": "",
        "outputs": {},
        "resources": [],
        "check_results": None,
    }
)
MANAGED_STATE = json.dumps(
    {
        "version": 4,
        "serial": 7,
        "lineage": "x",
        "outputs": {},
        "resources": [
            {
                "mode": "managed",
                "type": "kubernetes_role",
                "name": "door_identity_keys_read",
                "instances": [],
            }
        ],
    }
)
HARNESS = """set -euo pipefail
fail() { echo "FAIL: $*" >&2; exit 1; }
warn() { :; }
ok() { :; }
"""
RBAC = [
    "kubernetes_role.door_identity_keys_read",
    "kubernetes_role_binding.door_identity_keys_read",
]
LOG_GROUP = "aws_cloudwatch_log_group.agent_bootstrap"


def functions():
    source = SCRIPT.read_text()
    start = source.index("webhook_state_addresses() {")
    log_group = source.index("import_bootstrap_log_group() {")
    end = source.index("\n}\n", log_group) + 3
    return source[start:end]


class WebhookStateInspectionTests(unittest.TestCase):
    def run_helper(
        self,
        call,
        pull_rc=0,
        pull_out="",
        list_rc=0,
        list_out="",
        live_objects=False,
        live_log_group=False,
    ):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            record = Path(directory) / "imports"
            (Path(directory) / "pull").write_text(pull_out)
            (Path(directory) / "list").write_text(list_out)
            stubs = {
                "terraform": f"""
                    case "$1 $2" in
                      "state pull") cat {directory}/pull; exit {pull_rc} ;;
                      "state list") [ {list_rc} -eq 0 ] || {{ echo 'No state file was found!' >&2; exit {list_rc}; }}
                                    cat {directory}/list ;;
                      *) echo "unexpected terraform $*" >&2; exit 99 ;;
                    esac""",
                "kubectl": f"exit {0 if live_objects else 1}",
                "aws": f"echo {'/adp/dev/agent-factory/bootstrap' if live_log_group else 'None'}",
                "tf-webhook": f'shift; echo "$*" >> {record}',
            }
            for name, body in stubs.items():
                path = bin_dir / name
                path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body) + "\n")
                path.chmod(0o755)
            harness = HARNESS + functions() + "\n" + call
            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                UPDATE_MODE="false",
                ENVIRONMENT="dev",
                AWS_REGION="us-east-1",
                TF_WEBHOOK=str(bin_dir / "tf-webhook"),
                BOOTSTRAP_LOG_GROUP="/adp/dev/agent-factory/bootstrap",
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            imports = record.read_text().split() if record.exists() else []
            return result, imports

    def test_fresh_local_backend_has_nothing_to_adopt(self):
        result, imports = self.run_helper(
            "adopt_door_identity_rbac", pull_out="", list_rc=1
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(imports, [])

    def test_fresh_s3_backend_empty_state_has_nothing_to_adopt(self):
        result, imports = self.run_helper(
            "adopt_door_identity_rbac", pull_out=EMPTY_S3_STATE, list_rc=1
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(imports, [])

    def test_fresh_state_adopts_objects_that_exist_live(self):
        result, imports = self.run_helper(
            "adopt_door_identity_rbac",
            pull_out=EMPTY_S3_STATE,
            list_rc=1,
            live_objects=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([i for i in imports if i in RBAC], RBAC)

    def test_managed_objects_are_not_imported_again(self):
        result, imports = self.run_helper(
            "adopt_door_identity_rbac",
            pull_out=MANAGED_STATE,
            list_out="\n".join(RBAC) + "\n",
            live_objects=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(imports, [])

    def test_fresh_state_imports_existing_bootstrap_log_group(self):
        result, imports = self.run_helper(
            "import_bootstrap_log_group",
            pull_out=EMPTY_S3_STATE,
            list_rc=1,
            live_log_group=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(LOG_GROUP, imports)

    def test_unreadable_state_stops_the_deploy(self):
        result, imports = self.run_helper("adopt_door_identity_rbac", pull_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect webhook Terraform state", result.stderr)
        self.assertEqual(imports, [])

    def test_invalid_state_document_stops_the_deploy(self):
        result, _ = self.run_helper(
            "adopt_door_identity_rbac", pull_out="<html>not state</html>"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect webhook Terraform state", result.stderr)

    def test_listing_failure_with_resources_stops_the_deploy(self):
        result, _ = self.run_helper(
            "adopt_door_identity_rbac", pull_out=MANAGED_STATE, list_rc=1
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect webhook Terraform state", result.stderr)


if __name__ == "__main__":
    unittest.main()
