import contextlib
import io
import json
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
release_module = runpy.run_path(str(ROOT / "modules/tools/scripts/release_scope.py"))
operator_sequence = release_module["operator_sequence"]
platform_push_deferred = release_module["platform_push_deferred"]


class OperatorSequenceTests(unittest.TestCase):
    def test_mixed_push_defers_but_schema_only_runs_automatically(self):
        self.assertTrue(platform_push_deferred(["modules/tools/platform/schemas/github.yaml",
                                                "modules/tools/agentcore/infra/browser.tf"]))
        self.assertFalse(platform_push_deferred(["modules/tools/platform/schemas/github.yaml"]))

    def test_real_bootstrap_change_defers_schema_push(self):
        changed = ["modules/tools/platform/schemas/github.yaml",
                   "platform/automation-infra/shared-tools-deployment.tf"]
        self.assertTrue(platform_push_deferred(changed))
        self.assertEqual([step["component"] for step in operator_sequence(changed)],
                         ["protected bootstrap", "platform targets"])

    def test_provider_apply_precedes_changed_interceptor_and_target_sync(self):
        changed = ["modules/tools/platform/bootstrap/shared-tools-build.tf",
                   "modules/tools/agentcore/agentcore_tools/handler.py",
                   "modules/tools/interceptor/tools_interceptor/handler.py"]
        steps = operator_sequence(changed)
        self.assertEqual([step["component"] for step in steps],
                         ["protected bootstrap", "shared Lambda and Browser", "interceptor", "platform targets"])
        self.assertEqual(steps[1]["workflow"], "shared-agentcore-tools-deploy.yml")
        self.assertEqual(steps[1]["trigger"], "dispatch apply")
        self.assertEqual(steps[-1]["trigger"], "dispatch apply after shared runtime receipt")
        for step in steps:
            if step["workflow"] is not None:
                self.assertTrue((WORKFLOWS / step["workflow"]).is_file())

    def test_schema_only_selects_push_apply_without_shared_dispatch(self):
        steps = operator_sequence(["modules/tools/platform/schemas/credentials.yaml"])
        self.assertEqual(steps, [{"component": "platform targets", "workflow": "tools-platform-deploy.yml",
                                  "trigger": "dispatch apply with native approval"}])

    def test_worker_client_and_door_use_actual_workflows(self):
        steps = operator_sequence(["modules/tools/task-sdk/protocol.mjs",
                                   "modules/agent-context/context_mcp/handler.py"])
        self.assertEqual([step["workflow"] for step in steps],
                         ["agent-worker-image.yml", "agent-context-deploy.yml"])
        self.assertEqual([step["trigger"] for step in steps], ["push image build", "dispatch"])

    def test_shared_only_and_mixed_schema_plans_wait_for_provider_before_facade(self):
        shared = "modules/tools/adp_tools/search_input.py"
        schema = "modules/tools/platform/schemas/websearch.yaml"
        expected = [
            {"component": "protected bootstrap", "workflow": None,
             "trigger": "coordinator prepares scoped build, release role and private state bindings"},
            {"component": "shared Lambda and Browser", "workflow": "shared-agentcore-tools-deploy.yml",
             "trigger": "dispatch apply"},
            {"component": "platform targets", "workflow": "tools-platform-deploy.yml",
             "trigger": "dispatch apply after shared runtime receipt"},
            {"component": "tool-adapters", "workflow": "tools-platform-deploy.yml",
             "trigger": "dispatch release stage tool-adapters with native approval"},
            {"component": "facade and reconciler", "workflow": "tools-facade-deploy.yml", "trigger": "push apply"},
        ]
        for changed in ([shared], [schema, shared], [shared, schema, shared, "modules/tools/pyproject.toml"]):
            with self.subTest(changed=changed):
                self.assertTrue(platform_push_deferred(changed))
                self.assertEqual(operator_sequence(changed), expected)

    def test_shared_metadata_has_no_provider_dependency(self):
        metadata = "modules/tools/pyproject.toml"
        self.assertFalse(platform_push_deferred([metadata]))
        self.assertEqual(operator_sequence([metadata]), [
            {"component": "facade and reconciler", "workflow": "tools-facade-deploy.yml", "trigger": "push apply"},
        ])
        mixed = ["modules/tools/platform/schemas/websearch.yaml", metadata]
        self.assertFalse(platform_push_deferred(mixed))
        self.assertEqual(operator_sequence(mixed)[0], {
            "component": "platform targets", "workflow": "tools-platform-deploy.yml",
            "trigger": "dispatch apply with native approval",
        })
        self.assertFalse(platform_push_deferred(["modules/tools/adp_tools_backup/search_input.py"]))

    def test_cli_guard_refuses_shared_source_before_emitting_a_release_plan(self):
        paths = ["modules/tools/platform/schemas/websearch.yaml", "modules/tools/adp_tools/search_input.py"]
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["release_scope", "--guard-platform-push", "--base", "a" * 40, "--source", "b" * 40]),
            patch("subprocess.run") as git,
            patch("subprocess.check_output", return_value="\n".join(paths)),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            self.assertRaises(SystemExit) as refused,
        ):
            release_module["main"]()
        self.assertEqual(refused.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("wait for protected shared provider apply", errors.getvalue())
        self.assertEqual(git.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "git" for call in git.call_args_list))

    def test_cli_metadata_only_is_plan_only_without_provider_work(self):
        output = io.StringIO()
        with (
            patch("sys.argv", ["release_scope", "--guard-platform-push", "--base", "a" * 40, "--source", "b" * 40]),
            patch("subprocess.run") as git,
            patch("subprocess.check_output", return_value="modules/tools/pyproject.toml\n"),
            contextlib.redirect_stdout(output),
        ):
            release_module["main"]()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["status"], "plan-only")
        self.assertEqual(plan["source_revision"], "b" * 40)
        self.assertEqual([step["component"] for step in plan["steps"]], ["facade and reconciler"])
        self.assertEqual(git.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "git" for call in git.call_args_list))


if __name__ == "__main__":
    unittest.main()
