import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"


class SharedStageWorkflowTests(unittest.TestCase):
    def test_public_guidance_uses_supported_help_flags(self):
        guidance = (ROOT / "docs/tools/architecture.md").read_text()
        manifest = (ROOT / "docs/adp-platform-deployment/deployment-manifest.md").read_text()
        for script, flags in (
            ("modules/tools/deploy.sh", ("--infra", "--tool-lambdas", "--interceptor",
                                         "--facade", "--targets", "--verify", "--plan", "--resume")),
            ("platform/scripts/deploy-all.sh", ("--tools-only", "--ci")),
            ("platform/scripts/undeploy.sh", ("--tools-only", "--dry-run", "--yes")),
        ):
            with self.subTest(script=script):
                output = subprocess.run(["bash", script, "--help"], cwd=ROOT,
                                        capture_output=True, text=True, check=True).stdout
                for flag in flags:
                    self.assertIn(flag, output)
                    self.assertIn(flag, guidance + manifest)

    def test_protected_workflows_select_shared_stages_without_inline_mutations(self):
        for name, stage in (
            ("tools-platform-deploy.yml", "--infra --targets"),
            ("tools-interceptor-deploy.yml", "--interceptor"),
            ("tools-facade-deploy.yml", "--facade"),
        ):
            with self.subTest(workflow=name):
                workflow = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
                paths = workflow["on"]["push"]["paths"]
                self.assertIn("modules/tools/deploy.sh", paths)
                self.assertIn("modules/tools/orchestrate.sh", paths)
                jobs = workflow["jobs"]
                self.assertTrue(any("adp-tools-deploy-" in str(job.get("environment", ""))
                                    for job in jobs.values()))
                commands = "\n".join(step.get("run", "") for job in jobs.values()
                                     for step in job.get("steps", []))
                self.assertIn(f"bash modules/tools/deploy.sh {stage}", commands)
                for mutation in ("terraform apply", "kubectl apply", "kubectl set image", "aws lambda update-function-code"):
                    self.assertNotIn(mutation, commands)

    def test_existing_pipeline_inputs_remain_checked_and_ci_follows_shared_script(self):
        platform = yaml.load((WORKFLOWS / "tools-platform-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        release = platform["jobs"]["release"]
        apply = next(step for step in release["steps"]
                     if "bash modules/tools/deploy.sh --infra --targets" in step.get("run", ""))
        for field in ("ADP_TOOLS_ACCOUNT_ID", "ADP_TOOLS_AWS_REGION", "ADP_TOOLS_DEPLOY_ROLE_ARN",
                      "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_TOOLS_RELEASE_DIR"):
            self.assertIn(field, apply["env"])
        interceptor = yaml.load((WORKFLOWS / "tools-interceptor-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        steps = interceptor["jobs"]["deploy"]["steps"]
        self.assertLess(next(index for index, step in enumerate(steps)
                             if step.get("name") == "Verify reviewed immutable source"),
                        next(index for index, step in enumerate(steps)
                             if "bash modules/tools/deploy.sh --interceptor" in step.get("run", "")))
        for ci_name in ("tools-platform-ci.yml", "tools-interceptor-ci.yml"):
            ci = yaml.load((WORKFLOWS / ci_name).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("modules/tools/orchestrate.sh", ci["on"]["pull_request"]["paths"])

    def test_platform_ci_executes_pytest_journal_cases_not_just_unittest_discovery(self):
        workflow = yaml.load((WORKFLOWS / "tools-platform-ci.yml").read_text(), Loader=yaml.BaseLoader)
        stage = next(step for step in workflow["jobs"]["plan"]["steps"]
                     if step.get("name") == "Test PR planning boundaries")
        self.assertIn("pytest==9.0.3", stage["run"])
        self.assertIn("python -m unittest discover", stage["run"])
        self.assertIn("python -m pytest -q modules/tools/platform/tests", stage["run"])

    def test_facade_passes_checked_identity_and_source_to_shared_stage(self):
        workflow = yaml.load((WORKFLOWS / "tools-facade-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["deploy"]
        for field in ("ADP_TOOLS_ACCOUNT_ID", "ADP_TOOLS_AWS_REGION", "ADP_TOOLS_ENVIRONMENT",
                      "ADP_TOOLS_DEPLOY_ROLE_ARN", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION"):
            self.assertIn(field, job["env"])
        steps = job["steps"]
        guard = next(index for index, step in enumerate(steps)
                     if step.get("name") == "Verify immutable reviewed source and engine inputs")
        assume = next(index for index, step in enumerate(steps)
                      if step.get("name") == "Establish protected deployment identity")
        stage = next(index for index, step in enumerate(steps)
                     if "bash modules/tools/deploy.sh --facade" in step.get("run", ""))
        self.assertLess(guard, assume)
        self.assertLess(assume, stage)


if __name__ == "__main__":
    unittest.main()
