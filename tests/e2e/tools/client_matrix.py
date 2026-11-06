"""Required laptop tool inventory and non-interactive installed-client invocation."""

import base64
import binascii
import hashlib
import json
import re
import stat
from pathlib import Path
from uuid import UUID

from tests.e2e.tools.pricing_envelope import reserve_all_in
from tests.e2e.tools.process_bounds import run_bounded

FAMILIES = {
    "websearch": ("search",),
    "code_interpreter": ("start", "execute", "result", "file", "close"),
    "browser": ("start", "step", "inspect", "close"),
    "knowledge": ("search", "understand", "impact", "browse", "remember", "experience", "secure"),
    "credentials": ("list", "proxy_request", "assume_role", "materialize"),
    "github": ("get_user",),
    "jira": ("get_issue",),
    "slack": ("get_user", "post_message"),
    "adp": ("operations_list", "operation_retry", "operation_status", "artifact_read", "execution_close"),
}
OPERATIONS = tuple(f"{family}___{action}" for family, actions in FAMILIES.items() for action in actions)
CLIENTS = ("cli", "claude-code", "codex")


def check_discovery(listing):
    tools = listing.get("tools")
    if not isinstance(tools, list):
        raise ValueError("client tool discovery is missing")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(OPERATIONS) or set(names) != set(OPERATIONS):
        raise ValueError("client discovery differs from the required public operation inventory")
    for tool in tools:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("x-adp-schema-version") != "1.0":
            raise ValueError("client discovery lacks pinned public schemas")
    return hashlib.sha256(json.dumps(sorted(tools, key=lambda item: item["name"]),
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def import_session(installed, environ, refresh_file):
    if not refresh_file or not Path(refresh_file).is_file():
        raise ValueError("protected refresh session file unavailable")
    metadata = Path(refresh_file).stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("protected refresh session must be private")
    token = Path(refresh_file).read_bytes().strip()
    if not token or len(token) > 16384:
        raise ValueError("protected refresh session invalid")
    gateway = json.loads((Path(environ["BG_CONFIG_DIR"]) / "config.json").read_text())["gateway_url"]
    result = run_bounded([str(installed.parent / "bg-cognito-auth.sh"), "import", "--gateway-url", gateway],
                         environ=environ, input_data=token + b"\n", timeout=30,
                         max_input_bytes=16385, max_output_bytes=4096)
    if result.returncode:
        raise ValueError("protected refresh session import failed")


def resolve_arguments(arguments, captured):
    if not isinstance(arguments, dict):
        raise ValueError("operation arguments must be an object")
    resolved = {}
    for name, value in arguments.items():
        if isinstance(value, str) and value.startswith("$owned."):
            field = value.removeprefix("$owned.")
            if field not in captured:
                raise ValueError("fixture requested an unavailable owned handle")
            value = captured[field]
        resolved[name] = value
    return resolved


def owned_result(receipt):
    state = {"operation_ref": receipt.get("operation_ref"),
             "execution_id": receipt.get("execution_id"), "receipt_id": receipt.get("receipt_id")}
    result = receipt.get("result")
    for _ in range(4):
        if not isinstance(result, dict):
            break
        for field in ("session_id", "view_id", "provider_id"):
            if isinstance(result.get(field), str) and result[field]:
                state[field] = result[field]
        result = result.get("result")
    artifact = receipt.get("artifact")
    if isinstance(artifact, dict) and re.fullmatch(r"[0-9a-f]{64}", artifact.get("content_sha256", "")):
        state["artifact_sha256"] = artifact["content_sha256"]
        state["artifact_operation_ref"] = receipt.get("operation_ref")
        state["artifact_receipt_id"] = receipt.get("receipt_id")
        if type(artifact.get("byte_length")) is int:
            state["artifact_size"] = artifact["byte_length"]
    return {key: value for key, value in state.items() if (isinstance(value, str) and value) or
            (key == "artifact_size" and type(value) is int and value >= 0)}


def prepare_owned(installed, environ, steps, *, client_factory):
    if not steps:
        return {}
    start_identity = None
    captured = {}
    with client_factory([str(installed), "mcp"], environ=environ) as client:
        check_discovery(client.request("tools/list"))
        for step in steps:
            operation = step["operation"]
            if operation not in OPERATIONS or operation.startswith("adp___"):
                raise ValueError("fixture setup must use a public provider operation")
            arguments = resolve_arguments(step["arguments"], captured)
            response = client.request("tools/call", {"name": operation, "arguments": arguments})
            receipt = receipt_from_response(response)
            if start_identity and receipt["execution_id"] != start_identity.get("execution_id"):
                raise ValueError("fixture child operation belongs to a different execution")
            raw = json.loads(response["content"][0]["text"])
            captured.update(owned_result(raw))
            captured["operation_ref"] = receipt["operation_ref"]
            if operation in {"code_interpreter___start", "browser___start"}:
                start_identity = {field: captured[field] for field in
                                  ("operation_ref", "execution_id", "receipt_id", "session_id") if field in captured}
            if start_identity:
                captured.update(start_identity)
    return captured


def receipt_from_response(response):
    if not isinstance(response, dict) or response.get("isError") is True:
        raise ValueError("installed client reported a failed tool call")
    contents = response.get("content")
    if not isinstance(contents, list) or len(contents) != 1 or contents[0].get("type") != "text":
        raise ValueError("installed client did not return a tool receipt")
    receipt = json.loads(contents[0]["text"])
    if receipt.get("status") != "confirmed" or not receipt.get("receipt_id"):
        raise ValueError("installed client did not confirm an operation")
    reference = receipt.get("operation_ref")
    if str(UUID(str(reference))) != reference:
        raise ValueError("installed client receipt has no canonical operation reference")
    return {"operation_ref": reference, "receipt_id": receipt["receipt_id"],
            "execution_id": receipt.get("execution_id"),
            "receipt_sha256": hashlib.sha256(contents[0]["text"].encode()).hexdigest()}


def recovery_result(response, operation, owned, arguments=None):
    if not owned or not owned.get("operation_ref") or not owned.get("receipt_id"):
        raise ValueError("recovery requires an operation created in this isolated home")
    contents = response.get("content") if isinstance(response, dict) else None
    if not isinstance(contents, list) or len(contents) != 1 or contents[0].get("type") != "text":
        raise ValueError("recovery response is missing")
    result = json.loads(contents[0]["text"])
    reference = owned["operation_ref"]
    receipt_id = owned["receipt_id"]
    if operation == "adp___artifact_read":
        reference = owned.get("artifact_operation_ref", reference)
        receipt_id = owned.get("artifact_receipt_id", receipt_id)
        if not isinstance(arguments, dict) or arguments.get("operation_ref") != reference:
            raise ValueError("recovery artifact must target the operation that produced its bytes")
    if response.get("isError") or ("operation_ref" in result and
                                    result["operation_ref"] != reference):
        raise ValueError("recovery returned a different or failed operation")
    if operation == "adp___operations_list":
        if not any(item.get("request_id") == owned["operation_ref"]
                   for item in result.get("operations", [])):
            raise ValueError("recovery list omitted the owned operation")
    elif operation == "adp___execution_close":
        if (result.get("execution_id") != owned.get("execution_id") or
                result.get("state") != "closed" or result.get("cleanup_status") != "confirmed"):
            raise ValueError("execution close did not confirm provider cleanup")
    elif operation == "adp___artifact_read":
        if not isinstance(arguments, dict):
            raise ValueError("recovery artifact range is missing")
        offset, length = arguments.get("offset", 0), arguments.get("length", 8192)
        digest = result.get("sha256")
        total = result.get("total_length")
        if (result.get("artifact_id") != "result" or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                digest != owned.get("artifact_sha256") or
                type(offset) is not int or type(length) is not int or not 0 <= offset or not 1 <= length <= 8192 or
                type(total) is not int or total <= 0 or total != owned.get("artifact_size") or
                result.get("offset") != offset or type(result.get("length")) is not int):
            raise ValueError("recovery artifact metadata or owned digest differs")
        try:
            chunk = base64.b64decode(result["data_base64"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ValueError("recovery artifact data is invalid") from exc
        if len(chunk) != result["length"] or len(chunk) != min(length, total - offset):
            raise ValueError("recovery artifact range is truncated or oversized")
        if offset == 0 and len(chunk) == total and hashlib.sha256(chunk).hexdigest() != digest:
            raise ValueError("recovery artifact full-result hash differs")
    elif result.get("status") != "confirmed":
        raise ValueError("recovery did not confirm the owned operation")
    verified = {"operation_ref": reference, "receipt_id": receipt_id,
                "execution_id": owned.get("execution_id"),
                "receipt_sha256": hashlib.sha256(contents[0]["text"].encode()).hexdigest()}
    if operation == "adp___artifact_read":
        verified.update(artifact_sha256=digest, artifact_chunk_sha256=hashlib.sha256(chunk).hexdigest(),
                        artifact_offset=offset, artifact_length=len(chunk), artifact_total_length=total)
    return verified


def cli_call(installed, environ, operation, arguments, *, client_factory, owned=None):
    with client_factory([str(installed), "mcp"], environ=environ) as client:
        listing = client.request("tools/list")
        discovery = check_discovery(listing)
        schema = next(tool["inputSchema"] for tool in listing["tools"]
                      if tool["name"] == operation)
        if (not isinstance(arguments, dict) or
                set(arguments) - set(schema.get("properties", {})) or
                set(schema.get("required", ())) - set(arguments)):
            raise ValueError("fixture arguments do not match the discovered public schema")
        response = client.request("tools/call", {"name": operation, "arguments": arguments})
        receipt = receipt_from_response(response) if not operation.startswith("adp___") else recovery_result(
            response, operation, owned, arguments)
        return {"source": "installed-cli-mcp", "client_version": client.version,
                "discovery_sha256": discovery, **receipt}


def native_events(client, output, operation):
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    if not events or any(not isinstance(event, dict) for event in events):
        raise ValueError("native transcript is missing or malformed")
    if client == "claude-code":
        if any(event.get("type") not in {"system", "assistant", "user", "result", "rate_limit_event"}
               for event in events):
            raise ValueError("Claude transcript contains unsupported execution events")
        if any(event.get("is_error") is True for event in events if event.get("type") == "result"):
            raise ValueError("Claude transcript ended with an error")
        calls = [item for event in events if event.get("type") == "assistant"
                 for item in event.get("message", {}).get("content", []) if item.get("type") == "tool_use"]
        results = [item for event in events if event.get("type") == "user"
                   for item in event.get("message", {}).get("content", []) if item.get("type") == "tool_result"]
        if (len(calls) != 1 or calls[0].get("name") != "mcp__adp-tools__" + operation or
                not calls[0].get("id") or len(results) != 1 or
                results[0].get("tool_use_id") != calls[0]["id"] or results[0].get("is_error") is True):
            raise ValueError("Claude Code did not complete exactly the selected MCP tool")
        return calls[0].get("input"), results[0].get("content")
    if client != "codex":
        raise ValueError("unknown native client")
    completed = [event["item"] for event in events if event.get("type") == "item.completed"
                 and isinstance(event.get("item"), dict)]
    started = [event["item"] for event in events if event.get("type") in {"item.started", "item.updated"}
               and isinstance(event.get("item"), dict)]
    allowed = {"mcp_tool_call", "agent_message", "reasoning"}
    if (any(event.get("type") not in {"thread.started", "turn.started", "item.started", "item.updated",
                                      "item.completed", "turn.completed"} for event in events) or
            any(item.get("type") not in allowed for item in (*started, *completed)) or
            len([item for item in completed if item.get("type") == "mcp_tool_call"]) != 1):
        raise ValueError("Codex transcript contains alternate or unsupported execution")
    call = next(item for item in completed if item["type"] == "mcp_tool_call")
    attempts = [item for item in started if item["type"] == "mcp_tool_call"]
    if any(item.get("server") != "adp-tools" or item.get("tool") != operation or
           not item.get("id") or item["id"] != call.get("id") for item in attempts):
        raise ValueError("Codex transcript contains alternate or unfinished MCP calls")
    if (call.get("server") != "adp-tools" or call.get("tool") != operation or
            call.get("error") or call.get("status") == "failed"):
        raise ValueError("Codex did not complete exactly the selected MCP tool")
    arguments = call.get("arguments")
    return json.loads(arguments) if isinstance(arguments, str) else arguments, call.get("result")


def native_call(client, installed, environ, operation, arguments, *, model, timeout, bounds, owned=None, native_launch=None, retain_receipt=False):
    if client not in {"claude-code", "codex"}:
        raise ValueError("unknown native client")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]+", model):
        raise ValueError("pinned native model required")
    launcher = [str(installed), "claude" if client == "claude-code" else "codex"]
    if native_launch is not None:
        from tests.e2e.tools.native_client_transport import NativeClientLaunch

        if (type(native_launch) is not NativeClientLaunch or native_launch.client != client or
                native_launch.installed != str(installed) or native_launch.model != model or
                native_launch.transport_file != environ.get("ADP_NATIVE_TEST_TRANSPORT_FILE")):
            raise ValueError("native client launch differs from its original session")
        launcher = list(native_launch.command)
    prompt = ("Invoke exactly the adp-tools MCP operation " + operation + " once using this JSON as arguments: "
              + json.dumps(arguments, separators=(",", ":")) + ". Do not invoke any other tool.")
    if len(prompt.encode()) > bounds["max_request_bytes"]:
        raise ValueError("native request exceeded quoted byte limit")
    binary = "claude" if client == "claude-code" else "codex"
    version = run_bounded([binary, "--version"], environ=environ, cwd=environ["HOME"],
                          timeout=15, max_output_bytes=512)
    if version.returncode or not version.stdout or len(version.stdout) > 100:
        raise ValueError("native client version unavailable")
    client_version = version.stdout.decode().strip()
    if client == "claude-code":
        command = [*launcher, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "dontAsk", "--allowedTools", "mcp__adp-tools__" + operation]
    else:
        command = [*launcher, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "-m", model, "-"]
    result = run_bounded(command, input_data=prompt.encode(), environ=environ, cwd=environ["HOME"],
                         timeout=timeout, max_input_bytes=bounds["max_request_bytes"],
                         max_output_bytes=min(1024 * 1024, bounds["max_response_bytes"]))
    if result.returncode or len(result.stdout) > min(1024 * 1024, bounds["max_response_bytes"]):
        raise ValueError("native client failed or exceeded quoted response bytes")
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    usage = ([event["message"]["usage"] for event in events if client == "claude-code" and
              event.get("type") == "assistant" and isinstance(event.get("message", {}).get("usage"), dict)] +
             [event["usage"] for event in events if client == "codex" and
              event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)])
    if (not 0 < len(usage) <= bounds["max_model_requests"] or
            any(type(item.get("input_tokens")) is not int or type(item.get("output_tokens")) is not int or
                item["input_tokens"] < 0 or item["output_tokens"] < 0 for item in usage) or
            sum(item["input_tokens"] for item in usage) > bounds["max_input_tokens"] or
            sum(item["output_tokens"] for item in usage) > bounds["max_output_tokens"]):
        raise ValueError("native model usage is unverified or exceeds quoted token/request limits")
    used_arguments, response = native_events(client, result.stdout.decode(), operation)
    if used_arguments != arguments:
        raise ValueError("native client called a different operation payload")
    if isinstance(response, str):
        response = [{"type": "text", "text": response}]
    is_error = isinstance(response, dict) and response.get("isError") is True
    if isinstance(response, dict) and "content" in response:
        response = response["content"]
    packet = {"content": response, "isError": is_error}
    receipt = (recovery_result(packet, operation, owned, arguments) if operation.startswith("adp___")
               else receipt_from_response(packet))
    return {"source": client + "-native-mcp", "client_version": client_version, "model_id": model,
            "model_requests": len(usage), "input_tokens": sum(item["input_tokens"] for item in usage),
            "output_tokens": sum(item["output_tokens"] for item in usage), **receipt,
            **({"private_receipt": json.loads(packet["content"][0]["text"])} if retain_receipt else {})}


def verify_readback(client_result, fixture, observer, *, operation, client):
    if observer is None or not callable(getattr(observer, "observe", None)):
        raise ValueError("independent provider observer unavailable")
    evidence = observer.observe(operation_ref=client_result["operation_ref"],
                                execution_id=client_result["execution_id"], operation=operation,
                                client=client, timeout=fixture["cleanup_seconds"])
    if (not isinstance(evidence, dict) or evidence.get("source") != "provider-control-plane" or
            evidence.get("operation_ref") != client_result["operation_ref"] or
            evidence.get("execution_id") != client_result["execution_id"] or
            evidence.get("receipt_id") != client_result["receipt_id"] or
            evidence.get("operation") != operation or
            ("artifact_sha256" in client_result and
             (evidence.get("artifact_sha256") != client_result["artifact_sha256"] or
              evidence.get("artifact_chunk_sha256") != client_result["artifact_chunk_sha256"])) or
            not isinstance(evidence.get("assertion"), dict) or
            evidence["assertion"] != {"source": "provider-control-plane", "operation": operation,
                                      "passed": True} or
            type(evidence.get("provider_request_count")) is not int or
            evidence["provider_request_count"] != fixture["expected_provider_calls"] or
            evidence.get("cleanup_status") != "confirmed" or
            not evidence.get("cleanup_observed_at") or
            type(evidence.get("session_count")) is not int or evidence["session_count"] > 1 or
            type(evidence.get("session_ttl_seconds")) is not int or
            not 0 <= evidence["session_ttl_seconds"] <= fixture["session_ttl_seconds"] or
            type(evidence.get("cleanup_elapsed_seconds")) not in (int, float) or
            not 0 <= evidence["cleanup_elapsed_seconds"] <= fixture["cleanup_seconds"] or
            type(evidence.get("cost_usd")) not in (int, float) or
            not 0 <= evidence["cost_usd"] <= fixture["max_cost_usd"]):
        raise ValueError("independent provider observation or cleanup does not match the client operation")
    return {"source": evidence["source"], "assertion": evidence["assertion"],
            "provider_request_count": evidence["provider_request_count"],
            "session_count": evidence["session_count"], "session_ttl_seconds": evidence["session_ttl_seconds"],
            "cleanup_elapsed_seconds": evidence["cleanup_elapsed_seconds"], "cost_usd": evidence["cost_usd"],
            "cleanup": {"status": "confirmed", "observed_at": evidence["cleanup_observed_at"]}}


def validate_fixture(fixture, *, client_name=None):
    if client_name is not None and client_name not in CLIENTS:
        raise ValueError("unknown installed client")
    fields = {"arguments", "setup", "all_in_quote", "max_seconds", "cleanup_seconds", "session_ttl_seconds", "max_cost_usd",
              "unit_cost_usd", "pricing_source", "authority_confirmed", "expected_provider_calls"}
    if (not isinstance(fixture, dict) or set(fixture) != fields or
            fixture["authority_confirmed"] is not True or
            not isinstance(fixture["arguments"], dict) or
            not isinstance(fixture["setup"], list) or len(fixture["setup"]) > 3 or
            any(not isinstance(step, dict) or set(step) != {"operation", "arguments"} or
                step["operation"] not in OPERATIONS or not isinstance(step["arguments"], dict)
                for step in fixture["setup"]) or
            sum(step.get("operation") in {"code_interpreter___start", "browser___start"}
                for step in fixture["setup"] if isinstance(step, dict)) > 1 or
            not isinstance(fixture["pricing_source"], str) or not fixture["pricing_source"] or
            any(type(fixture[key]) is not int or not 0 < fixture[key] <= limit for key, limit in
                (("max_seconds", 300), ("cleanup_seconds", 600), ("session_ttl_seconds", 300))) or
            any(type(fixture[key]) not in (int, float) or not 0 < fixture[key] <= 20 for key in
                ("max_cost_usd", "unit_cost_usd")) or
            fixture["unit_cost_usd"] > fixture["max_cost_usd"] or
            type(fixture["expected_provider_calls"]) is not int or
            not 0 <= fixture["expected_provider_calls"] <= 4 or
            fixture["expected_provider_calls"] < len(fixture["setup"]) + 1 or
            fixture["unit_cost_usd"] * max(1, fixture["expected_provider_calls"]) > fixture["max_cost_usd"]):
        raise ValueError("priced, authorized and bounded operation fixture required")
    return reserve_all_in(fixture, execution_kind="stdio" if client_name == "cli" else "model")
