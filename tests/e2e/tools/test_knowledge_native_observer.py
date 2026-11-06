"""Offline stored-record oracle; records never count as backend/billing proof."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.e2e.tools.knowledge_native_observer import (
    NativeKnowledgeOperationObserver,
    canonical,
    digest,
)
from tests.e2e.tools.scenarios import FixtureUnavailable


@pytest.fixture
def records():
    run, operation, execution, request, credential, revision, epoch = (
        str(uuid4()) for _ in range(7)
    )
    tool, arguments = "knowledge___search", {"query": "private indexed fact"}
    job = {
        "repository_id": "123",
        "sha": "a" * 40,
        "workflow_sha": "b" * 40,
        "run_id": "42",
        "run_attempt": "1",
    }
    designation = {
        "tenant_id": "tenant",
        "user_id": "owner",
        "team_id": "team",
        "department_id": "department",
        "subject_id": "subject",
        "deployment_id": "offline",
    }
    effect = {
        "effect_key": "native-effect:read",
        "action": "call",
        "tool": "knowledge.search",
        "source_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    approval = {
        "scope": "all_in",
        "run_id": run,
        "designation": designation,
        "job": job,
        "revision": revision,
        "credential_id": credential,
        "meter_epoch": epoch,
        "effects": [effect],
        "approved_at": 1000,
    }
    body = {
        "schema_version": "1.0",
        "action": "call",
        "tool": tool,
        "arguments": arguments,
        "request_id": request,
        "execution_id": None,
        "ttl_seconds": None,
    }
    binding = {
        "tenant_id": "tenant",
        "run_id": run,
        "operation_id": operation,
        "user_id": "owner",
        "subject_id": "subject",
        "team_id": "team",
        "department_id": "department",
        "credential_id": credential,
        "authority_revision": revision,
        "meter_epoch": epoch,
        "source_sha256": digest(
            {key: job[key] for key in ("repository_id", "sha", "workflow_sha")}
        ),
        "runtime_id": "github-actions:" + digest(job),
        "route": effect["effect_key"],
        "request_sha256": digest(body),
    }
    authority = {
        **binding,
        "revision": revision,
        "effects": [effect],
        "scope": "all_in",
        "cap_usd": "20",
    }
    reservation = "tools-test-effect:" + digest(
        {"tenant_id": "tenant", "run_id": run, "operation_id": operation}
    )
    answer = {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "result": {"content": [{"type": "text", "text": "private answer"}]},
    }
    original = {
        "tenant_id": "tenant",
        "run_id": run,
        "operation_id": operation,
        "binding": binding,
        "binding_digest": digest(binding),
        "version": 5,
        "owner_token": str(uuid4()),
        "status": "dispatch_started",
        "reservation_confirmed": True,
        "reservation_id": reservation,
        "quote": {
            "source_contract_sha256": effect["contract_sha256"],
            "request_sha256": digest(body),
            "authority_sha256": digest(authority),
        },
        "facade_dispatch": {
            "execution_id": execution,
            "new_execution": True,
            "session_deadline_at": 1300,
            "cleanup_deadline_at": 1600,
        },
        "native_knowledge_dispatch": {
            "status": "response_received",
            "request_sha256": digest(body),
            "target_binding_sha256": "e" * 64,
            "claimed_at": 1001,
            "received_at": 1002,
            "rpc_id_sha256": digest(answer["id"]),
            "response_sha256": digest(answer),
        },
    }
    answer["result"]["_meta"] = {"adp_receipt_id": reservation}
    common = {
        key: {"S": value}
        for key, value in {
            "schema_version": "1.0",
            "tenant_id": "tenant",
            "owner_id": "owner",
            "team_id": "team",
            "tool": tool,
            "binding": "run:" + run + ":native-test-" + credential,
            "native_test_run_id": run,
            "native_test_reservation_id": reservation,
        }.items()
    }
    facade_key = "OP#tenant#owner#" + tool + "#" + request
    facade = {
        **common,
        **{
            key: {"S": value}
            for key, value in {
                "execution_id": facade_key,
                "operation_execution_id": execution,
                "request_id": request,
                "receipt_id": reservation,
                "status": "confirmed",
                "result_status": "success",
                "dispatch_intent": "dispatched",
                "result_json": json.dumps(answer),
                "request_digest": digest(arguments),
            }.items()
        },
    }
    execution_row = {
        **common,
        "execution_id": {"S": execution},
        "state": {"S": "closed"},
        "cleanup_status": {"S": "confirmed"},
        "cleanup_evidence": {"S": "all_operations_settled"},
        "operation_tracking": {"S": "1"},
        "accepted_operations": {"N": "1"},
        "unresolved_operations": {"N": "0"},
        "cleanup_confirmed_at": {"N": "1003"},
    }

    def row(kind, value):
        return {"kind": {"S": kind}, "document": {"S": canonical(value)}}

    items = {
        "TOOLS_TEST_APPROVAL#" + run: row("native_test_approval_v1", approval),
        "TOOLS_TEST_RUN#" + run: row("tools_test_run_v1", authority),
        "TOOLS_TEST_EXECUTION#" + run + "#" + execution: row(
            "native_test_execution_binding_v1",
            {"run_id": run, "operation_id": operation, "execution_id": execution},
        ),
        "TOOLS_TEST_OP#" + run + "#" + operation: {
            "document": {"S": canonical(original)},
            "version": {"N": "5"},
            "owner_token": {"S": original["owner_token"]},
            "status": {"S": original["status"]},
            "binding_digest": {"S": digest(binding)},
        },
        facade_key: facade,
        execution: execution_row,
    }
    calls = []

    def read(**kw):
        assert kw["ConsistentRead"] is True
        calls.append(kw)
        key = kw["Key"].get("sk", kw["Key"].get("execution_id"))["S"]
        return {"Item": deepcopy(items.get(key))}

    reference = SimpleNamespace(
        run_id=run,
        tenant_id="tenant",
        approval_revision=revision,
        deployment_id="offline",
        source_revision="a" * 40,
        definition_revision="b" * 40,
        github_run_id="42",
        github_run_attempt="1",
    )
    observer = NativeKnowledgeOperationObserver(
        client=SimpleNamespace(
            get_item=read,
            meta=SimpleNamespace(
                config=SimpleNamespace(retries={"total_max_attempts": 1})
            ),
        ),
        authority_table="native-authority",
        execution_table="facade-execution",
        reference=reference,
        release_sha256="c" * 64,
        target_binding_sha256="e" * 64,
    )
    return SimpleNamespace(
        observer=observer,
        items=items,
        calls=calls,
        original=original,
        facade=facade,
        execution=execution_row,
        parameters={
            "operation_id": operation,
            "request_id": request,
            "tool": tool,
            "arguments": arguments,
        },
    )


def test_actual_records_agree_without_becoming_backend_or_billing_evidence(records):
    result = records.observer.observe(**records.parameters)
    assert result["cleanup_status"] == "confirmed" and len(records.calls) == 6
    assert result["backend_invocation_count"] is None
    assert (
        result["index_verified"]
        is result["billing_complete"]
        is result["governance_complete"]
        is False
    )
    assert "private answer" not in canonical(
        result
    ) and "private indexed fact" not in canonical(result)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "response",
        "rpc",
        "owner",
        "unresolved",
        "cleanup",
        "execution",
        "request",
        "arguments",
        "target",
    ],
)
def test_missing_partial_or_substituted_evidence_is_unknown(records, fault):
    r = records
    if fault == "missing":
        r.items.clear()
    elif fault in {"response", "rpc"}:
        answer = json.loads(r.facade["result_json"]["S"])
        if fault == "response":
            answer["result"]["content"][0]["text"] = "different"
        else:
            answer["id"] = str(uuid4())
        r.facade["result_json"]["S"] = json.dumps(answer)
    elif fault == "owner":
        r.facade["owner_id"]["S"] = "foreign"
    elif fault == "unresolved":
        r.execution["unresolved_operations"]["N"] = "1"
    elif fault == "cleanup":
        r.execution["cleanup_confirmed_at"]["N"] = "1601"
    elif fault == "execution":
        r.facade["operation_execution_id"]["S"] = str(uuid4())
    elif fault == "request":
        r.parameters["request_id"] = str(uuid4())
    elif fault == "arguments":
        r.parameters["arguments"] = {"query": "foreign"}
    else:
        r.observer.target_binding_sha256 = "f" * 64
    with pytest.raises(FixtureUnavailable):
        r.observer.observe(**r.parameters)
