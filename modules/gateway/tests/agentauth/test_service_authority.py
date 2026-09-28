"""Human approval -> native EventBridge writer -> bound worker -> child dispatch."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest, issue_bound_credential
from src.agentauth.dispatch import DispatchRequest
from src.agentauth.routes import AgentRuntime
from src.agentauth.service_authority import ServiceApproval, ServiceAuthorityStore, get_service_authorities, router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_human_dispatch import child_dispatch as child_fixture
from tests.agentauth.test_human_dispatch import store as store_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "agent-factory/webhook-ingress/lambda"))
from common.agent_authority import AuthorityProvisionError  # noqa: E402
from common.service_authority import VerifiedServiceEvent, provision_service_dispatch  # noqa: E402
from common.service_identity import ServiceIdentityResult  # noqa: E402

store = store_fixture
child_dispatch = child_fixture
SERVICE = "eventbridge:adp-dev-security-agent-dispatch"
RULE = "arn:aws:events:us-east-1:123456789012:rule/adp-dev-security-agent-dispatch"


@pytest.fixture
async def approval_context(store, child_dispatch, monkeypatch):
    store.client.create_table(
        TableName="identities",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "identity_type", "KeyType": "HASH"}, {"AttributeName": "identity_value", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "identity_type", "AttributeType": "S"}, {"AttributeName": "identity_value", "AttributeType": "S"}],
    )
    store.client.put_item(
        TableName="identities",
        Item={
            "identity_type": {"S": "service_account"},
            "identity_value": {"S": SERVICE},
            "tenant_id": {"S": "tenant"},
            "org_id": {"S": "tenant"},
            "repo": {"S": "org/repo"},
            "rule_arn": {"S": RULE},
            "allowed_personas": {"L": [{"S": "operations"}]},
            "allowed_child_personas": {"SS": ["operations", "developer", "reviewer"]},
        },
    )
    user = TokenContext(
        user_id="approver",
        org_id="tenant",
        team_id="",
        department_id="",
        account_type="human",
        auth_source="jwt",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    body = ServiceApproval(
        request_id=uuid4(),
        service_identity=SERVICE,
        repo="org/repo",
        root_personas={"operations"},
        child_personas={"operations", "developer", "reviewer"},
        child_issue_scope="repository",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    authority = ServiceAuthorityStore(store=store, identity_table="identities")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_service_authorities] = lambda: authority
    permission = AsyncMock()
    monkeypatch.setattr("src.agentauth.service_authority.AccessControl.check_permission", permission)
    identity = ServiceIdentityResult("tenant", "tenant", SERVICE, ["operations"], "org/repo", RULE)
    event = {"id": str(uuid4()), "account": "123456789012", "adp_rule_arn": RULE, "source": "adp.security-agent"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as http:
        yield SimpleNamespace(
            store=store,
            child=child_dispatch,
            authority=authority,
            user=user,
            body=body,
            http=http,
            permission=permission,
            identity=identity,
            event=event,
        )


async def approve(ctx, **changes):
    payload = ctx.body.model_dump(mode="json")
    payload.update(changes)
    return await ctx.http.post("/agent-authorities/service-grants", json=payload)


def provision(ctx, **changes):
    event = VerifiedServiceEvent.from_native_event(event=ctx.event, identity=ctx.identity)
    envelope = {
        "message_id": "untrusted-random-id",
        "tenant_id": "tenant",
        "persona": "operations",
        "arrived_at": "2026-09-13T00:00:00Z",
        "source_ref": {"repo": "org/repo", "issue": 42, "installation_id": 123},
        "actor": {"user_id": SERVICE, "org_id": "tenant", "kind": "service", "is_bot": True},
        "correlation": {"parent_invocation_id": "forged-parent", "root_human_id": "forged-human"},
    }
    envelope.update(changes)
    return provision_service_dispatch(envelope=envelope, event=event, client=ctx.store.client)


def bind(ctx, envelope):
    from src.agentauth.workload import VerifiedPod

    pod = VerifiedPod("service-pod", "service-worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.9")
    record = ctx.store.bind(invocation_id=envelope["message_id"], digest=envelope_digest(envelope), pod=pod, now=datetime.now(UTC))
    env = {"AGENT_RUN_CREDENTIAL_KEY": "gateway-test-key-not-shared-with-workers"}
    return record, issue_bound_credential(record, now=datetime.now(UTC), env=env)["credential"]


async def test_human_approved_service_preserves_actual_actor_and_dispatches_bounded_children(approval_context):
    ctx = approval_context
    response = await approve(ctx)
    assert response.status_code == 200, response.text
    ctx.permission.assert_awaited_once()
    envelope = provision(ctx)
    assert provision(ctx) == envelope
    assert envelope["actor"]["user_id"] == SERVICE and envelope["actor"]["kind"] == "service"
    assert envelope["correlation"]["root_human_id"] == "approver"
    assert envelope["correlation"]["parent_invocation_id"] is None
    record, credential = bind(ctx, envelope)
    grant = ctx.store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert grant.authority.kind == "service_policy"
    await AgentRuntime(store=ctx.store, workloads=None).validate_flow(record, grant)
    body = DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 99}, request_id="night-story")
    result = ctx.child.service.dispatch(body=body, credential_token=credential, workload_binding="service-pod")
    assert ctx.child.service.dispatch(body=body, credential_token=credential, workload_binding="service-pod") == result
    child = ctx.store.authority.load_execution(invocation_id=result["invocation_id"], tenant_id="tenant")
    assert child.parent_principal == record.principal
    assert child.flow_id == record.flow_id


@pytest.mark.parametrize("kind,source,org", [("service", "jwt", "tenant"), ("human", "iam", "tenant"), ("human", "jwt", "")])
async def test_nonhuman_or_unscoped_caller_cannot_approve(approval_context, kind, source, org):
    ctx = approval_context
    ctx.user.account_type, ctx.user.auth_source, ctx.user.org_id = kind, source, org
    assert (await approve(ctx)).status_code == 403
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None
    ctx.permission.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"repo": "other/repo"}, {"root_personas": ["developer"]}, {"tenant_id": "other"}, {"human_id": "forged"}])
async def test_approval_cannot_claim_unregistered_scope_or_identity(approval_context, changes):
    assert (await approve(approval_context, **changes)).status_code in {404, 422}


@pytest.mark.parametrize("scope", [None, {"L": []}, {"SS": ["reviewer"]}, {"L": [{"S": "reviewer"}]}])
async def test_child_delegation_cannot_exceed_registered_ceiling(approval_context, scope):
    ctx = approval_context
    key = {"identity_type": {"S": "service_account"}, "identity_value": {"S": SERVICE}}
    args = {"TableName": "identities", "Key": key}
    if scope is None:
        ctx.store.client.update_item(**args, UpdateExpression="REMOVE allowed_child_personas")
    else:
        ctx.store.client.update_item(**args, UpdateExpression="SET allowed_child_personas = :scope", ExpressionAttributeValues={":scope": scope})
    assert (await approve(ctx)).status_code == 404
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None
    assert (await approve(ctx, child_personas=[])).status_code == 200


@pytest.mark.parametrize("initially_present", [False, True])
async def test_registration_change_during_approval_cannot_commit(approval_context, monkeypatch, initially_present):
    ctx = approval_context
    key = {"identity_type": {"S": "service_account"}, "identity_value": {"S": SERVICE}}
    if not initially_present:
        ctx.store.client.update_item(TableName="identities", Key=key, UpdateExpression="REMOVE allowed_child_personas")
    original = ctx.store.client.transact_write_items

    def race(**kwargs):
        if initially_present:
            ctx.store.client.update_item(TableName="identities", Key=key, UpdateExpression="REMOVE allowed_child_personas")
        else:
            ctx.store.client.update_item(
                TableName="identities",
                Key=key,
                UpdateExpression="SET allowed_child_personas = :scope",
                ExpressionAttributeValues={":scope": {"SS": ["reviewer"]}},
            )
        return original(**kwargs)

    monkeypatch.setattr(ctx.store.client, "transact_write_items", race)
    changes = {} if initially_present else {"child_personas": []}
    assert (await approve(ctx, **changes)).status_code == 409
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None


async def test_approval_permission_denial_writes_nothing(approval_context):
    from fastapi import HTTPException

    ctx = approval_context
    ctx.permission.side_effect = HTTPException(403, "permission denied")
    assert (await approve(ctx)).status_code == 403
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None


async def test_service_without_human_approval_cannot_mint_authority(approval_context):
    with pytest.raises(AuthorityProvisionError):
        provision(approval_context)


@pytest.mark.parametrize("field,value", [("id", "not-uuid"), ("account", "other"), ("adp_rule_arn", "other-rule"), ("headers", {})])
async def test_unverified_native_source_is_refused(approval_context, field, value):
    ctx = approval_context
    ctx.event[field] = value
    with pytest.raises(AuthorityProvisionError):
        VerifiedServiceEvent.from_native_event(event=ctx.event, identity=ctx.identity)


async def test_revoke_stops_existing_service_and_children(approval_context):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    envelope = provision(ctx)
    record, credential = bind(ctx, envelope)
    body = DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 99}, request_id="night-story")
    child = ctx.child.service.dispatch(body=body, credential_token=credential, workload_binding="service-pod")
    assert (await ctx.http.post(f"/agent-authorities/service-grants/{reference}/revoke")).status_code == 200
    for invocation in [record.invocation_id, child["invocation_id"]]:
        with pytest.raises(BootstrapRefusedError):
            ctx.store.live_grant(invocation_id=invocation, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    with pytest.raises(AuthorityProvisionError):
        provision(ctx)


async def test_policy_replacement_is_conditional_and_revokes_old_runs(approval_context):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    envelope = provision(ctx)
    record, _ = bind(ctx, envelope)
    conflict = await approve(ctx, request_id=str(uuid4()), child_issue_scope="same_issue")
    assert conflict.status_code == 409
    replaced = await approve(ctx, request_id=str(uuid4()), replaces=reference, child_issue_scope="same_issue")
    assert replaced.status_code == 200, replaced.text
    with pytest.raises(BootstrapRefusedError):
        ctx.store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert (await approve(ctx)).status_code == 409


async def test_lost_approval_and_dispatch_replies_recover_same_records(approval_context, monkeypatch):
    ctx = approval_context
    original = ctx.store.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://store.test")

    monkeypatch.setattr(ctx.store.client, "transact_write_items", lost)
    first = await approve(ctx)
    assert first.status_code == 200, first.text
    assert (await approve(ctx)).json() == first.json()
    envelope = provision(ctx)
    assert provision(ctx) == envelope


async def test_explicit_same_issue_scope_blocks_cross_issue_dispatch(approval_context):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = approval_context
    assert (await approve(ctx, child_issue_scope="same_issue")).status_code == 200
    _, credential = bind(ctx, provision(ctx))
    with pytest.raises(BootstrapRefusedError):
        ctx.child.service.dispatch(
            body=DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 99}, request_id="cross-issue"),
            credential_token=credential,
            workload_binding="service-pod",
        )


async def test_revocation_recovers_lost_reply_and_preserves_original_actor(approval_context, monkeypatch):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    original = ctx.store.client.update_item

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://store.test")

    monkeypatch.setattr(ctx.store.client, "update_item", lost)
    url = f"/agent-authorities/service-grants/{reference}/revoke"
    assert (await ctx.http.post(url)).status_code == 200
    before = ctx.store._read("TENANT#tenant", f"AUTHORITY#{reference}")
    ctx.user.user_id = "second-human"
    assert (await ctx.http.post(url)).status_code == 200
    assert ctx.store._read("TENANT#tenant", f"AUTHORITY#{reference}") == before
    assert (await approve(ctx, request_id=str(uuid4()), replaces=reference)).status_code == 200
    assert ctx.store._read("TENANT#tenant", f"AUTHORITY#{reference}") == before


@pytest.mark.parametrize("read_fails", [False, True])
async def test_revocation_outage_is_not_reported_as_missing_or_success(approval_context, monkeypatch, read_fails):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    unavailable = Mock(side_effect=EndpointConnectionError(endpoint_url="https://store.test"))
    monkeypatch.setattr(ctx.store.client, "update_item", unavailable)
    if read_fails:
        monkeypatch.setattr(ctx.store.client, "get_item", unavailable)
    response = await ctx.http.post(f"/agent-authorities/service-grants/{reference}/revoke")
    assert response.status_code == 503


async def test_approval_outage_returns_retryable_failure_without_a_grant(approval_context, monkeypatch):
    ctx = approval_context
    monkeypatch.setattr(ctx.store.client, "transact_write_items", Mock(side_effect=EndpointConnectionError(endpoint_url="https://store.test")))
    assert (await approve(ctx)).status_code == 503
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None


async def test_registration_change_during_approval_cannot_authorize_old_scope(approval_context, monkeypatch):
    ctx = approval_context
    original = ctx.store.client.transact_write_items

    def change_registration(**kwargs):
        ctx.store.client.update_item(
            TableName="identities",
            Key={"identity_type": {"S": "service_account"}, "identity_value": {"S": SERVICE}},
            UpdateExpression="SET repo = :repo",
            ExpressionAttributeValues={":repo": {"S": "other/repo"}},
        )
        return original(**kwargs)

    monkeypatch.setattr(ctx.store.client, "transact_write_items", change_registration)
    assert (await approve(ctx)).status_code == 409
    assert ctx.store._read("TENANT#tenant", f"SERVICE#{SERVICE}") is None


async def test_service_cannot_dispatch_a_persona_outside_policy_or_exceed_budget(approval_context):
    from src.agentauth.policy import PolicyError

    ctx = approval_context
    assert (await approve(ctx, child_personas=["developer"], max_total_dispatches=1)).status_code == 200
    _, credential = bind(ctx, provision(ctx))

    def send(body):
        return ctx.child.service.dispatch(body=body, credential_token=credential, workload_binding="service-pod")

    with pytest.raises(BootstrapRefusedError):
        send(DispatchRequest(persona="operations", target={"repo": "org/repo", "issue": 99}, request_id="outside-persona"))
    child = send(DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 99}, request_id="one-child"))
    child_grant = ctx.store._read("TENANT#tenant", f"GRANT#{child['invocation_id']}#1")
    assert "dispatch_personas" not in child_grant  # Reviewer was not delegated.
    with pytest.raises(PolicyError):
        send(DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 100}, request_id="second-child"))


async def test_expired_service_policy_cannot_start_or_refresh_a_run(approval_context):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    envelope = provision(ctx)
    record, _ = bind(ctx, envelope)
    ctx.store.client.update_item(
        TableName="authority",
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"AUTHORITY#{reference}"}},
        UpdateExpression="SET expires_at = :past",
        ExpressionAttributeValues={":past": {"S": "2000-01-01T00:00:00Z"}},
    )
    with pytest.raises(AuthorityProvisionError):
        provision(ctx)
    with pytest.raises(BootstrapRefusedError):
        ctx.store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))


async def test_cross_tenant_revoke_does_not_change_approval(approval_context):
    ctx = approval_context
    reference = (await approve(ctx)).json()["authority_reference_id"]
    ctx.user.org_id = "other-tenant"
    assert (await ctx.http.post(f"/agent-authorities/service-grants/{reference}/revoke")).status_code == 404
    assert ctx.store._read("TENANT#tenant", f"AUTHORITY#{reference}")["status"] == {"S": "active"}


@pytest.fixture
def native_handler(approval_context, monkeypatch):
    from common import service_identity, sqs_publisher
    from common import spawn_persona as spawn_module
    from eventbridge import handler

    ctx = approval_context
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("EVENTS_TABLE", "events")
    monkeypatch.setattr(service_identity, "IDENTITY_INDEX_TABLE", "identities")
    monkeypatch.setattr(service_identity, "_dynamodb", None)
    monkeypatch.setattr(handler, "_service_identity_mod", service_identity)
    monkeypatch.setattr(handler, "_rate_limit_mod", SimpleNamespace(check_and_increment=Mock(return_value=SimpleNamespace(allowed=True))))
    monkeypatch.setattr("common.installation_resolver.resolve_installation_for_tenant", lambda org: 123)
    monkeypatch.setattr(sqs_publisher, "SUBMIT_QUEUE_URL", ctx.child.queue)
    monkeypatch.setattr(sqs_publisher, "_sqs", ctx.child.sqs)
    pointer, capture = Mock(), Mock(wraps=spawn_module._capture_invocation_event)
    monkeypatch.setattr(spawn_module, "_write_pointer_and_provenance", pointer)
    monkeypatch.setattr(spawn_module, "_capture_invocation_event", capture)
    monkeypatch.setattr(spawn_module, "_get_max_credential_chain_depth", lambda installation: 5)
    native = {
        **ctx.event,
        "detail-type": "Nightly security scan",
        "detail": {"adp_trigger": {"persona": "operations", "service_identity": SERVICE, "target": {"repo": "org/repo", "create_issue": True}}},
    }
    return SimpleNamespace(handle=handler.handle_eventbridge, event=native, pointer=pointer, capture=capture)


async def test_native_ingress_queues_bootstrappable_service_with_truthful_lineage(approval_context, native_handler):
    import json

    ctx, native = approval_context, native_handler
    assert (await approve(ctx)).status_code == 200
    first = native.handle(native.event, None)
    retry = native.handle(native.event, None)
    assert first["statusCode"] == retry["statusCode"] == 202
    messages = ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1
    envelope = json.loads(messages[0]["Body"])
    response = json.loads(first["body"])
    assert json.loads(retry["body"])["correlation_id"] == response["correlation_id"] == envelope["correlation"]["correlation_id"]
    assert response["is_human_rooted"] is True
    assert envelope["actor"]["kind"] == "service" and envelope["actor"]["user_id"] == SERVICE
    assert envelope["cognito_sub"] == ""
    assert envelope["source_ref"]["issue"] is None
    for recorded in [native.pointer, native.capture]:
        assert recorded.call_args.kwargs["correlation_ctx"]["correlation_id"] == response["correlation_id"]
    record, credential = bind(ctx, envelope)
    event_key = {"event_id": {"S": envelope["message_id"]}, "arrived_at": {"S": envelope["arrived_at"]}}
    event_row = ctx.store.client.get_item(TableName="events", Key=event_key)["Item"]
    assert event_row["actor_kind"] == {"S": "service"}
    assert event_row["actor_user_id"] == {"S": SERVICE}
    assert event_row["root_human_id"] == {"S": "approver"}
    assert "authorized_user_id" not in event_row
    # A delayed ingress retry must not erase the listener registration or
    # return a running invocation to webhook_received.
    ctx.store.client.update_item(
        TableName="events",
        Key=event_key,
        UpdateExpression="SET #s = :running, control_generation = :g",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":running": {"S": "running"}, ":g": {"N": "7"}},
    )
    assert native.handle(native.event, None)["statusCode"] == 202
    after = ctx.store.client.get_item(TableName="events", Key=event_key)["Item"]
    assert after["status"] == {"S": "running"} and after["control_generation"] == {"N": "7"}
    child = ctx.child.service.dispatch(
        body=DispatchRequest(persona="operations", target={"repo": "org/repo", "issue": 101}, request_id="nightly-evaluation"),
        credential_token=credential,
        workload_binding="service-pod",
    )
    assert ctx.store.authority.load_execution(invocation_id=child["invocation_id"], tenant_id="tenant").parent_principal == record.principal


@pytest.mark.parametrize("failure", ["no_approval", "forged_rule", "wrong_repo", "revoked"])
async def test_native_ingress_refuses_unapproved_service_without_queue_or_lineage_write(approval_context, native_handler, failure):
    ctx, native = approval_context, native_handler
    if failure != "no_approval":
        reference = (await approve(ctx)).json()["authority_reference_id"]
        if failure == "revoked":
            assert (await ctx.http.post(f"/agent-authorities/service-grants/{reference}/revoke")).status_code == 200
    if failure == "forged_rule":
        native.event["adp_rule_arn"] = "forged"
    if failure == "wrong_repo":
        native.event["detail"]["adp_trigger"]["target"]["repo"] = "other/repo"
    assert native.handle(native.event, None)["statusCode"] != 202
    assert "Messages" not in ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue)
    native.pointer.assert_not_called()
    native.capture.assert_not_called()
