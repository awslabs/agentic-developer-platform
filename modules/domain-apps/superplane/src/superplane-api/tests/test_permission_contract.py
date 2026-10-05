"""Versioned route and grant contract for #6484; no external services required."""

import ast
import importlib.util
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.auth import VerifiedCaller, authorize_organization_operation, load_workspace_authorization
from app.endpoint_inventory import (
    DOMAIN_ROUTES,
    PRIVATE_DOMAIN_ROUTES,
    Scope,
    mounted_operations,
)
from app.main import app
from app.models.organization import Organization
from app.models.organization_grant import OrganizationGrantRecord, ORGANIZATION_ADMINISTER, ORGANIZATION_READ
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
ORGANIZATION_CASES = [
    dict(zip(ACCESS["organization_columns"], values, strict=True)) for values in ACCESS["organization_cases"]
]


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
        assert all(surface.startswith("cli:") and surface != action["surface"] for surface in action.get("also_surfaces", []))
        assert all(surface.startswith("cli:") and surface not in [action["surface"], *action.get("also_surfaces", [])] for surface in action.get("unavailable_surfaces", []))
        if action["scope"] == "organization":
            assert action["organization_grant"] == (
                ORGANIZATION_READ if action["permission"] == Permission.READ.value else ORGANIZATION_ADMINISTER
            )
        else:
            assert "organization_grant" not in action
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



def test_fixed_permission_vocabulary_and_separate_cluster_scope():
    assert {permission.value for permission in Permission} == {
        "workspace:read", "workspace:spend", "workspace:provision",
        "workspace:renew_credential", "workspace:administer",
    }
    assert {ORGANIZATION_READ, ORGANIZATION_ADMINISTER}.isdisjoint({permission.value for permission in Permission})
    actions = {action["id"]: action for action in MATRIX["actions"]}
    assert all(actions[name]["scope"] == "organization" for name in (
        "workspace.create", "workspace.list", "org.settings", "approval.decision",
    ))
    assert actions["workspace.create"]["organization_grant"] == ORGANIZATION_ADMINISTER
    assert actions["workspace.list"]["organization_grant"] == ORGANIZATION_READ
    for name in ("cluster.use", "cluster.administer", "cluster.observe"):
        assert actions[name]["scope"] == "cluster"
        assert actions[name]["permission"] == name.replace("cluster.", "cluster:")
        assert actions[name]["routes"] == [] and actions[name]["surface"] == "absent"
    assert actions["workspace.list"]["scope"] == "organization"
    assert "cli:cluster list" in actions["workspace.list"]["also_surfaces"]
    assert "first installation grant" in MATRIX["scope_gate"]["bootstrap"]
    assert "revocation denies fallback" in MATRIX["scope_gate"]["organization"]


@pytest.mark.parametrize("case", ORGANIZATION_CASES, ids=lambda case: case["name"])
@pytest.mark.asyncio
async def test_bound_org_and_legacy_scope_grant_cases(case):
    assert ACCESS["version"] == 1
    org_ids = {name: uuid.uuid4() for name in ("O1", "O2")}
    subject = case["subject"]
    workspace_ids = []
    async with async_session_test() as db:
        for name, org_id in org_ids.items():
            db.add(Organization(
                id=org_id, name=f"contract-org-{name}", billing_plan="free",
                adp_org_id=f"selected-{name}" if case["bound"] else None,
            ))
        for index in range(case["workspace_count"]):
            workspace_id = uuid.uuid4()
            workspace_ids.append(workspace_id)
            db.add(Workspace(
                id=workspace_id, org_id=org_ids["O1"], name=f"contract-org-w{index}",
                isolation_mode="shared", status="active",
            ))
        if case["grant_org"]:
            db.add(OrganizationGrantRecord(
                org_id=org_ids[case["grant_org"]], principal=subject,
                principal_type=case["grant_type"], permissions=" ".join(case["org_permissions"]),
                granted_by="fixture", revoked_at=datetime.now(UTC) if case["revoked"] else None,
            ))
        for workspace_id in workspace_ids if case["all_workspaces"] else workspace_ids[:1]:
            if case["workspace_permissions"]:
                db.add(WorkspaceGrantRecord(
                    workspace_id=workspace_id, org_id=org_ids["O1"], principal=subject,
                    principal_type=case["principal_type"], permissions=" ".join(case["workspace_permissions"]),
                ))
        await db.commit()
        principal = DomainPrincipal(
            subject=subject, org_id=str(org_ids[case["org"]]), client_id="offline-test-client",
            account_type=case["principal_type"],
        )
        caller = VerifiedCaller(principal=principal, safe_headers={})
        if case["allow"]:
            assert await authorize_organization_operation(db, caller, Permission(case["request"])) is None
        else:
            with pytest.raises(HTTPException) as denied:
                await authorize_organization_operation(db, caller, Permission(case["request"]))
            assert denied.value.status_code == 403
        if case["bound"] and case["allow"] and workspace_ids and not case["workspace_permissions"]:
            model, stored_org = await load_workspace_authorization(db, workspace_ids[0], subject, case["principal_type"])
            with pytest.raises(AuthorizationDeniedError):
                model.authorize(principal, str(workspace_ids[0]), Permission.READ, workspace_org_id=stored_org)


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
    assert actions["workspace.access.manage"]["routes"] == []
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



def test_maintained_cli_and_tool_surface_parity():
    actions = {action["id"]: action for action in MATRIX["actions"]}
    cli = {
        surface
        for action in actions.values()
        for surface in [action["surface"], *action.get("also_surfaces", [])]
        if surface.startswith("cli:")
    }
    assert cli == {
        "cli:workspace delete", "cli:deploy profiles", "cli:events --workspace",
        "cli:events", "cli:provider-connection create", "cli:provider-connection show",
        "cli:provider-connection validate", "cli:provider-connection rotate",
        "cli:provider-connection revoke", "cli:research findings/proposal list/show, sources/stats",
        "cli:research proposal create", "cli:research proposal approve/reject",
        "cli:onboarding capabilities", "cli:workspace create", "cli:onboarding plan",
        "cli:onboarding create", "cli:onboarding adopt", "cli:workspace list", "cli:cluster list",
        "cli:workspace describe", "cli:node list", "cli:quota show", "cli:workspace kubeconfig",
        "cli:onboarding lifecycle list", "cli:onboarding lifecycle preview", "cli:onboarding lifecycle continue",
        "cli:deploy list", "cli:deploy create", "cli:deploy preview", "cli:deploy delete",
        "cli:deploy teardown-preview", "cli:quota set", "cli:cost --workspace", "cli:cost --org",
        "cli:onboarding operation show", "cli:onboarding operation recover",
        "cli:onboarding approval request", "cli:deploy preview --request-approval",
        "cli:onboarding approval show", "cli:onboarding approval decide", "cli:account list",
        "cli:provider list", "cli:onboarding connection credentials", "cli:account onboard",
        "cli:account delete", "cli:aws-onboard register", "cli:provider add", "cli:provider delete",
        "cli:onboarding connection bind", "cli:onboarding connection show",
        "cli:onboarding connection validate", "cli:onboarding connection revoke",
    }
    lifecycle = (DOMAIN / "cli/adp-superplane-lifecycle.py").read_text()
    assert '"delete"' in lifecycle and '"profiles"' in lifecycle
    assert 'for action in ("create", "show", "validate", "rotate", "revoke"):' in lifecycle
    assert 'base = BASE + "/workspaces/" + workspace + "/provider-connections"' in lifecycle
    assert '"validate": "/validation", "rotate": "/rotation"' in lifecycle
    assert '"DELETE" if action == "revoke" else "POST"' in lifecycle
    assert '"DELETE",\n        None,\n        before,' in lifecycle
    for name, route in {
        "workspace.retire": "DELETE /workspaces/{workspace_id}",
        "serving.read": "GET /workspaces/{workspace_id}/deployment-profiles",
        "provider_connection.create": "POST /workspaces/{workspace_id}/provider-connections",
        "provider_connection.read": "GET /workspaces/{workspace_id}/provider-connections/{connection_id}",
        "provider_connection.validate": "POST /workspaces/{workspace_id}/provider-connections/{connection_id}/validation",
        "provider_connection.rotate": "POST /workspaces/{workspace_id}/provider-connections/{connection_id}/rotation",
        "provider_connection.revoke": "DELETE /workspaces/{workspace_id}/provider-connections/{connection_id}",
        "workspace.events.read": "GET /events/workspaces/{workspace_id}",
    }.items():
        assert route in actions[name]["routes"]
    research = ast.parse((DOMAIN / "cli/adp-superplane-research.py").read_text())
    run = next(node for node in research.body if isinstance(node, ast.FunctionDef) and node.name == "execute")
    first_guard = next(node for node in ast.walk(run) if isinstance(node, ast.If) and "scan" in ast.unparse(node.test) and "generate" in ast.unparse(node.test))
    access_token = next(node for node in ast.walk(run) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "access_token")
    assert first_guard.lineno < access_token.lineno
    assert '"unavailable"' in ast.unparse(first_guard) or "'unavailable'" in ast.unparse(first_guard)
    gateway = (REPO / "modules/gateway/cli/adp-superplane.py").read_text()
    for fragment in (
        'args.subcommand == "kubeconfig"', 'api.request("POST", f"{API_BASE}/workspaces/{segment(identifier)}/kubeconfig")',
        'api.request("PATCH", path, body)', 'API_BASE + "/orgs/cost"',
        'API_BASE + "/workspaces"', 'API_BASE + "/accounts"',
        'DOMAIN_CREDENTIALS = "/vault/credentials"',
        'api.request("POST", API_BASE + DOMAIN_CREDENTIALS, body)',
        'api.request("POST", API_BASE + "/operation-approvals", approval)',
    ):
        assert fragment in gateway
    onboarding = ast.parse((REPO / "modules/gateway/cli/adp-superplane-onboarding.py").read_text())
    endpoint_table = next(node.value for node in onboarding.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "ENDPOINTS" for target in node.targets))
    endpoints = ast.literal_eval(endpoint_table)
    for action_id, endpoint_names in {
        "capabilities.read": ("capabilities",),
        "workspace.create": ("previewWorkspace", "createWorkspace", "adoptWorkspace"),
        "workspace.lifecycle_proposal": ("listLifecycleProposals", "previewLifecycleProposal", "continueLifecycleProposal"),
        "approval.request": ("requestApproval",),
        "approval.read": ("getApproval",),
        "approval.decision": ("decideApproval",),
        "org.operations.read": ("getOperation", "recoverOperation"),
        "provider_connection.create": ("registerConnection",),
        "provider_connection.read": ("getConnection",),
        "provider_connection.validate": ("validateConnection",),
        "provider_connection.revoke": ("revokeConnection",),
    }.items():
        for name in endpoint_names:
            endpoint = endpoints[name]
            assert endpoint["served"] is True
            assert f'{endpoint["method"]} {endpoint["path"]}' in actions[action_id]["routes"]
    assert '"org":' in gateway and '"user":' in gateway
    assert actions["org.settings"]["surface"] == actions["org.users.manage"]["surface"] == "absent"
    assert 'if argv and argv[0] in REDIRECTED:' in gateway
    assert actions["workspace.kubeconfig"]["surface"] == "cli:workspace kubeconfig"
    assert actions["workspace.quota.change"]["surface"] == "cli:quota set"
    assert actions["workspace.cost.read"]["surface"] == "cli:cost --workspace"
    assert actions["org.accounts.manage"]["surface"] == "cli:account onboard"
    assert actions["research.scan"]["surface"] == "absent"
    assert actions["research.scan"]["unavailable_surfaces"] == ["cli:research scan"]
    assert actions["research.propose"]["unavailable_surfaces"] == ["cli:research proposal generate"]
    assert "POST /api/v1/research/proposals/generate" in actions["research.propose"]["routes"]
    assert MATRIX["verification"]["surfaces"] == "ui_routes_checked_cli_gateway_lifecycle_onboarding_research_checked_no_mcp_domain_api_action"
    mcp = (DOMAIN / "tools/superplane-mcp/superplane_mcp/server.py").read_text()
    assert "from .contract import CONTRACT_VERSION, IS_MOCK, CapacityContract, ContractError" in mcp
    assert not any("tool:" in surface for action in actions.values() for surface in [action["surface"], *action.get("also_surfaces", [])])


def test_platform_roles_are_not_workspace_presets():
    gateway_config = REPO / "modules/gateway/src/admin/config.py"
    source = gateway_config.read_text()
    tree = ast.parse(source)
    admin_role = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AdminRole")
    names = {node.value.value for node in admin_role.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)}
    assert names == {"platform_admin", "org_admin", "dept_admin", "member"}
    assert not {"workspace_viewer", "workspace_operator", "workspace_provisioner", "workspace_owner"} & names
    spec = importlib.util.spec_from_file_location("gateway_admin_config_contract", gateway_config)
    gateway_roles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway_roles)
    assert gateway_roles.ASSIGNABLE_ROLES == ("member", "dept_admin", "org_admin", "platform_admin")
    for stored_role in ("platform_admin", "admin", "org_admin"):
        assert gateway_roles.membership_role_to_admin_role(stored_role) is gateway_roles.AdminRole.ORG_ADMIN
    for stored_role in ("member", "unknown_role", "workspace_owner", None):
        assert gateway_roles.membership_role_to_admin_role(stored_role) is gateway_roles.AdminRole.MEMBER
    for role in ("platform_admin", "org_admin", "dept_admin", "member", "service", "unknown_role"):
        assert permissions_for_adp_role(role) == frozenset()
    assert set(permissions_for_adp_role("workspace_viewer")) == {Permission.READ}
    dependencies = ast.parse((REPO / "modules/gateway/src/auth/dependencies.py").read_text())
    context_factory = next(node for node in dependencies.body if isinstance(node, ast.FunctionDef) and node.name == "_cognito_claims_to_context")
    context_source = ast.unparse(context_factory)
    assert "claims.role == 'platform_admin'" in context_source
    assert "claims.role == 'org_admin'" not in context_source
    assert "account_type == 'service'" in context_source
    assert "if not claims.client_id:" in context_source
    assert "user_id = claims.client_id" in context_source
    workspaces = ast.parse((REPO / "modules/gateway/src/auth/workspaces.py").read_text())
    selection = next(node for node in workspaces.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "select_workspace")
    assert {node.func.id for node in ast.walk(selection) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)} >= {"_require_human", "_memberships"}
    assert any(isinstance(node, ast.Name) and node.id == "membership_role_to_admin_role" for node in ast.walk(workspaces))


def test_role_labels_do_not_supply_domain_grants():
    without_grants = [case for case in CASES if case["grant_workspace"] is None and case["active"]]
    assert {case["adp_role"] for case in without_grants} >= {
        "platform_admin", "org_admin", "dept_admin", "member", "service", "unknown_role", "workspace_owner"
    }
    assert all(not case["allow"] for case in without_grants)
    for role in ("platform_admin", "org_admin", "dept_admin"):
        assert any(case["adp_role"] == role and case["grant_workspace"] and case["allow"] for case in CASES)
    assert any(case["principal_type"] == "service" and case["grant_type"] == "service" and case["allow"] for case in CASES)
    assert any(case["principal_type"] == "service" and case["grant_type"] == "human" and not case["allow"] for case in CASES)


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
