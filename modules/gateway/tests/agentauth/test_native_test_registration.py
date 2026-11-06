"""Offline complete compiler/registration/runtime contracts; no live prices."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from fastapi import FastAPI
from sqlalchemy import update

from src.agentauth.native_test_configuration import CONFIGURATION_KIND, NativeTestAllInDeployment, deployment_key
from src.agentauth.native_test_enrollment import AllInApprovalRequest, NativeTestStore
from src.agentauth.native_test_registration import (
    EnforcedQuantityProof,
    NativeRegistrationPlan,
    PricedQuantity,
    StatelessQuoteInputs,
    aws_offer_tariff,
    compile_stateless_contracts,
    register_initial_plan,
    registration_plan,
    runtime_environment,
)
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.native_test_stateless_runtime import NativeStatelessBinding, NativeStatelessRuntime, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from src.shared.models.organization import User
from src.shared.models.tool_entitlement import ToolEntitlement
from tests.agentauth import test_native_test_all_in_issuer as all_in_tests
from tests.agentauth.test_native_test_catalogue import install_control, offline_catalogue
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = all_in_tests.admission, all_in_tests.sessions, all_in_tests.native, all_in_tests.all_in


@pytest.fixture(autouse=True)
def stateless_fixture_scope(monkeypatch):
    monkeypatch.setattr(all_in_tests.old, "TOOL", "websearch.search")


def inputs_for(f):
    from src.agentauth.native_test_effect_quotes import COMPONENTS

    binding = NativeStatelessBinding(
        schema_version="2.0",
        deployment_id="offline-gateway",
        release_sha256="d" * 64,
        authority_table=f.store.table,
        execution_table="offline-facade-executions",
        facade_role_arn="arn:aws:iam::123456789012:role/offline-facade",
        reconciler_role_arn="arn:aws:iam::123456789012:role/offline-reconciler",
        catalogue=offline_catalogue()[0],
    )
    raw = canonical(
        {
            "products": {"offline": {"attributes": {"regionCode": "us-east-1"}}},
            "terms": {
                "OnDemand": {
                    "offline": {
                        "offline.term": {
                            "effectiveDate": "2026-01-01T00:00:00Z",
                            "priceDimensions": {"offline.rate": {"unit": "offline-unit", "rateCode": "offline.rate", "pricePerUnit": {"USD": "1"}}},
                        }
                    }
                }
            },
        }
    ).encode()
    price_sha = hashlib.sha256(raw).hexdigest()
    tariff = aws_offer_tariff(raw, expected_sha256=price_sha, sku="offline", term_code="offline.term", rate_code="offline.rate", region="us-east-1")
    source = b"Offline source quantity proof, not production evidence"
    source_sha = hashlib.sha256(source).hexdigest()
    evidence = {price_sha: raw, source_sha: source}
    rows = {}
    for action in ("setup", "list", "call"):
        rows[action] = []
        for component in sorted(COMPONENTS):
            quantity = "1" if component == "dynamodb_requests" else "0"
            proof = EnforcedQuantityProof(
                schema_version="1.0",
                release_sha256=binding.release_sha256,
                action=action,
                component=component,
                unit=tariff.unit,
                maximum_quantity=quantity,
                basis="source_enforced_maximum",
                explanation="Explicit offline substitution",
                source_sha256=[source_sha],
            )
            raw_proof = canonical(proof.model_dump()).encode()
            proof_sha = hashlib.sha256(raw_proof).hexdigest()
            evidence[proof_sha] = raw_proof
            rows[action].append(PricedQuantity(component=component, quantity=quantity, tariff=tariff, enforced_limit_sha256=proof_sha))
    now = int(f.fixture.clock[0].timestamp())
    return StatelessQuoteInputs(
        schema_version="1.0",
        binding=binding,
        query="explicit offline fixture query",
        not_before=now - 1,
        expires_at=now + 3600,
        setup=rows["setup"],
        listing=rows["list"],
        search=rows["call"],
    ), evidence


def configured(e, inputs, compiled):
    return NativeTestAllInDeployment.model_validate(
        {
            **e.configuration.model_dump(),
            "allowed_models": [],
            "allowed_routes": [],
            "allowed_tools": ["websearch.search"],
            "effect_coverage_digest": stateless_coverage(inputs.binding),
            "effects": compiled["effects"],
        }
    )


async def test_actual_compiler_drives_concrete_readiness_and_original_setup_hold(all_in, monkeypatch):
    from src.agentauth import native_test_routes
    from src.internal import tools_authorize

    e, f = all_in, all_in.f
    inputs, evidence = inputs_for(f)
    compiled = compile_stateless_contracts(inputs, evidence=evidence)
    assert compiled["maximum_original_smoke_usd"] == "4.000000"
    configuration = configured(e, inputs, compiled)
    for identifier, document in compiled["contracts"].items():
        key = {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#offline-gateway"}, "sk": {"S": "EFFECT_CONTRACT#" + identifier}}
        f.fixture.client.put_item(TableName=f.store.table, Item=f.store.item(key, "native_test_effect_contract_v1", document))
    f.fixture.client.put_item(
        TableName=f.store.table, Item=f.store.item(deployment_key("offline-gateway"), CONFIGURATION_KIND, configuration.model_dump())
    )
    f.request = AllInApprovalRequest.model_validate(
        {
            **f.request.model_dump(),
            "allowed_models": [],
            "allowed_routes": [],
            "tools": ["websearch.search"],
            "effects": compiled["effects"],
        }
    )
    app = FastAPI()
    app.include_router(tools_authorize.router, prefix="/internal/v1")
    app.include_router(native_test_routes.router)
    runtime = NativeTestRuntime(app, f.store, enrollment=f.issuer, admission=f.service)
    bridge = NativeStatelessRuntime(runtime=runtime, budget=f.fixture.budget, binding=inputs.binding, execution_client=f.fixture.client)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", inputs.binding.facade_role_arn)
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", inputs.binding.reconciler_role_arn)
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", inputs.binding.catalogue.gateway_role_arn)
    control_calls = install_control(monkeypatch, *offline_catalogue())

    def forbidden_catalogue():
        raise AssertionError("Native stateless freshness must not enumerate the ordinary catalogue")

    monkeypatch.setattr("src.internal.tools_catalogue.gateway_catalogue", forbidden_catalogue)
    f.issuer._effect_runtime = bridge
    f.store._deployment_verifier.effect_coverage = bridge.coverage
    approval, token = await all_in_tests.old.enroll(f, monkeypatch)
    assert control_calls == ["gateway", "target"] * 6  # Owner approval + five setup freshness checks.
    assert await bridge.require_ready(approval) is None
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 1
    body = canonical(
        {
            "schema_version": "1.0",
            "action": "call",
            "tool": "websearch___search",
            "arguments": {"query": inputs.query},
            "request_id": str(uuid4()),
            "execution_id": None,
            "ttl_seconds": None,
        }
    ).encode()
    offered = await runtime.tool_admission.prepare(context=f.login, credential=token, operation_id=str(uuid4()), body=body)
    assert offered["allowed"] is True
    assert control_calls == ["gateway", "target"] * 9  # Three fresh checks in prepare.
    assert (await f.fixture.reservations.snapshot(f.service._target(approval.authority()))).total_usd == 2

    # The exact metadata reader does not replace canonical SQL identity or live
    # policy/consent/entitlement rechecks. Change each after successful admission.
    async with f.sessions() as db:
        await db.execute(update(User).where(User.id == "canonical-human").values(is_shadow=True))
        await db.commit()
    with pytest.raises(AdmissionRefusedError, match="membership"):
        await f.issuer.current_fixture(approval)
    assert len(control_calls) == 18
    async with f.sessions() as db:
        await db.execute(update(User).where(User.id == "canonical-human").values(is_shadow=False))
        await db.execute(update(ToolEntitlement).values(enabled=False))
        await db.commit()
    with pytest.raises(AdmissionRefusedError, match="entitlement"):
        await f.issuer.current_fixture(approval)
    assert len(control_calls) == 20
    async with f.sessions() as db:
        await db.execute(update(ToolEntitlement).values(enabled=True))
        await db.commit()
    key = {"pk": {"S": "TENANT#tenant-one"}, "sk": {"S": "TOOLS_POLICY#canonical-human"}}
    f.fixture.client.update_item(
        TableName=f.store.table, Key=key, UpdateExpression="SET consented_tools = :none", ExpressionAttributeValues={":none": {"L": []}}
    )
    with pytest.raises(AdmissionRefusedError, match="consent_refused"):
        await f.issuer.current_fixture(approval)
    assert len(control_calls) == 22
    f.fixture.client.update_item(
        TableName=f.store.table,
        Key=key,
        UpdateExpression="SET consented_tools = :tools, #state = :state",
        ExpressionAttributeNames={"#state": "status"},
        ExpressionAttributeValues={":tools": {"L": [{"S": "websearch.search"}]}, ":state": {"S": "revoked"}},
    )
    with pytest.raises(AdmissionRefusedError, match="consent_unavailable"):
        await f.issuer.current_fixture(approval)
    assert len(control_calls) == 22
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", inputs.binding.facade_role_arn)
    with pytest.raises(AdmissionRefusedError, match="coverage"):
        await bridge.require_ready(approval)


@pytest.mark.parametrize("fault", ["account", "region", "metadata", "role"])
def test_registration_binds_catalogue_deployment_and_coverage(all_in, fault):
    e, f = all_in, all_in.f
    inputs, evidence = inputs_for(f)
    compiled = compile_stateless_contracts(inputs, evidence=evidence)
    deployment = configured(e, inputs, compiled)
    region, account = "us-east-1", "123456789012"
    if fault == "account":
        account = "999999999999"
    elif fault == "region":
        region = "eu-west-1"
    else:
        changes = {"target_configuration_sha256": "e" * 64} if fault == "metadata" else {"gateway_role_arn": "arn:aws:iam::123456789012:role/changed"}
        catalogue = inputs.binding.catalogue.model_copy(update=changes)
        inputs = inputs.model_copy(update={"binding": inputs.binding.model_copy(update={"catalogue": catalogue})})
    with pytest.raises(AdmissionRefusedError, match="registration_binding_refused"):
        registration_plan(
            inputs=inputs,
            evidence=evidence,
            designation=f.designation,
            deployment=deployment,
            account_id=account,
            region=region,
            registration_role_arn="arn:aws:iam::123456789012:role/offline-registrar",
        )


@pytest.mark.parametrize("fault", ["rate", "price_bytes", "proof_bytes", "quantity", "release", "missing_component"])
def test_compiler_refuses_incomplete_or_changed_price_and_bound_sources(all_in, fault):
    inputs, evidence = inputs_for(all_in.f)
    row = inputs.search[0]
    if fault == "rate":
        inputs.search[0] = row.model_copy(update={"tariff": row.tariff.model_copy(update={"usd_per_unit": "0.0001"})})
    elif fault == "price_bytes":
        evidence[row.tariff.source_sha256] = b"changed"
    elif fault == "proof_bytes":
        evidence[row.enforced_limit_sha256] = b"changed"
    elif fault == "quantity":
        inputs.search[0] = row.model_copy(update={"quantity": "999"})
    elif fault == "release":
        inputs = inputs.model_copy(update={"binding": inputs.binding.model_copy(update={"release_sha256": "e" * 64})})
    else:
        inputs.search.pop()
    with pytest.raises(AdmissionRefusedError):
        compile_stateless_contracts(inputs, evidence=evidence)


async def test_initial_registration_is_atomic_and_never_replaces_existing_authority(all_in):
    e, f = all_in, all_in.f
    inputs, evidence = inputs_for(f)
    inputs = inputs.model_copy(update={"binding": inputs.binding.model_copy(update={"authority_table": "offline-new-native-authority"})})
    compiled = compile_stateless_contracts(inputs, evidence=evidence)
    plan = registration_plan(
        inputs=inputs,
        evidence=evidence,
        designation=f.designation,
        deployment=configured(e, inputs, compiled),
        account_id="123456789012",
        region="us-east-1",
        registration_role_arn="arn:aws:iam::123456789012:role/offline-registrar",
    )
    assert plan.schema_version == "2.0" and plan.binding.schema_version == "2.0"
    overlay = runtime_environment(plan)
    assert json.loads(overlay["ADP_NATIVE_TEST_TOOL_BINDING"])["catalogue"] == inputs.binding.catalogue.model_dump()
    assert overlay["ADP_TOOLS_GATEWAY_ROLE_ARN"] == inputs.binding.catalogue.gateway_role_arn
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NativeRegistrationPlan.model_validate({**plan.model_dump(), "schema_version": "1.0"})
    client = boto3.client("dynamodb", region_name="us-east-1", config=Config(retries={"total_max_attempts": 1}))
    client.create_table(
        TableName=plan.table,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    )
    store = NativeTestStore(table_name=plan.table, client=client)
    sts = SimpleNamespace(
        meta=SimpleNamespace(config=Config(retries={"total_max_attempts": 1})),
        get_caller_identity=lambda: {"Account": plan.account_id, "Arn": "arn:aws:sts::123456789012:assumed-role/offline-registrar/operator"},
    )
    args = {"expected_plan_sha256": digest(plan.model_dump()), "store": store, "sts_client": sts, "now": f.issuer.clock()}
    with pytest.raises(AdmissionRefusedError, match="plan_refused"):
        register_initial_plan(plan, **{**args, "expected_plan_sha256": "0" * 64})
    valid_identity = sts.get_caller_identity
    for identity in (
        {"Account": "999999999999", "Arn": "arn:aws:sts::999999999999:assumed-role/offline-registrar/operator"},
        {"Account": plan.account_id, "Arn": "arn:aws:sts::123456789012:assumed-role/offline-facade/operator"},
    ):
        sts.get_caller_identity = lambda: identity
        with pytest.raises(AdmissionRefusedError, match="role_refused"):
            register_initial_plan(plan, **args)
        assert client.scan(TableName=plan.table)["Items"] == []
    sts.get_caller_identity = valid_identity
    assert register_initial_plan(plan, **args)["registered"] is True
    assert store.designation(f.designation.subject_id) == f.designation
    assert len(client.scan(TableName=plan.table)["Items"]) == 5
    with pytest.raises(AdmissionRefusedError):
        register_initial_plan(plan, **args)
    assert len(client.scan(TableName=plan.table)["Items"]) == 5


@pytest.mark.parametrize("form", ["get_products", "raw_price_list", "response_wrapper"])
def test_tariff_retained_api_formats_and_highest_tier(form):
    product = {
        "product": {"sku": "offline", "attributes": {"fromLocation": "US East (N. Virginia)"}},
        "terms": {
            "OnDemand": {
                "offline.term": {
                    "effectiveDate": "2026-01-01T00:00:00Z",
                    "priceDimensions": {
                        "offline.high": {"unit": "GB", "rateCode": "offline.high", "pricePerUnit": {"USD": "0.09"}},
                        "offline.low": {"unit": "GB", "rateCode": "offline.low", "pricePerUnit": {"USD": "0.08"}},
                    },
                }
            }
        },
    }
    if form == "get_products":
        offer = {"products": [product], "truncated": False}
    else:
        offer = {"PriceList": [json.dumps(product)]}
        if form == "response_wrapper":
            offer = {"response": offer}
    raw = canonical(offer).encode()
    args = {
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "sku": "offline",
        "term_code": "offline.term",
        "region": "US East (N. Virginia)",
        "geography_attribute": "fromLocation",
    }
    assert aws_offer_tariff(raw, **args, rate_code="offline.high").usd_per_unit == "0.09"
    with pytest.raises(AdmissionRefusedError, match="tariff_unavailable"):
        aws_offer_tariff(raw, **args, rate_code="offline.low")
    payload = offer.get("response", offer)
    payload["truncated" if form == "get_products" else "NextToken"] = True
    changed = canonical(offer).encode()
    with pytest.raises(AdmissionRefusedError, match="tariff_unavailable"):
        aws_offer_tariff(changed, **{**args, "expected_sha256": hashlib.sha256(changed).hexdigest()}, rate_code="offline.high")
