"""Real signed JWT/app/SQL/Moto/Lua, explicit offline control/price substitutes.

No live registration, consent, provider success or complete accounting claimed.
"""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import delete, update

from src.agentauth.native_test_configuration import CONFIGURATION_KIND, COVERAGE_CONTRACT, NativeOperatorDeployment, deployment_key
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_enrollment import OPERATOR_AUDIENCE, OperatorLocalApproval, OperatorLocalApprovalRequest
from src.agentauth.native_test_interpreter_registration import compile_interpreter_contracts
from src.agentauth.native_test_interpreter_runtime import NativeInterpreterRuntime
from src.agentauth.test_run_admission_store import canonical, digest
from src.shared.models.organization import TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from tests.agentauth import test_native_test_runtime as base
from tests.agentauth.test_native_test_interpreter_registration import quote_inputs  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, runtime = base.admission, base.sessions, base.native, base.runtime


@pytest.fixture
async def operator(runtime, quote_inputs, monkeypatch):  # noqa: F811
    from src.agentauth import native_test_interpreter_runtime as module
    from src.internal import tools_authorize

    f, r = runtime.native, runtime
    f.fixture.client.delete_item(TableName=f.store.table, Key=f.store.fixture_key("jwt-subject"))
    tool_names = sorted("code_interpreter." + step for step in ("start", "execute", "result", "file"))
    async with f.sessions() as db:
        db.add_all(
            [ToolEntitlement(org_id="tenant-one", team_id="", tool_name=tool, enabled=True, max_cost_usd=Decimal("20")) for tool in tool_names]
        )
        await db.commit()
    policy = {
        **f.policy,
        "allowed_tools": tool_names,
        "consented_tools": tool_names,
        "tool_max_cost_usd": {tool: Decimal("20") for tool in tool_names},
    }
    f.fixture.client.put_item(TableName=f.store.table, Item={**f.policy_key, **{k: TypeSerializer().serialize(v) for k, v in policy.items()}})
    inputs, evidence = quote_inputs
    configuration = {
        "mcp": {
            "lambda": {
                "lambdaArn": inputs.binding.catalogue.lambda_arn,
                "toolSchema": {
                    "inlinePayload": [
                        {"name": step, "description": "Offline " + step, "inputSchema": {"type": "object"}}
                        for step in ("start", "execute", "result", "file")
                    ]
                },
            }
        }
    }
    evidence[digest(configuration)] = canonical(configuration).encode()
    catalogue = inputs.binding.catalogue.model_copy(update={"target_configuration_sha256": digest(configuration)})
    binding = inputs.binding.model_copy(update={"deployment_id": "offline-gateway", "authority_table": f.store.table, "catalogue": catalogue})
    now = int(f.fixture.clock[0].timestamp())
    inputs = inputs.model_copy(update={"binding": binding, "not_before": now - 1, "expires_at": now + 3600})
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    calls = []

    class Control:
        def get_gateway(self, **kwargs):
            calls.append("gateway")
            return {
                "gatewayArn": catalogue.gateway_arn,
                "gatewayId": catalogue.gateway_id,
                "name": "adp-tools",
                "status": "READY",
                "roleArn": catalogue.gateway_role_arn,
                "protocolType": "MCP",
                "authorizerType": "AWS_IAM",
            }

        def get_gateway_target(self, **kwargs):
            calls.append("target")
            return {
                "gatewayArn": catalogue.gateway_arn,
                "targetId": catalogue.target_id,
                "name": "code-interpreter",
                "status": "READY",
                "targetConfiguration": configuration,
                "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            }

    monkeypatch.setattr(module, "boto3", SimpleNamespace(client=lambda *a, **kw: Control()))
    r.app.include_router(tools_authorize.router, prefix="/internal/v1")
    bridge = NativeInterpreterRuntime(runtime=r.composition, budget=f.fixture.budget, binding=binding, execution_client=f.fixture.client)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", binding.facade_role_arn)
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", binding.reconciler_role_arn)
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", catalogue.gateway_role_arn)
    f.issuer._effect_runtime = bridge
    f.store._deployment_verifier.effect_coverage = bridge.coverage
    contracts = {}
    for effect in compiled["effects"]:
        doc = compiled["contracts"][effect["contract_id"]]
        source = ProtectedEffectQuoteAdapter(
            store=f.store,
            deployment_id=binding.deployment_id,
            contract_id=effect["contract_id"],
            contract_sha256=effect["contract_sha256"],
            source_sha256=binding.release_sha256,
        )
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(source.key, "native_test_effect_contract_v1", doc))
        contracts[effect["effect_key"].removeprefix("native-effect:interpreter-")] = source, doc
    deployed = NativeOperatorDeployment(
        deployment_id=binding.deployment_id,
        tenant_id="tenant-one",
        revision=str(uuid4()),
        state="active",
        scope="operator_local",
        cap_usd="20",
        coverage_contract=COVERAGE_CONTRACT,
        coverage_digest=r.composition.coverage(),
        expires_at=now + 3600,
        source_revision=binding.source_revision,
        release_sha256=binding.release_sha256,
        gateway_origin=binding.gateway_origin,
        contract_sha256=digest(binding.model_dump()),
        allowed_models=[],
        allowed_routes=[],
        allowed_tools=tool_names,
        effect_coverage_contract="adp-native-operator-interpreter/v1",
        effect_coverage_digest=bridge.coverage(),
        effects=compiled["effects"],
    )
    f.fixture.client.put_item(
        TableName=f.store.table, Item=f.store.item(deployment_key(binding.deployment_id), CONFIGURATION_KIND, deployed.model_dump())
    )
    request = OperatorLocalApprovalRequest(
        producer="operator_local",
        run_id=str(uuid4()),
        team_id="team-one",
        deployment_revision=deployed.revision,
        contract_sha256=deployed.contract_sha256,
        dispatch_expires_at=now + 300,
        cleanup_expires_at=now + 600,
        meter_expires_at=now + 86400,
    )
    r.binding, r.deployed, r.bridge, r.operator_request, r.contracts, r.control_calls = binding, deployed, bridge, request, contracts, calls
    return r


async def issue(f, client):
    headers = {"Authorization": "Bearer " + f.login()}
    approval = await client.post("/v1/operator-tools/approvals", json=f.operator_request.model_dump(), headers=headers)
    assert approval.status_code == 200, approval.text
    exchange = await client.post("/v1/operator-tools/capabilities/exchange", json={"run_id": f.operator_request.run_id}, headers=headers)
    assert exchange.status_code == 200, exchange.text
    return headers, approval, exchange


async def test_signed_human_endpoint_issues_original_setup_without_fixture_or_ci(operator):
    f, n = operator, operator.native
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        headers, reply, exchange = await issue(f, client)
        approval = n.store.approval("tenant-one", f.operator_request.run_id)
        assert type(approval) is OperatorLocalApproval and approval.principal.user_id == "canonical-human"
        assert approval.principal.subject_id == "jwt-subject" and approval.principal.team_id == "team-one"
        assert n.store.designation("jwt-subject") is None
        assert not {"job", "designation"} & approval.model_dump().keys()
        cap = n.store.capability("tenant-one", approval.credential_id)
        assert cap["audience"] == OPERATOR_AUDIENCE and "designation_digest" not in cap and "principal_digest" in cap
        assert n.store.authority("tenant-one", approval.run_id) == approval.authority()
        assert (await n.fixture.reservations.snapshot(n.service._target(approval.authority()))).total_usd == Decimal("0.01")
        assert reply.json()["reference"] == exchange.json()["reference"]
        assert exchange.headers["cache-control"] == "no-store" and exchange.json()["capability"] not in canonical(cap)
        assert not f.authority_calls and not f.provider_calls
        repeat = await client.post("/v1/operator-tools/capabilities/exchange", json={"run_id": approval.run_id}, headers=headers)
        assert repeat.status_code == 403
        reset = await client.post("/v1/operator-tools/approvals", json={**f.operator_request.model_dump(), "run_id": str(uuid4())}, headers=headers)
        assert reset.status_code == 403
        assert (await n.fixture.reservations.snapshot(n.service._target(approval.authority()))).total_usd == Decimal("0.01")
        ci = await client.post(
            "/v1/native-tests/capabilities/exchange",
            json={"run_id": approval.run_id, "github_oidc_token": "forbidden", "producer_proof": "forbidden"},
            headers=headers,
        )
        assert ci.status_code == 403 and not f.authority_calls


@pytest.mark.parametrize("change", ["other_human", "team", "consent", "shadow", "membership", "expired", "source", "deployment", "windows", "job"])
async def test_untrusted_or_stale_authority_never_creates_operator_approval(operator, change):
    f, n, body = operator, operator.native, operator.operator_request.model_dump()
    login = f.login()
    if change == "other_human":
        login = f.login("owner-subject")
    elif change == "team":
        body["team_id"] = "foreign-team"
    elif change == "consent":
        n.fixture.client.update_item(
            TableName=n.store.table, Key=n.policy_key, UpdateExpression="SET consented_tools = :none", ExpressionAttributeValues={":none": {"L": []}}
        )
    elif change in {"shadow", "membership"}:
        async with n.sessions() as db:
            await db.execute(
                update(User).where(User.id == "canonical-human").values(is_shadow=True)
                if change == "shadow"
                else delete(TeamMembership).where(TeamMembership.user_id == "canonical-human")
            )
            await db.commit()
    elif change == "expired":
        login = f.login(exp=1)
    elif change == "source":
        body["contract_sha256"] = "f" * 64
    elif change == "deployment":
        body["deployment_revision"] = str(uuid4())
    elif change == "windows":
        body["dispatch_expires_at"] += 1
    elif change == "job":
        body["job"] = {"repository": "fake"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        reply = await client.post("/v1/operator-tools/approvals", json=body, headers={"Authorization": "Bearer " + login})
    assert reply.status_code in {400, 401, 403}, reply.text
    assert n.store.document(n.store.key("tenant-one", "APPROVAL", body["run_id"]), "native_test_approval_v1") is None
    assert not f.provider_calls and not f.authority_calls


@pytest.mark.parametrize("drift", ["contract", "source", "component", "payload"])
async def test_incomplete_protected_cost_or_payload_refuses_before_approval(operator, drift):
    f, n = operator, operator.native
    source, doc = f.contracts["execute_status"]
    if drift == "contract":
        n.fixture.client.delete_item(TableName=n.store.table, Key=source.key)
    else:
        doc = dict(doc)
        if drift == "source":
            doc["source_sha256"] = "e" * 64
        elif drift == "component":
            doc["components"] = doc["components"][:-1]
        else:
            doc["request_sha256"] = "e" * 64
        n.fixture.client.put_item(TableName=n.store.table, Item=n.store.item(source.key, "native_test_effect_contract_v1", doc))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        reply = await client.post(
            "/v1/operator-tools/approvals", json=f.operator_request.model_dump(), headers={"Authorization": "Bearer " + f.login()}
        )
    assert reply.status_code == 403
    assert n.store.document(n.store.key("tenant-one", "APPROVAL", f.operator_request.run_id), "native_test_approval_v1") is None


async def test_lost_operator_activation_ack_retains_one_original_setup_hold(operator, monkeypatch):
    f, n = operator, operator.native
    original = n.fixture.client.transact_write_items

    def lost_ack(**kwargs):
        result = original(**kwargs)
        if any('"status":"active"' in item.get("Put", {}).get("Item", {}).get("document", {}).get("S", "") for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="offline")
        return result

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        headers = {"Authorization": "Bearer " + f.login()}
        reply = await client.post("/v1/operator-tools/approvals", json=f.operator_request.model_dump(), headers=headers)
        assert reply.status_code == 200, reply.text
        monkeypatch.setattr(n.fixture.client, "transact_write_items", lost_ack)
        for _ in range(2):
            reply = await client.post("/v1/operator-tools/capabilities/exchange", json={"run_id": f.operator_request.run_id}, headers=headers)
            assert reply.status_code == 403 and reply.json()["error"] == "native_test_write_unconfirmed"
    approval = n.store.approval("tenant-one", f.operator_request.run_id)
    assert n.store.capability("tenant-one", approval.credential_id)["status"] == "active"
    assert (await n.fixture.reservations.snapshot(n.service._target(approval.authority()))).total_usd == Decimal("0.01")


async def test_revoked_dispatch_keeps_original_status_hold_and_rechecks_current_owner(operator):
    from src.agentauth.native_test_enrollment import CAPABILITY_HEADER

    f, n = operator, operator.native
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        headers, _, exchange = await issue(f, client)
        run_id = f.operator_request.run_id
        capability = exchange.json()["capability"]
        reply = await client.post("/v1/operator-tools/capabilities/revoke", json={"run_id": run_id}, headers=headers)
        assert reply.status_code == 200
        scoped = {**headers, CAPABILITY_HEADER: capability}
        reply = await client.get(f"/v1/operator-tools/runs/{run_id}/status", headers=scoped)
        assert reply.status_code == 200, reply.text
        assert reply.json()["funding_status"] == "held" and reply.json()["accounting_status"] == "pending"
        assert reply.json()["principal"] == {"user_id": "canonical-human", "tenant_id": "tenant-one"}
        assert reply.json()["governance_complete"] is False and Decimal(reply.json()["held_usd"]) == Decimal("0.01")
        wrong = await client.get(
            f"/v1/operator-tools/runs/{run_id}/status", headers={"Authorization": "Bearer " + f.login("owner-subject"), CAPABILITY_HEADER: capability}
        )
        assert wrong.status_code == 403
        async with n.sessions() as db:
            await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "canonical-human"))
            await db.commit()
        stale = await client.get(f"/v1/operator-tools/runs/{run_id}/status", headers=scoped)
        assert stale.status_code == 403
        approval = n.store.approval("tenant-one", run_id)
        assert (await n.fixture.reservations.snapshot(n.service._target(approval.authority()))).total_usd == Decimal("0.01")


async def test_verified_workspace_link_uses_canonical_consent_without_impersonation(operator):
    from src.shared.models.organization import Organization, Team
    from src.shared.models.vault import UserIdentity

    f, n = operator, operator.native
    async with n.sessions() as db:
        await db.execute(update(User).where(User.id == "canonical-human").values(cognito_sub=None))
        db.add(Organization(id="login-org", name="login"))
        await db.flush()
        db.add(Team(id="login-team", org_id="login-org", department_id="login-dept", name="login"))
        await db.flush()
        db.add(User(id="login-row", org_id="login-org", team_id="login-team", cognito_sub="jwt-subject", email="login@example.test"))
        db.add(
            UserIdentity(
                user_id="canonical-human",
                org_id="tenant-one",
                team_id="team-one",
                provider="cognito",
                provider_user_id="jwt-subject",
                verification_method="org_placement",
                verified_at=n.fixture.clock[0],
            )
        )
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=f.binding.gateway_origin) as client:
        headers, _, _ = await issue(f, client)
        approval = n.store.approval("tenant-one", f.operator_request.run_id)
        assert approval.principal.user_id == "canonical-human" and approval.principal.subject_id == "jwt-subject"
        reply = await client.post("/v1/operator-tools/capabilities/revoke", json={"run_id": approval.run_id}, headers=headers)
        assert reply.status_code == 200, reply.text
