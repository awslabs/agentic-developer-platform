"""The capability contract's four axes must stay independent — Issue #5621.

CLI-08-AC-01 in test form. The story's requirement is not "report capabilities";
it is that a supported-but-disabled operation, a permitted-but-unready one and an
undeterminable one are each **distinguishable**, because the CLI's whole value is
telling the user which of those five things went wrong. A test that only asserted
"the endpoint returns operations" would pass with every axis collapsed to one
boolean — the exact defect the contract exists to prevent.

So each test here pins one collapse shut:

* a disabled module must not read as a denial (`enabled=no`, `permitted` untouched)
* a denial must not read as a disabled module
* an unreachable role store must read UNKNOWN, never `no`
* a present-but-undrainable dependency must read UNKNOWN, never `yes`

These run the REAL route through the real FastAPI app and the real serializer, so
a field renamed or dropped in transit fails here. A hand-built dict asserted
against another hand-built dict would not be a contract test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.cli_capabilities import contract
from src.cli_capabilities.routes import get_access_control, router
from src.shared.schemas.auth import TokenContext

ORG = "org-alpha"


def token_context(org_id: str = ORG, *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id="user-1",
        org_id=org_id,
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class FakeAccessControl:
    """Stands in for the role store, with the three behaviours that matter.

    `AccessControl.check_permission` signals denial by RAISING, so a fake that
    returned False would not exercise the code path the real one drives. It
    therefore raises the real `AccessDeniedError`, matching the production
    authority boundary rather than relying on a class-name lookalike.
    """

    def __init__(self, granted=(), *, broken=False):
        self.granted = set(granted)
        self.broken = broken
        self.asked = []

    async def check_permission(self, context, permission, target_org_id=None, target_dept_id=None):
        self.asked.append((permission, target_org_id))
        if self.broken:
            raise RuntimeError("role store unreachable")
        if permission in self.granted:
            return True
        raise AccessDeniedError(f"{permission} denied")

    async def check_permission_for_discovery(self, context, permission, target_org_id=None, target_dept_id=None):
        return await self.check_permission(context, permission, target_org_id, target_dept_id)


def build_client(caller: TokenContext, access) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: caller
    app.dependency_overrides[get_access_control] = lambda: access
    return TestClient(app)


def operations(payload) -> dict:
    return {operation["id"]: operation for operation in payload["operations"]}


@pytest.fixture
def clean_flags(monkeypatch):
    """No feature flag set, so each test states the ones it depends on."""
    for name in (
        "FEATURE_AGENT_MODELS_ENABLED",
        "FEATURE_AGENT_CONTROL_ENABLED",
        "FEATURE_KNOWLEDGE_ENABLED",
        "AGENT_CONTEXT_ENABLED",
        "FEATURE_ORCHESTRATION_ENGINE_ENABLED",
        "FEATURE_SUPERPLANE_ENABLED",
        "FEATURE_LOGS_ENABLED",
        "FEATURE_CONNECTIONS_ENABLED",
        "INGESTION_QUEUE_URL",
        "AGENT_DISPATCH_QUEUE_URL",
        "WEBHOOK_QUEUE_URL",
        "GATEWAY_RELEASE",
        "GIT_SHA",
        "IMAGE_TAG",
    ):
        monkeypatch.delenv(name, raising=False)


# --- the axes stay independent ----------------------------------------------


def test_disabled_module_is_not_reported_as_a_denial(clean_flags):
    """`enabled=no` must not bleed into `permitted`.

    A flag is a deployment rollout control and says nothing about this caller's
    authority (src/features/routes.py is explicit that it is not a security
    boundary). If a disabled module also reported `permitted=no`, the CLI would
    tell a fully authorized administrator they lack a permission they hold.
    """
    client = build_client(token_context(), FakeAccessControl())
    found = operations(client.get("/me/cli-capabilities").json())

    catalogue = found["models.catalog.read"]
    assert catalogue["enabled"] == contract.NO
    # No permission is required for the catalogue read, so the caller IS permitted
    # — the disabled module has not been allowed to overwrite that fact.
    assert catalogue["permitted"] == contract.YES


def test_denial_is_not_reported_as_a_disabled_module(clean_flags, monkeypatch):
    """The inverse collapse: a permission refusal must leave `enabled` alone."""
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    client = build_client(token_context(), FakeAccessControl())
    found = operations(client.get("/me/cli-capabilities").json())

    approve = found["flows.approve.write"]
    assert approve["permitted"] == contract.NO
    assert approve["enabled"] == contract.YES


def test_granted_permission_is_reported_and_asked_in_the_callers_own_tenant(clean_flags, monkeypatch):
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    access = FakeAccessControl(granted={Permission.PLAN_APPROVE})
    client = build_client(token_context(), access)
    found = operations(client.get("/me/cli-capabilities").json())

    assert found["flows.approve.write"]["permitted"] == contract.YES
    # Every permission question is asked about the CALLER's org. A question asked
    # about any other tenant would be a cross-tenant read.
    assert {asked_org for _, asked_org in access.asked} == {ORG}


def test_unreachable_role_store_is_unknown_not_denied(clean_flags, monkeypatch):
    """A transport fault is not a denial.

    This is the collapse with teeth. If an outage rendered as `permitted=no`, the
    CLI would refuse a mutation the user is entitled to and blame their
    permissions — reporting as fact something the server never asserted.
    """
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    client = build_client(token_context(), FakeAccessControl(broken=True))
    found = operations(client.get("/me/cli-capabilities").json())

    assert found["flows.approve.write"]["permitted"] == contract.UNKNOWN


def test_real_access_control_preserves_unknown_when_membership_lookup_fails(clean_flags, monkeypatch):
    """The production fallback must not turn an authority outage into denial."""
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    attempts = 0

    async def unavailable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("membership store unavailable")

    monkeypatch.setattr("src.shared.identity.workspaces.memberships_for_login", unavailable)
    access = AccessControl(db=object())
    found = operations(build_client(token_context(), access).get("/me/cli-capabilities").json())

    assert found["flows.approve.write"]["permitted"] == contract.UNKNOWN
    assert access._role_cache == {}
    assert attempts == 1


def test_real_access_control_reports_confirmed_missing_membership_as_denied(clean_flags, monkeypatch):
    """A successful empty lookup is evidence of no authority, not an outage."""
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")

    async def no_memberships(*_args, **_kwargs):
        return object(), {}

    monkeypatch.setattr("src.shared.identity.workspaces.memberships_for_login", no_memberships)
    access = AccessControl(db=object())
    found = operations(build_client(token_context(), access).get("/me/cli-capabilities").json())

    assert found["flows.approve.write"]["permitted"] == contract.NO


def test_delegated_admin_capabilities_match_the_routes_platform_admin_gate(clean_flags):
    ordinary = operations(build_client(token_context(), FakeAccessControl()).get("/me/cli-capabilities").json())
    administrator = operations(build_client(token_context(is_admin=True), FakeAccessControl(broken=True)).get("/me/cli-capabilities").json())

    operation_ids = {
        "routing.bedrock.read",
        "routing.bedrock.write",
        "routing.bedrock.verify.write",
        "github.app.admin.read",
        "github.app.admin.setup.write",
        "github.app.admin.revalidate.write",
    }
    assert {ordinary[operation_id]["permitted"] for operation_id in operation_ids} == {contract.NO}
    assert {administrator[operation_id]["permitted"] for operation_id in operation_ids} == {contract.YES}
    assert {administrator[operation_id]["required_permission"] for operation_id in operation_ids} == {"platform:admin"}


def test_missing_dependency_is_not_ready_and_present_one_is_only_unknown(clean_flags, monkeypatch):
    """Readiness is three-valued for a reason config alone cannot settle.

    A missing dispatch queue is a definite NO. A present one is UNKNOWN, not YES:
    pods scale from zero, so a queue can exist with nothing draining it, and the
    only way to prove a worker consumes is to make one run — the paid/stateful
    probe this contract forbids by default.
    """
    client = build_client(token_context(), FakeAccessControl())
    assert operations(client.get("/me/cli-capabilities").json())["agents.activity.read"]["ready"] == contract.NO

    monkeypatch.setenv("AGENT_DISPATCH_QUEUE_URL", "https://sqs.example/queue")
    assert operations(client.get("/me/cli-capabilities").json())["agents.activity.read"]["ready"] == contract.UNKNOWN


def test_readiness_no_does_not_disable_or_deny(clean_flags, monkeypatch):
    """An unready dependency must not masquerade as disabled or unpermitted."""
    monkeypatch.setenv("FEATURE_KNOWLEDGE_ENABLED", "true")
    client = build_client(token_context(), FakeAccessControl())
    asset = operations(client.get("/me/cli-capabilities").json())["knowledge.asset.write"]

    assert asset["ready"] == contract.NO
    assert asset["enabled"] == contract.YES
    assert asset["permitted"] == contract.YES


# --- unknown is never silently a boolean ------------------------------------


def test_every_axis_is_one_of_the_three_documented_values(clean_flags):
    client = build_client(token_context(), FakeAccessControl())
    payload = client.get("/me/cli-capabilities").json()

    assert payload["operations"], "the registry must not serialize empty"
    for operation in payload["operations"]:
        for axis in ("supported", "enabled", "permitted", "ready"):
            assert operation[axis] in contract.TRISTATE, f"{operation['id']}.{axis}={operation[axis]!r}"


def test_tristate_keeps_unknown_distinct_from_false():
    assert contract.tristate(None) == contract.UNKNOWN
    assert contract.tristate(False) == contract.NO
    assert contract.tristate(True) == contract.YES
    assert contract.UNKNOWN != contract.NO


# --- the document's own framing ---------------------------------------------


def test_unknown_gateway_release_is_stated_not_faked(clean_flags):
    client = build_client(token_context(), FakeAccessControl())
    gateway = client.get("/me/cli-capabilities").json()["gateway"]

    assert gateway["state"] == contract.UNKNOWN
    assert gateway["release"] == ""


def test_gateway_image_embeds_the_immutable_source_revision():
    gateway_root = Path(__file__).parents[2]
    repository_root = Path(__file__).parents[4]

    assert 'ARG GATEWAY_RELEASE=""' in (gateway_root / "Dockerfile").read_text()
    assert "publish-shared-image.sh adp-gateway" in (repository_root / "codebuild/bs-gateway-build.yml").read_text()
    assert '--build-arg "GATEWAY_RELEASE=$ADP_SOURCE_SHA"' in (repository_root / "platform/scripts/publish-shared-image.sh").read_text()


def test_known_gateway_release_is_reported_with_its_source(clean_flags, monkeypatch):
    monkeypatch.setenv("GATEWAY_RELEASE", "2026.09.21-abc1234")
    client = build_client(token_context(), FakeAccessControl())
    gateway = client.get("/me/cli-capabilities").json()["gateway"]

    assert (gateway["state"], gateway["release"], gateway["source"]) == (
        contract.YES,
        "2026.09.21-abc1234",
        "GATEWAY_RELEASE",
    )


def test_response_states_its_schema_version_and_tenant(clean_flags):
    client = build_client(token_context(), FakeAccessControl())
    payload = client.get("/me/cli-capabilities").json()

    assert payload["schema_version"] == contract.SCHEMA_VERSION
    # A client must be able to refuse a cached document from another tenant.
    assert payload["tenant"]["org_id"] == ORG


def test_tenant_comes_from_the_token_and_cannot_be_asked_for(clean_flags):
    """There is no scope parameter to abuse — passing one changes nothing."""
    client = build_client(token_context(org_id="org-mine"), FakeAccessControl())
    payload = client.get("/me/cli-capabilities?org_id=org-theirs").json()

    assert payload["tenant"]["org_id"] == "org-mine"


def test_the_document_never_carries_a_secret(clean_flags, monkeypatch):
    """Discovery is published to a CLI; it must not become a config exfiltration path."""
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://sqs.example/secret-queue-name")
    monkeypatch.setenv("AGENT_DISPATCH_QUEUE_URL", "https://sqs.example/another-queue")
    client = build_client(token_context(), FakeAccessControl())
    body = client.get("/me/cli-capabilities").text

    # Readiness is reported as a state, never by echoing the value it read.
    assert "sqs.example" not in body
    assert "secret-queue-name" not in body


# --- registry hygiene, which the manifest check depends on -------------------


def test_operation_ids_are_unique_and_stably_shaped():
    ids = [operation.id for operation in contract.OPERATIONS]
    assert len(ids) == len(set(ids)), "a duplicate ID makes the document ambiguous"
    for operation_id in ids:
        assert operation_id.replace(".", "").replace("_", "").isalnum()
        assert operation_id == operation_id.lower()


def test_declared_readiness_classes_all_have_a_probe():
    """A typo'd dependency class would otherwise silently report UNKNOWN forever."""
    declared = {operation.readiness for operation in contract.OPERATIONS if operation.readiness}
    assert declared <= set(contract._READINESS), declared - set(contract._READINESS)


def test_mutating_operations_are_marked_so_the_cli_can_refuse_before_sending():
    found = {operation.id: operation.mutates for operation in contract.OPERATIONS}
    assert found["models.mapping.self.write"] is True
    assert found["flows.approve.write"] is True
    assert found["budget.self.read"] is False


def test_self_and_managed_model_mapping_permissions_are_distinct(clean_flags, monkeypatch):
    monkeypatch.setenv("FEATURE_AGENT_MODELS_ENABLED", "true")
    access = FakeAccessControl()
    found = operations(build_client(token_context(), access).get("/me/cli-capabilities").json())

    assert found["models.mapping.self.write"]["permitted"] == contract.YES
    assert found["models.mapping.self.write"]["required_permission"] == ""
    assert found["models.mapping.managed.write"]["permitted"] == contract.NO
    assert found["models.mapping.managed.write"]["required_permission"] == Permission.ORG_UPDATE.value
    assert (Permission.ORG_UPDATE, ORG) in access.asked


def test_request_lookup_reports_the_real_log_permission(clean_flags, monkeypatch):
    monkeypatch.setenv("FEATURE_LOGS_ENABLED", "true")
    access = FakeAccessControl()
    operation = operations(build_client(token_context(), access).get("/me/cli-capabilities").json())["logs.request.read"]

    assert operation["permitted"] == contract.NO
    assert operation["required_permission"] == Permission.LOGS_READ.value
    assert (Permission.LOGS_READ, ORG) in access.asked


def test_own_bedrock_discovery_is_not_an_admin_operation(clean_flags):
    found = operations(build_client(token_context(), FakeAccessControl()).get("/me/cli-capabilities").json())
    assert found["routing.bedrock.own.read"]["permitted"] == contract.YES
    assert found["routing.bedrock.read"]["permitted"] == contract.NO
