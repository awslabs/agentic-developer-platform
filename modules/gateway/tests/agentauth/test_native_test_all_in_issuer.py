"""Offline issuer/setup slice: real SQL/Moto/Lua, explicit lifecycle substitute.

The HTTP/provider/cleanup runtime is deliberately replaced below. These tests
prove issuance and setup reservation only, not production all-in readiness.
"""

import hashlib
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.native_test_configuration import (
    CONFIGURATION_KIND,
    COVERAGE_CONTRACT,
    NativeTestAllInDeployment,
    NativeTestDeploymentVerifier,
    deployment_key,
)
from src.agentauth.native_test_effect_admission import NativeTestEffectAdmission
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound
from src.agentauth.native_test_enrollment import ALL_IN_AUDIENCE, AllInApprovalRequest, EffectRegistration, NativeTestStore
from src.agentauth.native_test_setup_admission import NativeSetupHold, reserve_native_setup
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from tests.agentauth import test_native_test_enrollment as old
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native = old.admission, old.sessions, old.native
SETUP, EFFECT = "native-effect:test.setup", "native-effect:offline.call"
BODY = b'{"tool":"offline"}'


def write_contract(f, *, effect_key, phase, cost, body):
    now = int(f.fixture.clock[0].timestamp())
    terms = [
        CostBound(component, "offline-unit", "1" if component == "model_setup" else "0", cost, "b" * 64, "c" * 64) for component in sorted(COMPONENTS)
    ]
    document = dict(
        schema_version="1.0",
        state="active",
        scope="model_tool_setup_cleanup",
        effect_key=effect_key,
        request_sha256=hashlib.sha256(body).hexdigest(),
        source_sha256="d" * 64,
        pricing_revision="a" * 64,
        not_before=now - 1,
        expires_at=now + 3600,
        quote_seconds=60,
        session_seconds=300,
        cleanup_seconds=600,
        cleanup_attempts=2,
        components=[asdict(term) for term in terms],
    )
    source = ProtectedEffectQuoteAdapter(
        store=f.store, deployment_id="offline-gateway", contract_id=phase, contract_sha256=digest(document), source_sha256="d" * 64
    )
    f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(source.key, "native_test_effect_contract_v1", document))
    registration = EffectRegistration(
        effect_key=effect_key, action=phase, tool=old.TOOL, contract_id=phase, contract_sha256=digest(document), source_sha256="d" * 64
    )
    return registration, source, document


@pytest.fixture
async def all_in(native):
    f = native
    setup, setup_source, setup_doc = write_contract(
        f, effect_key=SETUP, phase="setup", cost="2", body=canonical({"effect_key": SETUP, "phase": "test_setup"}).encode()
    )
    effect, effect_source, effect_doc = write_contract(f, effect_key=EFFECT, phase="call", cost="6", body=BODY)
    f.request = AllInApprovalRequest(
        **{**f.request.model_dump(), "cleanup_expires_at": f.request.dispatch_expires_at + 600}, scope="all_in", effects=[setup, effect]
    )
    configuration = NativeTestAllInDeployment(
        deployment_id="offline-gateway",
        tenant_id="tenant-one",
        revision=str(uuid4()),
        state="active",
        scope="all_in",
        cap_usd="20",
        coverage_contract=COVERAGE_CONTRACT,
        coverage_digest="a" * 64,
        expires_at=f.request.cleanup_expires_at,
        jobs=[f.request.job],
        allowed_models=f.request.allowed_models,
        allowed_tools=f.request.tools,
        allowed_routes=f.request.allowed_routes,
        effect_coverage_contract="adp-native-test-tool-lifecycle/v1",
        effect_coverage_digest="b" * 64,
        effects=[setup, effect],
    )
    f.fixture.client.put_item(
        TableName=f.store.table, Item=f.store.item(deployment_key("offline-gateway"), CONFIGURATION_KIND, configuration.model_dump())
    )
    f.store._deployment_verifier = NativeTestDeploymentVerifier(
        store=f.store, deployment_id="offline-gateway", clock=f.issuer.clock, coverage=lambda: "a" * 64, effect_coverage=lambda: "b" * 64
    )

    class OfflineLifecycleRuntime:
        enrollment = f.issuer

        async def require_ready(self, approval):
            # Explicit missing production HTTP/provider/cleanup coverage proof.
            return None

        async def reserve_setup(self, context, approval, capability):
            return await reserve_native_setup(context=context, enrollment=f.issuer, approval=approval, capability=capability, budget=f.fixture.budget)

    f.issuer._effect_runtime = OfflineLifecycleRuntime()
    return SimpleNamespace(
        f=f, setup_source=setup_source, setup_doc=setup_doc, effect_source=effect_source, effect_doc=effect_doc, configuration=configuration
    )


async def test_all_in_issuance_holds_setup_then_shares_exact_model_and_effect_meter(all_in, monkeypatch):
    e, f = all_in, all_in.f
    approval, token = await old.enroll(f, monkeypatch)
    capability = f.store.capability("tenant-one", approval.credential_id)
    assert capability["status"] == "active" and capability["audience"] == ALL_IN_AUDIENCE
    with pytest.raises(AdmissionRefusedError, match="all_in_credential"):
        await f.issuer.verify_effect(f.login, token, SETUP)
    assert f.store.authority("tenant-one", approval.run_id) == approval.authority()
    target = f.service._target(approval.authority())
    assert (await f.fixture.reservations.snapshot(target)).total_usd == 2
    await old.prepare(f)
    assert (await f.fixture.reservations.snapshot(target)).total_usd == 14
    service = NativeTestEffectAdmission(
        effect_key=EFFECT, store=f.store, budget=f.fixture.budget, identity_verifier=f.issuer, quote_adapter=e.effect_source, clock=f.issuer.clock
    )
    prepared = await service.prepare_effect(context=f.login, credential=token, operation_id=str(uuid4()), body=BODY)
    assert (await f.fixture.reservations.snapshot(target)).total_usd == 20
    await service.begin_effect_dispatch(prepared, context=f.login, credential=token, body=BODY)
    with pytest.raises(AdmissionRefusedError, match="budget_refused"):
        await old.prepare(f)
    assert (await f.fixture.reservations.snapshot(target)).total_usd == 20


async def test_model_only_credential_never_becomes_all_in_identity(native, monkeypatch):
    _, token = await old.enroll(native, monkeypatch)
    with pytest.raises(AdmissionRefusedError, match="all_in_credential"):
        await native.issuer.verify_effect(native.login, token, EFFECT)


async def test_tool_only_all_in_approval_needs_no_invented_model_permission(all_in, monkeypatch):
    e, f = all_in, all_in.f
    f.request = AllInApprovalRequest(**{**f.request.model_dump(), "allowed_models": [], "allowed_routes": []})
    e.configuration = NativeTestAllInDeployment(**{**e.configuration.model_dump(), "allowed_models": [], "allowed_routes": []})
    f.fixture.client.put_item(
        TableName=f.store.table, Item=f.store.item(deployment_key("offline-gateway"), CONFIGURATION_KIND, e.configuration.model_dump())
    )
    approval, token = await old.enroll(f, monkeypatch)
    assert approval.allowed_models == approval.allowed_routes == []
    with pytest.raises(AdmissionRefusedError, match="unsupported_test_provider_quote"):
        await old.prepare(f)
    effect = NativeTestEffectAdmission(
        effect_key=EFFECT,
        store=f.store,
        budget=f.fixture.budget,
        identity_verifier=f.issuer,
        quote_adapter=e.effect_source,
        clock=f.issuer.clock,
    )
    prepared = await effect.prepare_effect(context=f.login, credential=token, operation_id=str(uuid4()), body=BODY)
    await effect.begin_effect_dispatch(prepared, context=f.login, credential=token, body=BODY)
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 8


@pytest.mark.parametrize("drift", ["other_contract", "copied_contract_key", "source_digest", "other_store"])
async def test_same_effect_key_cannot_substitute_unapproved_contract_or_adapter(all_in, monkeypatch, drift):
    e, f = all_in, all_in.f
    approval, token = await old.enroll(f, monkeypatch)
    source, body = e.effect_source, BODY
    if drift in {"other_contract", "copied_contract_key"}:
        document = dict(e.effect_doc)
        if drift == "other_contract":
            body = b'{"tool":"unapproved-other-request"}'
            document["request_sha256"] = hashlib.sha256(body).hexdigest()
        source = ProtectedEffectQuoteAdapter(
            store=f.store, deployment_id="offline-gateway", contract_id="other-contract", contract_sha256=digest(document), source_sha256="d" * 64
        )
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(source.key, "native_test_effect_contract_v1", document))
    elif drift == "source_digest":
        source.source_sha256 = "f" * 64
    else:
        source.store = NativeTestStore(table_name=f.store.table, client=f.fixture.client)
    service = NativeTestEffectAdmission(
        effect_key=EFFECT, store=f.store, budget=f.fixture.budget, identity_verifier=f.issuer, quote_adapter=source, clock=f.issuer.clock
    )
    with pytest.raises(AdmissionRefusedError):
        await service.prepare_effect(context=f.login, credential=token, operation_id=str(uuid4()), body=body)
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 2


async def test_missing_lifecycle_runtime_refuses_before_approval(all_in):
    f = all_in.f
    f.issuer._effect_runtime = None
    with pytest.raises(AdmissionRefusedError, match="all_in_runtime"):
        await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)
    assert f.store.document(f.store.key("tenant-one", "APPROVAL", f.request.run_id), "native_test_approval_v1") is None


@pytest.mark.parametrize("response", [False, True, {"ready": False}])
async def test_readiness_return_value_cannot_be_mistaken_for_success(all_in, monkeypatch, response):
    f = all_in.f
    monkeypatch.setattr(f.issuer._effect_runtime, "require_ready", AsyncMock(return_value=response))
    with pytest.raises(AdmissionRefusedError, match="all_in_runtime"):
        await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)
    assert f.store.document(f.store.key("tenant-one", "APPROVAL", f.request.run_id), "native_test_approval_v1") is None


@pytest.mark.parametrize("change", ["missing_hold", "request_id", "targets"])
async def test_activation_itself_requires_exact_original_setup_hold(all_in, monkeypatch, change):
    f = all_in.f
    reserve = f.issuer._effect_runtime.reserve_setup

    async def corrupted(*args):
        hold = await reserve(*args)
        if change == "missing_hold":
            return None
        if change == "request_id":
            hold.prepared._context._policy_request_id = "other-reservation"
        else:
            hold.prepared._context._budget_admission_targets = []
        return hold

    monkeypatch.setattr(f.issuer._effect_runtime, "reserve_setup", corrupted)
    approval = await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)
    monkeypatch.setattr(f.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    with pytest.raises(AdmissionRefusedError):
        await f.issuer.exchange(f.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert f.store.capability("tenant-one", approval.credential_id)["status"] == "issuing"
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 2


@pytest.mark.parametrize("fault", ["coverage", "contract_missing", "component_missing", "source_changed"])
async def test_registered_all_in_coverage_and_complete_source_are_mandatory(all_in, fault):
    e, f = all_in, all_in.f
    if fault == "coverage":
        f.store._deployment_verifier.effect_coverage = None
    elif fault == "contract_missing":
        f.fixture.client.delete_item(TableName=f.store.table, Key=e.setup_source.key)
    else:
        if fault == "component_missing":
            e.setup_doc["components"].pop()
        else:
            e.setup_doc["source_sha256"] = "f" * 64
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(e.setup_source.key, "native_test_effect_contract_v1", e.setup_doc))
    with pytest.raises(AdmissionRefusedError):
        await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)


async def test_contract_revocation_after_setup_hold_prevents_activation_and_keeps_original_hold(all_in, monkeypatch):
    e, f = all_in, all_in.f
    approval = await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)
    monkeypatch.setattr(f.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    confirm = NativeSetupHold.confirm

    async def revoked(*args, **kwargs):
        await confirm(*args, **kwargs)
        e.setup_doc["state"] = "revoked"
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(e.setup_source.key, "native_test_effect_contract_v1", e.setup_doc))

    monkeypatch.setattr(NativeSetupHold, "confirm", revoked)
    with pytest.raises(AdmissionRefusedError):
        await f.issuer.exchange(f.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert f.store.capability("tenant-one", approval.credential_id)["status"] == "issuing"
    assert f.store.authority("tenant-one", approval.run_id)["state"] == "issuing_setup"
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 2


async def test_lost_activation_ack_never_reissues_or_resets_setup_hold(all_in, monkeypatch):
    f = all_in.f
    approval = await f.issuer.approve(f.owner, subject_id=f.designation.subject_id, request=f.request)
    monkeypatch.setattr(f.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    original = f.fixture.client.transact_write_items

    def lost_ack(**kwargs):
        activates = any(
            '"status":"active"' in item.get("Put", {}).get("Item", {}).get("document", {}).get("S", "") for item in kwargs["TransactItems"]
        )
        result = original(**kwargs)
        if activates:
            raise EndpointConnectionError(endpoint_url="offline")
        return result

    monkeypatch.setattr(f.fixture.client, "transact_write_items", lost_ack)
    for _ in range(2):
        with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
            await f.issuer.exchange(f.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert f.store.capability("tenant-one", approval.credential_id)["status"] == "active"
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 2
