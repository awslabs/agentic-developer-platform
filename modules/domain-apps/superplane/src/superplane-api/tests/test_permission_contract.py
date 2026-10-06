"""Versioned route and grant contract for #6484; no external services required."""

import ast
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.auth import load_workspace_authorization
from app.endpoint_inventory import (
    DOMAIN_ROUTES,
    PRIVATE_DOMAIN_ROUTES,
    Scope,
    mounted_operations,
)
from app.main import app
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from superplane_auth.policy import (
    AuthorizationDeniedError,
    DomainPrincipal,
    Permission,
    expand_permissions,
    permissions_for_adp_role,
)
from tests.conftest import async_session_test

DOMAIN = Path(__file__).resolve().parents[3]
CONTRACTS = DOMAIN / "contracts"
REPO = DOMAIN.parents[2]


def _contract(name):
    return json.loads((CONTRACTS / name).read_text())


MATRIX = _contract("action-permissions-v1.json")
ACCESS = _contract("access-cases-v1.json")
CASES = [dict(zip(ACCESS["columns"], values, strict=True)) for values in ACCESS["cases"]]


def test_matrix_matches_real_mounted_domain_routes():
    assert MATRIX["version"] == 1
    mounted = mounted_operations(app)
    assert len(mounted) > 50
    expected = DOMAIN_ROUTES | PRIVATE_DOMAIN_ROUTES
    assert set(expected) <= mounted
    actual = {}
    identifiers = set()
    for action in MATRIX["actions"]:
        assert action["id"] not in identifiers
        identifiers.add(action["id"])
        assert action["principal"] in {"human", "service", "human_or_service"}
        assert isinstance(action["extra"], list)
        assert action["surface"] == "absent" or action["surface"].startswith(("cli:", "ui:", "tool:"))
        if not action["routes"]:
            assert action["surface"] == "absent"
            assert action["scope"] in {"workspace", "cluster"}
        for route in action["routes"]:
            method, path = route.split(" ", 1)
            key = (method, path)
            assert key not in actual, f"duplicate matrix route: {key}"
            actual[key] = (action["scope"], action["permission"])
            assert Permission(action["permission"]) in Permission
            expected_scope = Scope.WORKSPACE if "{workspace_id}" in path else Scope.ORGANIZATION
            assert action["scope"] == expected_scope
    assert len(actual) > 80
    assert set(actual) == set(expected), f"missing={set(expected) - set(actual)}, stale={set(actual) - set(expected)}"
    for key, (scope, permission) in expected.items():
        assert actual[key] == (scope.value, permission.value)


@pytest.mark.parametrize("granted", list(Permission))
@pytest.mark.parametrize("requested", list(Permission))
def test_all_permission_implications_and_non_implications(granted, requested):
    expected = requested == granted or requested is Permission.READ or granted is Permission.ADMINISTER
    assert (requested in expand_permissions({granted})) is expected
    assert not expand_permissions({"unknown-role-or-permission"}) & set(Permission)


def test_sensitive_actions_have_independent_requirements():
    actions = {action["id"]: action for action in MATRIX["actions"]}
    assert actions["serving.delete"]["permission"] == Permission.SPEND.value
    assert actions["serving.cancel"]["permission"] == Permission.PROVISION.value
    assert actions["batch.delete"]["permission"] == Permission.SPEND.value
    assert actions["batch.cancel"]["permission"] == Permission.PROVISION.value
    assert actions["workspace.kubeconfig"]["permission"] == Permission.PROVISION.value
    assert all(actions[name]["permission"] == Permission.RENEW_CREDENTIAL.value for name in (
        "provider_connection.create", "provider_connection.validate", "provider_connection.rotate", "provider_connection.revoke"
    ))
    assert actions["approval.decision"]["principal"] == "human"
    assert "selected_distinct_current_human_approver" in actions["approval.decision"]["extra"]
    assert actions["workspace.access.manage"]["routes"] == [
        "POST /workspaces/{workspace_id}/access/v1/grants"
    ]
    assert actions["cluster.use"]["scope"] != actions["workspace.read"]["scope"]
    # Do not turn first-install bootstrap or machine transport restrictions into
    # blanket action permissions for subsequent workspaces or cluster observers.
    assert "first_workspace_current_org_admin_bootstrap_only" not in actions["workspace.create"]["extra"]
    assert "organization_scoped_provisioning_authority" in actions["workspace.create"]["extra"]
    for name in ("cluster.use", "cluster.administer", "cluster.observe"):
        assert actions[name]["principal"] == "human_or_service"
    assert MATRIX["verification"]["principal_and_extra"] == "declared_requirements_not_full_enforcement_evidence"
    assert "service_run_delegation_if_service" in actions["batch.submit"]["extra"]
    assert "service_run_delegation_if_service" in actions["serving.submit"]["extra"]


def test_maintained_ui_action_names_and_routes_are_real():
    ui_contract = (DOMAIN / "ui/contract.ts").read_text()
    ui_actions = {
        match.group(1): f"{match.group(2)} {match.group(3)}"
        for match in re.finditer(
            r"(?m)^  (\w+): \{\s*method: '(\w+)',\s*path: '([^']+)'",
            ui_contract,
        )
    }
    assert len(ui_actions) > 30
    mapped = {}
    for action in MATRIX["actions"]:
        if action["surface"].startswith("ui:"):
            for name in action["surface"].removeprefix("ui:").split(","):
                assert name not in mapped
                assert ui_actions[name] in action["routes"]
                mapped[name] = ui_actions[name]
    assert mapped == ui_actions


def test_platform_roles_are_not_workspace_presets():
    source = (REPO / "modules/gateway/src/admin/config.py").read_text()
    tree = ast.parse(source)
    admin_role = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AdminRole")
    names = {node.value.value for node in admin_role.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)}
    assert {"platform_admin", "org_admin", "dept_admin", "member"} <= names
    assert not {"workspace_viewer", "workspace_operator", "workspace_provisioner", "workspace_owner"} & names
    for role in ("platform_admin", "org_admin", "dept_admin", "member", "service", "unknown_role"):
        assert permissions_for_adp_role(role) == frozenset()
    assert set(permissions_for_adp_role("workspace_viewer")) == {Permission.READ}
    assert "membership_role_to_admin_role" in source
    assert "account_type == \"service\"" in (REPO / "modules/gateway/src/auth/dependencies.py").read_text()
    assert "memberships_for_login" in (REPO / "modules/gateway/src/auth/workspaces.py").read_text()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
@pytest.mark.asyncio
async def test_named_tenant_and_principal_grant_cases(case):
    assert ACCESS["version"] == 1
    org_ids = {name: uuid.uuid4() for name in ("O1", "O2")}
    workspace_ids = {name: uuid.uuid4() for name in ("W1", "W2")}
    workspace_owner = "O2" if case["grant_org"] == "O2" else "O1"
    async with async_session_test() as db:
        for name, org_id in org_ids.items():
            db.add(Organization(id=org_id, name=f"contract-{name}", billing_plan="free"))
        for name, workspace_id in workspace_ids.items():
            owner = workspace_owner if name == case["workspace"] else "O1"
            db.add(Workspace(id=workspace_id, org_id=org_ids[owner], name=f"contract-{name}", isolation_mode="shared", status="active"))
        if case["grant_workspace"] is not None:
            db.add(WorkspaceGrantRecord(
                workspace_id=workspace_ids[case["grant_workspace"]], org_id=org_ids[case["grant_org"]],
                principal=case["subject"], principal_type=case["grant_type"],
                permissions=" ".join(case["permissions"]),
                revoked_at=datetime.now(UTC) if case["revoked"] else None,
            ))
        await db.commit()
        model, stored_org = await load_workspace_authorization(
            db, workspace_ids[case["workspace"]], case["subject"], case["principal_type"]
        )
    principal = DomainPrincipal(subject=case["subject"], org_id=str(org_ids[case["org"]]), client_id="test-client", account_type=case["principal_type"])
    try:
        model.authorize(principal, str(workspace_ids[case["workspace"]]), Permission(case["request"]), workspace_org_id=stored_org)
        allowed = True
    except AuthorizationDeniedError:
        allowed = False
    if not case["active"]:
        assert allowed, "fixture must expose the missing ADP active-membership gate"
        pytest.xfail("#6127: active ADP membership/disabled-principal check not yet consumed by domain ingress")
    assert allowed is case["allow"]
