"""Actual local Terraform plan creation under umask022; no AWS provider or network."""

import shutil
import subprocess

import pytest

from installation.config import Refusal
from installation.runtime_preparation import binary_plan_digest, create_saved_plan


def test_real_terraform_plan_becomes_private_before_hashing(tmp_path):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip(
            "offline Terraform binary is required for saved-plan permission regression"
        )
    (tmp_path / "main.tf").write_text(
        'resource "terraform_data" "fixture" { input = "offline-plan-permissions" }\n'
    )
    subprocess.run(
        [terraform, "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    path = tmp_path / "installation.tfplan"
    observed = []

    def create():
        subprocess.run(
            [
                terraform,
                "plan",
                "-input=false",
                "-no-color",
                "-out=installation.tfplan",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            timeout=30,
            umask=0o022,
        )
        observed.append(path.stat().st_mode & 0o777)

    create_saved_plan(path, create)
    assert observed == [0o644]  # Real Terraform behavior that the fake originally hid.
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(binary_plan_digest(path)) == 64
    original = path.read_bytes()
    with pytest.raises(Refusal, match="already exists"):
        create_saved_plan(path, lambda: pytest.fail("reviewed plan overwritten"))
    assert path.read_bytes() == original


def test_plan_creation_refuses_links_and_never_changes_target_permissions(tmp_path):
    original = tmp_path / "unowned"
    original.write_bytes(b"unreviewed")
    original.chmod(0o644)
    plan = tmp_path / "installation.tfplan"
    with pytest.raises(Refusal, match="linked"):
        create_saved_plan(plan, lambda: plan.symlink_to(original))
    assert original.stat().st_mode & 0o777 == 0o644
    with pytest.raises(Refusal, match="already exists"):
        create_saved_plan(plan, lambda: pytest.fail("linked plan reused"))
