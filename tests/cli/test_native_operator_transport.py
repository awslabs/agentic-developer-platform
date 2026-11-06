"""Private operator client guards; all identities and receipts are offline substitutes."""

import base64
import importlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules/gateway/cli"))
native = importlib.import_module("adp_native_test")


@pytest.fixture
def operator(tmp_path):
    binding = {"schema_version": "4.0", "deployment_id": "offline", "source_revision": "a" * 40,
               "gateway_origin": "https://gateway.example.test", "execute_code": "print(42)", "file_path": "/tmp/offline.txt"}
    claims = {"sub": "offline-human", "custom:org_id": "tenant-one", "exp": 2000}
    value = {"version": 2, "reference": {"producer": "operator_local", "run_id": str(uuid4()),
        "tenant_id": "tenant-one", "subject_id": "offline-human", "approval_revision": str(uuid4()),
        "deployment_id": "offline", "source_revision": "a" * 40, "gateway_origin": binding["gateway_origin"],
        "contract_sha256": native.digest(binding)}, "binding": binding,
        "access_token": "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".offline",
        "capability": "adpt1." + str(uuid4()) + "." + "s" * 43,
        "started_at": 990, "models": [], "max_discoveries": 1}
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return native.NativeTransport(path, clock=lambda: 1000)


def begin(t, tool, arguments, *, execution=None, request=None, recover=False):
    body = {"tool": "code_interpreter___" + tool, "arguments": arguments, "execution_id": execution,
            "request_id": request or str(uuid4()), "ttl_seconds": 300 if tool == "start" else None}
    return t.begin("POST", t.reference["gateway_origin"] + "/api/agent/tools/" +
                   ("operations/recover" if recover else "call"), native.canonical(body))


def receipt(attempt, *, execution=None, pending=False, original=None):
    reservation = "tools-test-effect:" + native.digest({"tenant_id": attempt.transport.reference["tenant_id"],
        "run_id": attempt.transport.reference["run_id"], "operation_id": (original or attempt).entry["operation_id"]})
    return native.canonical({"status": "unknown" if pending else "confirmed", "result_status": "success",
        "request_id": attempt.payload["request_id"], "execution_id": execution or str(uuid4()), "receipt_id": reservation,
        "result": {"result": {"result": {"session_id": "a" * 64, "provider_id": "offline-task"}}}})


def started(t):
    listed = t.begin("GET", t.reference["gateway_origin"] + "/api/agent/tools", None)
    listed.complete(200, native.canonical({"tools": [{"name": "code_interpreter___" + name}
        for name in ("start", "execute", "result", "file")]}))
    attempt = begin(t, "start", {})
    response = receipt(attempt)
    attempt.complete(200, response)
    return json.loads(response)["execution_id"]


def execution(t):
    root = started(t)
    attempt = begin(t, "execute", {"session_id": "a" * 64, "code": "print(42)", "language": "python"}, execution=root)
    attempt.complete(202, receipt(attempt, execution=root, pending=True))
    return root, attempt


def test_operator_pending_execute_allows_only_exact_original_status_and_cleanup(operator):
    root, execute = execution(operator)
    with pytest.raises(native.NativeTransportError):
        begin(operator, "file", {"session_id": "a" * 64, "path": "/tmp/offline.txt"}, execution=root)
    recovery = begin(operator, "execute", execute.payload["arguments"], execution=root,
                     request=execute.payload["request_id"], recover=True)
    recovery.complete(200, receipt(recovery, execution=root, original=execute))
    result = begin(operator, "result", {"session_id": "a" * 64, "provider_id": "offline-task"},
                   execution=root, request=execute.payload["request_id"])
    result.complete(200, receipt(result, execution=root))
    file = begin(operator, "file", {"session_id": "a" * 64, "path": "/tmp/offline.txt"}, execution=root)
    file.complete(200, receipt(file, execution=root))
    close = operator.begin("POST", operator.reference["gateway_origin"] + f"/api/agent/tools/executions/{root}/close?tool=code_interpreter___start", b"{}")
    close.complete(200, native.canonical({"execution_id": root, "state": "closed", "cleanup_status": "confirmed"}))
    with operator.journal() as state:
        assert [row["slot"] for row in state["attempts"]] == ["list", "start", "execute", "execute_status", "result", "file", "close"]
        assert state["attempts"][2]["status"] == "pending_received"


@pytest.mark.parametrize("change", ("request", "execution", "code", "session"))
def test_operator_status_cannot_change_original_execute(operator, change):
    root, execute = execution(operator)
    args, request = dict(execute.payload["arguments"]), execute.payload["request_id"]
    if change == "request":
        request = str(uuid4())
    elif change == "execution":
        root = str(uuid4())
    else:
        args["code" if change == "code" else "session_id"] = "changed"
    with pytest.raises(native.NativeTransportError):
        begin(operator, "execute", args, execution=root, request=request, recover=True)


def test_operator_lost_status_ack_never_replenishes_slot(operator):
    root, execute = execution(operator)
    begin(operator, "execute", execute.payload["arguments"], execution=root, request=execute.payload["request_id"], recover=True)
    with pytest.raises(native.NativeTransportError):
        begin(operator, "execute", execute.payload["arguments"], execution=root, request=execute.payload["request_id"], recover=True)
    for _ in range(2):
        operator.begin("POST", operator.reference["gateway_origin"] + f"/api/agent/tools/executions/{root}/close?tool=code_interpreter___start", b"{}")
    with pytest.raises(native.NativeTransportError):
        operator.begin("POST", operator.reference["gateway_origin"] + f"/api/agent/tools/executions/{root}/close?tool=code_interpreter___start", b"{}")


def test_operator_unknown_start_receipt_allows_only_original_cleanup(operator):
    listed = operator.begin("GET", operator.reference["gateway_origin"] + "/api/agent/tools", None)
    listed.complete(200, native.canonical({"tools": [{"name": "code_interpreter___" + name}
        for name in ("start", "execute", "result", "file")]}))
    start = begin(operator, "start", {})
    root = str(uuid4())
    start.complete(202, receipt(start, execution=root, pending=True))
    with pytest.raises(native.NativeTransportError):
        begin(operator, "execute", {"session_id": "a" * 64, "code": "print(42)", "language": "python"}, execution=root)
    operator.begin("POST", operator.reference["gateway_origin"] + f"/api/agent/tools/executions/{root}/close?tool=code_interpreter___start", b"{}")


@pytest.mark.parametrize("change", ("subject", "model", "binding", "ci", "discovery"))
def test_operator_reference_refuses_substitution_and_ci_fields(operator, change):
    value = json.loads(operator.path.read_text())
    if change == "subject":
        value["reference"]["subject_id"] = "other"
    elif change == "model":
        value["models"] = [{"model": "unquoted"}]
    elif change == "binding":
        value["binding"]["execute_code"] = "changed"
    elif change == "ci":
        value["reference"]["github_run_id"] = "invented"
    else:
        value["max_discoveries"] = 2
    operator.path.write_text(json.dumps(value))
    with pytest.raises(native.NativeTransportError):
        native.NativeTransport(operator.path, clock=lambda: 1000)
