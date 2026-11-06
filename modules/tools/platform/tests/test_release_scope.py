import runpy
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
release_scope = runpy.run_path(str(ROOT / "modules/tools/scripts/release_scope.py"))["release_scope"]


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


class ReleaseScopeTests(unittest.TestCase):
    def test_schema_only_change_triggers_protected_apply_and_ci(self):
        scope = release_scope(["modules/tools/platform/schemas/github.yaml"])
        self.assertEqual(scope, [{"component": "platform targets", "workflow": "tools-platform-deploy.yml",
                                  "trigger": "dispatch apply with native approval"}])
        for name, event in (("tools-platform-deploy.yml", "push"), ("tools-platform-ci.yml", "pull_request")):
            self.assertIn("modules/tools/platform/schemas/**", workflow(name)["on"][event]["paths"])
        apply = workflow("tools-platform-deploy.yml")["jobs"]["release"]["steps"]
        self.assertTrue(any("bash modules/tools/deploy.sh --infra --targets" in step.get("run", "")
                            for step in apply))

    def test_provider_and_bootstrap_changes_do_not_claim_automatic_apply(self):
        scope = release_scope(["modules/tools/platform/bootstrap/main.tf",
                               "modules/tools/agentcore/agentcore_tools/handler.py"])
        self.assertEqual([item["trigger"] for item in scope],
                         ["protected bootstrap prerequisite", "dispatch apply"])
        self.assertIsNone(scope[0]["workflow"])
        shared = workflow("shared-agentcore-tools-deploy.yml")["on"]
        self.assertNotIn("push", shared)
        self.assertEqual(shared["workflow_dispatch"]["inputs"]["mode"]["default"], "plan")
        self.assertIn("modules/tools/platform/bootstrap/**",
                      workflow("tools-platform-ci.yml")["on"]["pull_request"]["paths"])

    def test_change_selection_uses_real_workflows_and_rejects_traversal(self):
        targets = release_scope(["modules/tools/facade/handler.py", "modules/gateway/cli/client.py",
                                 "modules/agent-context/door.py", "modules/tools/facade/handler.py"])
        self.assertEqual([item["workflow"] for item in targets],
                         ["tools-facade-deploy.yml", "gateway-deploy.yml", "agent-context-deploy.yml"])
        self.assertEqual(targets[-1]["trigger"], "dispatch")
        with self.assertRaises(ValueError):
            release_scope(["modules/tools/platform/infra/../../secrets"])

    def test_shared_source_selects_both_tools_consumers_once(self):
        scope = release_scope([
            "modules/tools/adp_tools/search_input.py",
            "modules/tools/adp_tools/contracts.py",
            "modules/tools/facade/tools_facade/server.py",
            "modules/tools/agentcore/agentcore_tools/handler.py",
        ])
        self.assertEqual(scope, [
            {"component": "shared Lambda and Browser", "workflow": "shared-agentcore-tools-deploy.yml",
             "trigger": "dispatch apply"},
            {"component": "facade and reconciler", "workflow": "tools-facade-deploy.yml", "trigger": "push apply"},
            {"component": "tool-adapters", "workflow": "tools-platform-deploy.yml",
             "trigger": "dispatch release stage tool-adapters with native approval"},
        ])
        # Selecting a source dependency must not turn provider publication into
        # an automatic deployment or change the default reviewed-plan mode.
        shared = workflow("shared-agentcore-tools-deploy.yml")["on"]
        self.assertEqual(set(shared), {"workflow_dispatch"})
        self.assertEqual(shared["workflow_dispatch"]["inputs"]["mode"]["default"], "plan")

    def test_shared_metadata_selects_only_its_installed_facade_consumer(self):
        self.assertEqual(release_scope(["modules/tools/pyproject.toml"]), [
            {"component": "facade and reconciler", "workflow": "tools-facade-deploy.yml", "trigger": "push apply"},
        ])
        self.assertEqual(release_scope([
            "modules/tools/pyproject.toml.backup",
            "modules/tools/adp_tools_backup/search_input.py",
        ]), [])


    def test_adapter_only_release_uses_protected_stage_without_core_or_facade(self):
        from modules.tools.scripts.release_scope import operator_sequence

        self.assertEqual([step["component"] for step in operator_sequence([
            "modules/tools/adapters/tool_adapters/server.py"])], ["tool-adapters"])
        selected = release_scope(["modules/tools/adp_tools/adapter_contracts.py"])
        self.assertEqual({step["component"] for step in selected},
                         {"shared Lambda and Browser", "facade and reconciler", "tool-adapters"})
        stage = workflow("tools-platform-deploy.yml")["jobs"]["adapter-foundation"]["if"]
        self.assertIn("inputs.mode == 'release'", stage)
        self.assertIn("inputs.stage == 'tool-adapters'", stage)
        ci = workflow("tools-platform-ci.yml")
        self.assertIn("tests/e2e/tools/release_observer.py", ci["on"]["pull_request"]["paths"])
        planning = next(step for step in ci["jobs"]["plan"]["steps"]
                        if step.get("name") == "Test PR planning boundaries")
        self.assertIn("test_release_observer.py tests/e2e/tools/test_release_window.py", planning["run"])

if __name__ == "__main__":
    unittest.main()
