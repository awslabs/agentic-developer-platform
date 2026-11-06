"""Product/configuration/consent/setup APIs create their own authority rows."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import Request
from l5_services import runtime_devops, runtime_support
from l5_services.models.base import Base as L5Base
from l5_services.models.devops_space import DevopsSpaceIntent
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.aws_connection_authority import connection_binding
from src.auth.l5_execution_scope import current_execution_scope
from src.internal import assume_role_routes, l5_credential_routes
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.sts_assume_service import AssumeRoleResult
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_i05 import REQUEST
from tests.products.test_l5_configuration_host import l5_owner as l5_owner

__all__ = ["l5_owner"]


@pytest.fixture
async def api_setup(test_client, test_engine, db_session, app, l5_owner, monkeypatch, request):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    tenant = l5_owner.org_id
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "devops_api_product_0001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    product_path = f"/v1/products/{product_id}/projects/{project_id}"
    credential = UserCredential(
        id=str(uuid4()),
        org_id=tenant,
        user_id=l5_owner.user_id,
        service="aws",
        credential_type="aws_role",
        label="selected",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
        aws_external_id="offline-external-id",
        aws_verified_at=datetime.now(UTC),
        aws_verification_attempt=str(uuid4()),
        aws_verified_version_id="version-one",
        agent_consent=True,
        strict=True,
        scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/hosting", "status": "verified", "source": "imported_role"},
    )
    credential.aws_verified_binding = connection_binding(credential)
    db_session.add(credential)
    await db_session.commit()
    from src.auth.vault_routes import get_secrets_manager as get_source_secrets

    selected_secret = Mock()
    selected_secret.current_version_id.return_value = "version-one"
    selected_secret.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": credential.scopes["role_arn"],
                "external_id": credential.aws_external_id,
                "account_id": "123456789012",
                "default_region": "us-east-1",
            }
        ),
        "version-one",
    )
    app.dependency_overrides[get_source_secrets] = lambda: selected_secret
    bound = await test_client.post(
        product_path + "/bindings",
        json={
            "kind": "environment",
            "provider": "aws",
            "connection_ref": {"kind": "credential", "id": credential.id},
            "namespace": "123456789012",
            "native_id": "us-east-1",
            "environment": {"name": "Dedicated fixture", "environment_class": "test", "production": False},
        },
        headers={"If-Profile-Revision": "1", "Idempotency-Key": "devops_api_environment_01"},
    )
    assert bound.status_code == 201, bound.text
    assert bound.json()["status"] == "healthy"
    selected = await test_client.patch(
        product_path + "/configuration",
        json={
            "configuration": {
                "selected_capabilities": [{"capability": "devops", "ownership_mode": "integrated"}],
                "governance": {"data_classification": "internal", "criticality": "low"},
            },
        },
        headers={"If-Profile-Revision": "2", "Idempotency-Key": "devops_api_capability_01"},
    )
    assert selected.status_code == 200, selected.text
    path = product_path + "/services/devops"
    options = await test_client.get(path + "/configuration/options")
    assert options.status_code == 200, options.text
    reference, environment = options.json()["connections"][0], options.json()["sources"][0]
    adopted = getattr(request, "param", "create") in {"adopt", "adopt_enabled"}
    draft = await test_client.put(
        path + "/configuration",
        json={
            "service_id": "devops",
            "enabled": False,
            "capabilities": ["investigation"],
            "devops": {
                "hosting_connection_ref": reference,
                "region": "us-east-1",
                "space_mode": "adopt" if adopted else "create",
                **({"existing_space_id": "new-space", "authentication_mode": "scoped_iam_federation"} if adopted else {}),
                "accounts": [{"account_id": "123456789012", "environments": [{"environment_ref": environment}]}],
                "integrations": [],
                "browser_launch_policy": "scoped_session" if adopted else "disabled",
                "prevention_enabled": False,
            },
        },
        headers={"If-None-Match": "*", "Idempotency-Key": "devops_api_draft_00001"},
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["state"] == "disabled" and draft.json()["handoff_state"] == "linked"
    fixture = SimpleNamespace(
        credential=credential,
        reference=reference,
        role=credential.scopes["role_arn"],
        product_id=product_id,
        project_id=project_id,
        selection=SimpleNamespace(account_id="123456789012", region="us-east-1"),
    )
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    worker_actor = l5_owner.model_copy(
        update={
            "user_id": "iam-agent:l5-devops-worker",
            "agent_registry_id": "l5-devops-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def verified_worker(request: Request):
        request.state.token_context = worker_actor

    app.dependency_overrides[verify_internal_or_irsa] = verified_worker
    secrets = Mock()
    secrets.current_version_id.return_value = fixture.reference["record_revision"]
    secrets.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": fixture.role,
                "external_id": "offline-external-id",
                "account_id": fixture.selection.account_id,
                "session_duration_seconds": 900,
                "default_region": fixture.selection.region,
            }
        ),
        fixture.reference["record_revision"],
    )
    app.dependency_overrides[get_secrets_manager] = lambda: secrets
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="fixture-key",
            secret_access_key="fixture-secret",
            session_token="fixture-token",
            expiration=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            region=fixture.selection.region,
            profile_name="fixture",
        )
    )
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setattr(assume_role_routes, "assume_role", assumed)
    monkeypatch.setattr(
        l5_credential_routes,
        "verify_assumed_session",
        AsyncMock(
            return_value={
                "account_id": fixture.selection.account_id,
                "role_arn": fixture.role,
                "region": fixture.selection.region,
                "verified_at": datetime.now(UTC).isoformat(),
            }
        ),
    )
    state = {"space": {"agentSpaceId": "new-space", "name": "adopted-space"}, "tags": {}, "timeout": False, "goal_state": "DISABLED"}
    if getattr(request, "param", "create") == "adopt_enabled":
        state["goal_state"] = "ENABLED"
    native = Mock()

    def create_space(**payload):
        state["space"] = {"agentSpaceId": "new-space", "name": payload["name"]}
        state["tags"] = payload["tags"]
        if state["timeout"]:
            raise TimeoutError("lost creation response")
        return {"agentSpace": state["space"], "tags": state["tags"]}

    native.create_agent_space.side_effect = create_space
    native.get_agent_space.side_effect = lambda **payload: {"agentSpace": state["space"], "tags": state["tags"]}
    native.list_agent_spaces.side_effect = lambda **payload: {"agentSpaces": [state["space"]]}
    native.list_goals.side_effect = lambda **payload: {
        "goals": [
            {
                "goalId": "goal-one",
                "goalType": "ONCALL_REPORT",
                "evaluationSchedule": {"state": state["goal_state"], "expression": "rate(1 day)"},
            }
        ]
    }
    native.update_goal.side_effect = lambda **payload: state.update(goal_state=payload["evaluationSchedule"]["state"])
    provider = SimpleNamespace(client=lambda *args, **kwargs: native)
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setattr("src.l5_integration.service_publisher", lambda service: publish if service == "devops" else None)
    monkeypatch.setattr(runtime_support, "sessions", lambda: sessions)
    monkeypatch.setattr(runtime_support, "gateway_transport", lambda: test_client)
    monkeypatch.setattr(runtime_support, "archive_store", lambda service: Mock())
    monkeypatch.setattr(runtime_support, "queue_publisher", lambda service: publish)
    monkeypatch.setattr(runtime_support, "provider_session", lambda credentials, region: provider)
    return SimpleNamespace(
        fixture=fixture,
        tenant=tenant,
        sessions=sessions,
        messages=messages,
        worker=runtime_devops.factory(),
        native=native,
        state=state,
        assumed=assumed,
        path=path,
        draft=draft.json(),
        environment=environment,
    )


async def setup_consent(client, setup):
    params = {"for_control": "true", "control_action": "devops_setup"}
    preview = await client.get(setup.path + "/delegation-scope", params=params)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        setup.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "for_control": True,
            "control_action": "devops_setup",
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "devops_api_consent_0001"},
    )
    assert result.status_code == 200, result.text
    return result.json()


async def complete_setup(client, setup):
    consent = await setup_consent(client, setup)
    selected = setup.draft["settings"]["devops"]
    request = {"action": "create", "configuration_revision": setup.draft["revision"]}
    if selected["space_mode"] == "adopt":
        request.update(
            action="link",
            native_resource_ref={
                **setup.fixture.reference,
                "record_type": "agent_space",
                "record_id": selected["existing_space_id"],
                "record_revision": selected["existing_space_id"],
            },
        )
    accepted = await client.post(
        setup.path + "/binding-operations",
        json=request,
        headers={"Idempotency-Key": "devops_api_creation_0001"},
    )
    assert accepted.status_code == 202, accepted.text
    assert await setup.worker.execute(setup.messages.pop())
    readback = await client.get(setup.path + "/binding-operations/" + accepted.json()["operation_id"])
    assert readback.status_code == 200 and readback.json()["state"] == "succeeded", readback.text
    return consent, readback.json()


async def activation_settings(db, setup, operation):
    policy = BudgetConfig(
        org_id=setup.tenant,
        entity_type="org",
        entity_id=setup.tenant,
        period_type="daily",
        budget_amount_usd=Decimal("1.00"),
        enforcement_mode="hard",
    )
    db.add(policy)
    await db.commit()
    settings = deepcopy(setup.draft["settings"])
    settings.pop("profile_ref")
    settings.pop("overrides", None)
    settings["enabled"] = True
    settings["limits"] = {
        "max_cost": "1.00",
        "currency": "USD",
        "deadline_seconds": 300,
        "max_provider_attempts": 1,
        "max_polls": 10,
        "concurrency": 1,
        "policy_ref": {**setup.fixture.reference, "record_type": "policy", "record_id": policy.id},
    }
    settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = [setup.environment]
    settings["devops"]["trigger_policies"] = [
        {
            "trigger_id": "manual-investigation",
            "kind": "manual",
            "enabled": True,
            "capability": "investigation",
            "binding_id": operation["binding_id"],
            "environment_ref": setup.environment,
            "resource_refs": [setup.environment],
            "telemetry_refs": [],
        }
    ]
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("api_setup", ["create", "adopt"], indirect=True)
@pytest.mark.parametrize("budget_mode", ["missing", "exhausted", "soft"])
async def test_real_api_draft_setup_activation_and_new_owner_consent(test_client, db_session, api_setup, budget_mode):
    setup = api_setup
    grant_view, operation = await complete_setup(test_client, setup)
    grant = await db_session.get(CredentialServiceDelegation, grant_view["delegation_id"], populate_existing=True)
    action = "link" if setup.draft["settings"]["devops"]["space_mode"] == "adopt" else "create"
    assert grant.operations == [f"binding:{action}", "credential:session"] and grant.capabilities == []
    if action == "create":
        assert "setup_action" not in grant.execution_scope and "native_resource_ref" not in grant.execution_scope
    assert not await current_execution_scope(db_session, grant)
    settings = await activation_settings(db_session, setup, operation)
    budget = await db_session.get(BudgetConfig, settings["limits"]["policy_ref"]["record_id"])
    if budget_mode == "missing":
        await db_session.delete(budget)
        settings["limits"].pop("policy_ref")
        # Match the UI activation payload: no internal trigger identifier.
        settings["devops"]["trigger_policies"][0].pop("trigger_id")
    elif budget_mode == "exhausted":
        budget.budget_amount_usd = Decimal("0")
    else:
        budget.enforcement_mode = "soft"
    await db_session.commit()
    setup.native.reset_mock()
    activated = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + setup.draft["hash"] + '"', "Idempotency-Key": "devops_api_activation_01"},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "ready" and activated.json()["revision"] == 2
    assert setup.native.mock_calls == [] and setup.messages == []
    revoke = await test_client.delete(setup.path + "/delegations/" + grant.id, headers={"If-Match": '"1"'})
    assert revoke.status_code == 200, revoke.text
    preview = await test_client.get(setup.path + "/delegation-scope", params={"binding_id": operation["binding_id"]})
    assert preview.status_code == 200, preview.text
    normal = await test_client.post(
        setup.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "binding_id": operation["binding_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "devops_api_execution_01"},
    )
    assert normal.status_code == 200, normal.text
    execution = await db_session.get(CredentialServiceDelegation, normal.json()["delegation_id"])
    assert await current_execution_scope(db_session, execution)
    assert "purpose" not in execution.execution_scope and execution.capabilities == ["investigation"]
    run = await test_client.post(
        setup.path + "/runs",
        json={
            "service_id": "devops",
            "configuration_revision": 2,
            "binding_id": operation["binding_id"],
            "capability": "investigation",
            "devops_input": {
                "kind": "investigation",
                "question": "Investigate the selected fixture environment.",
                "resource_refs": [setup.environment],
                "telemetry_refs": [],
            },
        },
        headers={"Idempotency-Key": "devops_api_manual_run_01"},
    )
    assert run.status_code == 202, run.text
    assert len(setup.messages) == 1 and setup.native.mock_calls == []
    from l5_services.models.run import Run

    stored = await db_session.get(Run, (setup.tenant, run.json()["run_id"]))
    assert stored.trigger_id == "manual-investigation"
    replay = await test_client.post(
        setup.path + "/runs", json=json.loads(run.request.content), headers={"Idempotency-Key": "devops_api_manual_run_01"}
    )
    assert replay.status_code == 202 and replay.json()["run_id"] == run.json()["run_id"]
    assert len(setup.messages) == 1 and setup.native.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["stale_goal", "enabled_goal", "setup_failed", "foreign_environment", "unlimited", "integration", "native_ui", "empty_resources"],
)
async def test_activation_fails_closed_without_provider_effects(test_client, db_session, api_setup, change):
    setup = api_setup
    _, operation = await complete_setup(test_client, setup)
    settings = await activation_settings(db_session, setup, operation)
    receipt = await db_session.get(DevopsSpaceIntent, (setup.tenant, operation["operation_id"]))
    if change == "stale_goal":
        receipt.prevention_observation = {**receipt.prevention_observation, "observed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    elif change == "enabled_goal":
        receipt.prevention_observation = {**receipt.prevention_observation, "native_schedule": {"enabled": True}}
    elif change == "setup_failed":
        receipt.state = "failed"
    elif change == "foreign_environment":
        settings["source_refs"] = [{**setup.environment, "record_id": "other-environment"}]
    elif change == "unlimited":
        settings["limits"]["max_cost"] = "NaN"
    elif change == "integration":
        settings["devops"]["integrations"] = [{"kind": "cloudwatch", "resource_refs": []}]
    elif change == "native_ui":
        settings["devops"]["browser_launch_policy"] = "scoped_session"
    elif change == "empty_resources":
        settings["devops"]["trigger_policies"][0]["resource_refs"] = []
        settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = []
    await db_session.commit()
    setup.native.reset_mock()
    response = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + setup.draft["hash"] + '"', "Idempotency-Key": "devops_api_bad_activation_01"},
    )
    assert response.status_code in {404, 409, 422}, response.text
    current = await test_client.get(setup.path + "/configuration")
    assert current.json()["revision"] == 1 and current.json()["settings"]["enabled"] is False
    assert setup.native.mock_calls == [] and setup.messages == []


@pytest.mark.asyncio
async def test_setup_consent_isolated_from_other_service_operations(test_client, db_session, api_setup, l5_owner):
    from src.shared.identity.product_scope import service_product_scope

    setup = api_setup
    grant_view = await setup_consent(test_client, setup)
    grant = await db_session.get(CredentialServiceDelegation, grant_view["delegation_id"])
    worker = l5_owner.model_copy(update={"account_type": "service", "auth_source": "iam", "agent_registry_id": "l5-devops-worker"})
    for operation in ("product:read", "source:read", "source:pin", "credential:session"):
        assert await service_product_scope(db_session, worker, grant.id, grant.product_id, grant.project_id, operation) is None
    for action in ("link", "check", "update", "suspend", "resume", "unlink", "cleanup"):
        denied = await test_client.post(
            setup.path + "/binding-operations",
            json={
                "action": action,
                "configuration_revision": 1,
                "binding_id": "foreign-binding",
            },
            headers={"Idempotency-Key": "devops_forbidden_" + action, "If-Match": '"1"'},
        )
        assert denied.status_code in {403, 409, 422}, (action, denied.text)
    assert setup.native.mock_calls == [] and setup.messages == []
    accepted = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "devops_create_unique_001"},
    )
    assert accepted.status_code == 202, accepted.text
    repeat = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "devops_create_unique_002"},
    )
    assert repeat.status_code == 409 and repeat.json()["detail"]["code"] == "setup_already_retained"
    replay = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "devops_create_unique_001"},
    )
    assert replay.status_code == 202 and replay.json() == accepted.json()
    assert len(setup.messages) == 1


@pytest.mark.parametrize("api_setup", ["adopt"], indirect=True)
@pytest.mark.parametrize("enabled_goal", [False, True])
async def test_exact_adoption_is_read_only_idempotent_and_never_creation(test_client, db_session, api_setup, enabled_goal):
    from l5_services.devops.setup import original_setup
    from l5_services.models.binding import Binding

    setup = api_setup
    setup.state["goal_state"] = "ENABLED" if enabled_goal else "DISABLED"
    view = await setup_consent(test_client, setup)
    grant = await db_session.get(CredentialServiceDelegation, view["delegation_id"])
    assert grant.operations == ["binding:link", "credential:session"]
    reference = grant.execution_scope["native_resource_ref"]
    body = {"action": "link", "configuration_revision": 1, "native_resource_ref": reference}
    for changed in ({**body, "native_resource_ref": {**reference, "record_id": "other-space"}}, {"action": "create", "configuration_revision": 1}):
        denied = await test_client.post(setup.path + "/binding-operations", json=changed, headers={"Idempotency-Key": "devops_adoption_wrong_01"})
        assert denied.status_code == 403, denied.text
    accepted = await test_client.post(setup.path + "/binding-operations", json=body, headers={"Idempotency-Key": "devops_adoption_exact_01"})
    assert accepted.status_code == 202, accepted.text
    replay = await test_client.post(setup.path + "/binding-operations", json=body, headers={"Idempotency-Key": "devops_adoption_exact_01"})
    assert replay.json() == accepted.json() and len(setup.messages) == 1
    another = await test_client.post(setup.path + "/binding-operations", json=body, headers={"Idempotency-Key": "devops_adoption_exact_02"})
    assert another.status_code == 409 and another.json()["detail"]["code"] == "setup_already_retained"
    assert await setup.worker.execute(setup.messages.pop())
    readback = (await test_client.get(setup.path + "/binding-operations/" + accepted.json()["operation_id"])).json()
    binding = await db_session.get(Binding, (setup.tenant, readback["binding_id"]), populate_existing=True)
    assert binding.ownership == "customer_linked" and binding.creation_receipt is None
    assert binding.state == ("blocked" if enabled_goal else "ready")
    assert (await original_setup(db_session, binding)).operation_id == readback["operation_id"]
    assert setup.state["goal_state"] == ("ENABLED" if enabled_goal else "DISABLED")
    assert {call[0] for call in setup.native.mock_calls} == {"get_agent_space", "list_goals"}
    policy = json.loads(setup.assumed.call_args.kwargs["session_policy"])
    assert [statement["Action"] for statement in policy["Statement"]] == [["aidevops:GetAgentSpace", "aidevops:ListGoals"]]
    assert all(statement["Resource"].endswith("/new-space") for statement in policy["Statement"])
    cleanup = await test_client.get(
        setup.path + "/delegation-scope",
        params={
            "binding_id": binding.binding_id,
            "for_control": "true",
            "control_action": "cleanup",
        },
    )
    assert cleanup.status_code == 422 and cleanup.json()["detail"]["code"] == "cleanup_scope_invalid"
    setup.native.delete_agent_space.assert_not_called()
    if enabled_goal:
        settings = await activation_settings(db_session, setup, readback)
        activation = await test_client.put(
            setup.path + "/configuration",
            json=settings,
            headers={"If-Match": '"' + setup.draft["hash"] + '"', "Idempotency-Key": "devops_adoption_blocked_01"},
        )
        assert activation.status_code == 409


@pytest.mark.parametrize("api_setup", ["adopt"], indirect=True)
@pytest.mark.parametrize("change", ["native_id", "configuration_space", "connection_revision", "missing_action", "missing_reference", "revoked"])
async def test_adoption_worker_rechecks_exact_consent_before_provider_io(test_client, db_session, api_setup, change):
    setup = api_setup
    view = await setup_consent(test_client, setup)
    grant = await db_session.get(CredentialServiceDelegation, view["delegation_id"])
    body = {"action": "link", "configuration_revision": 1, "native_resource_ref": grant.execution_scope["native_resource_ref"]}
    accepted = await test_client.post(setup.path + "/binding-operations", json=body, headers={"Idempotency-Key": "devops_adoption_recheck_01"})
    assert accepted.status_code == 202, accepted.text
    scope = deepcopy(grant.execution_scope)
    if change == "native_id":
        scope["native_resource_ref"]["record_id"] = "different-space"
    elif change == "configuration_space":
        from l5_services.models.configuration import ConfigurationVersion

        version = await db_session.get(ConfigurationVersion, (setup.tenant, setup.draft["configuration_id"], 1))
        version.settings = {**version.settings, "devops": {**version.settings["devops"], "existing_space_id": "different-space"}}
    elif change == "connection_revision":
        setup.fixture.credential.aws_verified_version_id = "changed"
    elif change == "missing_action":
        del scope["setup_action"]
    elif change == "missing_reference":
        del scope["native_resource_ref"]
    else:
        grant.revoked_at = datetime.now(UTC)
    grant.execution_scope = scope
    await db_session.commit()
    assert not await setup.worker.execute(setup.messages.pop())
    assert setup.native.mock_calls == []
    setup.assumed.assert_not_called()


@pytest.mark.parametrize("api_setup", ["adopt"], indirect=True)
@pytest.mark.parametrize("change", ["native_id", "connection_revision", "duplicate_original", "original_action"])
async def test_adoption_provenance_rejects_changed_or_ambiguous_original(test_client, db_session, api_setup, change):
    from l5_services.api.bindings import _intent_hash
    from l5_services.devops.setup import original_setup
    from l5_services.models.binding import Binding
    from l5_services.models.setup import BindingOperation, SetupIntent

    setup = api_setup
    _, completed = await complete_setup(test_client, setup)
    binding = await db_session.get(Binding, (setup.tenant, completed["binding_id"]))
    original = await db_session.get(SetupIntent, (setup.tenant, completed["operation_id"]))
    if change == "native_id":
        binding.native_resource_ref = {**binding.native_resource_ref, "record_id": "another-space"}
    elif change == "connection_revision":
        binding.connection_ref = {**binding.connection_ref, "record_revision": "another-version"}
    elif change == "original_action":
        original.request = {**original.request, "action": "create"}
    else:
        new_id = str(uuid4())

        def copy_row(row, **overrides):
            return type(row)(**{column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns} | overrides)

        duplicate = copy_row(original, operation_id=new_id, idempotency_key="another_original_01")
        duplicate.intent_hash = _intent_hash(duplicate)
        operation = await db_session.get(BindingOperation, (setup.tenant, completed["operation_id"]))
        receipt = await db_session.get(DevopsSpaceIntent, (setup.tenant, completed["operation_id"]))
        db_session.add_all(
            [duplicate, copy_row(operation, operation_id=new_id), copy_row(receipt, operation_id=new_id, intent_hash=duplicate.intent_hash)]
        )
    await db_session.commit()
    with pytest.raises(ValueError, match="setup_provenance_unavailable"):
        await original_setup(db_session, binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "enabled", "connection", "purpose", "operations", "membership"])
async def test_worker_rechecks_current_setup_consent_before_assuming_role(test_client, db_session, api_setup, l5_owner, change):
    from l5_services.models.configuration import ConfigurationVersion
    from sqlalchemy import delete

    from src.shared.models.organization import TeamMembership

    setup = api_setup
    view = await setup_consent(test_client, setup)
    accepted = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "devops_create_recheck_001"},
    )
    assert accepted.status_code == 202, accepted.text
    grant = await db_session.get(CredentialServiceDelegation, view["delegation_id"])
    if change == "revoke":
        grant.revoked_at = datetime.now(UTC)
    elif change == "enabled":
        version = await db_session.get(ConfigurationVersion, (setup.tenant, setup.draft["configuration_id"], 1))
        version.settings = {**version.settings, "enabled": True}
    elif change == "connection":
        setup.fixture.credential.aws_verified_version_id = "changed"
    elif change == "purpose":
        grant.execution_scope = {**grant.execution_scope, "purpose": "owned_cleanup"}
    elif change == "operations":
        grant.operations = [*grant.operations, "product:read"]
    else:
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
    await db_session.commit()
    assert not await setup.worker.execute(setup.messages.pop())
    assert setup.native.mock_calls == []
    setup.assumed.assert_not_called()
