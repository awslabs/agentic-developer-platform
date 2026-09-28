"""Gateway-owned snapshots survive storage/bootstrap; changes revoke bindings."""

# ruff: noqa: F811
import copy
import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import rfc8785

from src.agentauth import task_harness as harness
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.task_runtime import TaskRuntime
from src.tasks.store import WorkBindingError
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, _request, client, store  # noqa: F401

ROOT = Path(__file__).resolve().parents[4]


def snapshot(key="intent-refinement"):
    definition = json.loads((ROOT / f"modules/agent-factory/codex-harness/personas/{key}.json").read_text())
    raw = rfc8785.dumps(definition).decode()
    return {"definition": raw, "digest": hashlib.sha256(raw.encode()).hexdigest(), "instructions": definition["instructions"], "skillSources": "[]"}


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    value = snapshot()
    file = tmp_path / "catalogue.json"
    file.write_text(json.dumps({"schemaVersion": 1, "snapshots": [value]}))
    monkeypatch.setattr(harness, "persona_compatibility_class", lambda name: "codex-sdk" if name == "agent-task-gpt-intent-refinement" else None)
    model = {**_request().model_binding, "model_id": "gpt-5-codex", "transport": "openai_responses"}
    policy = {"allowed_personas": ["agent-task-gpt-intent-refinement"], "limits": {"max_duration_minutes": 30}}
    arguments = {
        "persona": policy["allowed_personas"][0],
        "model_binding": model,
        "limits": _request().run_limits,
        "service_policy": policy,
        "env": {"ADP_CODEX_PERSONA_CATALOG_FILE": str(file)},
    }
    return arguments, harness.freeze_harness(**arguments), file


def test_catalogue_requires_registered_authority_and_preserves_exact_snapshot(frozen):
    arguments, value, file = frozen
    assert value["snapshot"] == snapshot()
    assert value["policy"]["limits"]["maxTurns"] == 8
    assert value["policy"]["limits"]["maxDurationMs"] == 1800000
    with pytest.raises(harness.TaskHarnessError):
        harness.freeze_harness(**{**arguments, "env": {}})
    with pytest.raises(harness.TaskHarnessError):
        harness.freeze_harness(**{**arguments, "persona": "agent-task-unregistered"})
    with pytest.raises(harness.TaskHarnessError):
        harness.freeze_harness(**{**arguments, "service_policy": {**arguments["service_policy"], "allowed_personas": []}})
    file.write_text("invalid changed catalogue")
    assert harness.validate_harness(value, **{k: arguments[k] for k in ("persona", "model_binding", "limits")}) == value


@pytest.mark.parametrize("mutation", ["instructions", "digest", "skill", "duplicate", "executable", "boolean"])
def test_catalogue_rejects_tampering_and_unimplemented_permissions(frozen, mutation):
    arguments, _, file = frozen
    value = snapshot("developer" if mutation == "executable" else "intent-refinement")
    if mutation == "instructions":
        value["instructions"] = "changed"
    if mutation == "digest":
        value["digest"] = "0" * 64
    if mutation == "skill":
        value["skillSources"] = json.dumps([["unbound", "content"]])
    if mutation == "boolean":
        definition = json.loads(value["definition"])
        definition["schemaVersion"] = True
        value["definition"] = rfc8785.dumps(definition).decode()
        value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    if mutation == "executable":
        definition = json.loads(value["definition"])
        definition["key"] = "gpt-intent-refinement"
        value["definition"] = rfc8785.dumps(definition).decode()
        value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    file.write_text(json.dumps({"schemaVersion": 1, "snapshots": [value] * (2 if mutation == "duplicate" else 1)}))
    with pytest.raises(harness.TaskHarnessError):
        harness.freeze_harness(**arguments)


def admit_frozen(client, store, frozen):
    arguments, value, _ = frozen
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}},
        UpdateExpression="SET personas = :p",
        ExpressionAttributeValues={":p": {"SS": [arguments["persona"]]}},
    )
    request = _request(persona=arguments["persona"], model_binding=arguments["model_binding"], harness=value)
    store.accept(request)
    assert store.accept(request).replayed
    return request


@pytest.mark.parametrize("mutation", ["remove", "instructions", "capabilities"])
def test_snapshot_is_protected_by_durable_grant_digest(client, store, frozen, mutation):
    request = admit_frozen(client, store, frozen)
    store.resolve_work(request.dispatch_id)
    key = {"pk": {"S": "TENANT#" + request.tenant}, "sk": {"S": request.grant_reference}}
    update = {"UpdateExpression": "REMOVE harness"}
    if mutation != "remove":
        path = "harness.snapshot.instructions" if mutation == "instructions" else "harness.policy.capabilityLayers.runtime"
        names = {"#p" + str(i): part for i, part in enumerate(path.split("."))}
        update = {
            "UpdateExpression": "SET " + ".".join(names) + " = :v",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": {":v": {"S": "changed"} if mutation == "instructions" else {"L": [{"S": "aws.mutate"}]}},
        }
    client.update_item(TableName=AUTHORITY_TABLE, Key=key, **update)
    with pytest.raises(WorkBindingError):
        store.resolve_work(request.dispatch_id)


def test_bootstrap_returns_frozen_snapshot_without_reading_changed_catalogue(client, store, frozen):
    request = admit_frozen(client, store, frozen)
    frozen[2].write_text("changed catalogue")
    runtime = TaskRuntime(store, env={"AGENT_RUN_CREDENTIAL_KEY": "fixture-key-012345678901234567890123456789"}, clock=lambda: NOW)
    pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace="adp-agents")
    body = {
        "task_id": request.task_id,
        "invocation_id": request.invocation_id,
        "envelope_digest": envelope_digest(request.envelope),
        "workload": {"pod_uid": pod.uid, "namespace": pod.namespace},
    }
    delivery = SimpleNamespace(require_assignment=lambda *args: None, read=lambda uid: {"body": json.dumps(request.envelope)})
    result = runtime.bootstrap(body=body, pod=pod, delivery=delivery)
    assert result["harness"] == request.harness
    assert result["harness"]["snapshot"]["digest"] == frozen[1]["snapshot"]["digest"]
    bad = copy.deepcopy(request.harness)
    bad["policy"]["deadlineMs"] += 1
    from src.tasks.store import TaskStoreError

    with pytest.raises(TaskStoreError):
        store.accept(replace(request, harness=bad, idempotency_key="bad"))


@pytest.mark.parametrize("with_harness", [True, False])
def test_atomic_operation_fence_detects_harness_change_after_read(client, store, frozen, with_harness):
    from botocore.exceptions import ClientError

    from src.tasks.store import _serialize_authority

    if with_harness:
        request = admit_frozen(client, store, frozen)
    else:
        request = _request()
        store.accept(request)
    checks = store._authority_condition_checks(snapshot=store.read_task(request.task_id))
    key = {"pk": {"S": "TENANT#" + request.tenant}, "sk": {"S": request.grant_reference}}
    if with_harness:
        client.update_item(TableName=AUTHORITY_TABLE, Key=key, UpdateExpression="REMOVE harness")
    else:
        client.update_item(
            TableName=AUTHORITY_TABLE, Key=key, UpdateExpression="SET harness = :h", ExpressionAttributeValues=_serialize_authority({":h": frozen[1]})
        )
    with pytest.raises(ClientError, match="TransactionCanceledException"):
        client.transact_write_items(TransactItems=checks)


def test_large_persona_and_input_are_refused_before_worker_handoff(frozen):
    args, value, _ = frozen
    harness.assert_bootstrap_size(value, immutable_input={"instructions": "fixture"}, model_binding=args["model_binding"], limits=args["limits"])
    with pytest.raises(harness.TaskHarnessError, match="frame bound"):
        harness.assert_bootstrap_size(
            value, immutable_input={"instructions": "x" * 65536}, model_binding=args["model_binding"], limits=args["limits"]
        )


def test_exported_codex_schemas_match_gateway_models():
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_harness", ROOT / "modules/gateway/scripts/export_task_harness_contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, content in module.documents().items():
        assert (ROOT / "docs/task-api/contracts/v1/schemas" / name).read_text() == content


@pytest.mark.asyncio
async def test_admission_freezes_before_reservation_and_replay_ignores_changed_catalogue(client, store, frozen, monkeypatch):
    from src.agentauth.task_admission import TaskAdmission, TaskAdmissionError

    arguments, _, file = frozen
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}},
        UpdateExpression="SET personas = :p",
        ExpressionAttributeValues={":p": {"SS": [arguments["persona"]]}},
    )
    policy = {
        **arguments["service_policy"],
        "status": "active",
        "task_scopes": ["submit"],
        "version": 1,
        "model_policy_version": "1",
        "limits": {"max_duration_minutes": 30, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1},
    }
    reservations = []

    async def reserve(**kwargs):
        reservations.append(kwargs)
        return {"status": "reserved", "reservation_id": "fixture-only"}

    async def model(*args, **kwargs):
        return arguments["model_binding"]

    service = TaskAdmission(
        store,
        policies=SimpleNamespace(get=lambda **kw: policy),
        budget=SimpleNamespace(reserve_admission=reserve),
        model_resolver=model,
        clock=lambda: NOW,
    )
    caller = SimpleNamespace(tenant_id="tenant-a", principal_id="svc-principal-1", require=lambda scope: None)
    submit = {"persona": arguments["persona"], "instructions": "Use supplied evidence."}
    monkeypatch.delenv("ADP_CODEX_PERSONA_CATALOG_FILE", raising=False)
    with pytest.raises(TaskAdmissionError, match="prerequisite_unavailable"):
        await service.admit(caller=caller, submit=submit, idempotency_key="new-task", db=None)
    assert not reservations
    monkeypatch.setenv("ADP_CODEX_PERSONA_CATALOG_FILE", str(file))
    receipt = await service.admit(caller=caller, submit=submit, idempotency_key="new-task", db=None)
    task = store.read_task(receipt["task_id"])
    grant = store._get_authority("TENANT#tenant-a", task["grant_reference"])
    assert grant["harness"]["snapshot"] == snapshot()
    file.write_text("changed catalogue")
    replay = await service.admit(caller=caller, submit=submit, idempotency_key="new-task", db=None)
    assert replay["task_id"] == receipt["task_id"] and replay["idempotent_replay"]
    assert len(reservations) == 1


def test_shared_sdk_catalogue_fixture_validates_without_reencoding():
    value = json.loads((ROOT / "docs/task-api/contracts/v1/fixtures/valid/bootstrap-codex-response.json").read_text())
    assert harness.validate_snapshot(value["harness"]["snapshot"])[0].digest == value["harness"]["snapshot"]["digest"]
    assert (
        harness.validate_harness(value["harness"], persona=value["persona"], model_binding=value["model_binding"], limits=value["limits"])
        == value["harness"]
    )


def test_storage_refuses_combined_snapshot_and_input_larger_than_start_frame(store, frozen):
    from src.tasks.store import TaskStoreError

    args, value, _ = frozen
    value = copy.deepcopy(value)
    definition = json.loads(value["snapshot"]["definition"])
    definition["instructions"] = "p" * 24000
    raw = rfc8785.dumps(definition).decode()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    value["snapshot"].update(definition=raw, digest=digest, instructions=definition["instructions"])
    value["policy"]["personaDigest"] = digest
    request = _request(persona=args["persona"], model_binding=args["model_binding"], harness=value, request_payload={"instructions": "t" * 16000})
    with pytest.raises(TaskStoreError, match="frame bound"):
        store.accept(request)
    assert store.read_task(request.task_id) is None


def test_responses_acceptance_cannot_omit_snapshot(store, frozen):
    from src.tasks.store import TaskStoreError

    args, _, _ = frozen
    with pytest.raises(TaskStoreError, match="requires a frozen harness"):
        store.accept(_request(persona=args["persona"], model_binding=args["model_binding"]))


def test_executable_catalogue_freezes_only_granted_schemas_and_requires_tool_probe(frozen):
    from src.agentauth.task_model_binding import TASK_RESPONSES_TOOLS_REQUEST_SHAPE
    from src.agentauth.task_tool_policy import codex_tool_name

    arguments, _, file = frozen
    tool = {
        "permission": "repository.read_change",
        "capability": "repository.read",
        "definition": {
            "type": "function",
            "name": codex_tool_name("repository.read_change"),
            "description": "Read bound change.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": False,
        },
    }
    catalogue = json.loads(file.read_text())
    catalogue["tools"] = [tool]
    file.write_text(json.dumps(catalogue))
    arguments = {
        **arguments,
        "tool_grants": [tool["permission"]],
        "service_policy": {**arguments["service_policy"], "allowed_tools": [tool["permission"]]},
    }
    with pytest.raises(harness.TaskHarnessError):
        harness.freeze_harness(**arguments)
    arguments["model_binding"] = {**arguments["model_binding"], "request_shape_version": TASK_RESPONSES_TOOLS_REQUEST_SHAPE}
    value = harness.freeze_harness(**arguments)
    assert value["tools"] == [tool]
    assert all("repository.read" in layer for layer in value["policy"]["capabilityLayers"].values())
    file.write_text("unavailable")
    assert harness.validate_harness(value, **{k: arguments[k] for k in ("persona", "model_binding", "limits")}) == value
    file.write_text(json.dumps(catalogue))
    for bad in [[], ["repository.merge_change"]]:
        with pytest.raises(harness.TaskHarnessError):
            harness.freeze_harness(**{**arguments, "service_policy": {**arguments["service_policy"], "allowed_tools": bad}})


def test_trace_context_is_frozen_from_gateway_span_only(frozen):
    trace = pytest.importorskip("opentelemetry.trace")
    arguments, _, _ = frozen
    parent = trace.SpanContext(
        trace_id=int("1234567890abcdef" * 2, 16), span_id=int("1234567890abcdef", 16), is_remote=True, trace_flags=trace.TraceFlags(1)
    )
    with trace.use_span(trace.NonRecordingSpan(parent)):
        value = harness.freeze_harness(**arguments)
    assert value["traceparent"] == "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    for invalid in ("00-" + "0" * 32 + "-1234567890abcdef-01", "not-a-trace", "00-" + "1" * 32 + "-" + "0" * 16 + "-01"):
        with pytest.raises(ValueError):
            harness.Harness.model_validate({**value, "traceparent": invalid})


def test_tracing_extra_is_optional_for_admission(frozen, monkeypatch):
    import sys

    arguments, _, _ = frozen
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    assert "traceparent" not in harness.freeze_harness(**arguments)


def projected_snapshot():
    value = snapshot("product")
    definition = json.loads(value["definition"])
    source = "personas/product.md"
    content = (ROOT / "modules/agent-factory/rules" / source).read_bytes()
    definition["sharedRules"] = {"version": 1, "persona": "product", "sources": [{"path": source, "sha256": hashlib.sha256(content).hexdigest()}]}
    value["definition"] = rfc8785.dumps(definition).decode()
    value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    return value


def test_shared_rules_are_preserved_as_small_digest_bound_references():
    value = projected_snapshot()
    frozen, definition = harness.validate_snapshot(value)
    assert frozen.model_dump() == value
    assert definition.sharedRules.persona == "product"
    assert len(json.dumps(value).encode()) < 16000


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "personas/../../secret", "personas//product.md"])
def test_shared_rule_reference_path_escape_fails_before_admission(path):
    value = projected_snapshot()
    definition = json.loads(value["definition"])
    definition["sharedRules"]["sources"][0]["path"] = path
    value["definition"] = rfc8785.dumps(definition).decode()
    value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    with pytest.raises(ValueError):
        harness.validate_snapshot(value)


def test_shared_rule_persona_cannot_replace_the_admitted_persona():
    value = projected_snapshot()
    definition = json.loads(value["definition"])
    definition["sharedRules"]["persona"] = "operations"
    value["definition"] = rfc8785.dumps(definition).decode()
    value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    with pytest.raises(ValueError, match="another persona"):
        harness.validate_snapshot(value)


@pytest.mark.parametrize("dependency", [{"requiredCapabilities": ["aws.assume"]}, {"requiredTools": ["unavailable_tool"]}])
def test_skill_dependencies_cannot_create_authority_at_admission(frozen, dependency):
    arguments, _, file = frozen
    value = snapshot()
    definition = json.loads(value["definition"])
    content = "Use only the explicitly admitted tool."
    definition["skills"] = [{"id": "fixture", "sha256": hashlib.sha256(content.encode()).hexdigest(), **dependency}]
    value["definition"] = rfc8785.dumps(definition).decode()
    value["digest"] = hashlib.sha256(value["definition"].encode()).hexdigest()
    value["instructions"] += "\n\n" + content
    value["skillSources"] = json.dumps([["fixture", content]])
    file.write_text(json.dumps({"schemaVersion": 1, "snapshots": [value]}))
    with pytest.raises(harness.TaskHarnessError, match="prerequisite unavailable"):
        harness.freeze_harness(**arguments)
