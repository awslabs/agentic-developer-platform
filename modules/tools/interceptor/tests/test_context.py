import json
import os
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
MODULE = runpy.run_path(str(ROOT / "modules/tools/interceptor/deployment_context.py"))


@pytest.fixture
def target():
    env = {
        "CONTEXT_SOURCE": "a" * 40,
        "CONTEXT_REVISION": "b" * 40,
        "CONTEXT_CORRELATION": "c" * 64,
        "CONTEXT_REPOSITORY_ID": "17",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "2",
        "INPUT_ENVIRONMENT": "dev",
        "INPUT_ROLLBACK_TO_VERSION": "",
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "TOOLS_INTERCEPTOR_FUNCTION_NAME": "example-interceptor",
    }
    function = {"FunctionName": "example-interceptor", "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor"}
    read = Mock(side_effect=[{"Account": env["ACCOUNT_ID"]}, {"Configuration": function}])
    return env, function, read


@pytest.mark.parametrize("rollback,correlation", [("", "c" * 64), ("", ""), ("41", "")])
def test_context_records_verified_target_and_effective_inputs(target, rollback, correlation):
    env, function, read = target
    env.update(INPUT_ROLLBACK_TO_VERSION=rollback, CONTEXT_CORRELATION=correlation)
    document = MODULE["context"](env, read)
    assert document["resource_kind"] == "lambda-function"
    assert document["resource_id"] == function["FunctionArn"]
    assert document["inputs"] == {"environment": "dev", "rollback_to_version": rollback}
    assert document["correlation"] == correlation
    assert (document["repository_id"], document["run_id"], document["run_attempt"]) == (17, 42, 2)
    read.assert_called_with(["lambda", "get-function", "--function-name", "example-interceptor", "--region", "us-east-1"])


@pytest.mark.parametrize("field,value", [
    ("ACCOUNT_ID", "000000000000"), ("AWS_REGION", "us-west-2"),
    ("TOOLS_INTERCEPTOR_FUNCTION_NAME", "other-function"), ("CONTEXT_SOURCE", "main"),
    ("CONTEXT_REVISION", "main"), ("CONTEXT_CORRELATION", "invalid"), ("GITHUB_RUN_ATTEMPT", "0"),
])
def test_context_refuses_wrong_target_or_unbounded_identity(target, field, value):
    env, _, read = target
    env[field] = value
    with pytest.raises(ValueError):
        MODULE["context"](env, read)


def test_context_entrypoint_writes_only_bounded_context(target, tmp_path, monkeypatch):
    env, _, read = target
    env["RUNNER_TEMP"] = str(tmp_path)
    env["UNRELATED_SECRET"] = "must-not-appear"
    monkeypatch.setattr(os, "environ", env)

    def aws_response(command, timeout):
        assert command[0] == "aws" and command[-2:] == ["--output", "json"]
        assert timeout == 20
        return json.dumps(read(command[1:-2])).encode()

    monkeypatch.setattr(MODULE["subprocess"], "check_output", aws_response)
    MODULE["main"]()
    payload = (tmp_path / "adp-deployment-context/deployment-context.json").read_bytes()
    assert len(payload) <= 65536
    assert b"must-not-appear" not in payload
    assert json.loads(payload)["workflow_path"] == ".github/workflows/tools-interceptor-deploy.yml"


def test_workflow_publishes_context_after_identity_before_mutation():
    workflow = yaml.load((ROOT / ".github/workflows/tools-interceptor-deploy.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    record = steps[names.index("Record deployment workflow context")]
    upload = steps[names.index("Publish deployment workflow context")]
    assert record["run"] == "python3 modules/tools/interceptor/deployment_context.py"
    assert record["env"] == {
        "CONTEXT_REVISION": "${{ github.workflow_sha }}",
        "CONTEXT_SOURCE": "${{ inputs.adp_source_revision || github.sha }}",
        "CONTEXT_CORRELATION": "${{ inputs.adp_correlation || '' }}",
        "CONTEXT_REPOSITORY_ID": "${{ github.repository_id }}",
        "INPUT_ENVIRONMENT": "${{ inputs.environment || 'dev' }}",
        "INPUT_ROLLBACK_TO_VERSION": "${{ inputs.rollback_to_version || '' }}",
        "TOOLS_INTERCEPTOR_FUNCTION_NAME": "${{ vars.ADP_TOOLS_INTERCEPTOR_FUNCTION_NAME }}",
    }
    assert "uses" not in upload, "deployment evidence is published to S3, never to GitHub artifacts"
    assert upload["run"] == (
        'python3 "$RUNNER_TEMP/adp-deployment-evidence/scripts/publish-deployment-evidence.py" context '
        'tools-interceptor-deploy.yml "$RUNNER_TEMP/adp-deployment-context/deployment-context.json"'
    )
    assert workflow["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert names.index("Load deployment evidence publisher from workflow revision") < names.index(upload["name"])
    assert names.index("Load deployment config") < names.index(record["name"]) < names.index(upload["name"])
    assert names.index(upload["name"]) < names.index("Publish verified Lambda version and promote live alias")
