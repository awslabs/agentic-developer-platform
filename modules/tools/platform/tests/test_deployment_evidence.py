"""The tools-platform context and bound release derive every identity field from the run itself."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("deployment_evidence", ROOT / "modules/tools/platform/scripts/deployment_evidence.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SOURCE, DEFINITION = "a" * 40, "b" * 40
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-abc123"


def _env(tmp_path, **overrides):
    release = {
        "component": "tools-platform",
        "environment": "dev",
        "source_revision": SOURCE,
        "definition_revision": DEFINITION,
        "gateway_id": "adp-tools-abc123",
        "gateway_arn": GATEWAY_ARN,
        "gateway_url": "https://adp-tools-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "targets": {"websearch": "T1", "code-interpreter": "T2"},
    }
    release.update(overrides.pop("release", {}))
    release_dir = tmp_path / "release"
    release_dir.mkdir(exist_ok=True)
    (release_dir / "release.json").write_text(json.dumps(release))
    env = {
        "CONTEXT_SOURCE": SOURCE,
        "CONTEXT_REVISION": DEFINITION,
        "CONTEXT_CORRELATION": "",
        "CONTEXT_REPOSITORY_ID": "42",
        "GITHUB_RUN_ID": "7",
        "GITHUB_RUN_ATTEMPT": "1",
        "INPUT_ENVIRONMENT": "dev",
        "INPUT_MODE": "release",
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_RELEASE_DIR": str(release_dir),
        "RUNNER_TEMP": str(tmp_path),
    }
    env.update(overrides)
    return env


def _read(args):
    assert args == ["sts", "get-caller-identity"]
    return {"Account": "123456789012"}


class DeploymentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_context_binds_run_identity_and_gateway_resource(self):
        context = MODULE.context(_env(self.tmp_path), _read)
        self.assertEqual(
            context,
            {
                "schema_version": 1,
                "repository_id": 42,
                "run_id": 7,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/tools-platform-deploy.yml",
                "workflow_revision": DEFINITION,
                "source_revision": SOURCE,
                "account_id": "123456789012",
                "region": "us-east-1",
                "resource_kind": "agentcore-gateway",
                "resource_id": GATEWAY_ARN,
                "inputs": {"environment": "dev", "mode": "release", "manual_source_revision": ""},
                "correlation": "",
            },
        )

    def test_bound_release_adds_identity_without_inventing_evidence(self):
        env = _env(self.tmp_path)
        path, bound = MODULE.bind_release(env, _read)
        self.assertEqual(path, Path(env["ADP_TOOLS_RELEASE_DIR"]) / "release.json")
        self.assertEqual(bound["component"], "tools-platform")
        self.assertEqual(bound["workflow_revision"], DEFINITION)
        self.assertEqual((bound["repository_id"], bound["run_id"], bound["run_attempt"]), (42, 7, 1))
        self.assertEqual(bound["resource_id"], GATEWAY_ARN)
        self.assertEqual(bound["target_ids"], ["T1", "T2"])
        self.assertTrue(bound["produced_at"].endswith("+00:00"))
        self.assertNotIn("target_sync_revision", bound)
        self.assertNotIn("tools_list_sha256", bound)

    def test_refuses_evidence_that_does_not_belong_to_this_run(self):
        cases = [
            ({"ACCOUNT_ID": "999999999999"}, "account"),
            ({"release": {"gateway_arn": "arn:aws:bedrock-agentcore:eu-west-1:123456789012:gateway/x"}}, "region"),
            ({"release": {"definition_revision": "c" * 40}}, "definition"),
            ({"release": {"component": "tools-facade"}}, "deployment"),
            ({"INPUT_MODE": "apply"}, "inputs"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    MODULE.context(_env(self.tmp_path, **overrides), _read)

    def test_main_writes_context_and_rewrites_release(self):
        env = _env(self.tmp_path)
        with mock.patch.object(MODULE.os, "environ", env), mock.patch.object(
            subprocess, "check_output", lambda *a, **k: json.dumps({"Account": "123456789012"}).encode()
        ):
            MODULE.main(["x", "context"])
            MODULE.main(["x", "bind-release"])
        context = json.loads((self.tmp_path / "adp-deployment-context" / "deployment-context.json").read_bytes())
        self.assertEqual(context["resource_kind"], "agentcore-gateway")
        self.assertEqual(json.loads((Path(env["ADP_TOOLS_RELEASE_DIR"]) / "release.json").read_bytes())["run_id"], 7)


if __name__ == "__main__":
    unittest.main()
