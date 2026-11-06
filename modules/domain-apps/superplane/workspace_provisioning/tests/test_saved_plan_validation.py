"""A retained plan is evidence only; mutations and fabricated reports refuse."""

import copy
import json
from pathlib import Path

import pytest

from workspace_provisioning.saved_plan_validation import (
    OPAQUE_CREDENTIAL_FILES,
    SAVED_PLAN_FILES,
    initial_plan_only,
    saved_plan_snapshot,
    verify_saved_entries,
)
from workspace_provisioning.terraform import workspace_source
from .test_planning_boundary import incomplete_init, preparation  # noqa: F401


@pytest.fixture
def saved_preparation(incomplete_init):  # noqa: F811
    _, _, directory, target, backend, documents = incomplete_init
    package = (
        directory
        / "module/.terraform/providers/registry.terraform.io/hashicorp/aws/6.65.0/linux_amd64"
    )
    package.mkdir(parents=True)
    (package / "LICENSE.txt").write_text("license")
    (package / "terraform-provider-aws_v6.65.0_x5").write_bytes(b"test provider")
    for name in SAVED_PLAN_FILES:
        (directory / "review" / name).write_text("retained " + name)
    return directory, target, backend, documents


def test_saved_files_are_bound_and_credentials_are_never_opened(
    saved_preparation, monkeypatch
):
    directory, target, backend, documents = saved_preparation
    original = Path.open

    def opened(path, *args, **kwargs):
        assert str(path.relative_to(directory)) not in OPAQUE_CREDENTIAL_FILES
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opened)
        entries = saved_plan_snapshot(directory)
    assert entries[".aws/cli/cache/session.db"]["kind"] == "opaque-credential-cache"
    first = verify_saved_entries(
        entries, workspace_source(), documents, target=target, backend=backend
    )
    (directory / "review/workspace.tfplan").write_text("different artifact")
    second = verify_saved_entries(
        saved_plan_snapshot(directory),
        workspace_source(),
        documents,
        target=target,
        backend=backend,
    )
    assert first != second


@pytest.mark.parametrize(
    "extra",
    [
        "review/workspace-backend.json",
        "module/terraform.tfstate",
        "applied-inventory.json",
        "review/extra",
        ".aws/credentials",
    ],
)
def test_unknown_or_completed_outputs_refuse(saved_preparation, extra):
    directory, target, backend, documents = saved_preparation
    path = directory / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must not accept")
    with pytest.raises(ValueError):
        verify_saved_entries(
            saved_plan_snapshot(directory),
            workspace_source(),
            documents,
            target=target,
            backend=backend,
        )


def test_missing_plan_and_symlink_refuse(saved_preparation):
    directory, target, backend, documents = saved_preparation
    plan = directory / "review/workspace.tfplan"
    plan.unlink()
    with pytest.raises(ValueError):
        verify_saved_entries(
            saved_plan_snapshot(directory),
            workspace_source(),
            documents,
            target=target,
            backend=backend,
        )
    plan.symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        saved_plan_snapshot(directory)


@pytest.fixture
def initial():
    return {
        "terraform_version": "1.9.8",
        "applyable": True,
        "complete": True,
        "errored": False,
        "resource_changes": [
            {"mode": "managed", "change": {"actions": ["create"], "before": None}}
        ],
        "prior_state": {"values": {"root_module": {"resources": [{"mode": "data"}]}}},
    }


def test_initial_create_and_deferred_read(initial):
    initial["resource_changes"].append(
        {"mode": "data", "change": {"actions": ["read"], "before": None}}
    )
    initial_plan_only(initial)


@pytest.mark.parametrize(
    "change",
    [
        "update",
        "replace",
        "delete",
        "import",
        "move",
        "prior",
        "nested-prior",
        "errored",
        "incomplete",
        "not-applyable",
    ],
)
def test_not_a_generic_failed_plan_recovery(initial, change):
    plan = copy.deepcopy(initial)
    resource = plan["resource_changes"][0]
    if change in {"update", "delete", "replace"}:
        resource["change"]["actions"] = (
            [change] if change != "replace" else ["delete", "create"]
        )
    elif change == "import":
        resource["change"]["importing"] = {"id": "existing"}
    elif change == "move":
        resource["previous_address"] = "existing"
    elif change == "prior":
        plan["prior_state"]["values"]["root_module"]["resources"] = [
            {"mode": "managed"}
        ]
    elif change == "nested-prior":
        plan["prior_state"]["values"]["root_module"]["child_modules"] = [
            {"resources": [{"mode": "managed"}]}
        ]
    elif change == "errored":
        plan["errored"] = True
    else:
        plan["complete" if change == "incomplete" else "applyable"] = False
    with pytest.raises(ValueError):
        initial_plan_only(json.loads(json.dumps(plan)))
