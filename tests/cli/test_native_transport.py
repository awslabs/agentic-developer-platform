"""Private native transport records attempts before networking, offline."""

import base64
import importlib
import importlib.util
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

CLI = Path(__file__).resolve().parents[2] / "modules/gateway/cli"
sys.path.insert(0, str(CLI))
native = importlib.import_module("adp_native_test")


@pytest.fixture
def config(tmp_path):
    claims = {"sub": "offline-human", "custom:org_id": "tenant-one", "exp": 2000}
    token = (
        "e30."
        + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        + ".offline"
    )
    value = {
        "version": 1,
        "reference": {
            "run_id": str(uuid4()),
            "tenant_id": "tenant-one",
            "approval_revision": str(uuid4()),
            "deployment_id": "offline",
            "gateway_origin": "https://gateway.example.test",
            "source_revision": "a" * 40,
            "definition_revision": "b" * 40,
            "github_run_id": "123",
            "github_run_attempt": "1",
        },
        "access_token": token,
        "capability": "adpt1." + str(uuid4()) + "." + "s" * 43,
        "started_at": 990,
        "calls": [
            {
                "tool": "knowledge___search",
                "arguments_sha256": native.digest({"query": "private"}),
                "max_requests": 2,
            }
        ],
        "models": [
            {
                "model": "offline-model",
                "route": "/openai/v1/responses",
                "max_requests": 2,
                "max_request_bytes": 65536,
                "max_output_tokens": 128,
                "max_response_bytes": 262144,
            }
        ],
        "max_discoveries": 1,
    }
    path = tmp_path / "native.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def transport(config, **kw):
    return native.NativeTransport(config[0], clock=kw.get("clock", lambda: 1000))


def test_client_matrix_discovery_bound_is_finite(config):
    path, value = config
    value["max_discoveries"] = 9
    path.write_text(json.dumps(value))
    selected = transport(config)
    for _ in range(9):
        attempt = selected.begin(
            "GET", value["reference"]["gateway_origin"] + "/api/agent/tools", None
        )
        attempt.complete(200, b'{"tools": []}')
    with pytest.raises(native.NativeTransportError):
        selected.begin(
            "GET", value["reference"]["gateway_origin"] + "/api/agent/tools", None
        )
    value["max_discoveries"] = 10
    path.write_text(json.dumps(value))
    with pytest.raises(native.NativeTransportError):
        transport(config)


def test_finalized_matrix_cannot_start_another_native_call(config):
    selected = transport(config)
    (config[0].parent / "native-finalized.json").write_text("{}")
    with pytest.raises(native.NativeTransportError, match="finalized"):
        call(selected)
    with pytest.raises(native.NativeTransportError, match="finalized"):
        selected.begin(
            "GET", selected.reference["gateway_origin"] + "/api/agent/tools", None
        )


def call(t, **changes):
    payload = {
        "tool": "knowledge___search",
        "arguments": {"query": "private"},
        "request_id": str(uuid4()),
        **changes,
    }
    return t.begin(
        "POST",
        t.reference["gateway_origin"] + "/api/agent/tools/call",
        native.canonical(payload),
    )


def confirmed(attempt):
    t = attempt.transport
    return native.canonical(
        {
            "status": "confirmed",
            "result_status": "success",
            "request_id": attempt.payload["request_id"],
            "execution_id": str(uuid4()),
            "receipt_id": "tools-test-effect:"
            + native.digest(
                {
                    "tenant_id": t.reference["tenant_id"],
                    "run_id": t.reference["run_id"],
                    "operation_id": attempt.entry["operation_id"],
                }
            ),
        }
    )


def test_attempt_is_durable_before_headers_and_contains_no_credentials_or_payload(
    config,
):
    t = transport(config)
    attempt = call(t)
    raw = (t.directory / "attempts.json").read_text()
    assert json.loads(raw)["attempts"][0]["status"] == "attempted"
    assert (
        config[1]["capability"] not in raw
        and config[1]["access_token"] not in raw
        and '"private"' not in raw
    )
    assert attempt.headers[native.OPERATION_HEADER] == attempt.entry["operation_id"]
    assert attempt.headers[native.CAPABILITY_HEADER] == config[1]["capability"]
    attempt.complete(200, confirmed(attempt))
    with pytest.raises(native.NativeTransportError):
        t.begin(
            "POST",
            t.reference["gateway_origin"] + "/api/agent/tools/call",
            attempt.body,
        )


def test_lost_response_blocks_fresh_ids_and_survives_process_restart(config):
    first = call(transport(config))
    with pytest.raises(native.NativeTransportError):
        call(transport(config))
    assert (
        json.loads((first.transport.directory / "attempts.json").read_text())[
            "attempts"
        ][0]["operation_id"]
        == first.entry["operation_id"]
    )


@pytest.mark.parametrize("tool", sorted(native.CALLS - native.READS))
def test_atlassian_uses_exact_approved_arguments_and_retains_unknown_barrier(
    config, tool
):
    path, value = config
    arguments = {"cloudId": "owned-site", "summary": "private snowman ☃"}
    value["calls"] = [
        {"tool": tool, "arguments_sha256": native.digest(arguments), "max_requests": 1}
    ]
    path.write_text(json.dumps(value))
    t = transport(config)
    with pytest.raises(native.NativeTransportError):
        call(t, tool=tool, arguments={**arguments, "cloudId": "another-site"})
    first = call(t, tool=tool, arguments=arguments)
    assert (
        json.loads((t.directory / "attempts.json").read_text())["attempts"][0][
            "operation_id"
        ]
        == first.entry["operation_id"]
    )
    with pytest.raises(native.NativeTransportError):
        call(transport(config), tool=tool, arguments=arguments)


def test_atlassian_receipt_recovery_uses_original_header_and_finite_read_budget(config):
    path, value = config
    tool, arguments = (
        "atlassian___create_jira_issue",
        {"cloudId": "owned-site", "summary": "sentinel"},
    )
    value["calls"] = [
        {"tool": tool, "arguments_sha256": native.digest(arguments), "max_requests": 1}
    ]
    path.write_text(json.dumps(value))
    first = call(transport(config), tool=tool, arguments=arguments, execution_id=None)
    response = confirmed(first)
    restarted = transport(config, clock=lambda: 1301)
    url = value["reference"]["gateway_origin"] + "/api/agent/tools/operations/recover"
    for changed in ("request_id", "arguments", "tool"):
        payload = {
            **first.payload,
            changed: str(uuid4())
            if changed == "request_id"
            else {}
            if changed == "arguments"
            else "knowledge___search",
        }
        with pytest.raises(native.NativeTransportError):
            restarted.begin("POST", url, native.canonical(payload))
    lookup = restarted.begin("POST", url, first.body)
    assert lookup.headers[native.OPERATION_HEADER] == first.entry["operation_id"]
    assert lookup.entry["operation_id"] != first.entry["operation_id"]
    lookup.complete(200, response)
    with restarted.journal() as journal:
        original = journal["attempts"][0]
        assert original["status"] == "response_recovered"
        assert original["recovered_by"] == lookup.entry["operation_id"]
        assert original["receipt"]["receipt_id"] == json.loads(response)["receipt_id"]
    second = restarted.begin("POST", url, first.body)
    second.complete(200, response)
    with pytest.raises(native.NativeTransportError):
        restarted.begin("POST", url, first.body)
    with pytest.raises(native.NativeTransportError):
        call(restarted, tool=tool, arguments=arguments, execution_id=None)


def recovered_after_interrupted_receipt_read(config, *, unrelated=None):
    path, value = config
    tools = (
        "atlassian___create_jira_issue",
        "atlassian___get_jira_issue",
        "atlassian___search_jira",
    )
    arguments = (
        {"cloudId": "owned-site", "summary": "sentinel"},
        {"cloudId": "owned-site", "issueIdOrKey": "10001"},
        {"cloudId": "owned-site", "jql": "id = 10001"},
    )
    value["calls"] = [
        {"tool": tool, "arguments_sha256": native.digest(args), "max_requests": 1}
        for tool, args in zip(tools, arguments)
    ]
    path.write_text(json.dumps(value))
    first = call(
        transport(config), tool=tools[0], arguments=arguments[0], execution_id=None
    )
    response = confirmed(first)
    url = value["reference"]["gateway_origin"] + "/api/agent/tools/operations/recover"
    if unrelated is not None:
        first.complete(200, response)
        if unrelated == "model":
            first.transport.begin(
                "POST",
                value["reference"]["gateway_origin"] + "/api/openai/v1/responses",
                native.canonical(
                    {"model": "offline-model", "input": "original unfinished request"}
                ),
            )
        elif unrelated == "close":
            first.transport.begin(
                "POST",
                value["reference"]["gateway_origin"]
                + "/api/agent/tools/executions/"
                + json.loads(response)["execution_id"]
                + "/close?tool="
                + tools[0],
                None,
            )
        else:
            other = call(
                first.transport,
                tool=tools[1],
                arguments=arguments[1],
                execution_id=None,
            )
            if unrelated == "foreign-receipt":
                other.complete(200, confirmed(other))
                first.transport.begin("POST", url, other.body)
    first.transport.begin("POST", url, first.body)
    restarted = transport(config)
    lookup = restarted.begin("POST", url, first.body)
    lookup.complete(200, response)
    return restarted, first, lookup, tools[2], arguments[2]


def test_interrupted_receipt_read_recovers_then_admits_a_distinct_cell(config):
    restarted, first, lookup, tool, arguments = (
        recovered_after_interrupted_receipt_read(config)
    )
    with restarted.journal() as journal:
        original, unanswered, successful = journal["attempts"]
        assert original["status"] == "response_recovered"
        assert (
            original["recovered_by"]
            == successful["operation_id"]
            == lookup.entry["operation_id"]
        )
        assert unanswered["status"] == "attempted"
        assert "receipt" not in unanswered and "response_sha256" not in unanswered
    next_cell = call(restarted, tool=tool, arguments=arguments, execution_id=None)
    assert next_cell.entry["operation_id"] != first.entry["operation_id"]
    with pytest.raises(native.NativeTransportError):
        restarted.begin(
            "POST",
            restarted.reference["gateway_origin"] + "/api/agent/tools/call",
            first.body,
        )


@pytest.mark.parametrize("unrelated", ["model", "call", "close", "foreign-receipt"])
def test_interrupted_receipt_read_does_not_clear_unrelated_uncertainty(
    config, unrelated
):
    restarted, _, _, tool, arguments = recovered_after_interrupted_receipt_read(
        config, unrelated=unrelated
    )
    with pytest.raises(native.NativeTransportError, match="unresolved"):
        call(restarted, tool=tool, arguments=arguments, execution_id=None)


@pytest.mark.parametrize(
    "changed", ["recovered_by", "receipt", "response_sha256", "response_value_sha256"]
)
def test_interrupted_receipt_barrier_requires_exact_successful_link(config, changed):
    restarted, _, _, tool, arguments = recovered_after_interrupted_receipt_read(config)
    with restarted.journal() as journal:
        journal["attempts"][0][changed] = "mismatched"
    with pytest.raises(native.NativeTransportError, match="unresolved"):
        call(restarted, tool=tool, arguments=arguments, execution_id=None)


def jira_dependency_plan(config, tool):
    path, value = config
    create_arguments = {"cloudId": "owned-site", "summary": "generated sentinel"}
    template = {
        "cloudId": "owned-site",
        "maxResults": 1,
        **(
            {"jql": 'id = "' + native.JIRA_CREATED_ID + '"'}
            if tool == "atlassian___search_jira"
            else {"issueIdOrKey": native.JIRA_CREATED_ID}
        ),
    }
    value["calls"] = [
        {
            "tool": "atlassian___create_jira_issue",
            "arguments_sha256": native.digest(create_arguments),
            "max_requests": 1,
        },
        {
            "tool": tool,
            "arguments_sha256": native.digest(template),
            "max_requests": 1,
            "jira_dependency": {
                "create_arguments_sha256": native.digest(create_arguments),
                "arguments": template,
            },
        },
    ]
    path.write_text(json.dumps(value))
    return create_arguments, template


@pytest.mark.parametrize("tool", sorted(native.JIRA_DEPENDENTS))
@pytest.mark.parametrize("recover", [False, True])
def test_generated_jira_dependency_retains_original_plan_and_limits(
    config, tool, recover
):
    create_arguments, template = jira_dependency_plan(config, tool)
    before = config[0].read_bytes()
    selected = transport(config)
    arguments = native.jira_arguments(tool, template, "10001")
    with pytest.raises(native.NativeTransportError, match="outside"):
        call(selected, tool=tool, arguments=arguments, execution_id=None)
    first = call(
        selected,
        tool="atlassian___create_jira_issue",
        arguments=create_arguments,
        execution_id=None,
    )
    response = native.canonical(
        {
            **json.loads(confirmed(first)),
            "result": {"structuredContent": {"id": "10001"}},
        }
    )
    if recover:
        url = (
            selected.reference["gateway_origin"] + "/api/agent/tools/operations/recover"
        )
        selected.begin("POST", url, first.body)
        selected = transport(config)
        lookup = selected.begin("POST", url, first.body)
        lookup.complete(200, response)
        with pytest.raises(native.NativeTransportError):
            selected.begin("POST", url, first.body)
    else:
        first.complete(200, response)
    for changed in (
        {**arguments, "cloudId": "foreign"},
        native.jira_arguments(tool, template, "10002"),
        {**arguments, "extra": "not approved"},
        {**arguments, "maxResults": True},
    ):
        with pytest.raises(native.NativeTransportError, match="outside"):
            call(selected, tool=tool, arguments=changed, execution_id=None)
    with pytest.raises(native.NativeTransportError, match="horizon"):
        call(
            transport(config, clock=lambda: 1290),
            tool=tool,
            arguments=arguments,
            execution_id=None,
        )
    dependent = call(selected, tool=tool, arguments=arguments, execution_id=None)
    dependent.complete(200, confirmed(dependent))
    with pytest.raises(native.NativeTransportError):
        call(selected, tool=tool, arguments=arguments, execution_id=None)
    assert config[0].read_bytes() == before
    with selected.journal() as journal:
        assert journal["binding_sha256"] == selected.binding
        assert (
            len([entry for entry in journal["attempts"] if entry["kind"] == "call"])
            == 2
        )
        assert journal["attempts"][0]["jira_created"] == {
            "site_id": "owned-site",
            "provider_id": "10001",
        }


@pytest.mark.parametrize(
    "change", ["parent", "template", "site-slot", "replay", "tool"]
)
def test_generated_dependency_rejects_changed_contract(config, change):
    jira_dependency_plan(config, "atlassian___get_jira_issue")
    path, value = config
    dependent = value["calls"][1]
    if change == "parent":
        dependent["jira_dependency"]["create_arguments_sha256"] = "a" * 64
    elif change == "template":
        dependent["jira_dependency"]["arguments"]["cloudId"] = "foreign"
    elif change == "site-slot":
        dependent["jira_dependency"]["arguments"]["cloudId"] = native.JIRA_CREATED_ID
        dependent["arguments_sha256"] = native.digest(
            dependent["jira_dependency"]["arguments"]
        )
    elif change == "replay":
        dependent["max_requests"] = 2
    else:
        dependent["tool"] = "atlassian___get_confluence_page"
    path.write_text(json.dumps(value))
    with pytest.raises(native.NativeTransportError):
        transport(config)


@pytest.mark.parametrize(
    "resource", [{}, {"id": None}, {"id": True}, {"id": "10001 OR id = 10002"}]
)
def test_generated_dependency_requires_bounded_provider_id(config, resource):
    create_arguments, template = jira_dependency_plan(
        config, "atlassian___get_jira_issue"
    )
    selected = transport(config)
    original = call(
        selected,
        tool="atlassian___create_jira_issue",
        arguments=create_arguments,
        execution_id=None,
    )
    response = native.canonical(
        {**json.loads(confirmed(original)), "result": {"structuredContent": resource}}
    )
    with pytest.raises(native.NativeTransportError, match="unconfirmed"):
        original.complete(200, response)
    with pytest.raises(native.NativeTransportError):
        call(
            selected,
            tool="atlassian___get_jira_issue",
            arguments=native.jira_arguments(
                "atlassian___get_jira_issue", template, "10001"
            ),
        )


@pytest.fixture
def facade_normalizer(monkeypatch):
    tools = CLI.parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    monkeypatch.syspath_prepend(str(tools / "facade"))
    return importlib.import_module("tools_facade.server").normalize_lambda_result


@pytest.mark.parametrize("rpc_id", [42, "22222222-2222-4222-8222-222222222222"])
@pytest.mark.parametrize("encoding", ["structured", "text"])
@pytest.mark.parametrize("recover", [False, True])
def test_created_jira_dependency_uses_resource_id_inside_rpc_envelope(
    config, rpc_id, encoding, recover, facade_normalizer
):
    tool = "atlassian___get_jira_issue"
    create_arguments, template = jira_dependency_plan(config, tool)
    selected = transport(config)
    original = call(
        selected,
        tool="atlassian___create_jira_issue",
        arguments=create_arguments,
        execution_id=None,
    )
    resource = {"id": "10001", "key": "OWNED-1"}
    mcp_result = (
        {"structuredContent": resource}
        if encoding == "structured"
        else {"content": [{"type": "text", "text": json.dumps(resource)}]}
    )
    response = native.canonical(
        {
            **json.loads(confirmed(original)),
            "result": facade_normalizer(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": mcp_result,
                },
                "atlassian___create_jira_issue",
            ),
        }
    )
    if recover:
        selected = transport(config)
        target = selected.begin(
            "POST",
            selected.reference["gateway_origin"]
            + "/api/agent/tools/operations/recover",
            original.body,
        )
    else:
        target = original
    target.complete(200, response)
    with selected.journal() as journal:
        assert journal["attempts"][0]["jira_created"]["provider_id"] == "10001"
    with pytest.raises(native.NativeTransportError, match="outside"):
        call(
            selected,
            tool=tool,
            arguments=native.jira_arguments(tool, template, str(rpc_id)),
            execution_id=None,
        )
    call(
        selected,
        tool=tool,
        arguments=native.jira_arguments(tool, template, "10001"),
        execution_id=None,
    )


@pytest.mark.parametrize(
    "mcp_result",
    [
        {},
        {"structuredContent": {}},
        {"content": [{"type": "text", "text": "{}"}]},
        {"structuredContent": {"jsonrpc": "2.0", "id": "10001"}},
        {"content": [{"type": "text", "text": '{"id": "10001", "id": "10002"}'}]},
    ],
)
def test_created_jira_dependency_never_falls_back_to_an_envelope_id(config, mcp_result):
    tool = "atlassian___get_jira_issue"
    create_arguments, template = jira_dependency_plan(config, tool)
    selected = transport(config)
    original = call(
        selected,
        tool="atlassian___create_jira_issue",
        arguments=create_arguments,
        execution_id=None,
    )
    response = native.canonical(
        {
            **json.loads(confirmed(original)),
            "result": {
                "jsonrpc": "2.0",
                "id": "10001",
                "result": mcp_result,
            },
        }
    )
    with pytest.raises(native.NativeTransportError):
        original.complete(200, response)
    with selected.journal() as journal:
        assert "jira_created" not in journal["attempts"][0]
    with pytest.raises(native.NativeTransportError):
        call(
            selected,
            tool=tool,
            arguments=native.jira_arguments(tool, template, "10001"),
            execution_id=None,
        )


def test_concurrent_attempts_cannot_cross_unresolved_barrier(config):
    def one(_):
        try:
            return call(transport(config))
        except native.NativeTransportError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(one, range(4)))
    assert sum(reply is not None for reply in replies) == 1


def test_installed_status_recovers_native_operation_with_lost_execution_id(
    config, monkeypatch
):
    path, value = config
    tool, arguments = "atlassian___create_jira_issue", {"cloudId": "owned-site"}
    value["calls"] = [
        {"tool": tool, "arguments_sha256": native.digest(arguments), "max_requests": 1}
    ]
    path.write_text(json.dumps(value))
    original_transport = transport(config)
    first = call(original_transport, tool=tool, arguments=arguments, execution_id=None)
    response = confirmed(first)
    spec = importlib.util.spec_from_file_location(
        "offline_native_recovery_mcp", CLI / "adp-mcp.py"
    )
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    record = {
        **first.payload,
        "status": "unknown",
        "binding": original_transport.context()["binding"],
    }
    saved, sent = [], []
    monkeypatch.setattr(
        mcp.NativeTransport, "configured", classmethod(lambda cls: original_transport)
    )
    monkeypatch.setattr(mcp, "load_operation", lambda operation_ref: record)
    monkeypatch.setattr(
        mcp, "store_operation", lambda operation: saved.append(dict(operation))
    )

    class Reply(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            assert request.full_url.endswith("/api/agent/tools/operations/recover")
            assert json.loads(request.data) == first.payload
            assert (
                request.get_header(native.OPERATION_HEADER.capitalize())
                == first.entry["operation_id"]
            )
            return Reply(response)

    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: Opener())
    packet = mcp.lifecycle_call(
        "adp___operation_status", {"operation_ref": first.payload["request_id"]}
    )
    assert packet["isError"] is False
    result = json.loads(packet["content"][0]["text"])
    assert result["operation_ref"] == first.payload["request_id"]
    assert result["execution_id"] == json.loads(response)["execution_id"]
    assert len(sent) == 1 and saved[-1]["status"] == "confirmed"


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_origin",
        "foreign_query",
        "other_tool",
        "execution",
        "expired",
        "changed_plan",
        "public_file",
        "symlink",
    ],
)
def test_binding_drift_refuses_before_credentials_are_returned(config, fault):
    path, value = config
    t = transport(config)
    if fault == "foreign_origin":
        with pytest.raises(native.NativeTransportError):
            t.begin("GET", "https://foreign.example.test/api/agent/tools", None)
    elif fault in {"foreign_query", "other_tool", "execution"}:
        changes = {
            "foreign_query": {"arguments": {"query": "foreign"}},
            "other_tool": {"tool": "knowledge___remember"},
            "execution": {"execution_id": str(uuid4())},
        }[fault]
        with pytest.raises(native.NativeTransportError):
            call(t, **changes)
    elif fault == "expired":
        with pytest.raises(native.NativeTransportError):
            call(transport(config, clock=lambda: 1291))
    elif fault == "changed_plan":
        attempt = call(t)
        attempt.complete(200, confirmed(attempt))
        value["max_discoveries"] = 2
        path.write_text(json.dumps(value))
        with pytest.raises(native.NativeTransportError):
            call(transport(config))
    elif fault == "public_file":
        path.chmod(0o644)
        with pytest.raises(native.NativeTransportError):
            transport(config)
    else:
        other = path.with_name("other.json")
        path.rename(other)
        path.symlink_to(other)
        with pytest.raises(native.NativeTransportError):
            transport(config)


def test_model_limit_is_on_actual_upstream_bytes_and_unknown_stream_blocks_retry(
    config,
):
    t = transport(config)
    url = t.reference["gateway_origin"] + "/api/openai/v1/responses"
    attempt = t.begin(
        "POST",
        url,
        native.canonical({"model": "offline-model", "input": "private prompt"}),
    )
    assert json.loads(attempt.body)["max_output_tokens"] == 128
    with pytest.raises(native.NativeTransportError):
        attempt.complete(200, b'data: {"type":"response.output_text.delta"}\n\n')
    with pytest.raises(native.NativeTransportError):
        t.begin(
            "POST",
            url,
            native.canonical({"model": "offline-model", "input": "changed retry"}),
        )


def test_complete_model_stream_permits_a_distinct_funded_request_and_honors_request_limit(
    config,
):
    t = transport(config)
    url = t.reference["gateway_origin"] + "/api/openai/v1/responses"
    for index in range(2):
        attempt = t.begin(
            "POST",
            url,
            native.canonical({"model": "offline-model", "input": "turn-" + str(index)}),
        )
        attempt.complete(
            200,
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n',
        )
    with pytest.raises(native.NativeTransportError):
        t.begin(
            "POST",
            url,
            native.canonical({"model": "offline-model", "input": "extra-turn"}),
        )


@pytest.mark.parametrize(
    "content",
    [
        b"[]",
        b"null",
        b"data: invalid private provider content\n\n",
        b'{"status":"completed","usage":null}',
        b'{"status":"completed","usage":{"input_tokens":1,"output_tokens":true}}',
        b'{"status":"completed","usage":{"input_tokens":1,"output_tokens":129}}',
        b'{"status":"completed","usage":{"input_tokens":1}}',
        b'{"status":"completed","status":"failed","usage":{"input_tokens":1,"output_tokens":1}}',
        b'data: {"type":"response.completed","response":null}\n\n',
        b'data: {"type":"response.completed","response":{"status":"incomplete","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\ndata: {"type":"response.failed"}\n\n',
    ],
)
def test_malformed_or_incomplete_model_content_retains_unknown_attempt(config, content):
    t = transport(config)
    url = t.reference["gateway_origin"] + "/api/openai/v1/responses"
    body = native.canonical({"model": "offline-model", "input": "private prompt"})
    attempt = t.begin("POST", url, body)
    with pytest.raises(native.NativeTransportError, match="unconfirmed") as failure:
        attempt.complete(200, content)
    assert "private provider content" not in str(failure.value)
    with pytest.raises(native.NativeTransportError):
        t.begin("POST", url, body)
    assert (
        json.loads((t.directory / "attempts.json").read_text())["attempts"][0]["status"]
        == "attempted"
    )


@pytest.mark.parametrize("stream", [False, True])
def test_anthropic_completion_requires_original_usage_and_terminal(config, stream):
    path, value = config
    value["models"][0]["route"] = "/v1/messages"
    path.write_text(json.dumps(value))
    t = transport(config)
    attempt = t.begin(
        "POST",
        t.reference["gateway_origin"] + "/api/v1/messages",
        native.canonical({"model": "offline-model", "messages": []}),
    )
    if stream:
        packets = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        content = b"".join(
            b"data: " + native.canonical(packet) + b"\n\n" for packet in packets
        )
    else:
        content = native.canonical(
            {
                "type": "message",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
    attempt.complete(200, content)
    assert (
        json.loads((t.directory / "attempts.json").read_text())["attempts"][0]["status"]
        == "response_received"
    )


@pytest.mark.parametrize(
    "fault", ["stop_only", "bad_start", "bad_delta", "no_stop_reason", "after_stop"]
)
def test_incomplete_anthropic_stream_is_never_confirmed(config, fault):
    path, value = config
    value["models"][0]["route"] = "/v1/messages"
    path.write_text(json.dumps(value))
    t = transport(config)
    attempt = t.begin(
        "POST",
        t.reference["gateway_origin"] + "/api/v1/messages",
        native.canonical({"model": "offline-model", "messages": []}),
    )
    packets = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    if fault == "stop_only":
        packets = packets[-1:]
    elif fault == "bad_start":
        packets[0]["message"] = []
    elif fault == "bad_delta":
        packets[1]["usage"] = None
    elif fault == "no_stop_reason":
        packets[1]["delta"] = {}
    else:
        packets.append({"type": "message_delta", "usage": {"output_tokens": 3}})
    with pytest.raises(native.NativeTransportError, match="unconfirmed"):
        attempt.complete(
            200,
            b"".join(
                b"data: " + native.canonical(packet) + b"\n\n" for packet in packets
            ),
        )


def test_cleanup_remains_bound_to_original_confirmed_execution(config):
    t = transport(config)
    attempt = call(t)
    receipt = confirmed(attempt)
    attempt.complete(200, receipt)
    execution = json.loads(receipt)["execution_id"]
    url = (
        t.reference["gateway_origin"]
        + "/api/agent/tools/executions/"
        + execution
        + "/close?tool=knowledge___search"
    )
    cleanup = transport(config, clock=lambda: 1300).begin("POST", url, b"{}")
    cleanup.complete(
        200,
        native.canonical(
            {
                "execution_id": execution,
                "state": "closed",
                "cleanup_status": "confirmed",
            }
        ),
    )
    with pytest.raises(native.NativeTransportError):
        t.begin("POST", url.replace(execution, str(uuid4())), b"{}")


@pytest.mark.parametrize("lost", [False, True])
def test_actual_proxy_handoff_records_before_send_and_never_refreshes_original_login(
    config, monkeypatch, lost
):
    spec = importlib.util.spec_from_file_location(
        "offline_native_proxy", CLI / "bg-gateway-proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    t = transport(config)
    monkeypatch.setattr(proxy.NativeTransport, "configured", classmethod(lambda cls: t))
    sent, errors = [], []
    body = native.canonical({"model": "offline-model", "input": "private prompt"})

    class Reply(io.BytesIO):
        status, reason = 200, "OK"

        def getheader(self, name):
            return None

        def getheaders(self):
            return [("Content-Type", "text/event-stream")]

    class Connection:
        def request(self, method, path, **kw):
            state = json.loads((t.directory / "attempts.json").read_text())
            assert state["attempts"][-1]["status"] == "attempted"
            sent.append((method, path, kw))

        def getresponse(self):
            if lost:
                raise OSError("offline lost response")
            return Reply(
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n'
            )

        def close(self):
            pass

    handler = object.__new__(proxy.GatewayProxyHandler)
    handler.command, handler.headers = "POST", {}
    handler.upstream_scheme, handler.upstream_host, handler.upstream_port = (
        "https",
        "gateway.example.test",
        None,
    )
    handler._refuse_unentitled = lambda: False
    handler._read_request_body = lambda: body
    handler._normalize_model = lambda value: value
    handler._upstream_path = lambda: "/api/openai/v1/responses"
    handler._upstream_headers = lambda token, body: {"accept-encoding": "gzip"}
    handler._open_upstream = Connection
    handler._send_error_body = lambda status, *args: errors.append(status)
    handler.log_message = lambda *args: None
    handler._log_path = lambda: "/openai/v1/responses"
    handler.send_response_only = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    # No token_source exists: the fixed original native login must be used.
    handler._proxy()
    assert len(sent) == 1
    headers = sent[0][2]["headers"]
    assert headers["Authorization"] == "Bearer " + config[1]["access_token"]
    assert headers[native.CAPABILITY_HEADER] == config[1]["capability"]
    assert sum(name.lower() == "accept-encoding" for name in headers) == 1
    assert headers["Accept-Encoding"] == "identity"
    assert json.loads(sent[0][2]["body"])["max_output_tokens"] == 128
    state = json.loads((t.directory / "attempts.json").read_text())["attempts"][0]
    assert state["status"] == ("attempted" if lost else "response_received")
    if lost:
        handler._read_request_body = lambda: native.canonical(
            {"model": "offline-model", "input": "retry changed bytes"}
        )
        handler._proxy()
        assert len(sent) == 1 and errors == [502, 403]


@pytest.mark.parametrize("chunked", [False, True])
def test_native_proxy_refuses_oversized_input_before_reading_body(chunked):
    spec = importlib.util.spec_from_file_location(
        "offline_bounded_proxy", CLI / "bg-gateway-proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    handler = object.__new__(proxy.GatewayProxyHandler)
    handler.native_request_limit = 65536
    handler.headers = (
        {"Transfer-Encoding": "chunked"} if chunked else {"Content-Length": "65537"}
    )
    handler.rfile = io.BytesIO(b"10001\r\n" if chunked else b"")
    with pytest.raises(native.NativeTransportError, match="byte bound"):
        handler._read_request_body()


@pytest.mark.parametrize("failure", [None, 401, "lost"])
def test_mcp_handoff_preserves_original_headers_and_unresolved_guidance(
    config, monkeypatch, failure
):
    from urllib.error import HTTPError, URLError
    from urllib.request import ProxyHandler

    spec = importlib.util.spec_from_file_location(
        "offline_native_mcp", CLI / "adp-mcp.py"
    )
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    t = transport(config)
    monkeypatch.setattr(mcp.NativeTransport, "configured", classmethod(lambda cls: t))
    sent = []

    class Reply(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            attempts = json.loads((t.directory / "attempts.json").read_text())[
                "attempts"
            ]
            assert attempts[-1]["status"] == "attempted"
            sent.append(request)
            assert (
                request.get_header(native.CAPABILITY_HEADER.capitalize())
                == config[1]["capability"]
            )
            assert (
                request.get_header(native.OPERATION_HEADER.capitalize())
                == attempts[-1]["operation_id"]
            )
            if failure == 401:
                raise HTTPError(
                    request.full_url, 401, "private upstream message", {}, None
                )
            if failure == "lost":
                raise URLError("private upstream message")
            return Reply(b'{"tools": []}')

    def opener(*handlers):
        assert any(
            isinstance(handler, ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(mcp, "build_opener", opener)
    if failure is None:
        assert mcp.gateway_request("/agent/tools", context=t.context()) == {"tools": []}
    else:
        with pytest.raises(mcp.ToolError, match="existing journal") as caught:
            mcp.gateway_request("/agent/tools", context=t.context())
        assert all(
            word not in str(caught.value)
            for word in ("login", "retry", "private upstream")
        )
        with pytest.raises(mcp.ToolError):
            mcp.gateway_request("/agent/tools", context=t.context())
    assert len(sent) == 1


def test_native_model_completion_is_journaled_before_client_can_start_mcp(config):
    spec = importlib.util.spec_from_file_location(
        "offline_ordered_proxy", CLI / "bg-gateway-proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    t = transport(config)
    attempt = t.begin(
        "POST",
        t.reference["gateway_origin"] + "/api/openai/v1/responses",
        native.canonical({"model": "offline-model", "input": "private prompt"}),
    )
    eof_requested, release_eof = Event(), Event()
    states_at_delivery, client_failures = [], []

    class Client(io.BytesIO):
        def write(self, content):
            states_at_delivery.append(
                json.loads((t.directory / "attempts.json").read_text())["attempts"][0][
                    "status"
                ]
            )
            # A native client can react to the terminal event immediately.
            try:
                call(t)
            except native.NativeTransportError as exc:
                client_failures.append(str(exc))
            return super().write(content)

    class Reply(io.BytesIO):
        status, reason = 200, "OK"

        def getheader(self, name):
            return None

        def getheaders(self):
            return [("Content-Type", "text/event-stream")]

        def read1(self, maximum):
            content = super().read1(maximum)
            if not content:
                eof_requested.set()
                assert release_eof.wait(3), "test did not release delayed upstream EOF"
            return content

    handler = object.__new__(proxy.GatewayProxyHandler)
    handler.command, handler.wfile = "POST", Client()
    handler.send_response_only = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    reply = Reply(
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n'
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(handler._relay_response, reply, native_attempt=attempt)
        try:
            assert eof_requested.wait(3), "response failed before reaching delayed EOF"
            assert handler.wfile.getvalue() == b"" and states_at_delivery == []
            assert (
                json.loads((t.directory / "attempts.json").read_text())["attempts"][0][
                    "status"
                ]
                == "attempted"
            )
        finally:
            release_eof.set()
        task.result(timeout=3)
    assert states_at_delivery == ["response_received"]
    assert client_failures == []
    assert [
        entry["kind"]
        for entry in json.loads((t.directory / "attempts.json").read_text())["attempts"]
    ] == ["model", "call"]


@pytest.mark.parametrize("fault", ["error", "malformed", "oversized"])
def test_native_proxy_never_delivers_unconfirmed_model_bytes(config, fault):
    spec = importlib.util.spec_from_file_location(
        "offline_unknown_proxy", CLI / "bg-gateway-proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    t = transport(config)
    attempt = t.begin(
        "POST",
        t.reference["gateway_origin"] + "/api/openai/v1/responses",
        native.canonical({"model": "offline-model", "input": "private prompt"}),
    )

    class Reply(io.BytesIO):
        status = 500 if fault == "error" else 200

    handler = object.__new__(proxy.GatewayProxyHandler)
    handler.command, handler.wfile = "POST", io.BytesIO()
    handler.send_response_only = lambda *args: pytest.fail(
        "unconfirmed native headers delivered"
    )
    content = (
        b"x" * (attempt.maximum + 1)
        if fault == "oversized"
        else b"private incomplete response"
    )
    with pytest.raises(native.NativeTransportError):
        handler._relay_response(Reply(content), native_attempt=attempt)
    assert handler.wfile.getvalue() == b""
    assert (
        json.loads((t.directory / "attempts.json").read_text())["attempts"][0]["status"]
        == "attempted"
    )
    with pytest.raises(native.NativeTransportError):
        call(t)
