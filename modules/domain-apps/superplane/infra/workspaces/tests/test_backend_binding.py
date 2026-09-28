import json
import subprocess

import pytest

from backend_fixtures import backend_config, initialize, saved_plan
from test_apply_workspace_plan import _apply_fixture
from workspace_backend import verify_backend
from workspace_ownership import WorkspaceOwnershipError


TARGET = {
    "org_id": "test-org",
    "workspace_id": "alpha",
    "environment": "dev",
    "workspace_name": "alpha",
    "account_id": "111122223333",
}
PLAN = {"terraform_version": "1.9.8"}


def test_actual_backend_encoding_is_verified(tmp_path):
    config = backend_config(TARGET)
    initialize(tmp_path, config)
    saved_plan(tmp_path / "plan", PLAN, config)
    assert (
        verify_backend(tmp_path / "plan", PLAN, tmp_path, TARGET)["key"]
        == config["key"]
    )


@pytest.mark.parametrize(
    "problem",
    [
        "workspace-key",
        "environment-key",
        "bucket",
        "missing-init",
        "local-backend",
        "named-workspace",
        "backend-secret",
        "endpoint",
        "version",
    ],
)
def test_backend_mismatch_or_unsupported_identity_denies(tmp_path, problem):
    config = backend_config(TARGET)
    initialize(tmp_path, config)
    planned = dict(config)
    workspace, kind = "default", "s3"
    plan = dict(PLAN)
    if problem == "workspace-key":
        planned["key"] = planned["key"].replace("alpha", "other")
    elif problem == "environment-key":
        planned["key"] = planned["key"].replace("dev/", "prod/")
    elif problem == "bucket":
        planned["bucket"] = "different-state"
    elif problem == "missing-init":
        (tmp_path / ".terraform/terraform.tfstate").unlink()
    elif problem == "local-backend":
        kind = "local"
    elif problem == "named-workspace":
        workspace = "other"
    elif problem == "backend-secret":
        planned["access_key"] = "synthetic-disallowed-key"
    elif problem == "endpoint":
        planned["endpoint"] = "https://unrelated.invalid"
    else:
        plan["terraform_version"] = "1.15.3"
    saved_plan(tmp_path / "plan", plan, planned, workspace=workspace, kind=kind)
    with pytest.raises(WorkspaceOwnershipError):
        verify_backend(tmp_path / "plan", plan, tmp_path, TARGET)


@pytest.mark.parametrize(
    "problem",
    ["missing-authorized-backend", "wrong-authorized-backend", "reused-module"],
)
def test_apply_cannot_bypass_backend_binding(tmp_path, problem):
    command, _, auth, record = _apply_fixture(tmp_path)
    document = json.loads(auth.read_text())
    if problem == "missing-authorized-backend":
        del document["backend"]
    elif problem == "wrong-authorized-backend":
        document["backend"]["bucket"] = "wrong-state"
    else:
        path = tmp_path / ".terraform/terraform.tfstate"
        data = json.loads(path.read_text())
        data["backend"]["config"]["key"] = (
            "prod/modules/superplane-workspaces/other/terraform.tfstate"
        )
        path.write_text(json.dumps(data))
    auth.write_text(json.dumps(document))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert not record.exists()
