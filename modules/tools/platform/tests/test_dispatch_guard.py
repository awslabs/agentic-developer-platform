import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
GUARD = ROOT / "modules/tools/platform/scripts/guard_dispatch.sh"
WORKFLOW = ROOT / ".github/workflows/tools-platform-deploy.yml"


class DispatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            **os.environ,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SHA": "a" * 40,
            "ADP_CORRELATION": "b" * 64,
            "ADP_SOURCE": "a" * 40,
            "ADP_DEFINITION": "c" * 40,
            "ACTUAL_DEFINITION": "c" * 40,
            "ADP_MANUAL_SOURCE": "",
        }

    def guard(self, phase, cwd=None):
        return subprocess.run(
            ["bash", str(GUARD), phase], env=self.env, cwd=cwd,
            capture_output=True, text=True, check=False,
        )

    def _definition_repo(self, directory):
        """A reviewed main history whose workflow file the dispatched run must match byte for byte."""
        def git(*args):
            return subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, check=True).stdout.strip()

        workflow = Path(directory) / ".github/workflows/tools-platform-deploy.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: Deploy tools platform\n")
        git("init", "-b", "main")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "reviewed definition")
        reviewed = git("rev-parse", "HEAD")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "unrelated main commit")
        git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
        return git, reviewed, workflow

    def test_valid_engine_and_manual_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            git, reviewed, _workflow = self._definition_repo(directory)
            # The engine pins the reviewed definition; main has moved on with an
            # unrelated commit, so the actual workflow_sha differs while the file
            # is identical. That must pass: pins would otherwise go stale on every
            # merge to main.
            self.env.update(ADP_DEFINITION=reviewed, ACTUAL_DEFINITION=git("rev-parse", "HEAD"))
            self.assertEqual(self.guard("definition", directory).returncode, 0)
            self.env.update(ADP_CORRELATION="orch:12345678-1234-5234-9234-123456789abc")
            self.assertEqual(self.guard("definition", directory).returncode, 0)
            self.env.update(ADP_CORRELATION="", ADP_SOURCE="", ADP_DEFINITION="", ADP_MANUAL_SOURCE="a" * 40)
            self.assertEqual(self.guard("definition", directory).returncode, 0)

    def test_identity_definition_requires_digest_and_protected_target(self):
        with tempfile.TemporaryDirectory() as directory:
            git, reviewed, _ = self._definition_repo(directory)
            self.env.update(
                ADP_DEFINITION=reviewed, ACTUAL_DEFINITION=reviewed,
                ADP_SOURCE=reviewed,
                ADP_CORRELATION="orch:12345678-1234-5234-9234-123456789abc",
                ADP_TOOLS_MODE="identity", ADP_TOOLS_STAGE="tool-adapters",
                ADP_TOOLS_ENVIRONMENT="dev", ADP_CONTRACT_DIGEST="a" * 64,
            )
            self.assertEqual(self.guard("definition", directory).returncode, 0)
            for key, value in (
                ("ADP_CONTRACT_DIGEST", ""), ("ADP_TOOLS_STAGE", "platform"),
                ("ADP_TOOLS_ENVIRONMENT", "prod"),
                ("ADP_MANUAL_SOURCE", reviewed),
            ):
                old = self.env[key]
                self.env[key] = value
                self.assertNotEqual(self.guard("definition", directory).returncode, 0, key)
                self.env[key] = old
            self.env["ADP_TOOLS_MODE"] = "release"
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)

    def test_handoff_and_rollback_digest_are_limited_to_protected_adapter_release(self):
        with tempfile.TemporaryDirectory() as directory:
            _, reviewed, _ = self._definition_repo(directory)
            self.env.update(ADP_DEFINITION=reviewed, ADP_SOURCE=reviewed,
                            ADP_TOOLS_MODE="release", ADP_TOOLS_STAGE="tool-adapters",
                            ADP_TOOLS_ENVIRONMENT="dev", ADP_CONTRACT_DIGEST="a" * 64)
            for action in ("handoff", "rollback"):
                self.env["ADP_TOOLS_ADAPTER_ACTION"] = action
                self.assertEqual(self.guard("definition", directory).returncode, 0)
                for key, value in (("ADP_TOOLS_STAGE", "platform"), ("ADP_TOOLS_ENVIRONMENT", "prod"),
                                   ("ADP_TOOLS_ADAPTER_ACTION", "foundation"), ("ADP_CONTRACT_DIGEST", "")):
                    previous = self.env[key]
                    self.env[key] = value
                    self.assertNotEqual(self.guard("definition", directory).returncode, 0)
                    self.env[key] = previous

    def test_identity_producer_has_distinct_required_job_and_both_artifacts(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        assert {"identity", "release", "drift"} == set(inputs["mode"]["options"])
        assert "adp_contract_digest" in inputs
        assert "identity_gateway_revision" not in inputs
        job = workflow["jobs"]["adapter-identity"]
        assert job["needs"] == "guard" and job["environment"] == "adp-tools-deploy-dev"
        assert "inputs.mode == 'identity'" in job["if"]
        runs = [step.get("run", "") for step in job["steps"]]
        context = next(index for index, run in enumerate(runs) if "deploy.sh dispatch-context" in run)
        signed = next(index for index, run in enumerate(runs) if "context tools-platform-deploy.yml" in run)
        publication = next(index for index, run in enumerate(runs) if "deploy.sh publish" in run)
        applied = next(index for index, run in enumerate(runs) if "deploy.sh apply" in run)
        assert context < signed < publication < applied
        assert job["env"]["ADP_TOOLS_FOUNDATION_GATEWAY_REVISION"] == "${{ inputs.adp_source_revision }}"
        assert any("deploy.sh identity" in run for run in runs)
        assert any("repository-artifact atlassian-identity" in run for run in runs)
        assert any("identity_delivery.py" in run for run in runs)
        names = {step.get("with", {}).get("name") for step in job["steps"]}
        assert "atlassian-identity-${{ github.run_id }}-${{ github.run_attempt }}" in names
        assert workflow["env"]["ADP_DELIVERY_EVIDENCE_STORE"] == "s3-oidc-v1"
        assert any('delivery delivery-evidence "$RUNNER_TEMP/adp-adapter-stage/delivery.json"' in run for run in runs)
        assert workflow["jobs"]["adapter-foundation"]["if"].find("inputs.mode == 'release'") != -1

    def test_definition_rule_applies_to_the_named_workflow_file(self):
        """Sibling deploy workflows reuse the guard by naming their own file."""
        with tempfile.TemporaryDirectory() as directory:
            git, reviewed, _workflow = self._definition_repo(directory)
            sibling = Path(directory) / ".github/workflows/tools-interceptor-deploy.yml"
            sibling.write_text("name: Deploy tools interceptor\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "sibling definition")
            sibling_reviewed = git("rev-parse", "HEAD")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "later main commit")
            git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
            self.env.update(ADP_DEFINITION=sibling_reviewed, ACTUAL_DEFINITION=git("rev-parse", "HEAD"), ADP_WORKFLOW_FILE=".github/workflows/tools-interceptor-deploy.yml")
            self.assertEqual(self.guard("definition", directory).returncode, 0)
            # A pin that predates the sibling file cannot verify it.
            self.env.update(ADP_DEFINITION=reviewed)
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)
            # The file name is bounded to the workflows directory.
            self.env.update(ADP_DEFINITION=sibling_reviewed, ADP_WORKFLOW_FILE="../../etc/passwd")
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)

    def test_mismatch_and_missing_engine_fields_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            git, reviewed, workflow = self._definition_repo(directory)
            self.env.update(ADP_DEFINITION=reviewed, ACTUAL_DEFINITION=git("rev-parse", "HEAD"))
            workflow.write_text("name: Deploy tools platform\non: [push]\n")
            self.assertNotEqual(self.guard("definition", directory).returncode, 0, "changed workflow definition must be refused")
            workflow.write_text("name: Deploy tools platform\n")
            self.env["ADP_DEFINITION"] = "d" * 40
            self.assertNotEqual(self.guard("definition", directory).returncode, 0, "unknown definition commit must be refused")
            self.env["ADP_DEFINITION"] = ""
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)
            self.env["ADP_MANUAL_SOURCE"] = "e" * 40
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)
            self.env.update(ADP_MANUAL_SOURCE="", ADP_DEFINITION=reviewed, ADP_CORRELATION="not-a-correlation")
            self.assertNotEqual(self.guard("definition", directory).returncode, 0)

    def test_non_main_dispatch_refused(self):
        self.env["GITHUB_REF"] = "refs/heads/feature"
        self.assertNotEqual(self.guard("definition").returncode, 0)

    def test_scheduled_dev_drift_requires_main_and_refuses_prod(self):
        self.env.update(GITHUB_EVENT_NAME="schedule", GITHUB_SHA="a" * 40, ADP_TOOLS_MODE="drift", ADP_TOOLS_ENVIRONMENT="dev")
        self.assertEqual(self.guard("definition").returncode, 0)
        self.env["ADP_TOOLS_ENVIRONMENT"] = "prod"
        self.assertNotEqual(self.guard("definition").returncode, 0)
        self.env.update(ADP_TOOLS_ENVIRONMENT="dev", GITHUB_REF="refs/heads/feature")
        self.assertNotEqual(self.guard("definition").returncode, 0)

    def test_source_must_be_ancestor_of_main(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-b", "main")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "reviewed")
            git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
            self.env["ADP_SOURCE"] = base
            self.assertEqual(self.guard("source", directory).returncode, 0)
            git("checkout", "-b", "feature", base)
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "unreviewed")
            self.env["ADP_SOURCE"] = git("rev-parse", "HEAD")
            self.assertNotEqual(self.guard("source", directory).returncode, 0)

    def test_workflow_rejects_before_source_checkout_and_plan(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        steps = workflow["jobs"]["guard"]["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertIn("guard_dispatch.sh definition", steps[1]["run"])
        self.assertIn("guard_dispatch.sh source", steps[2]["run"])
        self.assertEqual(steps[3]["with"]["ref"], "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}")
        self.assertNotIn("terraform plan", "\n".join(step.get("run", "") for step in steps[:3]))


if __name__ == "__main__":
    unittest.main()
