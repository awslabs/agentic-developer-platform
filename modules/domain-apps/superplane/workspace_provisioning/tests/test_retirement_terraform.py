"""Saved-artifact binding and deletion-only effect classification."""

import hashlib
import json
from dataclasses import replace

import pytest
from harness_jobs.effects import CallEffect, call_effect
from harness_jobs.identity import OperationRefused

from workspace_provisioning.retirement_plan import compose_retirement_plan
from workspace_provisioning.retirement_terraform import ReviewedDestroy

from .test_retirement_plan import inventory


@pytest.fixture
def reviewed(tmp_path):
    record = inventory()
    target = {
        "org_id": record.org_id,
        "workspace_id": record.workspace_id,
        "account_id": record.cluster_arn.split(":")[4],
        "aws_region": "us-east-1",
        "environment": "dev",
        "workspace_name": "sample",
    }
    saved = tmp_path / "saved.tfplan"
    saved.write_bytes(b"reviewed binary artifact")
    rendered = tmp_path / "plan.json"
    rendered.write_text(
        json.dumps({"resource_changes": [{"change": {"actions": ["delete"]}}]})
    )
    backend = {"bucket": "reviewed-state", "key": "workspace.tfstate"}
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    backend_digest = hashlib.sha256(
        json.dumps(backend, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document = {
        **target,
        "plan_file_sha256": digest,
        "plan_sha256": hashlib.sha256(rendered.read_bytes()).hexdigest(),
        "backend": backend,
    }
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(document))
    artifact = ReviewedDestroy(
        "original-allocation",
        digest,
        backend_digest,
        saved,
        rendered,
        authorization,
        tmp_path,
        target,
    )
    parameters = {
        "allocation_id": "original-allocation",
        "original_allocation_id": "original-allocation",
        "terraform_plan_file_sha256": digest,
        "terraform_backend_sha256": backend_digest,
    }
    return artifact, record, parameters


def test_original_allocation_and_exact_reviewed_artifact_are_required(reviewed):
    artifact, record, parameters = reviewed
    assert artifact.read(record, parameters)[0] == artifact.plan_file.read_bytes()
    step = artifact.step()
    assert (
        call_effect(step.operation_kind, provider=step.provider) is CallEffect.REMOVES
    )
    assert step in compose_retirement_plan(record, managed_destroy=artifact).steps
    with pytest.raises(OperationRefused):
        artifact.read(record, {**parameters, "allocation_id": "fresh-allocation"})


def test_adopted_cluster_never_accepts_terraform_destroy(reviewed):
    artifact, record, parameters = reviewed
    with pytest.raises(OperationRefused):
        artifact.read(replace(record, cluster_ownership="adopted"), parameters)


@pytest.mark.parametrize("changed", ["artifact", "backend", "target"])
def test_replaced_review_inputs_are_refused(reviewed, changed):
    artifact, record, parameters = reviewed
    if changed == "artifact":
        artifact.plan_file.write_bytes(b"different plan")
    else:
        document = json.loads(artifact.authorization.read_text())
        if changed == "backend":
            document["backend"]["key"] = "another-workspace.tfstate"
        else:
            document["workspace_id"] = "another-workspace"
        artifact.authorization.write_text(json.dumps(document))
    with pytest.raises(OperationRefused):
        artifact.read(record, parameters)


@pytest.mark.parametrize(
    "actions", [["create"], ["update"], ["delete", "create"], ["create", "delete"]]
)
def test_removal_authority_cannot_apply_creating_or_replacement_plan(reviewed, actions):
    artifact, record, parameters = reviewed
    artifact.plan_json.write_text(
        json.dumps(
            {
                "resource_changes": [
                    {"change": {"actions": ["delete"]}},
                    {"change": {"actions": actions}},
                ]
            }
        )
    )
    document = json.loads(artifact.authorization.read_text())
    document["plan_sha256"] = hashlib.sha256(
        artifact.plan_json.read_bytes()
    ).hexdigest()
    artifact.authorization.write_text(json.dumps(document))
    with pytest.raises(OperationRefused, match="creating or replacement"):
        artifact.read(record, parameters)


@pytest.mark.asyncio
async def test_native_destroy_runs_guard_with_isolated_renewable_credentials(
    reviewed, monkeypatch, tmp_path
):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from harness_jobs.execution import CallOutcome

    from workspace_provisioning.process import WorkerProcesses
    from workspace_provisioning.retirement_terraform import TerraformDestroy

    artifact, record, parameters = reviewed
    marker = tmp_path / "guard-ran"
    guard = tmp_path / "guard.py"
    guard.write_text(
        "import os,pathlib\n"
        "assert 'AWS_ACCESS_KEY_ID' not in os.environ\n"
        "assert 'AWS_PROFILE' not in os.environ\n"
        "assert os.environ['AWS_EC2_METADATA_DISABLED']=='true'\n"
        "assert os.environ['AWS_CONTAINER_CREDENTIALS_FULL_URI'].startswith('http://127.0.0.1:')\n"
        f"pathlib.Path({str(marker)!r}).write_text('verified')\n"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-must-not-leak")
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    process = WorkerProcesses(
        binaries={"python": sys.executable},
        directory=tmp_path,
        session=SimpleNamespace(),
        region="us-east-1",
        verify=lambda: None,
    )
    destroy = TerraformDestroy(
        python_binary=sys.executable,
        guard_script=guard,
        terraform_binary=sys.executable,
        process=process,
    )
    authorize = AsyncMock()
    result = await destroy.execute(
        artifact, record, {**parameters, "max_runtime_seconds": "10"}, authorize
    )
    assert result[0] is CallOutcome.SUCCEEDED
    assert marker.read_text() == "verified"
    assert authorize.await_count >= 3


@pytest.mark.asyncio
async def test_native_destroy_stops_before_child_when_current_authority_is_revoked(
    reviewed, tmp_path
):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from workspace_provisioning.process import WorkerProcesses
    from workspace_provisioning.retirement_terraform import TerraformDestroy

    artifact, record, parameters = reviewed
    marker = tmp_path / "must-not-exist"
    guard = tmp_path / "guard.py"
    guard.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    process = WorkerProcesses(
        binaries={"python": sys.executable},
        directory=tmp_path,
        session=SimpleNamespace(),
        region="us-east-1",
        verify=lambda: None,
    )
    destroy = TerraformDestroy(
        python_binary=sys.executable,
        guard_script=guard,
        terraform_binary=sys.executable,
        process=process,
    )
    authorize = AsyncMock(side_effect=OperationRefused("fence revoked"))
    with pytest.raises(OperationRefused, match="revoked"):
        await destroy.execute(
            artifact, record, {**parameters, "max_runtime_seconds": "10"}, authorize
        )
    assert not marker.exists()
