import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake"
CONNECTORS = ROOT / "modules/connectors/devlake/infra"


class TerraformEnvironmentTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE))
        self.addCleanup(lambda: sys.path.remove(str(MODULE)))
        import deploy

        self.deploy = deploy
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = {
            "deployment_label": "fixture-deployer", "environment": "dev",
            "aws_account_id": "000000000000", "aws_region": "us-east-1",
            "cell_id": "fixture-cell",
        }

    def generated_environment(self, variables):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("TF_VAR_")}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(self.deploy.subprocess, "run") as run:
            self.deploy.terraform_run(self.config, variables, self.directory / "reviewed.tfplan")
        self.assertEqual([call.args[0][9] for call in run.call_args_list], ["init", "plan"])
        self.assertEqual(run.call_args_list[0].kwargs["env"], run.call_args_list[1].kwargs["env"])
        return run.call_args.kwargs["env"]

    def test_strings_stay_literal_and_other_values_are_json(self):
        variables = {
            "environment": "dev", "aws_account_id": "000000000000",
            "api_image": "registry.example.invalid/api@sha256:" + "a" * 64,
            "worker_image": "registry.example.invalid/worker@sha256:" + "b" * 64,
            "cell_id": "fixture-cell", "enabled": True, "connectors_enabled": False,
            "provider_egress_cidrs": ["198.51.100.0/24"],
            "deployment_role_boundaries": {
                "arn:aws:iam::000000000000:role/fixture-api":
                    "arn:aws:iam::000000000000:policy/fixture-boundary"},
        }
        environment = self.generated_environment(variables)
        for key, value in variables.items():
            with self.subTest(variable=key):
                self.assertEqual(environment["TF_VAR_" + key],
                                 value if isinstance(value, str) else json.dumps(value))

    @unittest.skipUnless(shutil.which("terraform"), "Terraform is required for mocked-provider plans")
    def test_generated_environment_passes_real_connector_plans(self):
        variables = {
            "enabled": False, "environment": "dev", "aws_region": "us-east-1",
            "aws_account_id": "000000000000",
            "api_image": "registry.example.invalid/api@sha256:" + "a" * 64,
            "deployment_role_boundaries": {}, "provider_egress_cidrs": [],
        }
        environment = self.generated_environment(variables)
        infra = self.directory / "infra"
        shutil.copytree(CONNECTORS, infra, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
        for arguments in (("init", "-backend=false", "-input=false", "-no-color"),
                          ("test", "-no-color")):
            result = subprocess.run(["terraform", f"-chdir={infra}", *arguments],
                                    env=environment, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("6 passed, 0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
