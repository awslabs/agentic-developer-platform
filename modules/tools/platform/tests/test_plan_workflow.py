import json
import re
import subprocess
import unittest
from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[4] / ".github/workflows/tools-platform-ci.yml"


class PlanWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.job = cls.workflow["jobs"]["plan"]

    def test_module_and_workflow_changes_trigger_pull_request_plan(self):
        paths = self.workflow[True]["pull_request"]["paths"]
        self.assertIn("modules/tools/platform/infra/**", paths)
        self.assertIn("modules/tools/platform/bootstrap/**", paths)
        self.assertIn("modules/tools/platform/scripts/**", paths)
        self.assertIn("modules/tools/platform/tests/**", paths)
        self.assertIn(".github/workflows/tools-platform-ci.yml", paths)
        self.assertIn(".github/workflows/tools-platform-deploy.yml", paths)
        self.assertIn("modules/tools/deploy.sh", paths)

    def test_bootstrap_is_validated_without_live_state(self):
        step = next(step for step in self.job["steps"] if step.get("name") == "Validate bootstrap-owned infrastructure with mock providers")
        self.assertEqual(step["working-directory"], "modules/tools/platform/bootstrap")
        self.assertIn("terraform test -no-color", step["run"])
        self.assertIn("-backend=false", step["run"])

    def test_pull_request_job_only_plans_and_uploads_redacted_actions(self):
        steps = self.job["steps"]
        scripts = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("terraform plan -input=false -lock=false", scripts)
        self.assertIn("terraform show -json", scripts)
        self.assertIn(".resource_changes[]?", scripts)
        self.assertNotIn("terraform apply", scripts)
        self.assertNotIn("terraform destroy", scripts)
        upload = next(step for step in steps if step.get("uses") == "./.github/actions/upload-ci-evidence-s3")
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/tools-platform-plan.json")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 7)

    def test_plan_artifact_requires_target_count_matching_interceptor_configuration(self):
        plan = next(step for step in self.job["steps"] if step.get("name") == "Validate and plan against the existing dev state")
        expression = re.search(r"terraform show -json .*?\| jq -e --argjson expected_targets \"\$expected_targets\" '(.*?)' > ", plan["run"], re.DOTALL).group(1)
        resource_types = ["aws_bedrockagentcore_gateway"]
        for types, expected_targets, expected in [
            (resource_types, 0, True),
            (resource_types + ["aws_bedrockagentcore_gateway_target"] * 3, 3, True),
            ([], 0, False),
            (resource_types + ["aws_bedrockagentcore_gateway"], 0, False),
            (resource_types + ["aws_bedrockagentcore_gateway_target"], 0, False),
            (resource_types + ["aws_bedrockagentcore_gateway_target"] * 2, 3, False),
            (resource_types + ["aws_cognito_user_pool"], 0, False),
            (resource_types + ["aws_kms_key"], 0, False),
            (resource_types + ["aws_iam_role"], 0, False),
        ]:
            with self.subTest(types=types):
                fixture = {"planned_values": {"root_module": {"resources": [{"type": kind} for kind in types]}}}
                result = subprocess.run(["jq", "-e", "--argjson", "expected_targets", str(expected_targets), expression], input=json.dumps(fixture), capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        missing_plan = subprocess.run(["jq", "-e", "--argjson", "expected_targets", "0", expression], input="{}", capture_output=True, text=True)
        self.assertNotEqual(missing_plan.returncode, 0)
        self.assertIn('test -s "$RUNNER_TEMP/tools-platform-plan.json"', plan["run"])
        self.assertIn('if (.interceptor_lambda_arn // "") == "" then 0 else 3 end', plan["run"])

    def test_evidence_retains_runner_identity_before_planning_role_replaces_credentials(self):
        steps = self.job["steps"]
        preserve = next(step for step in steps if step.get("name") == "Preserve runner identity for private CI evidence")
        credentials = next(step for step in steps if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
        self.assertLess(steps.index(preserve), steps.index(credentials))
        self.assertIn('CI_EVIDENCE_RUNNER_ROLE_ARN=%s', preserve["run"])
        self.assertIn('CI_EVIDENCE_RUNNER_TOKEN_FILE=%s', preserve["run"])
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', preserve["run"])

    def test_plan_uses_separate_read_only_role_and_private_variables(self):
        self.assertEqual(self.job["env"]["PLAN_ROLE_ARN"], "${{ vars.ADP_TOOLS_PLAN_ROLE_ARN }}")
        private_config = next(step for step in self.job["steps"] if step.get("name") == "Check read-only plan configuration")
        self.assertEqual(private_config["env"]["PLAN_TFVARS_JSON"], "${{ secrets.ADP_TOOLS_PLAN_TFVARS_JSON }}")
        self.assertNotIn("PLAN_TFVARS_JSON", self.job["env"])
        # Non-secret inputs are committed so a pull request can add a variable and its
        # value together; the secret stays an optional overlay for private values.
        self.assertIn("environments/dev/modules/tools-platform.tfvars.json", private_config["run"])
        self.assertIn("jq -s '.[0] * .[1]'", private_config["run"])
        self.assertNotIn('test -n "$PLAN_TFVARS_JSON"', private_config["run"])
        self.assertNotIn("ADP_DEPLOY_ROLE_ARN", WORKFLOW.read_text())
        self.assertIn("backend-config=\"bucket=$STATE_BUCKET\"", "\n".join(step.get("run", "") for step in self.job["steps"]))

    def test_plan_release_and_drift_pin_the_tested_terraform_version(self):
        deployment = yaml.safe_load(WORKFLOW.with_name("tools-platform-deploy.yml").read_text())
        jobs = [self.job, deployment["jobs"]["release"], deployment["jobs"]["drift"]]
        for job in jobs:
            setup = next(step for step in job["steps"] if step.get("uses", "").startswith("hashicorp/setup-terraform@"))
            self.assertEqual(setup["with"]["terraform_version"], "1.14.6")

    def test_mock_provider_checks_run_before_private_configuration(self):
        steps = self.job["steps"]
        mock_check = next(step for step in steps if step.get("name") == "Validate and test with mock providers")
        private_config = next(step for step in steps if step.get("name") == "Check read-only plan configuration")
        credentials = next(step for step in steps if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
        self.assertEqual(mock_check["working-directory"], "modules/tools/platform/infra")
        self.assertIn("terraform init -backend=false -lockfile=readonly -input=false", mock_check["run"])
        self.assertIn("terraform validate -no-color", mock_check["run"])
        self.assertIn("terraform test -no-color", mock_check["run"])
        self.assertLess(steps.index(mock_check), steps.index(private_config))
        self.assertLess(steps.index(mock_check), steps.index(credentials))


if __name__ == "__main__":
    unittest.main()
