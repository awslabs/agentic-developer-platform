"""Exercise the checked-in workflows' revision guard and provider context."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from src.orchestration.deployment_workflow_provider import TRANSPORT_INPUTS, WorkflowContext

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/gateway/scripts/deployment-workflow-context.py"
spec = importlib.util.spec_from_file_location("deployment_context", SCRIPT)
context_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_module)


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


@pytest.mark.parametrize("name", ["gateway-deploy.yml", "run-gateway-migrations.yml"])
def test_revision_guard_runs_before_checkout_and_rejects_moved_definition(name):
    document = workflow(name)
    events = document.get("on", document.get(True))
    assert TRANSPORT_INPUTS <= events["workflow_dispatch"]["inputs"].keys()
    steps = next(iter(document["jobs"].values()))["steps"]
    guard = steps[0]
    assert guard["name"] == "Guard engine delivery revisions"
    assert steps[1]["uses"].startswith("actions/checkout@")
    for source, definition, correlation, actual, expected in [
        ("", "", "", "a" * 40, 0),
        ("b" * 40, "a" * 40, "c" * 64, "a" * 40, 0),
        ("b" * 40, "a" * 40, "c" * 64, "d" * 40, 1),
        ("main", "a" * 40, "c" * 64, "a" * 40, 1),
        ("$(echo untrusted)", "a" * 40, "c" * 64, "a" * 40, 1),
        ("", "a" * 40, "", "a" * 40, 1),
    ]:
        result = subprocess.run(
            ["/bin/bash", "-c", guard["run"]],
            env={"ADP_SOURCE": source, "ADP_DEFINITION": definition, "ADP_CORRELATION": correlation, "ACTUAL_DEFINITION": actual},
            capture_output=True,
        )
        assert (result.returncode != 0) == bool(expected), result.stderr


def test_source_revision_reaches_every_checkout_and_build_tag():
    document = workflow("gateway-deploy.yml")
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ inputs.adp_source_revision || github.sha }}"
    backend = document["jobs"]["deploy-backend"]
    build = next(step for step in backend["steps"] if step.get("uses") == "./.github/actions/codebuild-run")
    assert "name=IMAGE_TAG,value=${{ inputs.adp_source_revision || github.sha }}" in build["with"]["environment_variables"]
    # Pass the immutable revision through the reusable workflow input: GitHub
    # suppresses job outputs containing the masked AWS account ID.
    assert document["jobs"]["run-migrations"]["with"]["expected_image_tag"] == "${{ inputs.adp_source_revision || github.sha }}"
    assert TRANSPORT_INPUTS <= document["jobs"]["run-migrations"]["with"].keys()
    migration = workflow("run-gateway-migrations.yml")
    assert TRANSPORT_INPUTS <= migration[True]["workflow_call"]["inputs"].keys()


@pytest.fixture
def evidence():
    env = dict(
        CONTEXT_WORKFLOW=".github/workflows/gateway-deploy.yml",
        CONTEXT_SOURCE="a" * 40,
        CONTEXT_REVISION="b" * 40,
        CONTEXT_REPOSITORY_ID="123",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="1",
        ACCOUNT_ID="123456789012",
        CUSTOMER_ACCOUNT_ID="999999999999",
        AWS_REGION="us-east-1",
        ENVIRONMENT="dev",
        INPUT_ACCOUNT_ID="",
        INPUT_CUSTOMER_ACCOUNT_ID="",
        INPUT_CUSTOMER_AWS_LABEL="",
        INPUT_CUSTOMER_USER_ID="",
        INPUT_ENVIRONMENT="dev",
        AWS_SECRET_ACCESS_KEY="must-never-be-published",
        UNTRUSTED_INPUT="must-not-be-published",
    )

    def read(args):
        if args[0] == "sts":
            return {"Account": "123456789012"}
        return {"cluster": {"name": "adp-dev-eks-cluster", "arn": "arn:aws:eks:us-east-1:123456789012:cluster/adp-dev-eks-cluster"}}

    return env, read


def test_context_is_valid_bounded_and_contains_only_explicit_nonsecret_fields(evidence):
    env, read = evidence
    document = context_module.context(env, read)
    accepted = WorkflowContext.model_validate(document)
    assert accepted.resource_id == "adp-dev-eks-cluster/adp-gateway"
    assert "must-never" not in json.dumps(document) and "must-not" not in json.dumps(document)
    assert accepted.inputs["environment"] == "dev"


@pytest.mark.parametrize("field,value", [("ACCOUNT_ID", "999999999999"), ("EKS_CLUSTER", "other-cluster"), ("AWS_REGION", "us-west-2")])
def test_actual_target_mismatch_cannot_publish_context(evidence, field, value):
    env, read = evidence
    with pytest.raises(ValueError):
        context_module.context({**env, field: value}, read)
