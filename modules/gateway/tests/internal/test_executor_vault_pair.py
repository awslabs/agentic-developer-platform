"""Real delivery decisions: signed client, run/pod/grant, registry and PostgreSQL.

Only infrastructure boundaries are simulated: API Gateway SigV4 forwarding,
Kubernetes HTTP, AWS DynamoDB (moto) and the secret provider. Authorization
functions are never patched. Concurrent mutations commit from separate sessions.
"""

import asyncio
import json
import sys
import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.auth
import botocore.awsrequest
import botocore.credentials
import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.routes import AgentRuntime
from src.agentauth.run_credential import mint_credential
from src.auth.agent_registry import AgentRegistryService
from src.auth.middleware import get_current_user_context
from src.auth.operation_contract import identity_contract
from src.auth.vault_authority_routes import router as owner_router
from src.auth.vault_routes import get_secrets_manager as owner_secrets_manager
from src.auth.vault_routes import router as vault_router
from src.internal.credential_routes import get_secrets_manager
from src.internal.vault_evidence_routes import DELIVERY_SCOPE, router
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import CredentialValidationEvidence, UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper
from tests.agentauth.test_bootstrap_routes import ENV, kubernetes, store  # noqa: F401
from tests.auth.test_vault_operation_authority_postgres import authority  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

DOMAIN = Path(__file__).resolve().parents[3] / "domain-apps/superplane"
# Both applications have a scripts package. Leave neither a search-path override
# nor a generic app module behind for the rest of Gateway's test collection.
_original_path = sys.path[:]
_original_modules = set(sys.modules)
try:
    sys.path.insert(0, str(DOMAIN / "contracts"))
    sys.path.insert(0, str(DOMAIN / "src/superplane-api"))
    from superplane_contracts.connections import CredentialReference
    from superplane_contracts.delivery import DELIVERY_PERMISSION, DeliveryLease, DeliveryRefused, ExecutorIdentity, RunBinding
    from superplane_contracts.delivery_executor import DeliveryRequest, ProviderExecutor
    from superplane_contracts.provisioning import ResolvedPrincipal

    from app.adapters.executor_vault_channel import ExecutorVaultChannel
    from app.adapters.executor_vault_transport import ExecutorVaultTransport
finally:
    sys.path[:] = _original_path
    for _name in set(sys.modules) - _original_modules:
        if _name == "app" or _name.startswith("app."):
            del sys.modules[_name]

VERSION = "validated-version"
VALUE = json.dumps({"role_arn": "arn:aws:iam::123456789012:role/synthetic-vault-role", "external_id": "synthetic-external"})
ROLE = "arn:aws:iam::123456789012:role/superplane-executor"
IAM = "arn:aws:sts::123456789012:assumed-role/superplane-executor/pod-a"


@pytest.fixture
async def pair(authority, pg_url, store, kubernetes, tmp_path, monkeypatch):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(Organization(id="org", name="Org"))
        db.add(Department(id="dept", org_id="org", name="Dept"))
        db.add(Team(id="team", org_id="org", department_id="dept", name="Team"))
        db.add(User(id="human", org_id="org", team_id="team", email="user@example.test"))
        await db.flush()
        for credential in ("cred-1", "cred-2"):
            db.add(
                UserCredential(
                    id=credential,
                    org_id="org",
                    user_id="human",
                    service="aws",
                    label="default" if credential == "cred-1" else "other",
                    credential_type="aws_role",
                    secret_arn="arn:test:" + credential,
                    strict=False,
                )
            )
            await db.flush()
        await db.execute(text("UPDATE harness_operation_leases SET holder='invocation#1'"))
        contract = identity_contract()
        request = contract.OperationRequest(
            action="provision",
            idempotency_key="key",
            parameters={
                "credential_id": "cred-1",
                "credential_service": "aws",
                "credential_label": "default",
                "provider": "aws",
                "provider_account_id": "123456789012",
            },
        )
        await db.execute(
            text("UPDATE harness_operations SET request_payload=:payload, plan_digest=:digest"),
            {"payload": contract.encode_payload(request), "digest": contract.payload_digest(request)},
        )
        await db.execute(text("UPDATE harness_approval_consumption SET plan_digest=:digest"), {"digest": contract.payload_digest(request)})
        await db.commit()

    # A real grant and run record, bound through the real bootstrap store.
    store.client.put_item(
        TableName=store.table,
        Item={
            "pk": {"S": "TENANT#org"},
            "sk": {"S": "AUTHORITY#approval"},
            "status": {"S": "active"},
            "human_id": {"S": "human"},
            "authority_kind": {"S": "service_policy"},
        },
    )
    now = datetime.now(UTC)
    grant = DelegatedGrant(
        grant_id="grant",
        tenant_id="org",
        principal="invocation#1",
        authority=AuthorityReference("service_policy", "approval", "human", "org"),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({"org/repo"}),
        expires_at=now + timedelta(hours=1),
    )
    envelope = {
        "message_id": "invocation",
        "tenant_id": "org",
        "persona": "developer",
        "source_ref": {"repo": "org/repo", "issue": 1},
        "arrived_at": now.isoformat(),
    }
    store.provision_pending(envelope=envelope, grant=grant, now=now)
    pod = kubernetes[0].verify("pod-token")
    store.bind(invocation_id="invocation", digest=envelope_digest(envelope), pod=pod, now=now)
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env=ENV)
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)

    store.client.create_table(
        TableName="delivery-registry",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "agent_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "agent_id", "AttributeType": "S"}, {"AttributeName": "role_arn", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[
            {"IndexName": "by-role-arn", "KeySchema": [{"AttributeName": "role_arn", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}
        ],
    )
    registry_row = {
        "agent_id": {"S": "superplane-executor"},
        "role_arn": {"S": ROLE},
        "agent_name": {"S": "Superplane"},
        "org_id": {"S": "org"},
        "team_id": {"S": "team"},
        "scope": {"S": "internal"},
        "status": {"S": "active"},
        "credential_scopes": {"SS": [DELIVERY_SCOPE]},
    }
    store.client.put_item(TableName="delivery-registry", Item=registry_row)
    registry = AgentRegistryService(table_name="delivery-registry")
    registry._dynamodb = store.client
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: SimpleNamespace(internal_api_key="evidence-key"))

    sm = MagicMock()
    sm.current_version_id.return_value = VERSION
    sm.get_secret_at_version.return_value = (VALUE, VERSION)
    app = FastAPI()
    app.include_router(router)
    app.include_router(owner_router)
    app.include_router(vault_router)
    owner = TokenContext(
        user_id="human", org_id="org", team_id="team", department_id="dept", account_type="human", is_admin=False, expires_at=now + timedelta(hours=1)
    )
    app.dependency_overrides[get_current_user_context] = lambda: owner
    profile = {
        "region": "us-east-1",
        "image_id": "ami-1234abcd",
        "instance_type": "g5.xlarge",
        "subnet_id": "subnet-1234abcd",
        "security_group_ids": ["sg-1234abcd"],
    }
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(credential_validation_profiles=json.dumps({"org": {"workspace": profile}}))
    app.dependency_overrides[owner_secrets_manager] = lambda: sm
    provider_state = {"permitted": True, "quota": 64.0, "instances": [], "account": "123456789012"}
    ec2 = MagicMock()

    def dry_run(**kwargs):
        assert kwargs == {
            "ImageId": profile["image_id"],
            "InstanceType": profile["instance_type"],
            "SubnetId": profile["subnet_id"],
            "SecurityGroupIds": profile["security_group_ids"],
            "MinCount": 1,
            "MaxCount": 1,
            "DryRun": True,
        }
        raise ClientError({"Error": {"Code": "DryRunOperation" if provider_state["permitted"] else "UnauthorizedOperation"}}, "RunInstances")

    ec2.run_instances.side_effect = dry_run

    def paginator(operation):
        pages = MagicMock()
        pages.paginate.side_effect = lambda **kwargs: (
            [{"Reservations": [{"Instances": provider_state["instances"]}]}] if operation == "describe_instances" else [{"CapacityReservations": []}]
        )
        return pages

    ec2.get_paginator.side_effect = paginator
    ec2.describe_instance_types.side_effect = lambda **kwargs: {
        "InstanceTypes": [{"InstanceType": kind, "VCpuInfo": {"DefaultVCpus": 4}} for kind in kwargs["InstanceTypes"]]
    }
    quota = MagicMock()
    quota.get_service_quota.side_effect = lambda **kwargs: {"Quota": {"Value": provider_state["quota"]}}
    sts = MagicMock()
    sts.get_caller_identity.side_effect = lambda: {"Account": provider_state["account"]}
    provider_session = SimpleNamespace(client=lambda service, **kwargs: {"ec2": ec2, "service-quotas": quota, "sts": sts}[service])
    monkeypatch.setattr("src.auth.provider_validation.boto3.Session", lambda **kwargs: provider_session)

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    api = TestClient(app)
    for credential_id in ("cred-1", "cred-2"):
        url = f"/auth/credentials/{credential_id}/workspaces/workspace"
        assert (await asyncio.to_thread(api.put, url)).status_code == 200
        response = await asyncio.to_thread(api.post, url + "/validation")
        assert response.status_code == 200, response.json()
        assert response.json()["validated_version_id"] == VERSION
        assert response.json()["validation"]["credential_valid"] is True
    sm.reset_mock()

    aws_credentials = botocore.credentials.Credentials("testing-key", "testing-secret", "testing-session")
    requests = []

    def api_gateway(request):
        requests.append(request)
        assert "X-Caller-Identity" not in request.headers
        assert "X-Internal-Api-Key" not in request.headers
        # API Gateway boundary verifies the signature over the actual bytes before
        # forwarding a trusted IAM identity. The application decisions stay real.
        signed_headers = request.headers["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
        signed = botocore.awsrequest.AWSRequest(
            method="POST", url=str(request.url), data=request.content, headers={key: request.headers[key] for key in signed_headers}
        )
        signed.context["timestamp"] = request.headers["X-Amz-Date"]
        signer = botocore.auth.SigV4Auth(aws_credentials, "execute-api", "us-east-1")
        expected = signer.signature(signer.string_to_sign(signed, signer.canonical_request(signed)), signed)
        assert request.headers["Authorization"].endswith("Signature=" + expected)
        path = request.url.path.removeprefix("/dev")
        response = api.post(path, content=request.content, headers={**dict(request.headers), "X-Caller-Identity": IAM})
        return httpx.Response(response.status_code, json=response.json())

    http = httpx.Client(transport=httpx.MockTransport(api_gateway))
    run_file, pod_file = tmp_path / "run", tmp_path / "pod"
    run_file.write_text(mint_credential(invocation_id="invocation", attempt=1, tenant_id="org", persona="developer", now=now, env=ENV))
    pod_file.write_text("pod-token")
    binding = RunBinding(
        operation_id="operation",
        job_id="job",
        attempt_id="execution-attempt",
        provider="aws",
        provider_account_id="123456789012",
        operation="provision",
        principal=ResolvedPrincipal(subject="human", org_id="org", workspace_id="workspace"),
        recipient=ExecutorIdentity("invocation#1"),
        permission=DELIVERY_PERMISSION,
        expires_at=now + timedelta(minutes=5),
    )
    lease = DeliveryLease(
        lease_id="delivery",
        reference=CredentialReference(credential_id="cred-1", service="aws", label="default"),
        workspace_id="workspace",
        binding=binding,
        provenance={"trusted_delivery": "test"},
    )
    transport = ExecutorVaultTransport(
        endpoint="https://test.execute-api.us-east-1.amazonaws.com/dev",
        binding=binding,
        run_credential_file=run_file,
        workload_token_file=pod_file,
        session=SimpleNamespace(get_credentials=lambda: aws_credentials),
        client_factory=lambda: nullcontext(http),
    )
    adapter = ExecutorVaultChannel(transport=transport)
    try:
        yield SimpleNamespace(
            adapter=adapter,
            lease=lease,
            sm=sm,
            sessions=sessions,
            store=store,
            registry_row=registry_row,
            kubernetes=kubernetes,
            requests=requests,
            api=api,
            run_file=run_file,
            provider_state=provider_state,
            owner=owner,
        )
    finally:
        http.close()
        api.close()
        await engine.dispose()


async def test_signed_client_reaches_real_authorization_and_admitted_lease(pair):
    assert (await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)).reveal() == VALUE
    pair.sm.get_secret_at_version.assert_called_once_with("arn:test:cred-1", VERSION)
    # Both pre/post reads reached Kubernetes, not cached authorization.
    assert len(pair.kubernetes[3]) >= 6
    # A fresh token file is read on the next call, not captured by the singleton.
    pair.run_file.write_text("invalid-replacement")
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    assert pair.sm.get_secret_at_version.call_count == 1


def provider_execution(pair, tmp_path):
    seen = []

    class ReadOnlyOperation:
        provider = "aws"
        provider_account_id = "123456789012"
        operation = "provision"

        def perform(self, credential_path, *, lease):
            assert credential_path.read_text() == VALUE
            seen.append(credential_path)
            return {"checked": True}

    executor = ProviderExecutor(identity=pair.lease.recipient, channel=pair.adapter, isolation_base=tmp_path, clock=lambda: datetime.now(UTC))
    return executor, DeliveryRequest(provider="aws", operation="provision"), ReadOnlyOperation(), seen


async def test_complete_provider_executor_preflight_and_delivery_need_no_shared_key(pair, tmp_path, monkeypatch):
    for name in ("BG_INTERNAL_API_KEY", "ADP_GATEWAY_INTERNAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    executor, request, operation, seen = provider_execution(pair, tmp_path)
    outcome = await asyncio.to_thread(executor.run, pair.lease, request, operation)
    assert outcome.provider_observation == {"checked": True}
    assert len(seen) == 1 and not seen[0].exists()
    assert [request.url.path for request in pair.requests] == [
        "/dev/internal/v1/credential-delivery/preflight",
        "/dev/internal/v1/credential-delivery",
    ]
    assert all("X-Internal-Api-Key" not in request.headers for request in pair.requests)
    assert not hasattr(pair.adapter, "_api_key")
    pair.sm.get_secret_at_version.assert_called_once_with("arn:test:cred-1", VERSION)


@pytest.mark.parametrize("revoked", ["run", "capability", "lease", "budget", "account", "delegation"])
async def test_executor_preflight_refuses_without_fetching_material(pair, tmp_path, revoked):
    if revoked == "run":
        pair.run_file.write_text("invalidated-run-token")
    elif revoked == "capability":
        row = dict(pair.registry_row)
        row.pop("credential_scopes")
        pair.store.client.put_item(TableName="delivery-registry", Item=row)
    elif revoked == "delegation":
        assert (await asyncio.to_thread(pair.api.delete, "/auth/credentials/cred-1/workspaces/workspace")).status_code == 200
    else:
        async with pair.sessions() as writer:
            if revoked == "account":
                await writer.execute(update(CredentialValidationEvidence).values(provider_account_id="999999999999"))
            else:
                await writer.execute(
                    text(
                        "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'"
                        if revoked == "lease"
                        else "UPDATE harness_approval_consumption SET reservation_state='retained'"
                    )
                )
            await writer.commit()
    executor, request, operation, seen = provider_execution(pair, tmp_path)
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(executor.run, pair.lease, request, operation)
    assert not seen
    pair.sm.get_secret_at_version.assert_not_called()
    assert len(pair.requests) == 1
    assert pair.requests[0].url.path.endswith("/preflight")


async def test_shared_key_alone_cannot_authorize_executor_preflight(pair):
    response = await asyncio.to_thread(
        pair.api.post,
        "/internal/v1/credential-delivery/preflight",
        json=pair.adapter._delivery_payload(pair.lease),
        headers={"X-Internal-Api-Key": "evidence-key"},
    )
    assert response.status_code == 403
    pair.sm.get_secret_at_version.assert_not_called()


@pytest.mark.parametrize("mutation", ["fence", "capability"])
async def test_preflight_rechecks_current_authority_after_version_io(pair, mutation):
    entered, resume = threading.Event(), threading.Event()

    def describe(*args):
        entered.set()
        assert resume.wait(20)
        return VERSION

    pair.sm.current_version_id.side_effect = describe
    task = asyncio.create_task(asyncio.to_thread(pair.adapter.revocation_state, pair.lease))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if mutation == "fence":
            async with pair.sessions() as writer:
                await writer.execute(text("UPDATE harness_operation_leases SET fence_token=fence_token+1"))
                await writer.commit()
        else:
            row = dict(pair.registry_row)
            row.pop("credential_scopes")
            pair.store.client.put_item(TableName="delivery-registry", Item=row)
    finally:
        resume.set()
    assert (await task).admits_work is False
    pair.sm.get_secret_at_version.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing", "unversioned", "old_version", "invalid", "permissions", "quota", "future"])
async def test_validation_must_admit_exact_current_version_before_io(pair, mutation):
    async with pair.sessions() as writer:
        if mutation == "missing":
            await writer.execute(delete(CredentialValidationEvidence))
        else:
            changes = {
                "unversioned": {"validated_version_id": None},
                "old_version": {"validated_version_id": "old"},
                "invalid": {"credential_valid": False},
                "permissions": {"permissions_sufficient": False},
                "quota": {"quota_available": False},
                "future": {"checked_at": datetime.now(UTC) + timedelta(hours=1)},
            }[mutation]
            await writer.execute(update(CredentialValidationEvidence).values(**changes))
        await writer.commit()
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    pair.sm.get_secret_at_version.assert_not_called()


async def test_another_valid_delegated_credential_cannot_replace_approved_reference(pair):
    from dataclasses import replace

    other = replace(pair.lease, reference=CredentialReference(credential_id="cred-2", service="aws", label="other"))
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, other)
    pair.sm.get_secret_at_version.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "grant",
        "authority",
        "terminal",
        "attempt",
        "epoch",
        "pod",
        "capability",
        "registry_role",
        "validation_version",
        "validation_state",
        "validation_account",
        "approved_reference",
        "approved_target",
        "payload_tamper",
        "fence",
    ],
)
async def test_committed_revocation_during_parked_fetch_never_returns_material(pair, mutation):
    entered, resume = threading.Event(), threading.Event()

    def fetch(*_):
        entered.set()
        assert resume.wait(20)
        return VALUE, VERSION

    pair.sm.get_secret_at_version.side_effect = fetch
    task = asyncio.create_task(asyncio.to_thread(pair.adapter.fetch_material, pair.lease))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if mutation in {"grant", "authority", "terminal", "attempt", "epoch"}:
            sk, field, value = {
                "grant": ("GRANT#invocation#1", "revoked", {"BOOL": True}),
                "authority": ("AUTHORITY#approval", "status", {"S": "revoked"}),
                "terminal": ("EXEC#invocation", "status", {"S": "completed"}),
                "attempt": ("EXEC#invocation", "current_attempt", {"N": "2"}),
                "epoch": ("EXEC#invocation", "min_acceptable_credential_epoch", {"N": "2"}),
            }[mutation]
            pair.store.client.update_item(
                TableName=pair.store.table,
                Key={"pk": {"S": "TENANT#org"}, "sk": {"S": sk}},
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": field},
                ExpressionAttributeValues={":value": value},
            )
        elif mutation == "pod":
            pair.kubernetes[1]["deleted"] = True
        elif mutation in {"capability", "registry_role"}:
            row = dict(pair.registry_row)
            if mutation == "capability":
                row.pop("credential_scopes")
            else:
                row["role_arn"] = {"S": ROLE + "-replacement"}
            pair.store.client.put_item(TableName="delivery-registry", Item=row)
        else:
            async with pair.sessions() as writer:
                if mutation.startswith("validation"):
                    values = {
                        "validation_version": {"validated_version_id": "replacement"},
                        "validation_state": {"credential_valid": False},
                        "validation_account": {"provider_account_id": "999999999999"},
                    }[mutation]
                    await writer.execute(update(CredentialValidationEvidence).values(**values))
                elif mutation == "fence":
                    await writer.execute(text("UPDATE harness_operation_leases SET fence_token=fence_token+1"))
                else:
                    contract = identity_contract()
                    request = contract.OperationRequest(
                        action="provision",
                        idempotency_key="key",
                        parameters={
                            "credential_id": "cred-1" if mutation == "approved_target" else "cred-2",
                            "credential_service": "aws",
                            "credential_label": "default" if mutation == "approved_target" else "other",
                            "provider": "aws",
                            "provider_account_id": "999999999999" if mutation == "approved_target" else "123456789012",
                        },
                    )
                    await writer.execute(
                        text("UPDATE harness_operations SET request_payload=:payload"), {"payload": contract.encode_payload(request)}
                    )
                    if mutation in {"approved_reference", "approved_target"}:
                        digest = contract.payload_digest(request)
                        await writer.execute(text("UPDATE harness_operations SET plan_digest=:digest"), {"digest": digest})
                        await writer.execute(text("UPDATE harness_approval_consumption SET plan_digest=:digest"), {"digest": digest})
                await writer.commit()
    finally:
        resume.set()
    with pytest.raises(DeliveryRefused):
        await task
    pair.sm.get_secret_at_version.assert_called_once()


async def test_shared_key_cannot_grant_delivery_even_with_valid_run_tokens(pair):
    response = await asyncio.to_thread(
        pair.api.post,
        "/internal/v1/credential-delivery",
        json=pair.adapter._delivery_payload(pair.lease),
        headers={"X-Internal-Api-Key": "evidence-key", "X-Adp-Run-Credential": pair.run_file.read_text(), "X-Adp-Workload-Token": "pod-token"},
    )
    assert response.status_code == 403
    pair.sm.get_secret_at_version.assert_not_called()


async def test_valid_signed_executor_still_requires_registry_capability(pair):
    row = dict(pair.registry_row)
    row.pop("credential_scopes")
    pair.store.client.put_item(TableName="delivery-registry", Item=row)
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    pair.sm.get_secret_at_version.assert_not_called()


async def test_attempt_owned_transport_refuses_another_operation_before_http(pair):
    from dataclasses import replace

    other = replace(pair.lease, binding=replace(pair.lease.binding, operation_id="other-operation"))
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, other)
    assert not pair.requests


@pytest.mark.parametrize("field,value", [("user_id", "another-user"), ("org_id", "another-org")])
async def test_only_vault_owner_can_manage_workspace_authority(pair, field, value):
    setattr(pair.owner, field, value)
    url = "/auth/credentials/cred-1/workspaces/workspace"
    for method, path in ((pair.api.put, url), (pair.api.delete, url), (pair.api.post, url + "/validation")):
        response = await asyncio.to_thread(method, path)
        assert response.status_code == 404
    pair.sm.get_secret_at_version.assert_not_called()


async def test_withdrawal_and_regrant_require_new_independent_validation(pair):
    url = "/auth/credentials/cred-1/workspaces/workspace"
    assert (await asyncio.to_thread(pair.api.delete, url)).status_code == 200
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    assert (await asyncio.to_thread(pair.api.put, url)).status_code == 200
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    assert (await asyncio.to_thread(pair.api.post, url + "/validation")).status_code == 200
    assert (await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)).reveal() == VALUE


@pytest.mark.parametrize("failure", ["permission", "quota"])
async def test_owner_cannot_forge_positive_provider_readings(pair, failure):
    if failure == "permission":
        pair.provider_state["permitted"] = False
    else:
        pair.provider_state["quota"] = 4.0
        pair.provider_state["instances"] = [{"InstanceType": "g5.xlarge"}]
    response = await asyncio.to_thread(
        pair.api.post,
        "/auth/credentials/cred-1/workspaces/workspace/validation",
        json={
            "credential_valid": True,
            "permissions_sufficient": True,
            "quota_available": True,
            "observed_capacity": 999,
        },
    )
    assert response.status_code == 200
    report = response.json()["validation"]
    assert report["permissions_sufficient" if failure == "permission" else "quota_available"] is False
    assert report["observed_capacity"] is None
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)


async def test_produced_validation_is_attested_without_echoing_a_report(pair):
    response = await asyncio.to_thread(pair.api.post, "/auth/credentials/cred-1/workspaces/workspace/validation")
    assert response.status_code == 200
    produced = response.json()
    assert produced["provider_account_id"] == pair.lease.binding.provider_account_id
    response = await asyncio.to_thread(
        pair.api.post,
        "/internal/v1/credential-evidence",
        headers={"X-Internal-Api-Key": "evidence-key"},
        json={
            "org_id": "org",
            "workspace_id": "workspace",
            "credential_id": "cred-1",
            "principal": "user:human",
            "service": "aws",
            "label": "default",
            "report_digest": produced["report_digest"],
        },
    )
    assert response.status_code == 200
    assert response.json()["attested_report_digest"] == produced["report_digest"]
    assert VALUE not in response.text


async def test_rotation_during_validation_invalidates_the_old_positive_reading(pair):
    pair.sm.current_version_id.side_effect = [VERSION, "rotated"]
    response = await asyncio.to_thread(pair.api.post, "/auth/credentials/cred-1/workspaces/workspace/validation")
    assert response.status_code == 409
    pair.sm.current_version_id.side_effect = None
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)


@pytest.mark.parametrize("rotated", [False, True])
async def test_independently_validated_wrong_account_never_reaches_delivery(pair, rotated):
    if rotated:
        assert (await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)).reveal() == VALUE
    other_material = json.dumps({"role_arn": "arn:aws:iam::999999999999:role/replacement"})
    version = "rotated-account-version" if rotated else VERSION
    pair.sm.current_version_id.return_value = version
    pair.sm.get_secret_at_version.return_value = (other_material, version)
    pair.provider_state["account"] = "999999999999"
    response = await asyncio.to_thread(pair.api.post, "/auth/credentials/cred-1/workspaces/workspace/validation")
    assert response.status_code == 200, response.text
    assert response.json()["provider_account_id"] == "999999999999"
    assert response.json()["validated_version_id"] == version
    assert response.json()["validation"]["credential_valid"] is True
    pair.sm.reset_mock()
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    pair.sm.get_secret_at_version.assert_not_called()


async def test_old_evidence_without_an_authenticated_provider_account_refuses(pair):
    async with pair.sessions() as writer:
        await writer.execute(update(CredentialValidationEvidence).values(provider_account_id=None))
        await writer.commit()
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
    pair.sm.get_secret_at_version.assert_not_called()


@pytest.mark.parametrize(
    "version_metadata",
    [pytest.param({}, id="missing"), pytest.param({"VersionId": ""}, id="empty")],
)
async def test_provider_validation_refuses_an_unidentified_served_version(pair, version_metadata):
    client = MagicMock()
    client.describe_secret.return_value = {"VersionIdsToStages": {VERSION: ["AWSCURRENT"]}}
    client.get_secret_value.return_value = {"SecretString": VALUE, **version_metadata}
    helper = SecretsManagerHelper(client=client)
    pair.api.app.dependency_overrides[owner_secrets_manager] = lambda: helper

    response = await asyncio.to_thread(pair.api.post, "/auth/credentials/cred-1/workspaces/workspace/validation")

    assert response.status_code == 503
    client.get_secret_value.assert_called_once_with(SecretId="arn:test:cred-1", VersionId=VERSION)


@pytest.mark.parametrize(
    "version_metadata",
    [pytest.param({}, id="missing"), pytest.param({"VersionId": ""}, id="empty")],
)
async def test_material_delivery_refuses_an_unidentified_served_version(pair, version_metadata):
    client = MagicMock()
    client.describe_secret.return_value = {"VersionIdsToStages": {VERSION: ["AWSCURRENT"]}}
    client.get_secret_value.return_value = {"SecretString": VALUE, **version_metadata}
    helper = SecretsManagerHelper(client=client)
    pair.api.app.dependency_overrides[get_secrets_manager] = lambda: helper

    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)

    client.get_secret_value.assert_called_once_with(SecretId="arn:test:cred-1", VersionId=VERSION)


async def test_validation_cannot_restore_a_concurrently_withdrawn_delegation(pair):
    entered, resume = threading.Event(), threading.Event()

    def fetch(*args):
        entered.set()
        assert resume.wait(20)
        return VALUE, VERSION

    pair.sm.get_secret_at_version.side_effect = fetch
    url = "/auth/credentials/cred-1/workspaces/workspace"
    task = asyncio.create_task(asyncio.to_thread(pair.api.post, url + "/validation"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert (await asyncio.to_thread(pair.api.delete, url)).status_code == 200
    finally:
        resume.set()
    assert (await task).status_code == 409
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)


@pytest.mark.parametrize("failure", ["provider", "configuration"])
async def test_unavailable_revalidation_invalidates_old_positive_evidence(pair, failure):
    if failure == "provider":
        pair.sm.get_secret_at_version.side_effect = RuntimeError("secret-sentinel")
    else:
        pair.api.app.dependency_overrides[get_settings] = lambda: SimpleNamespace(credential_validation_profiles="{}")
    response = await asyncio.to_thread(pair.api.post, "/auth/credentials/cred-1/workspaces/workspace/validation")
    assert response.status_code == 503
    assert "secret-sentinel" not in response.text
    pair.sm.get_secret_at_version.side_effect = None
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)


@pytest.mark.parametrize("action", ["metadata", "deletion", "newer_validation"])
async def test_late_validation_cannot_overwrite_a_newer_authority_generation(pair, action):
    entered, resume = threading.Event(), threading.Event()

    def fetch(*args):
        entered.set()
        assert resume.wait(20)
        return VALUE, VERSION

    pair.sm.get_secret_at_version.side_effect = fetch
    url = "/auth/credentials/cred-1/workspaces/workspace/validation"
    task = asyncio.create_task(asyncio.to_thread(pair.api.post, url))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if action == "metadata":
            result = await asyncio.to_thread(pair.api.patch, "/auth/credentials/cred-1", json={"strict": True})
            assert result.status_code == 200, result.text
        elif action == "deletion":
            result = await asyncio.to_thread(pair.api.delete, "/auth/credentials/cred-1")
            assert result.status_code == 204, result.text
        else:
            pair.sm.get_secret_at_version.side_effect = None
            pair.provider_state["permitted"] = False
            result = await asyncio.to_thread(pair.api.post, url)
            assert result.status_code == 200, result.text
            assert result.json()["validation"]["permissions_sufficient"] is False
    finally:
        resume.set()
    assert (await task).status_code == (404 if action == "deletion" else 409)
    with pytest.raises(DeliveryRefused):
        await asyncio.to_thread(pair.adapter.fetch_material, pair.lease)
