"""Real bootstrap HTTP adapter with TokenReview transport and persisted state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import boto3
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, envelope_digest
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.routes import AgentRuntime, get_agent_runtime, router
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, verify_credential
from src.agentauth.workload import BOOTSTRAP_AUDIENCE, WORKLOAD_HEADER, KubernetesWorkloadVerifier, VerifiedPod, WorkloadRefusedError

DIGEST = "sha256:" + "a" * 64
ENV = {CREDENTIAL_KEY_ENV: "test-only-isolated-gateway-key"}


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority-test",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = BootstrapStore(table_name="authority-test", dynamodb_client=client)
        # Represents the verified human event written by the trusted dispatcher.
        client.put_item(
            TableName=store.table,
            Item={
                "pk": {"S": "TENANT#tenant"},
                "sk": {"S": "AUTHORITY#approval"},
                "status": {"S": "active"},
                "human_id": {"S": "human"},
                "authority_kind": {"S": "github_event"},
            },
        )
        yield store


def provision(store, invocation="run-a", **envelope_changes):
    envelope = {
        "message_id": invocation,
        "tenant_id": "tenant",
        "persona": "developer",
        "arrived_at": "2026-09-13T09:00:00Z",
        "source_ref": {"repo": "org/repo", "issue": 1},
        **envelope_changes,
    }
    now = datetime.now(UTC)
    grant = DelegatedGrant(
        grant_id=f"grant-{invocation}",
        tenant_id="tenant",
        principal=f"{invocation}#1",
        authority=AuthorityReference("github_event", "approval", "human", "tenant"),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({"org/repo"}),
        expires_at=now + timedelta(hours=1),
    )
    store.provision_pending(envelope=envelope, grant=grant, now=now)
    return envelope, grant


def test_direct_override_request_and_resolution_are_protected_separately(store):
    provision(
        store,
        model_requested="sonnet46",
        model_resolved="global.anthropic.claude-sonnet-4-6",
    )

    execution = store._read("TENANT#tenant", "EXEC#run-a")
    assert execution["direct_model_requested"] == {"S": "sonnet46"}
    assert execution["direct_model_override"] == {"S": "global.anthropic.claude-sonnet-4-6"}


def test_direct_override_is_not_inherited_by_a_descendant_dispatch(store):
    provision(
        store,
        invocation="root-run",
        model_requested="sonnet46",
        model_resolved="global.anthropic.claude-sonnet-4-6",
    )
    provision(
        store,
        invocation="child-run",
        correlation={"parent_principal": "root-run#1"},
    )

    root = store._read("TENANT#tenant", "EXEC#root-run")
    child = store._read("TENANT#tenant", "EXEC#child-run")
    assert root["direct_model_requested"] == {"S": "sonnet46"}
    assert root["direct_model_override"] == {"S": "global.anthropic.claude-sonnet-4-6"}
    assert "direct_model_requested" not in child
    assert "direct_model_override" not in child


@pytest.fixture
def kubernetes(tmp_path):
    token_path = tmp_path / "gateway-token"
    token_path.write_text("gateway-token")
    state = {"uid": "pod-a", "digest": DIGEST, "phase": "Running", "audiences": [BOOTSTRAP_AUDIENCE], "deleted": False}
    seen = []

    def transport(request):
        seen.append(request)
        assert request.headers["Authorization"] == f"Bearer {token_path.read_text()}"
        if request.method == "POST":
            import json

            body = json.loads(request.content)
            assert body["spec"]["audiences"] == [BOOTSTRAP_AUDIENCE]
            assert body["spec"]["token"] == "pod-token"
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": state["audiences"],
                        "user": {
                            "username": "system:serviceaccount:adp-agents:agent-scaledjob-sa",
                            "extra": {
                                "authentication.kubernetes.io/pod-name": ["worker-a"],
                                "authentication.kubernetes.io/pod-uid": ["pod-a"],
                            },
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "uid": state["uid"],
                    "name": "worker-a",
                    "namespace": "adp-agents",
                    "deletionTimestamp": "now" if state["deleted"] else None,
                },
                "spec": {
                    "serviceAccountName": "agent-scaledjob-sa",
                    "containers": [{"name": "agent-worker", "env": [{"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"}]}],
                },
                "status": {
                    "phase": state["phase"],
                    "podIP": "10.0.1.2",
                    "containerStatuses": [{"name": "agent-worker", "imageID": f"registry/image@{state['digest']}", "state": {"running": {}}}],
                },
            },
        )

    verifier = KubernetesWorkloadVerifier(
        client=httpx.Client(base_url="https://kubernetes.test", transport=httpx.MockTransport(transport)),
        image_digests=frozenset({DIGEST}),
        gateway_token_path=token_path,
    )
    return verifier, state, token_path, seen


def http_client(store, kubernetes, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env=ENV)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    auth = AsyncMock()
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", auth)
    return TestClient(app), auth


def test_bootstrap_http_binds_pod_and_recovers_identical_retry(store, kubernetes, monkeypatch):
    envelope, _ = provision(store)
    client, auth = http_client(store, kubernetes, monkeypatch)
    body = {"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}
    headers = {"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"}
    first = client.post("/internal/v1/agent/bootstrap", json=body, headers=headers)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    credential = verify_credential(first.json()["credential"], env=ENV)
    assert (credential.invocation_id, credential.attempt, credential.credential_epoch) == ("run-a", 1, 1)
    kubernetes[2].write_text("rotated-gateway-token")
    second = client.post("/internal/v1/agent/bootstrap", json=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["credential_epoch"] == 1
    assert store.authority.load_execution(invocation_id="run-a", tenant_id="tenant").workload_binding == "pod-a"
    assert auth.await_args.kwargs == {"x_caller_identity": "registered-worker-transport", "x_internal_api_key": None}


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Api-Key": "legacy-shared-secret"}])
def test_bootstrap_rejects_missing_iam_even_with_shared_secret(store, kubernetes, monkeypatch, headers):
    envelope, _ = provision(store)
    client, auth = http_client(store, kubernetes, monkeypatch)
    result = client.post(
        "/internal/v1/agent/bootstrap", json={"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}, headers=headers
    )
    assert result.status_code == 403
    auth.assert_not_awaited()
    assert not kubernetes[3]


def test_bound_pod_cannot_claim_another_pending_message(store):
    a, _ = provision(store, "run-a")
    b, _ = provision(store, "run-b")
    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    store.bind(invocation_id="run-a", digest=envelope_digest(a), pod=pod, now=datetime.now(UTC))
    with pytest.raises(BootstrapRefusedError):
        store.bind(invocation_id="run-b", digest=envelope_digest(b), pod=pod, now=datetime.now(UTC))
    assert store.authority.load_execution(invocation_id="run-b", tenant_id="tenant").workload_binding is None


def test_fresh_pod_cannot_supersede_live_attempt(store):
    envelope, _ = provision(store)

    def bind(uid):
        return store.bind(
            invocation_id="run-a",
            digest=envelope_digest(envelope),
            pod=VerifiedPod(uid, "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
            now=datetime.now(UTC),
        )

    bind("pod-a")
    with pytest.raises(BootstrapRefusedError):
        bind("pod-b")
    assert store._read("POD#pod-b", "BINDING") is None


def test_cancelled_authority_cannot_recover_credential(store):
    envelope, _ = provision(store)
    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    store.bind(invocation_id="run-a", digest=envelope_digest(envelope), pod=pod, now=datetime.now(UTC))
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "AUTHORITY#approval"}},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "cancelled"}},
    )
    with pytest.raises(BootstrapRefusedError):
        store.bind(invocation_id="run-a", digest=envelope_digest(envelope), pod=pod, now=datetime.now(UTC))


@pytest.mark.parametrize(
    "field,value",
    [("uid", "another-pod"), ("digest", "sha256:" + "b" * 64), ("phase", "Succeeded"), ("audiences", ["sts.amazonaws.com"]), ("deleted", True)],
)
def test_tokenreview_must_match_live_approved_pod(kubernetes, field, value):
    kubernetes[1][field] = value
    with pytest.raises(WorkloadRefusedError):
        kubernetes[0].verify("pod-token")


def test_gateway_token_is_not_returned_in_failure(kubernetes):
    kubernetes[2].unlink()
    with pytest.raises(WorkloadRefusedError, match="workload verifier unavailable"):
        kubernetes[0].verify("pod-token")


@pytest.fixture
def connected_http(store, kubernetes, monkeypatch):
    from src.agentauth.bootstrap import issue_bound_credential
    from src.agentauth.composition import build_control_adapter

    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="events-test",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    credentials = {}
    for run, uid in [("run-a", "pod-a"), ("run-b", "pod-b"), ("unrelated", "pod-c")]:
        envelope, _ = provision(store, run)
        record = store.bind(
            invocation_id=run,
            digest=envelope_digest(envelope),
            pod=VerifiedPod(uid, uid, "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
            now=datetime.now(UTC),
        )
        credentials[run] = issue_bound_credential(record, now=datetime.now(UTC), env=ENV)["credential"]
        resource.Table("events-test").put_item(
            Item={
                "event_id": run,
                "arrived_at": envelope["arrived_at"],
                "tenant_id": "tenant",
                "status": "in_progress",
                "control_generation": 1,
                "control_address": "http://10.0.1.2:8080",
                "control_token": "private-never-returned",
                "control_token_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        )
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-b"}},
        UpdateExpression="SET parent_principal = :parent",
        ExpressionAttributeValues={":parent": {"S": "run-a#1"}},
    )
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
        UpdateExpression="SET allowed_actions = :actions, target_relationships = :targets",
        ExpressionAttributeValues={":actions": {"SS": ["monitor", "pause"]}, ":targets": {"SS": ["self", "descendant"]}},
    )
    adapter = build_control_adapter(
        authority_table=store.table, events_table="events-test", dynamodb_client=store.client, dynamodb_resource=resource, env=ENV
    )
    app = FastAPI()
    app.include_router(router)
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env=ENV, adapter=adapter)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    return (
        TestClient(app),
        {"X-Caller-Identity": "same-shared-worker-role", WORKLOAD_HEADER: "pod-token", "X-Adp-Run-Credential": credentials["run-a"]},
        credentials,
    )


def test_connected_status_reads_self_and_authorized_child_without_secrets(connected_http):
    client, headers, _ = connected_http
    for run in ("run-a", "run-b"):
        response = client.get("/internal/v1/agent/status", params={"run": run}, headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "in_progress"
        assert response.json()["capabilities"] == {}
        assert "private-never-returned" not in response.text
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/internal/v1/agent/status?run=unrelated", headers=headers).status_code == 404


def test_connected_status_refuses_stolen_credential_despite_shared_role(connected_http):
    client, headers, credentials = connected_http
    headers["X-Adp-Run-Credential"] = credentials["run-b"]
    assert client.get("/internal/v1/agent/status?run=run-b", headers=headers).status_code == 404


@pytest.mark.parametrize(
    "row,expression,values",
    [
        ("GRANT#run-a#1", "SET revoked = :v", {":v": {"BOOL": True}}),
        ("AUTHORITY#approval", "SET #st = :v", {":v": {"S": "cancelled"}}),
        ("AUTHORITY#approval", "SET expires_at = :v", {":v": {"S": "2020-01-01T00:00:00Z"}}),
        ("AUTHORITY#approval", "SET expires_at = :v", {":v": {"S": "malformed"}}),
    ],
)
def test_connected_status_rechecks_revocation_on_each_request(store, connected_http, row, expression, values):
    client, headers, _ = connected_http
    assert client.get("/internal/v1/agent/status?run=run-b", headers=headers).status_code == 200
    kwargs = {"ExpressionAttributeNames": {"#st": "status"}} if "#st" in expression else {}
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": row}},
        UpdateExpression=expression,
        ExpressionAttributeValues=values,
        **kwargs,
    )
    assert client.get("/internal/v1/agent/status?run=run-b", headers=headers).status_code == 404


def test_connected_control_authorized_unsupported_returns_501(connected_http):
    client, headers, _ = connected_http
    response = client.post("/internal/v1/agent/control/run-b/pause", json={"command_id": "same-command"}, headers=headers)
    assert response.status_code == 501
    assert client.post("/internal/v1/agent/control/unrelated/pause", json={"command_id": "same-command"}, headers=headers).status_code == 404
    assert client.post("/internal/v1/agent/control/run-b/abort", json={"command_id": "same-command"}, headers=headers).status_code == 404


def test_connected_status_requires_both_iam_and_pod_proof(connected_http):
    client, headers, _ = connected_http
    without_iam = {k: v for k, v in headers.items() if k != "X-Caller-Identity"}
    without_iam["X-Internal-Api-Key"] = "shared-secret"
    assert client.get("/internal/v1/agent/status?run=run-a", headers=without_iam).status_code == 403
    without_proof = {k: v for k, v in headers.items() if k != WORKLOAD_HEADER}
    assert client.get("/internal/v1/agent/status?run=run-a", headers=without_proof).status_code == 404


@pytest.fixture
def broker_harness(store, kubernetes, monkeypatch):
    from types import SimpleNamespace

    from fastapi import Depends

    from src.agentauth.routes import BootstrapRequest
    from src.internal.auth_deps import verify_internal_or_irsa

    envelope, _ = provision(store)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET installation_id = :id",
        ExpressionAttributeValues={":id": {"N": "123"}},
    )
    store.client.create_table(
        TableName="broker-events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    store.client.put_item(
        TableName="broker-events",
        Item={
            "event_id": {"S": "run-a"},
            "arrived_at": {"S": envelope["arrived_at"]},
            "authorized_user_id": {"S": "user-a"},
        },
    )
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env=ENV)
    lease = runtime.bootstrap(BootstrapRequest(invocation_id="run-a", envelope_digest=envelope_digest(envelope)), "pod-token")
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.config.get_settings", lambda: SimpleNamespace(webhook_events_table="broker-events"))
    identity = SimpleNamespace(scope="internal", user_id="shared-worker", credential_scopes=["credential:raw-read", "credential:materialize"])
    monkeypatch.setattr("src.internal.auth_deps.extract_iam_identity_from_headers", lambda request: identity)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    app = FastAPI()
    effects = []
    from src.agentauth.broker_identity import BROKER_PATHS

    for path in BROKER_PATHS:

        async def broker(_: None = Depends(verify_internal_or_irsa)):
            effects.append("mint")
            return {"minted": True}

        if path == "/internal/v1/user-credentials":
            app.get(path)(broker)
        else:
            app.post(path)(broker)
    headers = {"X-Caller-Identity": "shared-worker", "X-Adp-Run-Credential": lease["credential"], WORKLOAD_HEADER: "pod-token"}
    return TestClient(app), headers, effects, identity


@pytest.mark.parametrize(
    "path", ["github-installation-token", "credential-assume-role", "credential-raw-read", "proxy-request", "credential-materialize"]
)
def test_broker_requires_same_verified_worker_and_permits_own_run(broker_harness, kubernetes, path):
    client, headers, effects, _ = broker_harness
    body = {"invocation_id": "run-a", "user_id": "user-a", "installation_id": 123, "repo_owner": "org", "repo_name": "repo"}
    url = "/internal/v1/" + path
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert effects == ["mint"]
    for altered in ({**body, "invocation_id": "run-b"}, {**body, "invocation_id": None}):
        assert client.post(url, json=altered, headers=headers).status_code == 404
    assert client.post(url, json=body, headers={"X-Caller-Identity": "shared-worker"}).status_code == 404
    kubernetes[1]["uid"] = "pod-b"
    assert client.post(url, json=body, headers=headers).status_code == 404
    assert effects == ["mint"]


@pytest.mark.parametrize("changes", [{"user_id": "user-b"}, {"user_id": ""}])
def test_broker_cannot_select_other_users_in_legacy_shadow_mode(broker_harness, changes):
    client, headers, effects, _ = broker_harness
    assert client.post("/internal/v1/credential-assume-role", json={"invocation_id": "run-a", **changes}, headers=headers).status_code == 404
    assert effects == []


def test_vault_metadata_requires_bound_user_and_invocation(broker_harness):
    client, headers, effects, _ = broker_harness
    path = "/internal/v1/user-credentials"
    query = {"invocation_id": "run-a", "user_id": "user-a"}
    assert client.get(path, params=query, headers=headers).status_code == 200
    for change in ({"user_id": "user-b"}, {"invocation_id": "run-b"}):
        assert client.get(path, params={**query, **change}, headers=headers).status_code == 404
    assert client.get(path, params=query, headers={"X-Internal-Api-Key": "shared-secret"}).status_code == 403
    assert effects == ["mint"]


@pytest.mark.parametrize("path,scope", [("credential-raw-read", "credential:raw-read"), ("credential-materialize", "credential:materialize")])
def test_vault_scope_header_cannot_grant_missing_registry_capability(broker_harness, path, scope):
    client, headers, effects, identity = broker_harness
    identity.credential_scopes = []
    assert (
        client.post(
            "/internal/v1/" + path, json={"invocation_id": "run-a", "user_id": "user-a"}, headers={**headers, "X-Agent-Scopes": scope}
        ).status_code
        == 404
    )
    assert effects == []


def test_broker_repo_and_installation_cannot_expand(broker_harness):
    client, headers, effects, _ = broker_harness
    body = {"invocation_id": "run-a", "installation_id": 123, "repo_owner": "org", "repo_name": "repo"}
    for change in ({"installation_id": 456}, {"repo_name": "other"}, {"repo_owner": "other"}):
        assert client.post("/internal/v1/github-installation-token", json={**body, **change}, headers=headers).status_code == 404
    assert effects == []


def test_broker_no_shared_key_fallback_and_platform_deploy_stays_supported(broker_harness):
    client, headers, effects, identity = broker_harness
    url = "/internal/v1/credential-assume-role"
    assert client.post(url, json={}, headers={"X-Internal-Api-Key": "shared-secret"}).status_code == 403
    identity.scope = "platform"
    assert client.post(url, json={}, headers={"X-Caller-Identity": "deploy-role"}).status_code == 200
    assert effects == ["mint"]


def test_broker_cancellation_prevents_further_credentials(broker_harness, store):
    client, headers, effects, _ = broker_harness
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "AUTHORITY#approval"}},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "cancelled"}},
    )
    assert (
        client.post("/internal/v1/credential-assume-role", json={"invocation_id": "run-a", "user_id": "user-a"}, headers=headers).status_code == 404
    )
    assert effects == []


def test_authority_child_github_mint_uses_protected_assignment_not_missing_legacy_event_fields(broker_harness, monkeypatch):
    from unittest.mock import MagicMock

    from src.internal import routes as internal_routes
    from src.shared.database import get_db

    _, headers, _, _ = broker_harness
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    app = FastAPI()
    app.include_router(internal_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(internal_routes, "resolve_installation_binding", MagicMock(side_effect=AssertionError("legacy lookup must not be reached")))
    ownership = AsyncMock()
    monkeypatch.setattr(internal_routes, "assert_installation_owned_by", ownership)
    monkeypatch.setattr(internal_routes, "resolve_tenant_app_credentials", AsyncMock(return_value=("app", "private-test-key")))
    mint = AsyncMock(return_value=("repo-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr(internal_routes, "mint_installation_token_with_expiry", mint)
    response = TestClient(app).post(
        "/internal/v1/github-installation-token",
        headers=headers,
        json={
            "invocation_id": "run-a",
            "installation_id": 123,
            "repo_owner": "org",
            "repo_name": "repo",
        },
    )
    assert response.status_code == 200, response.text
    ownership.assert_awaited_once_with("tenant", 123, db=db)
    assert mint.await_args.kwargs["repositories"] == ["repo"]
