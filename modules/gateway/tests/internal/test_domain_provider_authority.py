"""Governed authority never substitutes tenant assertions for current proof."""

import copy
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from src.domain_proxy import superplane as proxy
from src.internal import domain_provider_authority as authority
from src.internal import domain_provider_session as broker
from src.shared.domain_provider_contract import OWNER, WORKSPACE_NAMESPACE, canonical, digest, handle, managed_policies, reserved, validate

ACCOUNT = "123456789012"
ROLE_ID = "AROA" + "A" * 17
ORG = "12345678-1234-1234-1234-123456789012"
REQUEST = "87654321-1234-1234-1234-123456789012"
WORKSPACE = str(uuid.uuid5(WORKSPACE_NAMESPACE, f"{ORG}/{REQUEST}"))
ROLE_NAME = "adp-dev-spp-" + digest([ORG, WORKSPACE])[:32]
ROLE = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"


@dataclass
class Binding:
    org_id: str = ORG
    adp_org_id: str = "tenant"
    worker_namespace: str = "superplane-demo"


def record():
    return dict(
        version=1,
        credential_id=handle(ROLE),
        owner=OWNER,
        domain="superplane",
        installation_id="a" * 24,
        adp_org_id="tenant",
        org_id=ORG,
        workspace_id=str(uuid.uuid5(WORKSPACE_NAMESPACE, f"{ORG}/{REQUEST}")),
        request_id=REQUEST,
        subject="subject",
        user_id="canonical",
        membership_id="membership",
        account_id=ACCOUNT,
        region="us-east-1",
        service="aws",
        label="installation-provider",
        role_arn=ROLE,
        role_id=ROLE_ID,
        policy_sha256="b" * 64,
        secret_arn=f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:provider-abcdef",
        secret_version="d" * 32,
        binding_sha256=digest(asdict(Binding())),
        generation=1,
        status="active",
        expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        child_boundary_arn=ROLE.replace(":role/", ":policy/") + "-child-boundary",
        managed_policy_arns=managed_policies(ROLE),
        child_boundary_sha256="c" * 64,
        validation_profile={
            "region": "us-east-1",
            "image_id": "ami-12345678",
            "instance_type": "t3.small",
            "subnet_id": "subnet-12345678",
            "security_group_ids": ["sg-12345678"],
        },
    )


class Store:
    def __init__(self, value):
        self.value = value
        self.reads = 0
        self.fail = False
        self.transaction = None

    def get_item(self, **kw):
        assert kw["ConsistentRead"] is True
        self.reads += 1
        if self.fail:
            raise RuntimeError("private SDK diagnostics must not escape")
        if self.value is None:
            return {}
        return {
            "Item": {"record_id": {"S": self.value["credential_id"]}, "document": {"S": canonical(self.value)}, "revision": {"S": digest(self.value)}}
        }

    def transact_write_items(self, **kw):
        condition = kw["TransactItems"][0]["ConditionCheck"]
        if condition["ExpressionAttributeValues"][":revision"]["S"] != digest(self.value):
            raise RuntimeError("condition changed")
        self.transaction = kw


@pytest.fixture
def context(monkeypatch):
    value = record()
    store = Store(value)
    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.setenv("ADP_DOMAIN_PROVIDER_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("BG_SUPERPLANE_ROUTE_BUCKET", f"adp-terraform-state-{ACCOUNT}")
    monkeypatch.setenv("ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE", "adp-dev-superplane-provider-authorities")
    monkeypatch.setenv("ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE", "adp-dev-superplane-provider-evidence")
    monkeypatch.setattr(authority, "aws_client", lambda service: store)
    monkeypatch.setattr(authority, "binding_for", lambda *args: Binding())
    monkeypatch.setattr(authority, "current_registrations", lambda *args: None)
    monkeypatch.setattr(proxy, "registration", lambda **kw: {"installation_id": "a" * 24, "namespace": "superplane-demo"})
    monkeypatch.setattr(authority, "policy_identity", lambda *args: (ROLE_ID, "b" * 64))
    monkeypatch.setattr(authority, "boundary_identity", lambda *args: "c" * 64)
    identity = dict(subject="subject", adp_org_id="tenant", principal_type="human", active=True, enabled=True, membership_id="membership")
    monkeypatch.setattr(authority, "current_human_identity", AsyncMock(side_effect=lambda *args, **kw: identity.copy()))
    user = SimpleNamespace(id="canonical", user_kind="human", is_shadow=False)
    db = SimpleNamespace(scalar=AsyncMock(return_value=user), commit=AsyncMock())
    secret = {"role_arn": ROLE, "account_id": ACCOUNT, "external_id": "owner-generated-external-id"}
    sm = SimpleNamespace(current_version_id=lambda arn: "d" * 32, get_secret_at_version=lambda arn, version: (json.dumps(secret), version))
    return SimpleNamespace(value=value, store=store, db=db, sm=sm, identity=identity, user=user, secret=secret)


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "tenant-admin"},
        {"domain": "other"},
        {"workspace_id": REQUEST},
        {"request_id": ORG},
        {"version": True},
        {"generation": True},
        {"role_id": "invalid"},
        {"extra": True},
        {"credential_id": handle(ROLE).upper()},
        {"credential_id": "spda1:not-a-uuid"},
    ],
)
def test_closed_provenance_record_refuses_aliases_or_forged_shape(change):
    with pytest.raises(ValueError):
        validate({**record(), **change})


def test_reservation_is_derived_without_fabricating_workspace():
    assert validate(record())["workspace_id"] == str(uuid.uuid5(WORKSPACE_NAMESPACE, f"{ORG}/{REQUEST}"))
    assert len(handle(ROLE)) == 42 and reserved(handle(ROLE).upper())
    assert not reserved(str(uuid.uuid4()))


@pytest.mark.parametrize("change", [{"status": "revoked"}, {"expires_at": "2020-01-01T00:00:00+00:00"}])
def test_current_store_refuses_revocation_and_expiry(context, change):
    context.value.update(change)
    with pytest.raises(HTTPException) as error:
        authority.read(handle(ROLE))
    assert error.value.status_code == 403


def test_unavailable_inventory_is_not_personal_fallback(context):
    context.store.fail = True
    with pytest.raises(HTTPException) as error:
        authority.read(handle(ROLE))
    assert error.value.status_code == 503 and "SDK" not in str(error.value.detail)


async def test_current_resolver_establishes_beneficiary_and_rechecks_store(context):
    assert await authority.resolve(context.db, context.sm, handle(ROLE), subject="subject", org_id=ORG) == context.value
    assert context.store.reads == 2
    assert context.db.scalar.await_count == 1


@pytest.mark.parametrize("kind", ["subject", "membership", "canonical", "disabled", "secret", "installation", "role", "policy", "boundary"])
async def test_current_identity_and_provider_drift_refuse(context, monkeypatch, kind):
    subject = "subject"
    if kind == "subject":
        subject = "other"
    elif kind == "membership":
        context.identity["membership_id"] = "other"
    elif kind == "canonical":
        context.user.id = "other"
    elif kind == "disabled":
        context.identity["enabled"] = False
    elif kind == "secret":
        context.sm.current_version_id = lambda arn: "rotated"
    elif kind == "installation":
        monkeypatch.setattr(proxy, "registration", lambda **kw: {})
    elif kind == "boundary":
        monkeypatch.setattr(authority, "boundary_identity", lambda *args: "expanded")
    else:
        monkeypatch.setattr(
            authority, "policy_identity", lambda *args: ("recreated" if kind == "role" else ROLE_ID, "expanded" if kind == "policy" else "b" * 64)
        )
    with pytest.raises(HTTPException):
        await authority.resolve(context.db, context.sm, handle(ROLE), subject=subject)


async def test_revocation_during_metadata_io_refuses(context):
    def version(arn):
        context.value["status"] = "revoked"
        return "d" * 32

    context.sm.current_version_id = version
    with pytest.raises(HTTPException):
        await authority.resolve(context.db, context.sm, handle(ROLE), subject="subject")


def reading():
    return SimpleNamespace(
        credential_valid=True,
        permissions_sufficient=True,
        quota_available=True,
        observed_capacity=None,
        detail="EC2 DryRun only",
        checked_at=datetime.now(UTC),
        provider_account_id=ACCOUNT,
    )


def test_validation_write_atomically_conditions_authority_and_preserves_observations(context):
    authority.write_report(context.value, reading())
    transaction = context.store.transaction["TransactItems"]
    assert len(transaction) == 3
    assert transaction[0]["ConditionCheck"]["TableName"].endswith("authorities")
    for item in transaction[1:]:
        assert item["Put"]["TableName"].endswith("evidence")
        assert item["Put"]["ConditionExpression"] == "attribute_not_exists(record_id)"
    assert transaction[2]["Put"]["Item"]["record_id"]["S"].endswith("/current")


def test_validation_cannot_write_across_authority_generation_race(context):
    before = copy.deepcopy(context.value)
    context.value["generation"] += 1
    with pytest.raises(RuntimeError):
        authority.write_report(before, reading())
    assert context.store.transaction is None


@pytest.mark.parametrize("foreign", ["org", "workspace", "service", "label", "principal"])
async def test_evidence_denies_foreign_reference(context, foreign):
    args = dict(
        credential_id=handle(ROLE),
        org_id=ORG,
        workspace_id=context.value["workspace_id"],
        service="aws",
        label=context.value["label"],
        principal="subject",
        report_digest=None,
    )
    args[{"org": "org_id", "workspace": "workspace_id"}.get(foreign, foreign)] = "other"
    assert await authority.evidence(context.db, context.sm, **args) is None


async def test_evidence_truthfully_reports_installation_owner_and_exact_delegation(context):
    answer = await authority.evidence(
        context.db,
        context.sm,
        credential_id=handle(ROLE),
        org_id=ORG,
        workspace_id=context.value["workspace_id"],
        service="aws",
        label=context.value["label"],
        principal="subject",
        report_digest=None,
    )
    assert answer.owner_principal == "domain_app:superplane/" + "a" * 24
    assert answer.delegated_to_workspaces == frozenset({context.value["workspace_id"]})
    assert "external_id" not in asdict(answer)


async def test_account_adapter_uses_owner_material_and_redacts_response(context, monkeypatch):
    forwarded = {}

    async def upstream(request, path, *, content, before_send):
        await before_send()
        forwarded.update(json.loads(content))
        return Response(content, status_code=201)

    monkeypatch.setattr(proxy, "_proxy_to_domain", upstream)
    body = proxy.AccountRegistrationRequest(name="demo", provider="aws", account_id=ACCOUNT, adp_credential_id=handle(ROLE))
    answer = await proxy.register_governed_account(body, None, SimpleNamespace(user_id="subject"), context.db, context.sm, "tenant")
    assert forwarded["adp_credential_ids"] == [handle(ROLE)] and forwarded["external_id"] == context.secret["external_id"]
    assert "external_id" not in json.loads(answer.body) and "role_arn" not in json.loads(answer.body)


@pytest.mark.parametrize("mutation", ["none", "revoke", "role-id", "after-audit", "lease"])
async def test_governed_broker_checks_paid_authority_and_races(context, monkeypatch, mutation):
    from src.internal.sts_assume_service import AssumeRoleResult

    monkeypatch.setattr(authority, "read_report", lambda *args: datetime.now(UTC))
    issued = False

    async def refresh(**kwargs):
        if issued and mutation == "lease":
            raise HTTPException(403, "lease changed")

    def assume(**kw):
        nonlocal issued
        issued = True
        assert kw["user_id"] == "canonical" and kw["session_duration_seconds"] == 900
        assert kw["session_policy"] == "cleanup-policy" and kw["agent_id"] == "superplane-operation"
        if mutation == "revoke":
            context.value["status"] = "revoked"
        return AssumeRoleResult(
            "key",
            "secret",
            "token",
            (datetime.now(UTC) + timedelta(seconds=899)).isoformat(),
            "us-east-1",
            "unused",
            f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE_NAME}/session",
            ("AROA" + "B" * 17 if mutation == "role-id" else ROLE_ID) + ":session",
        )

    async def commit():
        if mutation == "after-audit":
            context.value["generation"] += 1

    context.db.commit = commit
    monkeypatch.setattr(broker, "assume_role", assume)
    monkeypatch.setattr("src.internal.credential_routes._write_audit", AsyncMock())
    args = dict(
        binding=Binding(),
        principal="run#1",
        operation={"requester": "subject", "workspace_id": context.value["workspace_id"]},
        credential_id=handle(ROLE),
        service="aws",
        label=context.value["label"],
        account=ACCOUNT,
        deadline=datetime.now(UTC) + timedelta(hours=1),
        refresh=refresh,
        policy="cleanup-policy",
        entry_identity=None,
        entry_arn=None,
        preflight_only=False,
    )
    call = broker.governed_session(SimpleNamespace(region="us-east-1", operation_id="operation"), context.db, context.sm, **args)
    if mutation == "none":
        answer = await call
        assert answer["credential_id"] == handle(ROLE) and "external_id" not in answer
    else:
        with pytest.raises(HTTPException):
            await call
    assert issued


async def test_generic_exact_lookup_refuses_reserved_handle_before_db(context):
    from src.auth.vault_evidence import resolve_exact_credential

    assert await resolve_exact_credential(context.db, org_id="tenant", credential_id=handle(ROLE)) is None
    context.db.scalar.assert_not_awaited()


async def test_actual_api_account_and_evidence_consumers_accept_typed_handle(context):
    answer = await authority.evidence(
        context.db,
        context.sm,
        credential_id=handle(ROLE),
        org_id=ORG,
        workspace_id=context.value["workspace_id"],
        service="aws",
        label=context.value["label"],
        principal="subject",
        report_digest=None,
    )
    evidence = asdict(answer)
    evidence["delegated_to_workspaces"] = list(answer.delegated_to_workspaces)
    evidence["expires_at"] = answer.expires_at.isoformat()
    app = Path(__file__).resolve().parents[3] / "domain-apps/superplane"
    code = """
import json, sys
from app.schemas.account import RegisterAccountRequest
from app.adapters.adp_vault_client import AdpVaultClient
from superplane_contracts.connections import CredentialReference, authorize_delegation
data=json.load(sys.stdin)
account=RegisterAccountRequest.model_validate(data["account"])
reference=CredentialReference(account.adp_credential_ids[0], "aws", data["evidence"]["label"])
reader=AdpVaultClient(base_url="https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev", region="us-east-1")
wire=data["evidence"]
evidence=reader._evidence_from(wire, org_id=wire["org_id"], workspace_id=wire["workspace_id"], reference=reference, report_digest=None)
assert evidence is not None
for workspace, expected in ((wire["workspace_id"], True), ("foreign", False)):
    decision=authorize_delegation(principal="subject", workspace_id=workspace, ownership=evidence.ownership, reference=reference,
                                  granted_permissions=frozenset({"workspace:renew_credential"}))
    assert decision.allowed is expected
assert evidence.ownership.owner_principal.startswith("domain_app:superplane/")
"""
    payload = {
        "evidence": evidence,
        "account": {
            "name": "demo",
            "provider": "aws",
            "account_id": ACCOUNT,
            "role_arn": ROLE,
            "external_id": "synthetic",
            "adp_credential_ids": [handle(ROLE)],
        },
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+asyncpg://localhost/superplane_offline_test",
            "SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS": "true",
            "PYTHONPATH": os.pathsep.join([str(app / "contracts"), str(app / "src/superplane-api")]),
        },
    )
    assert completed.returncode == 0, completed.stderr


async def test_full_paid_broker_uses_governed_preflight_and_retains_final_fence_check(context, monkeypatch):
    future = datetime.now(UTC) + timedelta(hours=1)
    operation = dict(
        requester="subject",
        plan_digest="sealed",
        approval_id="approval",
        approval_expires_at=future,
        job_id="job",
        workspace_id=context.value["workspace_id"],
        request_payload="sealed",
    )
    lease = dict(
        operation_id="operation",
        org_id=ORG,
        workspace_id=context.value["workspace_id"],
        holder="run#1",
        attempt_id="run#1",
        fence_token=1,
        runtime_deadline=future,
    )
    state = (Binding(), "run#1", "record", SimpleNamespace(expires_at=future), operation, lease)
    monkeypatch.setattr(broker, "current", AsyncMock(return_value=state))
    monkeypatch.setattr(broker, "sealed_target", lambda *args: ((handle(ROLE), "aws", context.value["label"]), ACCOUNT))
    personal = AsyncMock(side_effect=AssertionError("governed handles must not use personal delivery"))
    monkeypatch.setattr(broker, "deliver_credential", personal)
    monkeypatch.setattr(authority, "read_report", lambda *args: datetime.now(UTC))
    body = SimpleNamespace(region="us-east-1", operation_id="operation")
    answer = await broker.provider_session(None, body, context.db, context.sm, preflight_only=True)
    assert answer["admits_work"] is True
    personal.assert_not_awaited()

    def changed_fence(*args):
        lease["fence_token"] += 1

    monkeypatch.setattr(authority, "read_report", changed_fence)
    with pytest.raises(HTTPException):
        await broker.provider_session(None, body, context.db, context.sm, preflight_only=True)


@pytest.mark.parametrize("shorten", [False, True])
async def test_minted_session_near_deadline_uses_actual_expiry_and_still_rechecks_shortening(context, monkeypatch, shorten):
    from src.internal.sts_assume_service import AssumeRoleResult

    start = datetime.now(UTC)
    elapsed = [0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=elapsed[0])

    monkeypatch.setattr(broker, "datetime", Clock)
    deadline = start + timedelta(seconds=901)
    operation = dict(
        requester="subject",
        plan_digest="sealed",
        approval_id="approval",
        approval_expires_at=deadline,
        job_id="job",
        workspace_id=context.value["workspace_id"],
        request_payload="sealed",
    )
    lease = dict(
        operation_id="operation",
        org_id=ORG,
        workspace_id=context.value["workspace_id"],
        holder="run#1",
        attempt_id="run#1",
        fence_token=1,
        runtime_deadline=deadline,
    )
    state = (Binding(), "run#1", "record", SimpleNamespace(expires_at=deadline), operation, lease)
    monkeypatch.setattr(broker, "current", AsyncMock(return_value=state))
    monkeypatch.setattr(broker, "sealed_target", lambda *args: ((handle(ROLE), "aws", context.value["label"]), ACCOUNT))
    monkeypatch.setattr(authority, "read_report", lambda *args: datetime.now(UTC))
    monkeypatch.setattr("src.internal.credential_routes._write_audit", AsyncMock())

    def assume(**kwargs):
        elapsed[0] = 10  # Valid actual session; fewer than another 900 seconds remain.
        if shorten:
            operation["approval_expires_at"] = start + timedelta(seconds=899)
        return AssumeRoleResult(
            "key",
            "secret",
            "token",
            (start + timedelta(seconds=900)).isoformat(),
            "us-east-1",
            "unused",
            f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE_NAME}/session",
            ROLE_ID + ":session",
        )

    monkeypatch.setattr(broker, "assume_role", assume)
    call = broker.provider_session(None, SimpleNamespace(region="us-east-1", operation_id="operation"), context.db, context.sm)
    if shorten:
        with pytest.raises(HTTPException):
            await call
    else:
        answer = await call
        assert datetime.fromisoformat(answer["expiration"]) == start + timedelta(seconds=900)


def test_exact_managed_shards_pin_default_versions_and_canonical_documents():
    from src.shared.domain_provider_contract import policy_identity

    state = {"version": "v1", "document": {"Statement": []}, "attached": managed_policies(ROLE), "inline": []}
    iam = SimpleNamespace(
        get_role=lambda **kw: {"Role": {"Arn": ROLE, "RoleId": ROLE_ID, "AssumeRolePolicyDocument": {"Statement": []}}},
        list_role_policies=lambda **kw: {"PolicyNames": state["inline"]},
        list_attached_role_policies=lambda **kw: {"AttachedPolicies": [{"PolicyArn": arn} for arn in state["attached"]]},
        get_policy=lambda **kw: {"Policy": {"DefaultVersionId": state["version"]}},
        get_policy_version=lambda **kw: {"PolicyVersion": {"Document": state["document"]}},
    )
    original = policy_identity(iam, ROLE, managed_policies(ROLE))
    state["version"] = "v2"
    assert policy_identity(iam, ROLE, managed_policies(ROLE)) != original
    state["version"] = "v1"
    state["document"] = {"Statement": [{"Action": "changed"}]}
    assert policy_identity(iam, ROLE, managed_policies(ROLE)) != original
    state["attached"] += ["arn:aws:iam::aws:policy/AdministratorAccess"]
    with pytest.raises(ValueError):
        policy_identity(iam, ROLE, managed_policies(ROLE))
    state["attached"] = managed_policies(ROLE)
    state["inline"] = ["hidden-policy"]
    with pytest.raises(ValueError):
        policy_identity(iam, ROLE, managed_policies(ROLE))


async def test_bedrock_account_is_not_installation_authority(context, monkeypatch):
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "000000000000")
    assert await authority.resolve(context.db, context.sm, handle(ROLE), subject="subject") == context.value
    monkeypatch.delenv("ADP_DOMAIN_PROVIDER_ACCOUNT_ID")
    with pytest.raises(HTTPException):
        await authority.resolve(context.db, context.sm, handle(ROLE), subject="subject")
