"""Saved removal review is path-free; only exact private bytes become executable."""

import json
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRefused

from workspace_provisioning import retirement_destroy_producer as producer
from workspace_provisioning.artifacts import digest
from workspace_provisioning.retirement_terraform import ReviewedDestroy
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.terraform import operation_directory, sha, source_digest

from .test_retirement_terraform import reviewed  # noqa: F401


@pytest.fixture
def persisted(reviewed, tmp_path, monkeypatch):  # noqa: F811
    artifact, inventory, parameters = reviewed
    row = {
        "org_id": inventory.org_id,
        "workspace_id": inventory.workspace_id,
        "account_id": artifact.target["account_id"],
        "source_operation_id": "paid-access",
        "producer_attempt_id": "attempt",
        "producer_fence_token": 1,
    }
    context = SimpleNamespace(state_root=tmp_path / "state")
    root = operation_directory(
        context.state_root,
        row["org_id"],
        row["workspace_id"],
        row["source_operation_id"],
        create=True,
    )
    directory = root / ("access-" + digest(["attempt", 1])[:24]) / "destroy"
    module, output = directory / "module", directory / "review"
    module.mkdir(mode=0o700, parents=True)
    output.mkdir(mode=0o700)
    (module / "main.tf").write_text("# maintained fixture")
    (output / "workspace.tfplan").write_bytes(artifact.plan_file.read_bytes())
    (output / "workspace-plan.json").write_bytes(artifact.plan_json.read_bytes())
    (output / "workspace-authorization.proposed.json").write_bytes(
        artifact.authorization.read_bytes()
    )
    for name in producer.FILES - {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
    }:
        (output / name).write_text("{}")
    metadata = {
        "version": 1,
        "target": artifact.target,
        "files": {name: sha(output / name) for name in producer.FILES},
        "module_sha256": source_digest(module),
        "plan_file_sha256": artifact.plan_file_sha256,
        "plan_json_sha256": sha(output / "workspace-plan.json"),
        "backend_sha256": artifact.backend_sha256,
        "original_allocation_id": artifact.original_allocation_id,
    }
    row["artifact_metadata_json"] = json.dumps({"reviewed_destroy": metadata})
    monkeypatch.setattr(
        producer, "maintained_digest", lambda: metadata["module_sha256"]
    )
    return row, context, inventory, parameters, metadata, directory


def test_review_reference_cannot_read_or_apply_but_exact_saved_result_can(persisted):
    row, context, inventory, parameters, metadata, _ = persisted
    review = producer.destroy_reference(metadata)
    assert not isinstance(review, ReviewedDestroy)
    assert not hasattr(review, "read")
    executable = producer.reviewed_destroy_from_access(row, context)
    assert executable.step() == review.step()
    assert executable.read(inventory, parameters)[0] == b"reviewed binary artifact"


@pytest.mark.parametrize(
    "change", ["plan", "module", "scope", "attempt", "extra", "allocation"]
)
def test_replaced_saved_destroy_or_source_is_refused(persisted, change):
    row, context, inventory, parameters, metadata, directory = persisted
    if change == "plan":
        (directory / "review/workspace.tfplan").write_bytes(b"changed")
    elif change == "module":
        (directory / "module/main.tf").write_text("# changed")
    elif change == "scope":
        row["workspace_id"] = "another-workspace"
    elif change == "attempt":
        row["producer_attempt_id"] = "another-attempt"
    elif change == "extra":
        metadata["plan_file"] = "/caller-selected.tfplan"
        row["artifact_metadata_json"] = json.dumps({"reviewed_destroy": metadata})
    else:
        parameters["original_allocation_id"] = "another-allocation"
    with pytest.raises((LifecycleRefused, OperationRefused, ValueError)):
        producer.reviewed_destroy_from_access(row, context).read(inventory, parameters)


@pytest.mark.parametrize("change", [None, "creating_plan", "foreign_target"])
def test_producer_uses_original_target_and_saved_deletion_only_bytes(
    reviewed,  # noqa: F811
    tmp_path,
    monkeypatch,
    change,
):
    from harness_jobs.identity import OperationRequest

    from .test_lifecycle_policy import runtime_config

    saved, inventory, _ = reviewed
    target = saved.target
    original = OperationRequest(
        "provision",
        "bootstrap-request",
        {
            "workspace_name": target["workspace_name"],
            "lifecycle_request": json.dumps(
                {
                    "mode": "existing-account-managed",
                    "organization_id": "o-fixture1234",
                    "management_account_id": "000000000001",
                    "management_cluster": "management",
                    "region": target["aws_region"],
                    "workspace_id": inventory.workspace_id,
                    "target_account_id": target["account_id"],
                    "vpc_cidr": "10.64.0.0/16",
                    "availability_zones": ["us-east-1a", "us-east-1b"],
                    "cluster_version": "1.31",
                }
            ),
        },
    )
    calls = []

    class PreparedProcess:
        def __init__(self, **kwargs):
            self.directory = kwargs["directory"]
            assert kwargs["session"] is session

        def checked(self, argv, **kwargs):
            calls.append(argv)
            assert argv[-1] == "--destroy"
            output = self.directory / "review"
            output.mkdir()
            rendered = json.loads(saved.plan_json.read_text())
            if change == "creating_plan":
                rendered["resource_changes"][0]["change"]["actions"] = ["create"]
            (output / "workspace.tfplan").write_bytes(saved.plan_file.read_bytes())
            (output / "workspace-plan.json").write_text(json.dumps(rendered))
            authorization = json.loads(saved.authorization.read_text())
            authorization["plan_sha256"] = sha(output / "workspace-plan.json")
            (output / "workspace-authorization.proposed.json").write_text(
                json.dumps(authorization)
            )
            (output / "workspace-inventory.json").write_text(
                '{"destructive_addresses":["aws_eks_cluster.workspace"]}'
            )
            (output / "workspace-estimate.json").write_text('{"bounded_monthly_usd":0}')
            (output / "workspace-backend.json").write_text(
                json.dumps(authorization["backend"])
            )

    monkeypatch.setattr(producer, "WorkerProcesses", PreparedProcess)
    config = runtime_config()
    config["environment"] = target["environment"]
    lease = SimpleNamespace(
        org_id=inventory.org_id, workspace_id=inventory.workspace_id
    )
    original_target = {**target}
    if change == "foreign_target":
        original_target["workspace_id"] = "other-workspace"
    facts = SimpleNamespace(
        source=SimpleNamespace(admitted_request=lambda: original),
        inventory=inventory,
        config=config,
        artifact={"target_json": json.dumps(original_target)},
        operation=SimpleNamespace(
            grant=SimpleNamespace(lease=lease), max_runtime_seconds=900
        ),
        plan=SimpleNamespace(original_allocation_id=saved.original_allocation_id),
    )
    session = object()
    directory = tmp_path / "preparation"
    directory.mkdir()
    if change:
        with pytest.raises((OperationRefused, LifecycleRefused)):
            producer.prepare_destroy(
                facts, SimpleNamespace(), session, directory, lambda: None
            )
    else:
        metadata = producer.prepare_destroy(
            facts, SimpleNamespace(), session, directory, lambda: None
        )
        assert metadata["target"] == target
        assert metadata["plan_file_sha256"] == saved.plan_file_sha256
        assert metadata["original_allocation_id"] == saved.original_allocation_id
        assert metadata["backend_sha256"] == saved.backend_sha256
    assert len(calls) == 1
