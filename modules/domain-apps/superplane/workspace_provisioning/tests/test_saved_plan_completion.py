# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""The completion path preserves original evidence and cannot silently replan."""

import json
import sys
from types import SimpleNamespace

import pytest

from workspace_provisioning.artifacts import digest
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.saved_plan_completion import (
    finalize_files,
    stage_saved_plan,
)
from workspace_provisioning.saved_plan_validation import (
    SAVED_PLAN_FILES,
    saved_plan_snapshot,
)
from workspace_provisioning.terraform import sha

from .test_saved_plan_validation import saved_preparation  # noqa: F401
from .test_planning_boundary import preparation, incomplete_init  # noqa: F401


def receipt(directory):
    entries = saved_plan_snapshot(directory)
    return {
        "recovery_scope": "saved-plan-completion",
        "reauthorization_id": "completion-decision",
        "proof": json.dumps(
            {
                "evidence_kind": "saved-plan-finalization-v1",
                "planning_started": True,
                "apply_started": False,
                "preparation_boundary": {
                    "kind": "saved-plan-finalization-v1",
                    "entries": entries,
                    "sha256": digest(entries),
                    "observation": {
                        "offline_guard_passed": True,
                        "files": {
                            name: sha(directory / "review" / name)
                            for name in SAVED_PLAN_FILES
                        },
                    },
                },
            }
        ),
    }


def test_original_preserved_and_new_stage_has_no_credentials(saved_preparation):  # noqa: F811
    directory, *_ = saved_preparation
    original = saved_plan_snapshot(directory)
    # Kubernetes fsGroup may restore group bits when the PVC is mounted.
    directory.parent.chmod(0o770)
    directory.chmod(0o770)
    reviewed = receipt(directory)
    staged = stage_saved_plan(
        directory.parent.parent, ("org", "workspace", "operation"), reviewed
    )
    (archive,) = directory.parent.glob(directory.name + ".completed-from-*")
    assert saved_plan_snapshot(archive) == original
    assert sha(staged / "review/workspace.tfplan") == sha(
        archive / "review/workspace.tfplan"
    )
    assert not (staged / ".aws").exists()
    assert not (staged / ".terraform.d").exists()
    assert not (staged / "review/workspace-backend.json").exists()
    with pytest.raises(LifecycleRefused):
        stage_saved_plan(
            directory.parent.parent, ("org", "workspace", "operation"), reviewed
        )
    assert saved_plan_snapshot(archive) == original


def test_insufficient_space_preserves_original_directory(
    saved_preparation, monkeypatch
):
    from workspace_provisioning import saved_plan_completion as completion

    directory, *_ = saved_preparation
    original = saved_plan_snapshot(directory)
    monkeypatch.setattr(
        completion.shutil, "disk_usage", lambda _: SimpleNamespace(free=0)
    )
    with pytest.raises(LifecycleRefused, match="insufficient space"):
        stage_saved_plan(
            directory.parent.parent,
            ("org", "workspace", "operation"),
            receipt(directory),
        )
    assert saved_plan_snapshot(directory) == original
    assert not list(directory.parent.glob("*.completed-from-*"))


@pytest.mark.parametrize(
    "change", ["plan", "source", "scope", "extension", "already-applied", "evidence"]
)
def test_stage_refuses_before_moving_original(saved_preparation, change):  # noqa: F811
    directory, *_ = saved_preparation
    reviewed = receipt(directory)
    proof = json.loads(reviewed["proof"])
    if change == "plan":
        (directory / "review/workspace.tfplan").write_text("changed")
    elif change == "source":
        (directory / "module/main.tf").write_text("changed")
    elif change == "scope":
        reviewed["recovery_scope"] = "unstarted-planning"
    elif change == "extension":
        proof["attempt_extension"] = {"additional_attempts": 1}
    elif change == "already-applied":
        proof["apply_started"] = True
    else:
        proof["preparation_boundary"]["observation"]["offline_guard_passed"] = False
    reviewed["proof"] = json.dumps(proof)
    with pytest.raises(LifecycleRefused):
        stage_saved_plan(
            directory.parent.parent, ("org", "workspace", "operation"), reviewed
        )
    assert directory.is_dir()
    assert not list(directory.parent.glob("*.completed-from-*"))


@pytest.mark.parametrize("failed_check", [None, "account", "kms", "principal"])
def test_final_receipts_exist_only_after_all_current_preflights(
    saved_preparation, monkeypatch, failed_check
):  # noqa: F811
    from workspace_provisioning import saved_plan_completion as completion

    directory, *_ = saved_preparation
    review = directory / "review"
    (review / "workspace-plan.json").write_text("{}")
    (review / "workspace-authorization.proposed.json").write_text(
        '{"original":"authorization"}'
    )
    plan_hash = sha(review / "workspace.tfplan")
    before = {name: (review / name).read_bytes() for name in SAVED_PLAN_FILES}
    monkeypatch.setattr(
        completion,
        "inspect_saved_plan",
        lambda *a, **kw: {"target": {"account_id": "111122223333"}},
    )
    calls = []

    def checked(name):
        def read(*args):
            calls.append(name)
            if failed_check == name:
                raise LifecycleRefused("current preflight refused")
            return {"verified": name}

        return read

    monkeypatch.setitem(
        sys.modules,
        "workspace_backend",
        SimpleNamespace(verify_backend=checked("backend")),
    )
    monkeypatch.setitem(
        sys.modules,
        "workspace_kms",
        SimpleNamespace(
            verify_account_prerequisites=checked("account"),
            verify_supplied_key=checked("kms"),
            verify_provisioning_principal=checked("principal"),
        ),
    )
    old_path = list(sys.path)
    try:
        if failed_check:
            with pytest.raises(LifecycleRefused):
                finalize_files(
                    directory, terraform_binary="terraform", plan_sha256=plan_hash
                )
            assert not (review / "workspace-backend.json").exists()
            assert {
                name: (review / name).read_bytes() for name in SAVED_PLAN_FILES
            } == before
        else:
            finalize_files(
                directory, terraform_binary="terraform", plan_sha256=plan_hash
            )
            assert calls == ["backend", "account", "kms", "principal"]
            document = json.loads(
                (review / "workspace-authorization.proposed.json").read_text()
            )
            assert document["original"] == "authorization"
            assert document["provisioning_principal"] == {"verified": "principal"}
            assert (review / "workspace-backend.json").exists()
            assert sha(review / "workspace.tfplan") == plan_hash
    finally:
        sys.path[:] = old_path


@pytest.mark.parametrize(
    "change", [None, "terraform", "derived", "replacement", "module-digest"]
)
def test_pricing_completion_preserves_three_originals_and_checks_owner_evidence(
    saved_preparation, monkeypatch, change
):
    from workspace_provisioning import pricing_plan_validation as pricing
    from workspace_provisioning.terraform import maintained_digest, source_digest

    directory, *_ = saved_preparation
    corrected_digest = maintained_digest()
    (directory / "module/regional-pricing.json").unlink()
    guard = directory / "module/scripts/check_workspace_plan.py"
    guard.write_text("historical guard")
    monkeypatch.setattr(pricing, "OLD_GUARD_SHA256", sha(guard))
    reviewed = receipt(directory)
    derived = {
        name: (directory / "review" / name).read_bytes()
        for name in pricing.DERIVED_FILES
    }
    for name in pricing.DERIVED_FILES:
        (directory / "review" / name).unlink()
    proof = json.loads(reviewed["proof"])
    boundary = proof["preparation_boundary"]
    boundary["entries"] = saved_plan_snapshot(directory)
    boundary["sha256"] = digest(boundary["entries"])
    observation = boundary["observation"]
    observation["pricing_revalidation"] = {
        "replacements": {
            name: sha(pricing.workspace_source() / name)
            for name in pricing.REPLACEMENTS
        },
        "original_module_sha256": source_digest(directory / "module"),
        "corrected_module_sha256": corrected_digest,
    }
    import copy

    reproduced = copy.deepcopy(observation)
    monkeypatch.setattr(pricing, "reproduce_pricing", lambda *_: (reproduced, derived))
    if change == "terraform":
        (directory / "module/main.tf").write_text("changed")
    elif change == "derived":
        reproduced["files"]["workspace-estimate.json"] = "e" * 64
    elif change == "replacement":
        observation["pricing_revalidation"]["replacements"]["regional-pricing.json"] = (
            "e" * 64
        )
    elif change == "module-digest":
        observation["pricing_revalidation"]["corrected_module_sha256"] = "e" * 64
    reviewed["proof"] = json.dumps(proof)
    if change:
        with pytest.raises(LifecycleRefused):
            stage_saved_plan(
                directory.parent.parent, ("org", "workspace", "operation"), reviewed
            )
        assert directory.is_dir()
        assert not list(directory.parent.glob("*.completed-from-*"))
        return
    staged = stage_saved_plan(
        directory.parent.parent, ("org", "workspace", "operation"), reviewed
    )
    (archive,) = directory.parent.glob(directory.name + ".completed-from-*")
    assert saved_plan_snapshot(archive) == boundary["entries"]
    assert {p.name for p in (archive / "review").iterdir()} == pricing.ORIGINAL_FILES
    assert {p.name for p in (staged / "review").iterdir()} == SAVED_PLAN_FILES
    assert source_digest(staged / "module") == corrected_digest
    assert not (staged / ".aws").exists()
    for name in pricing.ORIGINAL_FILES:
        assert (staged / "review" / name).read_bytes() == (
            archive / "review" / name
        ).read_bytes()
