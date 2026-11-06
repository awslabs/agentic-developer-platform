"""Operator registration against Moto with explicit offline identity/price proofs."""

import json
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws
from pydantic import ValidationError

from src.agentauth.native_operator_registration import (
    NativeOperatorRegistrationPlan,
    register_initial_plan,
    registration_plan,
    runtime_environment,
)
from src.agentauth.native_test_configuration import CONFIGURATION_KIND, NativeOperatorDeployment, NativeTestDeploymentVerifier, deployment_key
from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.native_test_interpreter_registration import compile_interpreter_contracts
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from tests.agentauth import test_native_test_interpreter_registration as compiler

quote_inputs = compiler.quote_inputs

ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/offline-operator-registrar"
IDENTITY = {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/offline-operator-registrar/offline"}
NOW = datetime.fromtimestamp(1100, UTC)
PROTECTED_RUNTIME_KEYS = (
    "ADP_NATIVE_TEST_AUTHORITY_TABLE",
    "ADP_NATIVE_TEST_DEPLOYMENT_ID",
    "ADP_OPERATOR_TOOL_BINDING",
    "ADP_TOOLS_FACADE_ROLE_ARN",
    "ADP_TOOLS_RECONCILER_ROLE_ARN",
    "ADP_TOOLS_GATEWAY_ROLE_ARN",
)


@pytest.fixture
def plan_inputs(quote_inputs):
    inputs, evidence = quote_inputs
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    binding = inputs.binding
    deployment = NativeOperatorDeployment(
        deployment_id=binding.deployment_id,
        tenant_id="offline-existing-tenant",
        revision=str(uuid4()),
        state="active",
        scope="operator_local",
        cap_usd="20",
        coverage_contract="adp-native-test-paid-model-routes/v1",
        coverage_digest="9" * 64,
        expires_at=1900,
        source_revision=binding.source_revision,
        release_sha256=binding.release_sha256,
        gateway_origin=binding.gateway_origin,
        contract_sha256=compiled["binding_sha256"],
        allowed_models=[],
        allowed_routes=[],
        allowed_tools=["code_interpreter." + step for step in ("start", "execute", "result", "file")],
        effect_coverage_contract="adp-native-operator-interpreter/v1",
        effect_coverage_digest=compiled["effect_coverage_digest"],
        effects=compiled["effects"],
    )
    return dict(inputs=inputs, evidence=evidence, deployment=deployment, account_id=ACCOUNT, region=REGION, registration_role_arn=ROLE)


@pytest.fixture
def plan(plan_inputs):
    return registration_plan(**plan_inputs)


@pytest.fixture
def authority(plan, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with mock_aws():
        config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
        client = boto3.client("dynamodb", region_name=REGION, config=config)
        client.create_table(
            TableName=plan.table,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = NativeTestStore(table_name=plan.table, client=client)
        sts = boto3.client("sts", region_name=REGION, config=config)
        monkeypatch.setattr(sts, "get_caller_identity", Mock(return_value=dict(IDENTITY)))
        transaction = Mock(wraps=client.transact_write_items)
        monkeypatch.setattr(client, "transact_write_items", transaction)
        yield SimpleNamespace(store=store, sts=sts, transaction=transaction, client=client)


def apply(plan, authority, **changes):
    arguments = dict(expected_plan_sha256=digest(plan.model_dump()), store=authority.store, sts_client=authority.sts, clock=lambda: NOW)
    arguments.update(changes)
    return register_initial_plan(plan, **arguments)


def test_exact_operator_plan_roundtrip_and_explicit_runtime_overlay(plan):
    assert NativeOperatorRegistrationPlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.producer == plan.deployment.scope == "operator_local"
    assert len(plan.contracts) == len(plan.deployment.effects) == 7
    assert plan.maximum_original_smoke_usd == "0.090000"
    overlay = runtime_environment(plan, current_environment={})
    assert "ADP_NATIVE_TEST_TOOL_BINDING" not in overlay
    assert NativeOperatorRegistrationPlan.model_fields.keys().isdisjoint({"designation", "consent", "approval", "credential", "run"})
    assert overlay == {
        "ADP_NATIVE_TEST_AUTHORITY_TABLE": plan.table,
        "ADP_NATIVE_TEST_DEPLOYMENT_ID": plan.binding.deployment_id,
        "ADP_OPERATOR_TOOL_BINDING": plan.binding.model_dump_json(),
        "ADP_TOOLS_FACADE_ROLE_ARN": plan.binding.facade_role_arn,
        "ADP_TOOLS_RECONCILER_ROLE_ARN": plan.binding.reconciler_role_arn,
        "ADP_TOOLS_GATEWAY_ROLE_ARN": plan.binding.catalogue.gateway_role_arn,
    }
    assert runtime_environment(plan, current_environment=overlay) == overlay


@pytest.mark.parametrize(
    "environment", [{"ADP_NATIVE_TEST_TOOL_BINDING": "{}"}, {"ADP_OPERATOR_TOOL_BINDING": "{}"}, {"ADP_NATIVE_TEST_TOOL_BINDING": None}]
)
def test_runtime_output_refuses_conflicting_or_ambiguous_bindings(plan, environment):
    with pytest.raises(AdmissionRefusedError, match="runtime_binding_conflict"):
        runtime_environment(plan, current_environment=environment)


@pytest.mark.parametrize("key", PROTECTED_RUNTIME_KEYS)
def test_runtime_output_refuses_partial_protected_configuration_conflicts(plan, key):
    existing = {key: "different-protected-value", "UNRELATED_SETTING": "keep"}
    with pytest.raises(AdmissionRefusedError, match="runtime_binding_conflict"):
        runtime_environment(plan, current_environment=existing)
    assert existing == {key: "different-protected-value", "UNRELATED_SETTING": "keep"}


@pytest.mark.parametrize("key", PROTECTED_RUNTIME_KEYS)
def test_runtime_output_accepts_equal_partial_configuration_and_preserves_unrelated_data(plan, key):
    protected = runtime_environment(plan, current_environment={})
    existing = {key: protected[key], "UNRELATED_SETTING": "keep"}
    assert runtime_environment(plan, current_environment=existing) == {**protected, "UNRELATED_SETTING": "keep"}
    assert existing == {key: protected[key], "UNRELATED_SETTING": "keep"}


def test_runtime_output_fills_empty_values_without_removing_other_configuration(plan):
    existing = {key: "" for key in PROTECTED_RUNTIME_KEYS}
    existing.update(ADP_NATIVE_TEST_TOOL_BINDING="", UNRELATED_SETTING="keep")
    protected = runtime_environment(plan, current_environment={})
    assert runtime_environment(plan, current_environment=existing) == {**existing, **protected}


@pytest.mark.parametrize("field,value", [("designation", {}), ("producer", "ci"), ("schema_version", "2.0")])
def test_operator_plan_never_accepts_ci_designation_or_generic_schema(plan, field, value):
    with pytest.raises(ValidationError):
        NativeOperatorRegistrationPlan.model_validate({**plan.model_dump(), field: value})


def test_plan_preserves_exact_near_cap_quote_without_rounding_down(plan_inputs):
    inputs, evidence = plan_inputs["inputs"], plan_inputs["evidence"]
    compiler.change_proof(inputs, evidence, "setup", "shared_allocation", maximum_quantity="1984.2375")
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    deployment = NativeOperatorDeployment.model_validate({**plan_inputs["deployment"].model_dump(), "effects": compiled["effects"]})
    plan = registration_plan(**{**plan_inputs, "deployment": deployment})
    assert plan.maximum_original_smoke_usd == "19.922375"
    compiler.change_proof(inputs, evidence, "setup", "shared_allocation", maximum_quantity="1992.000000001")
    with pytest.raises(AdmissionRefusedError, match="complete_contract_exceeds_cap"):
        registration_plan(**{**plan_inputs, "deployment": deployment})


@pytest.mark.parametrize(
    "fault", ["source", "release", "origin", "deployment", "expiry", "account", "region", "role_account", "runtime_role", "effects"]
)
def test_builder_refuses_wrong_deployment_source_role_scope_or_expiry(plan_inputs, fault):
    inputs = dict(plan_inputs)
    deployment = inputs["deployment"]
    changes = {
        "source": {"source_revision": "f" * 40},
        "release": {"release_sha256": "f" * 64},
        "origin": {"gateway_origin": "https://foreign.invalid"},
        "deployment": {"deployment_id": "foreign"},
        "expiry": {"expires_at": 2001},
        "effects": {"effects": list(reversed(deployment.effects))},
    }
    if fault in changes:
        inputs["deployment"] = deployment.model_copy(update=changes[fault])
    elif fault == "account":
        inputs["account_id"] = "999999999999"
    elif fault == "region":
        inputs["region"] = "eu-west-1"
    elif fault == "role_account":
        inputs["registration_role_arn"] = ROLE.replace(ACCOUNT, "999999999999")
    else:
        inputs["registration_role_arn"] = inputs["inputs"].binding.catalogue.gateway_role_arn
    with pytest.raises(AdmissionRefusedError, match="registration_binding_refused"):
        registration_plan(**inputs)


def test_initial_write_contains_only_eight_append_only_protected_documents(plan, authority):
    result = apply(plan, authority)
    assert result == {
        "schema_version": "1.0",
        "producer": "operator_local",
        "plan_sha256": digest(plan.model_dump()),
        "registered": True,
        "governance_complete": False,
    }
    authority.sts.get_caller_identity.assert_called_once_with()
    authority.transaction.assert_called_once()
    writes = authority.transaction.call_args.kwargs["TransactItems"]
    assert len(writes) == 8
    assert all(set(row) == {"Put"} and row["Put"]["ConditionExpression"] == "attribute_not_exists(pk)" for row in writes)
    items = authority.client.scan(TableName=plan.table)["Items"]
    assert len(items) == 8
    assert {row["pk"]["S"] for row in items} == {"TOOLS_TEST_DEPLOYMENT#" + plan.binding.deployment_id}
    assert {row["kind"]["S"] for row in items} == {CONFIGURATION_KIND, "native_test_effect_contract_v1"}
    verifier = NativeTestDeploymentVerifier(
        store=authority.store,
        deployment_id=plan.binding.deployment_id,
        clock=lambda: NOW,
        coverage=lambda: plan.deployment.coverage_digest,
        effect_coverage=lambda: plan.deployment.effect_coverage_digest,
    )
    assert verifier.current() == plan.deployment
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        apply(plan, authority)
    assert authority.client.scan(TableName=plan.table)["Items"] == items


@pytest.mark.parametrize("fault", ["hash", "store_table", "plan_table", "source", "contract", "scope", "total", "before", "expired"])
def test_writer_revalidates_reviewed_plan_before_identity_or_writes(plan, authority, fault):
    kwargs = {}
    if fault == "hash":
        kwargs["expected_plan_sha256"] = "f" * 64
    elif fault == "store_table":
        authority.store.table = "foreign-authority"
    elif fault == "plan_table":
        plan = plan.model_copy(update={"table": "foreign-authority"})
    elif fault == "source":
        plan = plan.model_copy(update={"binding": plan.binding.model_copy(update={"source_revision": "f" * 40})})
    elif fault == "contract":
        next(iter(plan.contracts.values()))["request_sha256"] = "f" * 64
    elif fault == "scope":
        plan.deployment.allowed_models.append("foreign-model")
    elif fault == "total":
        plan = plan.model_copy(update={"maximum_original_smoke_usd": "0.01"})
    else:
        kwargs["clock"] = lambda: datetime.fromtimestamp(999 if fault == "before" else 1900, UTC)
    with pytest.raises(AdmissionRefusedError, match="registration_plan_refused"):
        apply(plan, authority, **kwargs)
    authority.sts.get_caller_identity.assert_not_called()
    authority.transaction.assert_not_called()


@pytest.mark.parametrize("fault", ["account", "role", "ddb_retry", "sts_retry", "ddb_region", "sts_region"])
def test_writer_refuses_wrong_session_role_region_or_retry_configuration(plan, authority, fault):
    if fault == "account":
        authority.sts.get_caller_identity.return_value = {**IDENTITY, "Account": "999999999999"}
    elif fault == "role":
        authority.sts.get_caller_identity.return_value = {**IDENTITY, "Arn": IDENTITY["Arn"].replace("offline-operator-registrar", "offline-facade")}
    else:
        meta = authority.client.meta if fault.startswith("ddb") else authority.sts.meta
        if fault.endswith("retry"):
            meta.config.retries["total_max_attempts"] = 2
        else:
            meta.config.region_name = "eu-west-1"
    with pytest.raises(AdmissionRefusedError):
        apply(plan, authority)
    authority.transaction.assert_not_called()


def test_identity_delay_cannot_register_after_original_expiry(plan, authority):
    current = [NOW]

    def delayed_identity():
        current[0] = datetime.fromtimestamp(plan.deployment.expires_at, UTC)
        return dict(IDENTITY)

    authority.sts.get_caller_identity.side_effect = delayed_identity
    with pytest.raises(AdmissionRefusedError, match="registration_plan_refused"):
        apply(plan, authority, clock=lambda: current[0])
    authority.transaction.assert_not_called()


def test_registration_detaches_reviewed_snapshot_before_identity_lookup(plan, authority):
    original = plan.model_dump()

    def identity_after_caller_mutation():
        plan.deployment.allowed_models.append("foreign-model")
        next(iter(plan.contracts.values()))["request_sha256"] = "f" * 64
        return dict(IDENTITY)

    authority.sts.get_caller_identity.side_effect = identity_after_caller_mutation
    receipt = apply(plan, authority)
    assert receipt["plan_sha256"] == digest(original)
    assert authority.store.document(deployment_key(plan.binding.deployment_id), CONFIGURATION_KIND) == original["deployment"]
    for identifier, document in original["contracts"].items():
        key = {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + plan.binding.deployment_id}, "sk": {"S": "EFFECT_CONTRACT#" + identifier}}
        assert authority.store.document(key, "native_test_effect_contract_v1") == document


def test_contract_conflict_is_atomic_and_does_not_replace_existing_document(plan, authority):
    identifier = next(iter(plan.contracts))
    key = {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + plan.binding.deployment_id}, "sk": {"S": "EFFECT_CONTRACT#" + identifier}}
    original = authority.store.item(key, "offline-existing-protected-document", {"retained": True})
    authority.client.put_item(TableName=plan.table, Item=original)
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        apply(plan, authority)
    authority.transaction.assert_called_once()
    assert authority.client.scan(TableName=plan.table)["Items"] == [original]


@pytest.mark.parametrize("committed", [False, True])
def test_lost_transaction_ack_is_unconfirmed_without_replay_or_read_as_success(plan, authority, monkeypatch, committed):
    original = authority.transaction._mock_wraps

    def lost_ack(**kwargs):
        if committed:
            original(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://offline.invalid")

    authority.transaction.side_effect = lost_ack
    monkeypatch.setattr(authority.store, "_read", lambda *args: pytest.fail("ambiguous registration must not read as success"))
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        apply(plan, authority)
    authority.transaction.assert_called_once()
    assert len(authority.client.scan(TableName=plan.table)["Items"]) == (8 if committed else 0)
    if committed:
        row = authority.client.get_item(TableName=plan.table, Key=deployment_key(plan.binding.deployment_id))["Item"]
        assert row == authority.store.item(deployment_key(plan.binding.deployment_id), CONFIGURATION_KIND, plan.deployment.model_dump())


@pytest.fixture
def cli_inputs(plan_inputs, tmp_path):
    evidence_files = {}
    for sha, raw in plan_inputs["evidence"].items():
        location = tmp_path / sha
        location.write_bytes(raw)
        evidence_files[sha] = str(location)
    value = {
        "quote_inputs": plan_inputs["inputs"].model_dump(),
        "evidence_files": evidence_files,
        "deployment": plan_inputs["deployment"].model_dump(exclude={"effects", "effect_coverage_digest", "contract_sha256"}),
        "account_id": ACCOUNT,
        "region": REGION,
        "registration_role_arn": ROLE,
        "current_environment": {},
    }
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(value))
    return path, value


def test_offline_cli_prepares_private_plan_and_operator_environment(cli_inputs, plan, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/register_operator_interpreter.py"
    output, runtime = tmp_path / "plan.json", tmp_path / "runtime.json"
    result = subprocess.run(
        [sys.executable, str(script), "prepare", "--inputs", str(cli_inputs[0]), "--output", str(output), "--runtime-output", str(runtime)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"plan_sha256": digest(plan.model_dump()), "registered": False}
    assert NativeOperatorRegistrationPlan.model_validate_json(output.read_bytes()) == plan
    assert json.loads(runtime.read_text()) == {"data": runtime_environment(plan, current_environment={})}
    assert output.stat().st_mode & 0o777 == runtime.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["designation", "generic_binding", "injected_contracts"])
def test_cli_refuses_ci_identity_generic_binding_or_caller_contracts(cli_inputs, fault):
    path, value = cli_inputs
    if fault == "designation":
        value["designation"] = {"kind": "ci"}
    elif fault == "generic_binding":
        value["current_environment"] = {"ADP_NATIVE_TEST_TOOL_BINDING": "{}"}
    else:
        value["deployment"]["effects"] = []
    path.write_text(json.dumps(value))
    prepare = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/register_operator_interpreter.py"))["prepare"]
    with pytest.raises((ValueError, AdmissionRefusedError)):
        prepare(path)
