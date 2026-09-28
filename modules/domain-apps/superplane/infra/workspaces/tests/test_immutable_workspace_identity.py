"""Equal display names must never confer ownership or backend access across tenants."""

import copy
import json

import pytest

from backend_fixtures import backend_config, initialize, saved_plan
from test_plan_safety import (
    ACCOUNT,
    ENVIRONMENT,
    ORG_ID,
    WORKSPACE,
    _cluster,
    _plan,
    _run,
    _vpc,
    _write,
)
from workspace_backend import verify_backend
from workspace_identity import infrastructure_id, state_key
from workspace_ownership import WorkspaceOwnershipError, validate_plan


@pytest.mark.parametrize("field", ["org_id", "workspace_id"])
def test_same_name_cannot_authorize_another_identity_resources_or_state(
    tmp_path, field
):
    target = {
        "account_id": ACCOUNT,
        "environment": ENVIRONMENT,
        "workspace_name": WORKSPACE,
        "org_id": ORG_ID,
        "workspace_id": WORKSPACE,
    }
    other = dict(target, **{field: "another-immutable-id"})
    assert infrastructure_id(
        target["org_id"], target["workspace_id"]
    ) != infrastructure_id(other["org_id"], other["workspace_id"])
    assert state_key(target) != state_key(other)
    plan = _plan(_cluster(["delete"]), _vpc(["delete"]))
    flags = {k: v for k, v in target.items() if k != "account_id"}
    assert validate_plan(plan, account_id=ACCOUNT, **flags).ok
    flags[field] = other[field]
    report = validate_plan(plan, account_id=ACCOUNT, **flags)
    assert len(report.violations) >= 2
    config = backend_config(target)
    initialize(tmp_path, config)
    saved_plan(tmp_path / "saved", plan, config)
    with pytest.raises(WorkspaceOwnershipError, match="Backend key"):
        verify_backend(tmp_path / "saved", plan, tmp_path, other)
    result = _run(_write(tmp_path, plan), "--" + field.replace("_", "-"), other[field])
    assert result.returncode != 0
    assert "ownership" in result.stdout.lower() + result.stderr.lower()


@pytest.mark.parametrize("field", ["org_id", "workspace_id"])
def test_guard_authorization_binds_immutable_id_even_without_changes(tmp_path, field):
    plan = _plan()
    auth = tmp_path / "authorization.json"
    path = _write(tmp_path, plan)
    result = _run(path, "--emit-authorization", str(auth))
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(auth.read_text())
    assert document[field] == plan["variables"][field]["value"]
    document[field] = "another-immutable-id"
    auth.write_text(json.dumps(document))
    result = _run(path, "--authorize-destroy", str(auth))
    assert result.returncode != 0 and field in result.stdout + result.stderr


def test_display_rename_preserves_infrastructure_and_attribution():
    plan = _plan(_cluster(["delete"]), _vpc(["delete"]))
    for change in plan["resource_changes"]:
        tags = change["change"]["before"].get("tags_all")
        if tags:
            tags["Workspace"] = "previous-display-name"
    assert validate_plan(
        plan,
        environment=ENVIRONMENT,
        workspace_name="renamed-display",
        org_id=ORG_ID,
        workspace_id=WORKSPACE,
        account_id=ACCOUNT,
    ).ok


@pytest.mark.parametrize("tag", ["OrgId", "WorkspaceId"])
def test_replacement_cannot_borrow_new_identity_for_unattributed_old_resource(tag):
    plan = _plan(_vpc(["delete", "create"]))
    detail = plan["resource_changes"][0]["change"]
    detail["before"] = copy.deepcopy(detail["before"])
    del detail["before"]["tags_all"][tag]
    report = validate_plan(
        plan,
        environment=ENVIRONMENT,
        workspace_name=WORKSPACE,
        org_id=ORG_ID,
        workspace_id=WORKSPACE,
        account_id=ACCOUNT,
    )
    assert not report.ok
    assert any(tag in v.reason and "before" in v.reason for v in report.violations)


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_authorization_requires_fresh_identity_bound_plan(tmp_path, version):
    path = _write(tmp_path, _plan())
    auth = tmp_path / "authorization.json"
    assert _run(path, "--emit-authorization", str(auth)).returncode == 0
    document = json.loads(auth.read_text())
    document["schema_version"] = version
    auth.write_text(json.dumps(document))
    result = _run(path, "--authorize-destroy", str(auth))
    assert result.returncode != 0
    assert "requires 3" in result.stdout + result.stderr


def test_every_documented_apply_command_supplies_required_tenant_identity():
    import re
    import shlex
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    examples = [
        block
        for block in re.findall(r"```(?:bash|sh)\n(.*?)```", readme.read_text(), re.S)
        if "python3 scripts/apply_workspace_plan.py" in block
    ]
    assert len(examples) >= 2
    for block in examples:
        tokens = shlex.split(block.replace("\\\n", " "), comments=True)
        assert "--org-id" in tokens and "--workspace-id" in tokens
