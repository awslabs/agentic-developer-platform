"""The native adapter stage must not change the legacy tools release path."""

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/tools-platform-deploy.yml"


class AdapterFoundationStageTests(unittest.TestCase):
    def test_adapter_stage_is_protected_and_separate_from_platform_release(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["stage"]["default"], "platform")
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["stage"]["options"], ["platform", "tool-adapters"])
        jobs = workflow["jobs"]
        job = jobs["adapter-foundation"]
        self.assertEqual(job["needs"], "guard")
        self.assertEqual(job["environment"], "adp-tools-deploy-dev")
        self.assertEqual(job["permissions"]["id-token"], "write")
        steps = job["steps"]
        source_check = next(
            index for index, step in enumerate(steps) if step.get("name") == "Verify source belongs to reviewed main history"
        )
        trusted_deployment = next(
            index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment"
        )
        self.assertLess(source_check, trusted_deployment)
        self.assertIn("git merge-base --is-ancestor HEAD FETCH_HEAD", steps[source_check]["run"])
        self.assertIn('git show "$ADP_DEFINITION_REVISION:$file" | cmp - "$file"', steps[source_check]["run"])
        for selection in ("inputs.stage == 'tool-adapters'", "inputs.environment == 'dev'", "inputs.mode == 'release'"):
            self.assertIn(selection, job["if"])
        self.assertIn("inputs.stage != 'tool-adapters'", jobs["release"]["if"])
        self.assertIn("inputs.stage != 'tool-adapters'", jobs["drift"]["if"])

    def test_native_release_receipts_follow_pinned_image_and_runtime_checks(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["adapter-foundation"]
        steps = job["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        for action in ("publish", "configure", "apply", "verify", "receipt", "foundation"):
            self.assertEqual(commands.count(f"bash modules/tools/adapters/deploy.sh {action}"), 1)
        self.assertLess(commands.index("deploy.sh publish"), commands.index("deploy.sh configure"))
        self.assertLess(commands.index("deploy.sh configure"), commands.index("deploy.sh apply"))
        self.assertLess(commands.index("deploy.sh apply"), commands.index("deploy.sh verify"))
        self.assertLess(commands.index("deploy.sh verify"), commands.index("deploy.sh receipt"))
        self.assertLess(commands.index("deploy.sh receipt"), commands.index("deploy.sh foundation"))
        self.assertLess(commands.index("deploy.sh foundation"), commands.index("publish-deployment-evidence.py\" context"))
        self.assertEqual(job["env"]["ADP_FOUNDATION_EVIDENCE_STORE"], "s3-oidc-v1")
        self.assertIn("repository-artifact atlassian-foundation", commands)
        self.assertLess(commands.index("deploy.sh receipt"), commands.index("publish-deployment-evidence.py\" context"))
        self.assertLess(commands.index("publish-deployment-evidence.py\" context"), commands.index("publish-deployment-evidence.py\" release"))
        self.assertIn("ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN", job["env"])
        self.assertIn("ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON", job["env"])
        self.assertNotIn("runner.temp", " ".join(job["env"].values()))
        for path in (
            "ADP_TOOLS_ADAPTER_PUBLICATION_FILE",
            "ADP_TOOLS_ADAPTER_CONFIG_FILE",
            "ADP_TOOLS_ADAPTER_EVIDENCE_FILE",
            "ADP_TOOLS_ADAPTER_RELEASE_DIR",
        ):
            self.assertIn(path + "=%s/adp-adapter-stage/", steps[0]["run"])
        self.assertEqual(steps[-1]["if"], "always()")
        subprocess.run(["bash", "-n", str(ROOT / "modules/tools/adapters/deploy.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
