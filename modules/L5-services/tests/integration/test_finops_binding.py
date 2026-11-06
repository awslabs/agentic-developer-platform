"""Exercise registry, read-only adoption and admission through the normal Product API."""

import io
import json
import asyncio
from json import loads
from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from l5_services import runtime_finops, runtime_support
from l5_services.models import Binding, SetupWork
from l5_services.models.run import Run
from l5_services.models.work import Work
from src import l5_integration
from src.auth.aws_connection_authority import connection_binding
from src.internal.l5_credential_routes import CredentialSessionRequest, get_l5_work_authorizer
from src.l5_backend_finops import FinOpsAuthority
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from test_configuration import configuration_scenario as _configuration_scenario

configuration_scenario = _configuration_scenario


@pytest.fixture
async def adopted_scenario(configuration_scenario, app, monkeypatch):
    scenario = configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "finops-adoption", "internal", ["finops"])
    client = scenario["client"]
    path = product["path"].replace("/security/", "/finops/").removesuffix("/configuration")
    scenario["typed_connections"].update(enabled=True, provider="aws", project_id=product["project_id"])
    scenario["permissions"][(actor.org_id, product["project_id"])].clear()
    profile = (await client.get(product["profile_path"])).json()
    changed = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
        **profile["configuration"], "basic": {}}}, headers={"Idempotency-Key": "finops_remove_sources_0001",
        "If-Profile-Revision": str(profile["revision"])})
    assert changed.status_code == 200, changed.text
    connection_id = f"{actor.org_id}-connection-one"
    connection = {"tenant_id": actor.org_id, "project_id": product["project_id"],
                  "source_connection_id": connection_id, "record_type": "connection",
                  "record_id": connection_id, "record_revision": "revision-1"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Example", "region": "us-east-1"}
    view = "arn:aws:billing::123456789012:billingview/example-view"
    role = "arn:aws:iam::123456789012:role/ExampleData"
    async with scenario["sessions"]() as session:
        credential = UserCredential(id=connection_id, org_id=actor.org_id, user_id=actor.user_id,
            service="aws", credential_type="aws_role", label="Example", agent_consent=True, strict=False,
            secret_arn="fictional-secret", aws_external_id="fictional-external", aws_verified_at=datetime.now(UTC),
            aws_verification_attempt="verification-one", aws_verified_version_id="revision-1",
            scopes={**identity, "source": "imported_role", "status": "verified"})
        credential.aws_verified_binding = connection_binding(credential)
        root = CredentialServiceDelegation(id="finops-root-example", tenant_id=actor.org_id,
            credential_id=connection_id, service_id="l5-finops-worker", product_id=product["product_id"],
            project_id=product["project_id"], created_by=actor.user_id, capabilities=["cost_inquiry"],
            operations=["product:read", "credential:session"], sources=[{**connection, "record_type": "credential"}],
            visibility="restricted", binding_generation=1, expires_at=datetime.now(UTC) + timedelta(hours=4))
        budget = BudgetConfig(id="finops-budget-example", org_id=actor.org_id, entity_type="org", entity_id=actor.org_id,
                              period_type="monthly", budget_amount_usd=Decimal("2000"), enforcement_mode="hard")
        session.add_all([credential, root, budget])
        await session.commit()
    limits = {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 4,
              "concurrency": 5, "max_cost": "300.00", "currency": "USD",
              "policy_ref": {**connection, "record_type": "policy", "record_id": "finops-budget-example"}}
    selected = {"service_id": "finops", "capabilities": ["cost_inquiry"], "enabled": True,
                "connection_ref": connection, "region": "us-east-1", "limits": limits,
                "overrides": [{"field": "connection_ref", "value": connection, "reason": "Selected"},
                              {"field": "region", "value": "us-east-1", "reason": "Selected"}],
                "finops": {"billing_scope": {"account_ids": [identity["account_id"]], "view_ref": view},
                           "agent_mode": "adopt", "native_agent_ref": "agent-example",
                           "native_agent_display_name": "Example Agent", "data_role_ref": role}}
    saved = await client.put(path + "/configuration", json=selected, headers={
        "Idempotency-Key": "finops_adoption_config_0001", "If-None-Match": "*"})
    assert saved.status_code == 201, saved.text
    assert isinstance(app.state.l5_finops_authority, FinOpsAuthority)
    assert saved.json()["state"] != "ready"
    observations, policies, messages = [], [], []
    loop = asyncio.get_running_loop()
    fault = {"native": None, "broker": None, "view": None, "archive": None}

    class Transport:
        async def post(self, route, *, json):
            assert route == "/internal/v1/l5/credential-session"
            async with scenario["sessions"]() as session:
                grant = await session.get(CredentialServiceDelegation, json["delegation_id"])
                try:
                    permit = await get_l5_work_authorizer(session)(grant, CredentialSessionRequest(**json))
                except HTTPException as error:
                    return SimpleNamespace(status_code=error.status_code)
            policies.append(loads(permit.session_policy))
            result = {"operation_id": json["operation_id"], "attempt_id": json["attempt_id"],
                      "delegation_revision": json["delegation_revision"],
                      "provider_identity": {**identity, "verified_at": datetime.now(UTC).isoformat()},
                      "expires_at": permit.expires_at.isoformat(), "setup_verified": permit.setup_verified,
                      "access_key_id": "example-key", "secret_access_key": "example-secret", "session_token": "example-token"}
            if fault["broker"]:
                result["provider_identity"] = fault["broker"]
            return SimpleNamespace(status_code=200, json=lambda: result, raise_for_status=lambda: None)

    class Provider:
        def get_billing_view(self, **arguments):
            observations.append(("billing", arguments))
            return {"billingView": fault["view"] or {"arn": view, "ownerAccountId": identity["account_id"],
                "sourceAccountId": identity["account_id"],
                "billingViewType": "CUSTOM", "dataFilterExpression": {"dimensions": {"key": "LINKED_ACCOUNT", "values": [identity["account_id"]]}},
                "healthStatus": {"statusCode": "HEALTHY"}}}

        def get_role(self, **arguments):
            observations.append(("role", arguments))
            return {"Role": {"Arn": role}}

        def list_role_policies(self, **arguments):
            return {"PolicyNames": ["ExampleBilling"], "IsTruncated": False}

        def get_role_policy(self, **arguments):
            return {"PolicyDocument": {"Statement": [
                {"Effect": "Allow", "Action": ["ce:GetCostAndUsage", "ce:GetDimensionValues"], "Resource": view},
                {"Effect": "Deny", "Action": "ce:*", "NotResource": view}]}}

        def simulate_principal_policy(self, **arguments):
            observations.append(("permissions", arguments))
            assert arguments["PolicySourceArn"] == role and arguments["ResourceArns"] == [view]
            return {"EvaluationResults": [{"EvalActionName": action, "EvalDecision": "allowed",
                "ResourceSpecificResults": [{"EvalResourceName": view, "EvalResourceDecision": "allowed",
                                             "MissingContextValues": []}]} for action in arguments["ActionNames"]]}

        def list_attached_role_policies(self, **arguments):
            return {"AttachedPolicies": [], "IsTruncated": False}

        def put_object(self, **arguments):
            observations.append(("archive", arguments))
            return {} if fault["archive"] else {"VersionId": "version-example"}

        def invoke(self, **arguments):
            request = json.loads(arguments["Payload"])
            assert request["action"] == "read_agent"
            from l5_services.adapters.finops.executor_host import verify_executor_setup

            verification = asyncio.run_coroutine_threadsafe(
                verify_executor_setup(request["authority"], scenario["sessions"], transport), loop)
            verified = verification.result(timeout=10)
            assert verified["native_resource_ref"] == request["native_job_ref"]
            observations.append(("native", request))
            result = fault["native"] or {"state": "ready", "native_agent_display_name": "Example Agent",
                "data_role_ref": role, "native_resource_ref": request["native_job_ref"], "provider_identity": identity}
            return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps({"action": request["action"],
                "operation_id": request["authority"]["operation_id"], "attempt_id": request["authority"]["attempt_id"], "result": result}).encode())}

    provider = Provider()
    transport = Transport()

    async def publish(envelope, delay):
        messages.append(envelope)

    monkeypatch.setattr(l5_integration, "service_publisher", lambda service: publish)
    monkeypatch.setattr(runtime_support, "sessions", lambda: scenario["sessions"])
    monkeypatch.setattr(runtime_support, "gateway_transport", lambda: transport)
    monkeypatch.setattr(runtime_support, "archive_store", lambda service: SimpleNamespace(client=provider, bucket="example-private-bucket"))
    monkeypatch.setattr(runtime_support, "queue_publisher", lambda service: publish)
    monkeypatch.setattr(runtime_support, "provider_session", lambda result, region: SimpleNamespace(client=lambda *args, **kwargs: provider))
    monkeypatch.setattr(runtime_finops.boto3, "client", lambda *args, **kwargs: provider)
    for name, value in {"L5_SERVICE_ID": "finops", "AWS_REGION": "us-east-1", "L5_ARCHIVE_BUCKET": "example-private-bucket",
                        "L5_FINOPS_EXECUTOR_ARN": "arn:aws:lambda:us-east-1:123456789012:function:example-l5-finops-executor",
                        "L5_FINOPS_ADMISSION_ENABLED": "true"}.items():
        monkeypatch.setenv(name, value)
    worker = runtime_finops.factory()
    native_ref = {**connection, "record_type": "finops_agent", "record_id": "agent-example"}

    async def adopt(revision, receipt):
        response = await client.post(path + "/binding-operations", json={"action": "link",
            "configuration_revision": revision, "native_resource_ref": native_ref}, headers={"Idempotency-Key": receipt})
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "accepted"
        async with scenario["sessions"]() as session:
            work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == response.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await worker.execute(envelope)
        assert not await worker.execute(envelope)
        return (await client.get(path + "/binding-operations/" + response.json()["operation_id"])).json()

    return {**scenario, "path": path, "product": product, "selected": selected, "saved": saved,
            "adopt": adopt, "worker": worker, "transport": transport, "fault": fault,
            "identity": identity, "observations": observations, "policies": policies}


@pytest.mark.asyncio
async def test_normal_gateway_adoption_and_admission(adopted_scenario):
    scenario = adopted_scenario
    client, path = scenario["client"], scenario["path"]
    operation = await scenario["adopt"](1, "finops_adopt_first_0001")
    assert operation["state"] == "succeeded"
    binding_id = operation["binding_id"]
    binding_response = await client.get(path + "/bindings/" + binding_id)
    binding = binding_response.json()
    assert binding["state"] == "ready" and binding["native_agent_display_name"] == "Example Agent"
    assert "native_readback" not in json.dumps(binding)
    from jsonschema import Draft202012Validator, FormatChecker

    schemas = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    Draft202012Validator({"$defs": schemas["$defs"], "$ref": "#/$defs/Binding"}).validate(binding)
    tools = {tool["x-http-operation"]: tool for tool in json.loads(
        (Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/mcp-tools.json").read_text())}
    for name, response in (("saveConfiguration", scenario["saved"]), ("getBinding", binding_response)):
        Draft202012Validator(tools[name]["outputSchema"], format_checker=FormatChecker()).validate(
            {"data": response.json(), "http_status": response.status_code, "etag": response.headers["etag"]})
    binding_validator = Draft202012Validator(tools["getBinding"]["outputSchema"])
    assert not binding_validator.is_valid({"data": {**binding, "provider_identity": {
        **binding["provider_identity"], "session_token": "must-not-appear"}}, "http_status": 200,
        "etag": binding_response.headers["etag"]})
    saved = await client.get(path + "/configuration")
    selected = {**scenario["selected"], "triggers": [{"trigger_id": "manual-example", "kind": "manual",
        "enabled": True, "capability": "cost_inquiry", "binding_id": binding_id, "source_refs": []}]}
    changed = await client.put(path + "/configuration", json=selected, headers={
        "Idempotency-Key": "finops_enable_trigger_0001", "If-Match": saved.headers["etag"]})
    assert changed.status_code == 200, changed.text
    Draft202012Validator(tools["saveConfiguration"]["inputSchema"], format_checker=FormatChecker()).validate({
        "product_id": binding["scope"]["product_id"], "project_id": binding["scope"]["project_id"],
        "service_id": "finops", "input": selected, "idempotency_key": "finops_enable_trigger_0001",
        "if_match": saved.headers["etag"]})
    Draft202012Validator(tools["saveConfiguration"]["outputSchema"], format_checker=FormatChecker()).validate({
        "data": changed.json(), "http_status": changed.status_code, "etag": changed.headers["etag"]})
    revision = changed.json()["revision"]
    operation = await scenario["adopt"](revision, "finops_readback_current_0001")
    assert operation["binding_id"] == binding_id
    request = {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": revision,
               "binding_id": binding_id, "question": "What was spent?", "query": {"start": "2026-01-01", "end": "2026-02-01",
               "metric": "UnblendedCost", "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}}
    response = await client.post(path + "/runs", json=request, headers={"Idempotency-Key": "finops_actual_admission_0001"})
    assert response.status_code == 202, response.text
    replay = await client.post(path + "/runs", json=request, headers={"Idempotency-Key": "finops_actual_admission_0001"})
    assert replay.status_code == 202 and replay.json()["run_id"] == response.json()["run_id"]
    run_response = await client.get(path + "/runs/" + response.json()["run_id"])
    assert run_response.status_code == 200, run_response.text
    Draft202012Validator(tools["getRun"]["outputSchema"], format_checker=FormatChecker()).validate({
        "data": run_response.json(), "http_status": run_response.status_code})
    actor = scenario["actors"][0]
    async with scenario["sessions"]() as session:
        work = await session.scalar(select(Work).where(Work.run_id == response.json()["run_id"], Work.kind == "execute"))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
    assert await scenario["worker"].execute(envelope)
    async with scenario["sessions"]() as session:
        run = await session.get(Run, (actor.org_id, response.json()["run_id"]))
        assert run.admission["provider_identity"] == scenario["identity"]
        assert run.admission["limits"]["max_cost"] == "300.00"
        assert run.admission["limits"]["concurrency"] == 5
        work = await session.scalar(select(Work).where(Work.run_id == run.run_id, Work.kind == "submit"))
        assert work is not None, (run.state, run.provider_state)
        work.lease_owner, work.fence, work.lease_until = "worker-example", 1, datetime.now(UTC) + timedelta(minutes=2)
        await session.commit()
        claim = SimpleNamespace(work_id=work.work_id, owner=work.lease_owner, fence=work.fence, generation=work.generation, kind=work.kind)
        authority = await scenario["worker"].authorize(run, claim, "submit")
        assert authority["provider_identity"] == scenario["identity"]
        budget = await session.get(BudgetConfig, "finops-budget-example")
        budget.budget_amount_usd = Decimal("300.00")
        await session.commit()
        exhausted = await client.post(path + "/runs", json=request, headers={"Idempotency-Key": "finops_budget_admission_0001"})
        assert exhausted.status_code == 409 and exhausted.json()["detail"]["code"] == "budget_exhausted"
        budget.budget_amount_usd = Decimal("2000.00")
        root = await session.get(CredentialServiceDelegation, "finops-root-example")
        root.revoked_at = datetime.now(UTC)
        await session.commit()
        from l5_services.workers.execution import AuthorityRevoked

        with pytest.raises(AuthorityRevoked):
            await scenario["worker"].authorize(run, claim, "submit")
    denied = await client.post(path + "/runs", json=request, headers={"Idempotency-Key": "finops_revoked_admission_0001"})
    assert denied.status_code == 403
    assert [kind for kind, _ in scenario["observations"]].count("native") == 2
    actions = {action for policy in scenario["policies"] for statement in policy["Statement"] for action in statement["Action"]}
    assert {"ce:GetCostAndUsage", "billing:GetBillingView", "iam:SimulatePrincipalPolicy"} <= actions
    simulations = [statement for policy in scenario["policies"] for statement in policy["Statement"]
                   if "iam:SimulatePrincipalPolicy" in statement["Action"]]
    assert simulations
    assert all(statement["Resource"] == scenario["selected"]["finops"]["data_role_ref"] for statement in simulations)
    assert not any("iam:SimulatePrincipalPolicy" in statement["Action"] for statement in scenario["policies"][-1]["Statement"])
    assert not any("Delete" in action or "Accept" in action for action in actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("native", "view", "archive", "primary"))
async def test_adoption_denies_wrong_or_unversioned_provider_readback(adopted_scenario, boundary):
    scenario = adopted_scenario
    scenario["fault"][boundary] = True if boundary == "archive" else {"state": "ready", "view_ref": "foreign-view"}
    if boundary == "primary":
        scenario["fault"]["view"] = {"arn": scenario["selected"]["finops"]["billing_scope"]["view_ref"],
            "billingViewType": "PRIMARY", "ownerAccountId": scenario["identity"]["account_id"],
            "healthStatus": {"statusCode": "HEALTHY"}}
    with pytest.raises((HTTPException, ValueError)):
        await scenario["adopt"](1, "finops_denied_readback_0001")
    async with scenario["sessions"]() as session:
        assert not (await session.scalars(select(Binding))).all()
    assert [kind for kind, _ in scenario["observations"]].count("native") == 1


@pytest.mark.asyncio
async def test_adoption_rejects_foreign_scope_and_revoked_connection(adopted_scenario):
    scenario = adopted_scenario
    client, path = scenario["client"], scenario["path"]
    reference = {**scenario["selected"]["connection_ref"], "record_type": "finops_agent", "record_id": "agent-example"}
    body = {"action": "link", "configuration_revision": 1, "native_resource_ref": reference}
    foreign = await client.post(path + "/binding-operations", json={**body, "native_resource_ref": {
        **reference, "project_id": "foreign-project"}}, headers={"Idempotency-Key": "finops_foreign_link_0001"})
    assert foreign.status_code == 404
    async with scenario["sessions"]() as session:
        grant = await session.get(CredentialServiceDelegation, "finops-root-example")
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    denied = await client.post(path + "/binding-operations", json=body, headers={"Idempotency-Key": "finops_revoked_link_0001"})
    assert denied.status_code == 403
    assert not scenario["observations"]
