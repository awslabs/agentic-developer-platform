import json

import pytest
import yaml

from installation.config import Refusal, digest
from installation.operation_database import (
    COMPONENT,
    OperationInstaller,
    execute,
    main,
    preparation_plan,
)


@pytest.fixture
def inputs(environment, release):
    environment["image_execution"] = "cluster"
    environment["control_plane_only"] = True
    environment["secrets"].pop("workspace_access")
    release["images"][COMPONENT] = "sha256:" + "9" * 64
    release["image_sources"][COMPONENT] = {
        "registry": f"{environment['account_id']}.dkr.ecr.us-east-1.amazonaws.com",
        "repository": "adp-" + COMPONENT,
        "source_revision": release["source_revision"],
    }
    return environment, release


@pytest.mark.parametrize(
    "schema", ["public", "superplane", "skypilot", "pg_catalog", "x;drop table y"]
)
def test_shared_schema_cannot_overlap_domain_or_system(inputs, tmp_path, schema):
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    with pytest.raises(Refusal, match="distinct dedicated"):
        preparation_plan(installer, schema)


def test_approval_refused_before_any_live_action(inputs, tmp_path, monkeypatch):
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    plan = preparation_plan(installer, "superplane_operations")
    monkeypatch.setattr(
        installer, "target", lambda: pytest.fail("live action before approval")
    )
    with pytest.raises(Refusal, match="exact saved"):
        execute(installer, plan, "0" * 64, "private-admin-url")
    with pytest.raises(Refusal, match="SUPERPLANE_DATABASE_ADMIN_URL"):
        execute(installer, plan, digest(plan), "")


def test_offline_plan_replay_requires_same_mode_identity_and_schema(
    inputs, tmp_path, capsys
):
    environment, release = inputs
    env_path, lock_path = tmp_path / "environment.yaml", tmp_path / "release.yaml"
    env_path.write_text(yaml.safe_dump(environment))
    lock_path.write_text(yaml.safe_dump(release))
    output = tmp_path / "plan"
    args = [
        "--environment",
        str(env_path),
        "--release-lock",
        str(lock_path),
        "--output",
        str(output),
        "--schema",
        "superplane_operations",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    receipt = json.loads((output / "receipt.json").read_text())
    assert result["plan_sha256"] == digest(receipt["operation_database_plan"])
    assert (output / "receipt.json").stat().st_mode & 0o777 == 0o600
    assert main(args) == 2  # Same path never silently replaces a receipt.
    assert main([*args, "--resume"]) == 0
    assert main([*args[:-1], "other_operations", "--resume"]) == 2
    receipt["mode"] = "control-plane-only"
    (output / "receipt.json").write_text(json.dumps(receipt))
    assert main([*args, "--resume"]) == 2


def test_missing_or_unreviewed_paid_image_refuses(inputs, tmp_path):
    inputs[1]["image_sources"][COMPONENT]["source_revision"] = "b" * 40
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    with pytest.raises(Refusal, match="reviewed paid-worker"):
        preparation_plan(installer, "superplane_operations")
