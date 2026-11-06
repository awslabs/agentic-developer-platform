import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deployment_guard.py"
spec = importlib.util.spec_from_file_location("deployment_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class DeploymentGuardTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "complete": True, "applyable": True, "errored": False,
            "configuration": {"root_module": {
                "resources": [{"type": "aws_autoscaling_group"}, {"type": "aws_launch_template"}],
                "module_calls": {"manager": {"module": {"resources": [{"type": "helm_release"}]}}},
            }},
            "resource_changes": [{"change": {"actions": ["create"]}}],
        }

    def test_apply_accepts_capacity_manager_and_nondestructive_changes(self):
        guard.check_plan(self.plan, "apply")

    def test_discovery_only_stack_fails_closed(self):
        self.plan["configuration"]["root_module"]["resources"] = [{"type": "terraform_data"}]
        self.plan["configuration"]["root_module"]["module_calls"] = {}
        with self.assertRaisesRegex(ValueError, "runner capacity and manager"):
            guard.check_plan(self.plan, "apply")

    def test_apply_refuses_replacement_and_destroy(self):
        for actions in (["delete"], ["delete", "create"]):
            with self.subTest(actions=actions):
                self.plan["resource_changes"][0]["change"]["actions"] = actions
                with self.assertRaisesRegex(ValueError, "apply cannot destroy"):
                    guard.check_plan(self.plan, "apply")

    def test_destroy_refuses_create(self):
        with self.assertRaisesRegex(ValueError, "destroy cannot create"):
            guard.check_plan(self.plan, "destroy")

    def test_identity_requires_both_exact_account_and_role(self):
        role = "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment"
        identity = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-deployment/deploy-1"}
        guard.check_identity(identity, role)
        for change in ({"Account": "987654321098"}, {"Arn": "arn:aws:sts::123456789012:assumed-role/adp-admin/deploy-1"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "does not match"):
                guard.check_identity({**identity, **change}, role)


if __name__ == "__main__":
    unittest.main()

class BackendTests(unittest.TestCase):
    def test_exact_backend_required(self):
        backend = {"backend": {"type": "s3", "config": {
            "bucket": "protected-private-bucket", "key": "dev/modules/gitlab-runners/terraform.tfstate", "encrypt": True,
        }}}
        guard.check_backend(backend, "protected-private-bucket")
        for change in ({"key": "dev/modules/gitlab/terraform.tfstate"}, {"encrypt": False}, {"bucket": "other-bucket"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.check_backend({"backend": {"type": "s3", "config": {**backend["backend"]["config"], **change}}}, "protected-private-bucket")
        with self.assertRaises(ValueError):
            guard.check_backend(backend, "")


class WorkflowSafetyTests(unittest.TestCase):
    def test_manual_jobs_share_lock_and_use_one_local_plan(self):
        workflows = Path(__file__).resolve().parents[5] / ".github" / "workflows"
        for name in ("apply", "destroy"):
            with self.subTest(name=name):
                source = (workflows / f"gitlab-runners-{name}.yml").read_text()
                environment = "adp-gitlab-deploy-dev" if name == "apply" else "adp-deploy-dev"
                for required in ("refs/heads/main", environment, "trusted-deployment",
                                 "deployment_guard.py identity", "cancel-in-progress: false"):
                    self.assertIn(required, source)
                if name == "apply":
                    self.assertIn('python3 modules/source-control/gitlab/scripts/runner_workflow.py', source)
                    self.assertIn('GITLAB_RUNNER_CONFIG_JSON', source)
                    self.assertNotIn('terraform plan ', source)
                else:
                    for required in ("EXPECTED_BUCKET", "backend --expected-bucket", "runner-stack-plan-guard.py destroy",
                                     'terraform apply -input=false -no-color -auto-approve "$RUNNER_TEMP/runners.tfplan"',
                                     '-out="$RUNNER_TEMP/runners.tfplan"', "::add-mask::"):
                        self.assertIn(required, source)
                for forbidden in ("tee ", "upload-artifact", "set -x", "terraform destroy -auto-approve"):
                    self.assertNotIn(forbidden, source)

class BuildPathTests(unittest.TestCase):
    def test_build_requires_reviewed_source_and_real_s2_artifacts(self):
        workflows = Path(__file__).resolve().parents[5] / ".github" / "workflows"
        build = (workflows / "gitlab-runners-build.yml").read_text()
        check = (workflows / "gitlab-runners-check.yml").read_text()
        for required in ("refs/heads/main", "adp-build-dev", "./.github/actions/trusted-build",
                         "versions.lock.json", "codebuild/bs-gitlab-runner-bootstrap.yml", "batch-get-projects", "projects[0].serviceRole", "projects[0].source.location",
                         "./.github/actions/codebuild-run", "source_revision: ${{ github.sha }}",
                         "deployment_guard.py identity"):
            self.assertIn(required, build)
        for required in ("terraform -chdir=modules/source-control/gitlab/runners test",
                         "tests/test_rendered_runner.py", "tests/test_job_host.py", "tests/test_bootstrap.py"):
            self.assertIn(required, check)
        for forbidden in ("upload-artifact", "set -x", "service_role_arn:", "buildspec:", "source_namespace:"):
            self.assertNotIn(forbidden, build)
