"""The exact reviewed binary plan is consumed, never silently planned again."""

from types import SimpleNamespace
import json
import os
from pathlib import Path
import stat

import pytest

from workspace_provisioning.artifacts import canonical
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.terraform import apply, operation_directory, prepare

from .test_lifecycle_policy import runtime_config


class Process:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.wrong_account = False

    def checked(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if "prepare_workspace_plan.py" in argv[1]:
            output = self.directory / "review"
            output.mkdir()
            for name, content in {
                "workspace.tfplan": "exact-saved-plan",
                "workspace-plan.json": "{}",
                "workspace-authorization.proposed.json": "{}",
                "workspace-inventory.json": '{"destructive_addresses":[]}',
                "workspace-estimate.json": '{"bounded_monthly_usd":1}',
                "workspace-backend.json": "{}",
            }.items():
                (output / name).write_text(content)
        if argv[:2] == ["terraform", "output"]:
            return canonical(
                {
                    key: {"value": value}
                    for key, value in {
                        "account_id": "000000000003"
                        if self.wrong_account
                        else "000000000002",
                        "aws_region": "us-west-2",
                        "org_id": "org-1",
                        "workspace_id": "ws-1",
                    }.items()
                }
            )
        return ""


@pytest.mark.parametrize("mode", ["supplied", "invalid", None])
def test_unsupported_network_refused_before_copy_or_process(tmp_path, mode):
    config = runtime_config()
    config["workspace_variables"]["networking_mode"] = mode
    process = Process(tmp_path / "never-created")
    with pytest.raises(LifecycleRefused, match="requires owned networking"):
        prepare(None, None, config, None, "000000000002", process)
    assert process.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    source = tmp_path / "maintained"
    source.mkdir()
    (source / "main.tf").write_text("# maintained module")
    (source / ".terraform.lock.hcl").write_text("# maintained provider lock")
    (source / "scripts").mkdir()
    for name in ("prepare_workspace_plan.py", "apply_workspace_plan.py"):
        (source / "scripts" / name).write_text(
            "# reviewed source; transport is doubled"
        )
    monkeypatch.setattr(
        "workspace_provisioning.terraform.workspace_source", lambda: source
    )
    root = tmp_path / "worker-state"
    original = operation_directory(root, "org-1", "ws-1", "prepare-op", create=True)
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id="org-1", workspace_id="ws-1", operation_id="prepare-op"
            )
        ),
        request=SimpleNamespace(
            parameters={
                "workspace_name": "fixture",
                "lifecycle_allocation_max_cost_micros": "3000000",
            }
        ),
        max_runtime_seconds=900,
    )
    request = SimpleNamespace(
        region="us-west-2",
        vpc_cidr="10.64.0.0/16",
        availability_zones=("us-west-2a", "us-west-2b"),
        cluster_version="1.31",
        node_instance_type="m6i.large",
    )
    context = SimpleNamespace(state_root=root)
    config = runtime_config()
    process = Process(original)
    target, metadata = prepare(
        operation, context, config, request, "000000000002", process
    )
    row = {
        "org_id": "org-1",
        "workspace_id": "ws-1",
        "source_operation_id": "prepare-op",
        "artifact_id": "a" * 64,
        "target_json": canonical(target),
        "artifact_metadata_json": canonical(metadata),
    }
    operation.grant.lease.operation_id = "apply-op"
    applying = Process(
        operation_directory(root, "org-1", "ws-1", "apply-op", create=True)
    )
    return operation, context, config, row, applying, original, source


def test_apply_consumes_saved_review_and_retains_original_allocation_lineage(reviewed):
    operation, context, config, row, process, original, _ = reviewed
    target, metadata = apply(operation, context, config, row, process)
    assert len(process.calls) == 2
    assert process.calls[0][1].endswith("apply_workspace_plan.py")
    assert str(original / "review/workspace.tfplan") in process.calls[0]
    assert not any(
        "prepare_workspace_plan.py" in " ".join(call) or "plan" in call
        for call in process.calls
    )
    assert target["account_id"] == "000000000002"
    assert metadata["allocation_source_operation_id"] == "apply-op"
    assert metadata["source_artifact_id"] == row["artifact_id"]


@pytest.mark.parametrize(
    "file",
    [
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-backend.json",
    ],
)
def test_changed_reviewed_artifact_cannot_reach_apply(reviewed, file):
    operation, context, config, row, process, original, _ = reviewed
    path = original / "review" / file
    path.chmod(0o600)
    path.write_text("changed")
    with pytest.raises(LifecycleRefused, match="bytes changed"):
        apply(operation, context, config, row, process)
    assert process.calls == []


@pytest.mark.parametrize(
    "change",
    ["copied-source", "current-image-source", "injected-tfvars", "missing-file"],
)
def test_source_change_or_incomplete_inventory_cannot_reach_apply(reviewed, change):
    operation, context, config, row, process, original, source = reviewed
    if change == "copied-source":
        (original / "module/main.tf").write_text("different resources")
    elif change == "current-image-source":
        (source / "main.tf").write_text("different image recipe")
    elif change == "injected-tfvars":
        (original / "module/override.auto.tfvars").write_text(
            'account_id="000000000003"'
        )
    else:
        metadata = json.loads(row["artifact_metadata_json"])
        metadata["files"].pop("workspace.tfplan")
        row["artifact_metadata_json"] = canonical(metadata)
    with pytest.raises(LifecycleRefused):
        apply(operation, context, config, row, process)
    assert process.calls == []


def test_successful_apply_with_wrong_workspace_outputs_does_not_offer_bootstrap(
    reviewed,
):
    operation, context, config, row, process, _, _ = reviewed
    process.wrong_account = True
    with pytest.raises(LifecycleRefused, match="another workspace"):
        apply(operation, context, config, row, process)


def test_fsgroup_parent_ownership_does_not_require_chown_of_shared_pvc(
    tmp_path, monkeypatch
):
    mount = tmp_path / "mounted-persistence"
    mount.mkdir(mode=0o770)
    original = Path.stat

    def root_owned(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == mount:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o2770, st_uid=0)
        return info

    monkeypatch.setattr(Path, "stat", root_owned)
    result = operation_directory(mount, "org", "workspace", "operation", create=True)
    assert result.parent.name == "superplane-worker-" + str(os.getuid())
    assert result.parent.stat().st_uid == os.getuid()
    assert result.parent.stat().st_mode & 0o077 == 0
    assert result == operation_directory(mount, "org", "workspace", "operation")
