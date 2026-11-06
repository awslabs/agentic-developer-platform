import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.e2e.tools import client_matrix
from tests.e2e.tools.pricing_envelope import reserve_all_in


OPERATION = "websearch___search"
REFERENCE = str(uuid4())
RECEIPT = {"status": "confirmed", "operation_ref": REFERENCE,
           "receipt_id": "receipt-1", "execution_id": str(uuid4())}
LISTING = {"tools": [{"name": name, "inputSchema": {"properties": {"query": {}},
            "required": ["query"], "x-adp-schema-version": "1.0"}}
                     for name in client_matrix.OPERATIONS]}


def test_inventory_has_all_25_public_and_five_recovery_tools():
    assert len(client_matrix.OPERATIONS) == 30
    assert len(set(client_matrix.OPERATIONS)) == 30
    assert len(client_matrix.FAMILIES) == 9
    assert len(client_matrix.OPERATIONS) * len(client_matrix.CLIENTS) == 90
    assert "websearch___operation_get" not in client_matrix.OPERATIONS


@pytest.mark.parametrize("names", ([], LISTING["tools"][:-1], LISTING["tools"] + [{"name": "unexpected"}]))
def test_discovery_fails_for_missing_or_extra_operations(names):
    with pytest.raises(ValueError, match="inventory"):
        client_matrix.check_discovery({"tools": names})


def test_cli_call_uses_installed_mcp_and_retains_receipt():
    calls = []

    class Client:
        version = "1.0.0"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            calls.append((method, params))
            if method == "tools/list":
                return LISTING
            return {"content": [{"type": "text", "text": json.dumps(RECEIPT)}]}

    def factory(command, *, environ):
        assert command == ["/isolated/adp", "mcp"] and environ == {"HOME": "/isolated"}
        return Client()

    result = client_matrix.cli_call("/isolated/adp", {"HOME": "/isolated"}, OPERATION,
                                    {"query": "test"}, client_factory=factory)
    assert result["source"] == "installed-cli-mcp" and result["operation_ref"] == REFERENCE
    assert calls[-1] == ("tools/call", {"name": OPERATION, "arguments": {"query": "test"}})
    assert result["discovery_sha256"] and result["receipt_sha256"]


def test_native_events_match_real_tool_call_to_its_result():
    result = [{"type": "text", "text": json.dumps(RECEIPT)}]
    claude = '\n'.join(json.dumps(event) for event in (
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call-1",
            "name": "mcp__adp-tools__" + OPERATION, "input": {"query": "test"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1",
            "content": result}]}},
    ))
    codex = json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "adp-tools",
        "tool": OPERATION, "arguments": json.dumps({"query": "test"}), "result": result}})
    assert client_matrix.native_events("claude-code", claude, OPERATION) == ({"query": "test"}, result)
    assert client_matrix.native_events("codex", codex, OPERATION) == ({"query": "test"}, result)
    with pytest.raises(ValueError, match="exactly"):
        client_matrix.native_events("claude-code", claude + "\n" + claude, OPERATION)


@pytest.mark.parametrize("client", ("claude-code", "codex"))
@pytest.mark.parametrize("original_transport", (False, True))
def test_native_process_is_a_client_executable_not_the_mcp_shim(monkeypatch, client, original_transport):
    result = [{"type": "text", "text": json.dumps(RECEIPT)}]
    event = ({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call-1",
              "name": "mcp__adp-tools__" + OPERATION, "input": {"query": "test"}}]}} if client == "claude-code" else
             {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "adp-tools",
              "tool": OPERATION, "arguments": {"query": "test"}, "result": result}})
    output = [event]
    if client == "claude-code":
        output.append({"type": "assistant", "message": {"usage": {"input_tokens": 2,
                       "output_tokens": 3}, "content": []}})
        output.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1",
                       "content": result}]}})
    else:
        output.append({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}})
    commands = []
    execute_input = []

    def execute(command, **kwargs):
        commands.append(command)
        if command[-1] == "--version":
            assert kwargs["timeout"] == 15
            return subprocess.CompletedProcess(command, 0, b"installed-native 1.0.0\n", b"")
        execute_input.append(kwargs["input_data"])
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(item) for item in output).encode(), b"")

    monkeypatch.setattr(client_matrix, "run_bounded", execute)
    environ, launch = {"HOME": "/isolated"}, None
    if original_transport:
        from tests.e2e.tools.native_client_transport import NativeClientLaunch

        environ["ADP_NATIVE_TEST_TRANSPORT_FILE"] = "/private/original.json"
        launch = NativeClientLaunch(client, "/isolated/adp", "/private/original.json", "pinned-model",
                                    ("claude" if client == "claude-code" else "codex",))
    evidence = client_matrix.native_call(client, "/isolated/adp", environ, OPERATION,
                                          {"query": "test"}, model="pinned-model", timeout=10,
                                          bounds=matrix_fixture()["all_in_quote"], native_launch=launch)
    assert commands[0] == ["claude" if client == "claude-code" else "codex", "--version"]
    if original_transport:
        assert commands[1][0] == launch.command[0]
    else:
        assert commands[1][:2] == ["/isolated/adp", "claude" if client == "claude-code" else "codex"]
    if client == "codex":
        assert "--ephemeral" in commands[1]
    assert "test" not in " ".join(commands[1]) and b"test" in execute_input[0]
    assert evidence["source"] == client + "-native-mcp" and evidence["model_requests"] == 1
    assert evidence["client_version"] == "installed-native 1.0.0" and evidence["model_id"] == "pinned-model"
    assert evidence["operation_ref"] == REFERENCE


def matrix_fixture(client_name=None):
    fixture = {"arguments": {"query": "test"}, "setup": [],
            "all_in_quote": {"version": 1, "source_revision": "a" * 40,
                             "effective_at": datetime.now(UTC).isoformat(),
                             "max_model_requests": 1, "max_input_tokens": 10, "max_output_tokens": 10,
                             "max_request_bytes": 4096, "max_response_bytes": 4096, "max_retries": 0,
                             "input_token_usd": "0.001", "output_token_usd": "0.001",
                             "startup_usd": "0.01", "network_storage_usd": "0.01",
                             "cleanup_usd": "0.01", "retry_usd": "0"}, "max_seconds": 30, "cleanup_seconds": 60,
            "session_ttl_seconds": 30, "max_cost_usd": 1, "unit_cost_usd": 0.5,
            "pricing_source": "protected price sheet", "authority_confirmed": True,
            "expected_provider_calls": 1}
    if client_name == "cli":
        fixture["all_in_quote"].update(max_model_requests=0, max_input_tokens=0,
                                       max_output_tokens=0, input_token_usd="0", output_token_usd="0")
    return fixture


def provider_evidence():
    return {"source": "provider-control-plane", "operation_ref": REFERENCE,
            "execution_id": RECEIPT["execution_id"], "receipt_id": "receipt-1", "operation": OPERATION,
            "assertion": {"source": "provider-control-plane", "operation": OPERATION, "passed": True},
            "provider_request_count": 1, "cleanup_status": "confirmed", "cleanup_observed_at": "2026-01-01T00:00:00Z",
            "session_count": 0, "session_ttl_seconds": 0, "cleanup_elapsed_seconds": 10, "cost_usd": 0.5}


def test_independent_observer_corroborates_receipt_bounds_and_cleanup():
    class Observer:
        def observe(self, **kwargs):
            assert kwargs["operation_ref"] == REFERENCE and kwargs["client"] == "cli"
            return provider_evidence()

    client = {"operation_ref": REFERENCE, "execution_id": RECEIPT["execution_id"], "receipt_id": "receipt-1"}
    evidence = client_matrix.verify_readback(client, matrix_fixture(), Observer(), operation=OPERATION, client="cli")
    assert evidence["cleanup"]["status"] == "confirmed" and evidence["provider_request_count"] == 1

    class Unconfirmed(Observer):
        def observe(self, **kwargs):
            return {**super().observe(**kwargs), "cleanup_status": "pending"}

    with pytest.raises(ValueError, match="independent provider"):
        client_matrix.verify_readback(client, matrix_fixture(), Unconfirmed(), operation=OPERATION, client="cli")
    with pytest.raises(ValueError, match="independent provider"):
        client_matrix.verify_readback(client, matrix_fixture(), None, operation=OPERATION, client="cli")


@pytest.mark.parametrize("field,value", (("authority_confirmed", False), ("max_cost_usd", 21),
                                          ("session_ttl_seconds", 301), ("cleanup_seconds", 601)))
def test_unpriced_unbounded_or_unauthorized_fixtures_are_refused(field, value):
    fixture = {**matrix_fixture(), field: value}
    with pytest.raises(ValueError, match="priced, authorized"):
        client_matrix.validate_fixture(fixture)


def test_setup_dispatch_must_be_included_in_provider_reservation():
    fixture = matrix_fixture()
    fixture["setup"] = [{"operation": "code_interpreter___start", "arguments": {}}]
    with pytest.raises(ValueError, match="priced, authorized"):
        client_matrix.validate_fixture(fixture)
    fixture["expected_provider_calls"] = 2
    fixture["max_cost_usd"] = 2
    assert client_matrix.validate_fixture(fixture)["reserved_usd"] == Decimal("1.05")


def test_owned_setup_captures_session_for_following_call():
    receipts = [dict(RECEIPT, result={"result": {"session_id": "owned-session"}})]
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            calls.append((method, params))
            return LISTING if method == "tools/list" else {
                "content": [{"type": "text", "text": json.dumps(receipts.pop(0))}]}

    owned = client_matrix.prepare_owned("/isolated/adp", {"HOME": "/isolated"}, [
        {"operation": "code_interpreter___start", "arguments": {}}],
        client_factory=lambda command, *, environ: Client())
    arguments = client_matrix.resolve_arguments({"session_id": "$owned.session_id",
                                                  "execution_ref": "$owned.operation_ref"}, owned)
    assert arguments == {"session_id": "owned-session", "execution_ref": REFERENCE}
    assert calls[-1][1]["name"] == "code_interpreter___start"
    with pytest.raises(ValueError, match="unavailable owned handle"):
        client_matrix.resolve_arguments({"session_id": "$owned.foreign_session"}, owned)


@pytest.mark.parametrize("foreign_execution", (False, True))
def test_setup_child_cannot_replace_start_execution_reference(foreign_execution):
    import base64
    import hashlib

    artifact = b"child result\n"
    digest = hashlib.sha256(artifact).hexdigest()
    child = {**RECEIPT, "operation_ref": str(uuid4()),
             "execution_id": str(uuid4()) if foreign_execution else RECEIPT["execution_id"],
             "receipt_id": "child-receipt", "result": {"result": {"provider_id": "owned-result"}},
             "artifact": {"content_sha256": digest, "byte_length": len(artifact)}}
    receipts = [dict(RECEIPT, result={"result": {"session_id": "owned-session"}}), child]

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            if method == "tools/list":
                return LISTING
            return {"content": [{"type": "text", "text": json.dumps(receipts.pop(0))}]}

    steps = [
        {"operation": "code_interpreter___start", "arguments": {}},
        {"operation": "code_interpreter___execute", "arguments": {"session_id": "$owned.session_id",
         "execution_ref": "$owned.operation_ref", "code": "print(1)", "language": "python"}}]
    if foreign_execution:
        with pytest.raises(ValueError, match="different execution"):
            client_matrix.prepare_owned("/isolated/adp", {"HOME": "/isolated"}, steps,
                                       client_factory=lambda command, *, environ: Client())
        return
    owned = client_matrix.prepare_owned("/isolated/adp", {"HOME": "/isolated"}, steps,
                                       client_factory=lambda command, *, environ: Client())
    assert owned["operation_ref"] == REFERENCE
    assert owned["execution_id"] == RECEIPT["execution_id"]
    assert owned["session_id"] == "owned-session" and owned["provider_id"] == "owned-result"
    arguments = client_matrix.resolve_arguments(
        {"operation_ref": "$owned.artifact_operation_ref", "offset": 0, "length": 8192}, owned)
    assert arguments["operation_ref"] == child["operation_ref"] != owned["operation_ref"]
    response = {"content": [{"type": "text", "text": json.dumps({
        "artifact_id": "result", "operation_ref": child["operation_ref"], "offset": 0,
        "length": len(artifact), "total_length": len(artifact), "sha256": digest,
        "data_base64": base64.b64encode(artifact).decode()})}]}
    verified = client_matrix.recovery_result(response, "adp___artifact_read", owned, arguments)
    assert verified["operation_ref"] == child["operation_ref"]
    assert verified["receipt_id"] == child["receipt_id"]
    assert verified["execution_id"] == RECEIPT["execution_id"]
    with pytest.raises(ValueError, match="operation that produced"):
        client_matrix.recovery_result(response, "adp___artifact_read", owned,
                                      {**arguments, "operation_ref": owned["operation_ref"]})


def test_close_rejects_foreign_execution_even_with_matching_reference():
    owned = {"operation_ref": REFERENCE, "receipt_id": "receipt-1", "execution_id": RECEIPT["execution_id"]}
    response = {"content": [{"type": "text", "text": json.dumps({"operation_ref": REFERENCE,
        "execution_id": str(uuid4()), "state": "closed", "cleanup_status": "confirmed"})}]}
    with pytest.raises(ValueError, match="execution close"):
        client_matrix.recovery_result(response, "adp___execution_close", owned)


def test_recovery_list_must_report_seeded_operation():
    owned = {"operation_ref": REFERENCE, "receipt_id": "receipt-1", "execution_id": RECEIPT["execution_id"]}
    response = {"content": [{"type": "text", "text": json.dumps({"operations": [
        {"request_id": REFERENCE, "status": "confirmed"}]})}]}
    confirmed = client_matrix.recovery_result(response, "adp___operations_list", owned)
    assert confirmed["operation_ref"] == REFERENCE and confirmed["receipt_sha256"]
    response["content"][0]["text"] = json.dumps({"operations": []})
    with pytest.raises(ValueError, match="omitted"):
        client_matrix.recovery_result(response, "adp___operations_list", owned)
    with pytest.raises(ValueError, match="created in this isolated home"):
        client_matrix.recovery_result(response, "adp___operations_list", {})


def test_all_in_quote_reserves_model_provider_and_cleanup_before_dispatch():
    fixture = matrix_fixture()
    quote = reserve_all_in(fixture)
    assert quote["reserved_usd"] == Decimal("0.55")
    assert quote["limits"]["max_retries"] == 0
    assert client_matrix.validate_fixture(fixture)["reserved_usd"] == quote["reserved_usd"]


def test_stdio_quote_counts_provider_and_overhead_without_model_cost():
    fixture = matrix_fixture("cli")
    quote = client_matrix.validate_fixture(fixture, client_name="cli")
    assert quote["reserved_usd"] == Decimal("0.53")
    assert quote["limits"]["max_model_requests"] == 0
    for client in ("claude-code", "codex", None):
        with pytest.raises(ValueError, match="native model"):
            client_matrix.validate_fixture(fixture, client_name=client)


@pytest.mark.parametrize("field", ("max_model_requests", "max_input_tokens", "max_output_tokens",
                                  "input_token_usd", "output_token_usd"))
@pytest.mark.parametrize("value", (1, -1))
def test_stdio_quote_refuses_model_quantities_and_tariffs(field, value):
    fixture = matrix_fixture("cli")
    fixture["all_in_quote"][field] = value
    with pytest.raises(ValueError):
        client_matrix.validate_fixture(fixture, client_name="cli")


@pytest.mark.parametrize("client", ("cli", "claude-code", "codex"))
@pytest.mark.parametrize("change", (
    {"max_request_bytes": 0}, {"max_response_bytes": 0}, {"max_retries": 1},
    {"effective_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
    {"cleanup_usd": "NaN"}, {"network_storage_usd": "-1"}, {"startup_usd": "21"},
))
def test_every_driver_keeps_bounded_provider_and_overhead_quote(client, change):
    fixture = matrix_fixture(client)
    fixture["all_in_quote"].update(change)
    with pytest.raises(ValueError):
        client_matrix.validate_fixture(fixture, client_name=client)


@pytest.mark.parametrize("client", ("cli", "claude-code", "codex"))
@pytest.mark.parametrize("field,value", (("authority_confirmed", False), ("unit_cost_usd", 0),
                                        ("expected_provider_calls", 0), ("max_cost_usd", 21)))
def test_every_driver_keeps_fixture_authority_and_provider_requirements(client, field, value):
    fixture = {**matrix_fixture(client), field: value}
    with pytest.raises(ValueError, match="priced, authorized"):
        client_matrix.validate_fixture(fixture, client_name=client)


def test_fixture_cannot_choose_its_driver_mode():
    fixture = matrix_fixture("cli")
    fixture["execution_kind"] = "stdio"
    with pytest.raises(ValueError, match="priced, authorized"):
        client_matrix.validate_fixture(fixture)
    with pytest.raises(ValueError, match="unknown installed client"):
        client_matrix.validate_fixture(matrix_fixture("cli"), client_name="unknown")
    with pytest.raises(ValueError, match="unknown client execution kind"):
        reserve_all_in(matrix_fixture(), execution_kind="unknown")


@pytest.mark.parametrize("change", (
    {"max_model_requests": 18, "max_output_tokens": 2048, "output_token_usd": "0.00283632421875"},
    {"max_retries": 1}, {"input_token_usd": "0"},
    {"effective_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
    {"max_response_bytes": 0},
))
def test_all_in_quote_rejects_unbounded_stale_or_provider_only_estimates(change):
    fixture = matrix_fixture()
    fixture["all_in_quote"].update(change)
    with pytest.raises(ValueError):
        client_matrix.validate_fixture(fixture)


@pytest.mark.parametrize("extra", (
    {"type": "item.completed", "item": {"type": "command_execution", "command": "true"}},
    {"type": "item.started", "item": {"type": "command_execution", "command": "true"}},
    {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "other",
                                 "tool": OPERATION, "result": {"isError": False}}},
    {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "other-call",
                               "server": "adp-tools", "tool": OPERATION}},
    {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "other-call",
                               "server": "other", "tool": OPERATION}},
    {"type": "item.updated", "item": {"type": "mcp_tool_call", "id": "other-call",
                               "server": "adp-tools", "tool": "github___get_user"}},
))
def test_codex_transcript_rejects_alternate_or_unfinished_execution(extra):
    good = {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "owned-call", "server": "adp-tools",
                                                "tool": OPERATION, "arguments": {"query": "test"},
                                                "result": {"content": [{"type": "text", "text": json.dumps(RECEIPT)}]}}}
    with pytest.raises(ValueError, match="alternate or (?:unsupported|unfinished)|exactly"):
        client_matrix.native_events("codex", "\n".join(json.dumps(item) for item in (good, extra)), OPERATION)


def test_codex_transcript_matches_started_call_to_completion():
    started = {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "owned-call",
                                               "server": "adp-tools", "tool": OPERATION}}
    completed = {"type": "item.completed", "item": {**started["item"], "arguments": {"query": "test"},
                                                   "result": [{"type": "text", "text": json.dumps(RECEIPT)}]}}
    arguments, result = client_matrix.native_events(
        "codex", "\n".join(json.dumps(item) for item in (started, completed)), OPERATION)
    assert arguments == {"query": "test"} and json.loads(result[0]["text"]) == RECEIPT


def test_codex_native_result_iserror_never_promotes_confirmed_text(monkeypatch):
    result = {"content": [{"type": "text", "text": json.dumps(RECEIPT)}], "isError": True}
    output = "\n".join(json.dumps(item) for item in (
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "adp-tools",
         "tool": OPERATION, "arguments": {"query": "test"}, "result": result}},
        {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}},
    )).encode()
    def execute(command, **kwargs):
        stdout = b"installed-native 1.0.0\n" if command[-1] == "--version" else output
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(client_matrix, "run_bounded", execute)
    with pytest.raises(ValueError, match="failed tool call"):
        client_matrix.native_call("codex", "/isolated/adp", {"HOME": "/isolated"}, OPERATION,
                                  {"query": "test"}, model="pinned", timeout=10,
                                  bounds=matrix_fixture()["all_in_quote"])


def test_bounded_native_timeout_preserves_unknown_outcome_without_retry(monkeypatch):
    from tests.e2e.tools.process_bounds import ProcessLimitError

    launches = []

    def execute(command, **kwargs):
        launches.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, b"codex 1.0.0\n", b"")
        assert kwargs["max_input_bytes"] == 4096 and kwargs["max_output_bytes"] == 4096
        raise ProcessLimitError("client process deadline exceeded", stdout=b"private partial receipt")

    monkeypatch.setattr(client_matrix, "run_bounded", execute)
    with pytest.raises(ProcessLimitError, match="deadline") as error:
        client_matrix.native_call("codex", "/isolated/adp", {"HOME": "/isolated"}, OPERATION,
                                  {"query": "test"}, model="pinned", timeout=10,
                                  bounds=matrix_fixture()["all_in_quote"])
    assert len(launches) == 2 and launches[1][:2] == ["/isolated/adp", "codex"]
    assert "private partial" not in str(error.value)


def test_artifact_recovery_checks_bytes_range_owner_and_independent_chunk():
    import base64
    import hashlib

    full = b"private fixture bytes"
    digest = hashlib.sha256(full).hexdigest()
    owned = {"operation_ref": REFERENCE, "receipt_id": "receipt-1", "execution_id": RECEIPT["execution_id"],
             "artifact_sha256": digest, "artifact_size": len(full)}

    def response(data, *, offset, total, length=None, sha=digest):
        return {"content": [{"type": "text", "text": json.dumps({
            "artifact_id": "result", "operation_ref": REFERENCE, "offset": offset,
            "length": len(data) if length is None else length, "total_length": total,
            "sha256": sha, "data_base64": base64.b64encode(data).decode()})}]}

    result = client_matrix.recovery_result(response(full, offset=0, total=len(full)),
                                           "adp___artifact_read", owned,
                                           {"operation_ref": REFERENCE, "offset": 0, "length": len(full)})
    assert result["artifact_chunk_sha256"] == digest
    prefix = client_matrix.recovery_result(response(full[:5], offset=0, total=len(full)),
                                           "adp___artifact_read", owned,
                                           {"operation_ref": REFERENCE, "offset": 0, "length": 5})
    assert prefix["artifact_chunk_sha256"] == hashlib.sha256(full[:5]).hexdigest()
    for tampered in (response(b"changed fixture bytes", offset=0, total=len(full)),
                     response(full[:4], offset=0, total=len(full)),
                     response(full, offset=1, total=len(full)),
                     response(full, offset=0, total=len(full), sha="f" * 64)):
        with pytest.raises(ValueError):
            client_matrix.recovery_result(tampered, "adp___artifact_read", owned,
                                          {"operation_ref": REFERENCE, "offset": 0, "length": len(full)})
    provider = provider_evidence()
    client = {"operation_ref": REFERENCE, "execution_id": RECEIPT["execution_id"], "receipt_id": "receipt-1",
              **prefix}

    class MissingChunk:
        def observe(self, **kwargs):
            return provider

    with pytest.raises(ValueError, match="independent provider"):
        client_matrix.verify_readback(client, matrix_fixture(), MissingChunk(), operation=OPERATION, client="cli")


def test_github_client_inventory_is_read_only_user_profile():
    assert {name for name in client_matrix.OPERATIONS if name.startswith("github___")} == {"github___get_user"}
