"""The original Identity attempt must be bound before any native mutation."""

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/tools-platform-deploy.yml"


@pytest.fixture
def dispatch(monkeypatch):
    monkeypatch.syspath_prepend(str(WORKFLOW.parent.parent.parent / "modules/tools/adapters"))
    module = importlib.import_module("identity_dispatch_context")
    definition = WORKFLOW.read_text().split("  workflow_dispatch:\n    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
    inputs = {
        match.group(1): re.search(r"^        default: (.*)$", match.group(2), re.M).group(1).strip("'\"")
        for match in re.finditer(r"^      ([a-z_]+):\n((?:        .*\n)*)", definition, re.M)
    }
    inputs.update(
        mode="identity",
        stage="tool-adapters",
        environment="dev",
        adp_source_revision="a" * 40,
        adp_definition_revision="b" * 40,
        adp_correlation="c" * 64,
        adp_contract_digest="d" * 64,
    )
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "42",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_WORKFLOW_SHA": "e" * 40,
        "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "e" * 40,
        "ADP_CORRELATION": "c" * 64,
        "ADP_IDENTITY_CONTRACT_DIGEST": "d" * 64,
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "example-cluster",
        "ADP_TOOLS_ADAPTER_NAMESPACE": "tool-adapters",
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_TOOLS_STATE_BUCKET": "adp-dev-state",
    }
    env["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"] = json.dumps(inputs)
    return module, env, inputs


def test_context_matches_workflow_defaults_and_approved_contract(dispatch):
    module, env, inputs = dispatch
    document = module.context(env, inputs)
    assert document["inputs"] == {
        name: value for name, value in inputs.items() if not name.startswith("adp_") or name == "adp_contract_digest"
    }
    assert document["workflow_revision"] == env["GITHUB_WORKFLOW_SHA"]
    assert document["resource_id"] == "example-cluster/tool-adapters"
    assert document["correlation"] == inputs["adp_correlation"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "release"),
        ("adapter_action", "handoff"),
        ("adapter_action", "rollback"),
        ("stage", "platform"),
        ("environment", "prod"),
        ("adp_source_revision", "f" * 40),
        ("adp_correlation", "f" * 64),
        ("adp_contract_digest", "f" * 64),
        ("manual_source_revision", "a" * 40),
        ("adp_definition_revision", "not-a-commit"),
    ],
)
def test_context_refuses_changed_dispatch_input(dispatch, field, value):
    module, env, inputs = dispatch
    inputs[field] = value
    with pytest.raises(RuntimeError):
        module.context(env, inputs)


def test_context_refuses_unknown_and_missing_workflow_inputs(dispatch):
    module, env, inputs = dispatch
    inputs["identity_gateway_revision"] = "a" * 40
    with pytest.raises(RuntimeError):
        module.context(env, inputs)
    del inputs["identity_gateway_revision"]
    del inputs["adp_contract_digest"]
    with pytest.raises(RuntimeError):
        module.context(env, inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_WORKFLOW_REF", "example/repo/.github/workflows/other.yml@refs/heads/main"),
        ("GITHUB_RUN_ID", "0"),
    ],
)
def test_context_refuses_wrong_workflow_or_attempt(dispatch, field, value):
    module, env, inputs = dispatch
    env[field] = value
    with pytest.raises(RuntimeError):
        module.context(env, inputs)


@pytest.mark.parametrize("changed,role", [(False, True), (True, True), (False, False)])
def test_entrypoint_writes_only_verified_original_context(dispatch, monkeypatch, tmp_path, changed, role):
    module, env, inputs = dispatch
    commands = []

    class DeploymentStub:
        def __init__(self, settings):
            self.source = settings["ADP_SOURCE_REVISION"]
            self.account = settings["ADP_TOOLS_ACCOUNT_ID"]
            self.role = settings["ADP_TOOLS_DEPLOY_ROLE_ARN"]

        def aws(self, *parts):
            assert parts == ("sts", "get-caller-identity")
            return {
                "Account": self.account,
                "Arn": (
                    "arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-deployment/attempt"
                    if role
                    else "arn:aws:sts::123456789012:assumed-role/other/attempt"
                ),
            }

        def run(self, parts):
            commands.append(parts)
            if parts == ["git", "rev-parse", "HEAD"]:
                return self.source
            if parts[:2] == ["git", "show"]:
                return "different" if changed and inputs["adp_definition_revision"] in parts[2] else "definition"
            return ""

    monkeypatch.setattr(module, "Deployment", DeploymentStub)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    output = tmp_path / "evidence" / "dispatch-context.json"
    monkeypatch.setattr(sys, "argv", ["identity_dispatch_context.py", "--output", str(output)])
    if changed or not role:
        with pytest.raises(RuntimeError):
            module.main()
        assert not output.exists()
    else:
        module.main()
        document = json.loads(output.read_text())
        assert document["inputs"]["adp_contract_digest"] == inputs["adp_contract_digest"]
        assert [
            "git",
            "merge-base",
            "--is-ancestor",
            inputs["adp_definition_revision"],
            env["GITHUB_WORKFLOW_SHA"],
        ] in commands
        with pytest.raises(FileExistsError):
            module.main()
