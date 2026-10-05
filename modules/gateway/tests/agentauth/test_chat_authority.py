"""Production authority adapters with DynamoDB/SQL stores and a simulated Kube API."""

import json
from datetime import UTC, datetime

import boto3
import httpx
import pytest
from sqlalchemy import delete, text, update
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_authority import ChatRuntimeAuthority, chat_capabilities, current_chat_member
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunch
from src.agentauth.external_roots import provision_root
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV
from src.agentauth.workload import BOOTSTRAP_AUDIENCE, KubernetesWorkloadVerifier, VerifiedPod
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.agentauth.test_bootstrap_routes import store as store_fixture

store = store_fixture
DIGEST = "sha256:" + "a" * 64
POD = VerifiedPod("chat-pod", "chat-a", "adp-gateway-agents", "adp-agent", "127.0.0.1", image_digest=DIGEST)


@pytest.fixture
async def runtime(store, db_session_factory, tmp_path):
    async with db_session_factory() as db:
        db.add_all(
            [
                Organization(id="tenant", name="Tenant"),
                Team(id="team", org_id="tenant", department_id="department", name="Team"),
                User(id="human", org_id="tenant", team_id="team", email="human@example.test"),
                TeamMembership(id="member", org_id="tenant", user_id="human", team_id="team", is_primary=True),
                TenantMembership(id="tenant-member", tenant_id="tenant", user_id="human", role="member", is_active=False),
            ]
        )
        await db.commit()
    now = int(datetime.now(UTC).timestamp())
    envelope = {
        "message_id": "run-a",
        "tenant_id": "tenant",
        "persona": "developer",
        "source_ref": {"repo": "chat/session-a"},
        "arrived_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }
    provision_root(store, envelope, source="chat", human_id="human", now=datetime.fromtimestamp(now, UTC))
    store.bind(invocation_id="run-a", digest=envelope_digest(envelope), pod=POD, now=datetime.fromtimestamp(now, UTC))
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="context",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    header = {
        "PK": "session#session-a",
        "SK": "header",
        "orgId": "tenant",
        "tenantId": "tenant",
        "teamId": "team",
        "ownerUserId": "human",
        "status": "active",
        "ttl": now + 1000,
        "chatLease": {"run_id": "run-a", "sandbox_uid": "chat-pod", "generation": 1, "expires_at": now + 600},
    }
    table.put_item(Item=header)
    token_path = tmp_path / "kube-token"
    token_path.write_text("synthetic-kube-token")
    state = {"uid": "chat-pod", "image": DIGEST, "http_status": 200}

    def kube(request):
        if request.url.path.endswith("/tokenreviews"):
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": json.loads(request.content)["spec"]["token"] == "chat-token",
                        "audiences": [BOOTSTRAP_AUDIENCE],
                        "user": {
                            "username": "system:serviceaccount:adp-gateway-agents:adp-agent",
                            "extra": {
                                "authentication.kubernetes.io/pod-name": ["chat-a"],
                                "authentication.kubernetes.io/pod-uid": [state["uid"]],
                            },
                        },
                    }
                },
            )
        assert request.url.path == "/api/v1/namespaces/adp-gateway-agents/pods/chat-a"
        return httpx.Response(
            state["http_status"],
            json={
                "metadata": {"uid": state["uid"], "name": "chat-a", "namespace": "adp-gateway-agents"},
                "spec": {
                    "serviceAccountName": "adp-agent",
                    "containers": [
                        {"name": "chat-agent", "env": [{"name": "ADP_CHAT_MODEL_POLICY_ENABLED", "value": "true"}]},
                    ],
                },
                "status": {
                    "phase": "Running",
                    "podIP": "127.0.0.1",
                    "containerStatuses": [
                        {"name": "chat-agent", "imageID": f"registry/chat@{state['image']}", "state": {"running": {}}},
                    ],
                },
            },
        )

    with httpx.Client(base_url="https://kube.test", transport=httpx.MockTransport(kube)) as client:
        workloads = KubernetesWorkloadVerifier(
            client=client,
            image_digests=frozenset({DIGEST, "sha256:" + "b" * 64}),
            namespace="adp-gateway-agents",
            service_account="adp-agent",
            container_name="chat-agent",
            authority_flag="ADP_CHAT_MODEL_POLICY_ENABLED",
            gateway_token_path=token_path,
        )
        authority = ChatRuntimeAuthority(store, table, workloads)
        service = chat_capabilities(authority=authority, session_factory=db_session_factory, env={CREDENTIAL_KEY_ENV: "synthetic-chat-key"})
        launch = ChatLaunch(
            run_id="run-a",
            tenant_id="tenant",
            user_id="human",
            team_id="team",
            session_id="session-a",
            sandbox_uid=POD.uid,
            image_digest=DIGEST,
            attempt=1,
            credential_epoch=1,
            lease_generation=1,
            grant_id="root-run-a",
            grant_epoch=1,
            operations=frozenset({"history.read"}),
            expires_at=now + 600,
        )
        service.launches.register(launch)
        yield service, authority, table, header, state, launch, now


async def issue(runtime):
    service, _, _, _, _, launch, now = runtime
    return await run_in_threadpool(service.issue, launch.run_id, POD, now=now)


async def verify(runtime, token):
    service, _, _, _, _, launch, now = runtime
    return await run_in_threadpool(service.verify, token, run_id=launch.run_id, session_id=launch.session_id, operation="history.read", now=now)


async def test_production_callbacks_round_trip_and_current_team_removal(runtime, db_session_factory):
    token = await issue(runtime)
    assert await verify(runtime, token) == runtime[5]
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)
    with pytest.raises(ChatAuthorizationRefusedError):
        await issue(runtime)


async def test_tenant_revocation_overrides_legacy_user_pointer(runtime, db_session_factory):
    token = await issue(runtime)
    async with db_session_factory() as db:
        await db.execute(update(TenantMembership).values(revoked_at=datetime.now(UTC)))
        await db.commit()
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


@pytest.mark.parametrize("field,value", [("status", "closed"), ("ownerUserId", "other"), ("tenantId", "other"), ("teamId", "other")])
async def test_session_scope_or_end_revokes_capability(runtime, field, value):
    _, _, table, header, _, _, _ = runtime
    token = await issue(runtime)
    table.put_item(Item={**header, field: value})
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


@pytest.mark.parametrize("change", [{"run_id": "other"}, {"sandbox_uid": "other"}, {"generation": 2}, {"expires_at": 1}, {"generation": True}])
async def test_lease_replacement_expiry_or_malformed_generation_revokes(runtime, change):
    _, _, table, header, _, _, _ = runtime
    token = await issue(runtime)
    table.put_item(Item={**header, "chatLease": {**header["chatLease"], **change}})
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


async def test_missing_lease_never_uses_execution_as_a_fallback(runtime):
    _, _, table, header, _, _, _ = runtime
    table.put_item(Item={field: value for field, value in header.items() if field != "chatLease"})
    with pytest.raises(ChatAuthorizationRefusedError):
        await issue(runtime)


@pytest.mark.parametrize("field,value", [("current_attempt", {"N": "2"}), ("current_credential_epoch", {"N": "2"}), ("status", {"S": "aborted"})])
async def test_current_execution_revokes_old_capability(runtime, field, value):
    _, authority, _, _, _, _, _ = runtime
    token = await issue(runtime)
    authority.store.client.update_item(
        TableName=authority.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": field},
        ExpressionAttributeValues={":value": value},
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


async def test_root_revocation_is_read_fresh(runtime):
    _, authority, _, _, _, _, now = runtime
    token = await issue(runtime)
    grant = authority.store.live_grant(invocation_id="run-a", tenant_id="tenant", attempt=1, now=datetime.fromtimestamp(now, UTC))
    authority.store.client.update_item(
        TableName=authority.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"AUTHORITY#{grant.authority.reference_id}"}},
        UpdateExpression="SET #status = :revoked",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":revoked": {"S": "revoked"}},
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


@pytest.mark.parametrize("change", [{"uid": "replacement"}, {"image": "sha256:" + "b" * 64}])
async def test_live_pod_and_exact_launch_image_are_rechecked(runtime, change):
    token = await issue(runtime)
    runtime[4].update(change)
    with pytest.raises(ChatAuthorizationRefusedError):
        await verify(runtime, token)


async def test_workload_authority_outage_is_unavailable(runtime):
    token = await issue(runtime)
    runtime[4]["http_status"] = 503
    with pytest.raises(ChatAuthorizationUnavailableError):
        await verify(runtime, token)


async def test_membership_requires_canonical_human_and_current_team(runtime, db_session_factory):
    async with db_session_factory() as db:
        assert await current_chat_member(db, "tenant", "human", "")
        assert not await current_chat_member(db, "other-tenant", "human", "")
        assert not await current_chat_member(db, "tenant", "other-user", "team")
        assert not await current_chat_member(db, "tenant", "human", "other-team")
        await db.execute(update(User).where(User.id == "human").values(user_kind="bot"))
        await db.commit()
        assert not await current_chat_member(db, "tenant", "human", "")


async def test_directory_outage_does_not_reuse_previous_membership(runtime, db_session_factory):
    token = await issue(runtime)
    async with db_session_factory() as db:
        await db.execute(text("DROP TABLE team_memberships"))
        await db.commit()
    with pytest.raises(ChatAuthorizationUnavailableError):
        await verify(runtime, token)


async def test_session_store_outage_does_not_reuse_previous_lease(runtime):
    token = await issue(runtime)
    runtime[2].delete()
    with pytest.raises(ChatAuthorizationUnavailableError):
        await verify(runtime, token)
