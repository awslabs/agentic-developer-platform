#!/usr/bin/env python3
"""stdio MCP bridge to the selected ADP gateway's tool façade."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

import adp_native_credential_handoff as native_handoff
from adp_common import _jwt_claims, authenticated_scope
from adp_native_test import NativeTransport, NativeTransportError

MAX_EAGER_MCP_SCHEMA_BYTES = 128 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ToolError(Exception):
    pass


class ExistingOperationError(ToolError):
    pass


class GatewayHTTPError(ToolError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Gateway rejected tool request (HTTP {status})")


def gateway_context():
    try:
        native = NativeTransport.configured()
        if native is not None:
            if any(os.environ.get(key) for key in ("ADP_CREW_TOOLS_URL", "ADP_RUN_CREDENTIAL_FILE", "SIGV4_PROXY_TARGET")):
                raise NativeTransportError("Native and worker transports cannot be combined")
            return native.context()
    except (ValueError, OSError) as exc:
        raise ToolError("Original native transport is unavailable; inspect the original run") from exc
    if "ADP_CREW_TOOLS_URL" in os.environ:
        url = os.environ["ADP_CREW_TOOLS_URL"]
        token = os.environ.get("ADP_CREW_TOOLS_TOKEN", "")
        run_id = os.environ.get("ADP_CREW_RUN_ID", "")
        if (not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url)
                or not re.fullmatch(r"[a-f0-9]{64}", token)
                or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", run_id)):
            raise ToolError("Crew tools binding unavailable")
        return {"url": url, "headers": {"Authorization": "Bearer " + token}, "proxy": True,
                "binding": {"mode": "crew-proxy", "run_id": run_id}}
    proxy_port = os.environ.get("SIGV4_PROXY_PORT") if os.environ.get("SIGV4_PROXY_TARGET") else None
    if proxy_port:
        url = f"http://127.0.0.1:{int(proxy_port)}"
        headers = {}
        binding = None
        credential_file = os.environ.get("ADP_RUN_CREDENTIAL_FILE")
        if credential_file:
            try:
                with open(credential_file, encoding="utf-8") as source:
                    claims = _jwt_claims(source.read(16385).strip())
                run = {key: claims[key] for key in ("invocation_id", "attempt", "tenant_id")}
                if not all(run.values()):
                    raise ValueError("Missing run binding")
            except (OSError, KeyError, ValueError) as exc:
                raise ToolError("Proxy run identity unavailable; do not resubmit the operation") from exc
            binding = {"mode": "sigv4-proxy", "gateway": os.environ["SIGV4_PROXY_TARGET"].rstrip("/"), "run": run}
    else:
        config_file = os.path.join(os.environ.get("BG_CONFIG_DIR", os.path.expanduser("~/.bedrock-gateway")), "config.json")
        try:
            with open(config_file, encoding="utf-8") as config:
                gateway_url = json.load(config)["gateway_url"].rstrip("/")
        except (OSError, KeyError, ValueError) as exc:
            raise ToolError("Gateway not configured; run adp login") from exc
        if not gateway_url.startswith("https://") and not gateway_url.startswith("http://127.0.0.1:"):
            raise ToolError("Invalid configured gateway URL")
        url = gateway_url + ("" if gateway_url.endswith("/api") else "/api")
        token_command = [os.path.join(os.path.dirname(__file__), "adp"), "token"]
        try:
            token = subprocess.run(token_command, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ToolError("Session expired; run adp login") from exc
        if not token or any(char.isspace() for char in token):
            raise ToolError("Session expired; run adp login")
        headers = {"Authorization": "Bearer " + token}
        scope = authenticated_scope(token)
        binding = {"mode": "bearer", "gateway": url, "deployment_id": os.environ.get("ADP_DEPLOYMENT_ID", ""),
                   "identity": scope["identity"], "tenant": scope["tenant_claim"] or os.environ.get("ADP_TENANT_ID", "")}
    return {"url": url, "headers": headers, "binding": binding, "proxy": bool(proxy_port)}


def gateway_request(path, body=None, *, context=None):
    context = context if context is not None else gateway_context()
    if path.startswith("/v1/l5/mcp/") and (context["proxy"] or context["binding"]["mode"] != "bearer"):
        raise GatewayHTTPError(404)
    url = context["url"] + (path.removeprefix("/agent") if context["proxy"] else path)
    headers = dict(context["headers"])
    if body is not None:
        headers["Content-Type"] = "application/json"
    native, attempt = None, None
    try:
        data = json.dumps(body).encode() if body is not None else None
        native = NativeTransport.configured()
        if native is not None:
            if context["binding"] != native.context()["binding"]:
                raise NativeTransportError("Original native transport changed")
            attempt = native.begin("POST" if data is not None else "GET", url, data)
            data = attempt.body
            headers.update(attempt.headers)
        request = Request(url, data=data, headers=headers)
        opener = build_opener(ProxyHandler({}), NoRedirect) if attempt is not None else build_opener(NoRedirect)
        # Façade calls may refresh connector metadata before provider dispatch.
        # Match the main CLI's response wait only for those ordinary tool calls.
        timeout = 120 if attempt is None and path == "/agent/tools/call" else 30
        with opener.open(request, timeout=timeout) as response:
            if attempt is not None:
                content = response.read(attempt.maximum + 1)
                attempt.complete(response.status, content)
                return json.loads(content)
            return json.load(response)
    except HTTPError as exc:
        if native is not None:
            raise ToolError("Original native operation is unresolved; inspect its existing journal") from None
        if exc.code == 401:
            raise ToolError("Session expired; run adp login") from exc
        raise GatewayHTTPError(exc.code) from exc
    except NativeTransportError as exc:
        raise ToolError("Original native operation is unresolved or refused; inspect its existing journal") from exc
    except (URLError, OSError, ValueError) as exc:
        if native is not None:
            raise ToolError("Original native operation is unresolved; inspect its existing journal") from None
        raise ToolError("Gateway tools unavailable; retry or check adp status") from exc


LOCAL_TOOLS = [
    {"name": "adp___operations_list", "description": "List locally stored ADP operation references for recovery.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "adp___operation_retry", "description": "Read or explicitly retry a saved operation; supplied tool and arguments must match.",
     "inputSchema": {"type": "object", "properties": {"operation_ref": {"type": "string", "format": "uuid"},
         "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["operation_ref"],
         "additionalProperties": False}},
    {"name": "adp___operation_status", "description": "Read the owner-authorized receipt without resubmitting the operation.",
     "inputSchema": {"type": "object", "properties": {"operation_ref": {"type": "string", "format": "uuid"}},
                     "required": ["operation_ref"]}},
    {"name": "adp___artifact_read", "description": "Read a bounded chunk of an owned result artifact; sha256 covers the full result.",
     "inputSchema": {"type": "object", "properties": {"operation_ref": {"type": "string", "format": "uuid"},
         "offset": {"type": "integer", "minimum": 0}, "length": {"type": "integer", "minimum": 1, "maximum": 8192}},
         "required": ["operation_ref"]}},
    {"name": "adp___execution_close", "description": "Fence an owned execution; cleanup may remain pending after close.",
     "inputSchema": {"type": "object", "properties": {"operation_ref": {"type": "string", "format": "uuid"}},
                     "required": ["operation_ref"]}},
]
LOCAL_TOOLS = [{**tool, "inputSchema": {**tool["inputSchema"], "additionalProperties": False,
                                       "x-adp-schema-version": "1.0"}} for tool in LOCAL_TOOLS]

TOOL_ARGUMENTS = {
    "websearch___search": ({"query"}, set()),
    "code_interpreter___start": (set(), {"ttl_seconds"}),
    "code_interpreter___execute": ({"execution_ref", "session_id", "code", "language"}, set()),
    "code_interpreter___result": ({"execution_ref", "session_id", "provider_id"}, set()),
    "code_interpreter___file": ({"execution_ref", "session_id", "path"}, set()),
    "code_interpreter___close": ({"execution_ref", "session_id"}, set()),
    "browser___start": ({"url"}, {"ttl_seconds"}),
    "browser___step": ({"execution_ref", "session_id", "view_id", "action"},
                       {"candidate_id", "url", "seconds"}),
    "browser___inspect": ({"execution_ref", "session_id", "view_id"}, set()),
    "browser___close": ({"execution_ref", "session_id"}, set()),
}
AUTHORITY_ARGUMENTS = {"request_id", "execution_id", "adp_assertion", "user_id", "tenant_id",
                       "root_human_id", "run_id", "workload_id"}


def validate_arguments(tool, arguments):
    if AUTHORITY_ARGUMENTS & arguments.keys():
        raise ToolError("Server-owned authority arguments are not accepted")
    fields = TOOL_ARGUMENTS.get(tool)
    if fields is None:
        return
    required, optional = fields
    if required - arguments.keys() or arguments.keys() - required - optional:
        raise ToolError("Arguments do not match the advertised client schema")
    for name, value in arguments.items():
        if name == "ttl_seconds":
            valid = type(value) is int and 1 <= value <= 900
        elif name == "seconds":
            valid = type(value) is int
        else:
            valid = isinstance(value, str) and bool(value)
        if not valid:
            raise ToolError("Arguments do not match the advertised client schema")


def validate_local_arguments(tool, arguments):
    allowed = {"operation_ref", "request_id"} | ({"offset", "length"} if tool == "adp___artifact_read" else set())
    if tool == "adp___operation_retry":
        allowed |= {"tool", "arguments"}
        if (("tool" in arguments) != ("arguments" in arguments) or
                ("tool" in arguments and (not isinstance(arguments["tool"], str) or
                                          not isinstance(arguments["arguments"], dict)))):
            raise ToolError("Arguments do not match the advertised client schema")
    if (arguments.keys() - allowed or
            ("operation_ref" in arguments and "request_id" in arguments) or
            (tool != "adp___operations_list" and not isinstance(
                arguments.get("operation_ref", arguments.get("request_id")), str)) or
            (tool == "adp___operations_list" and arguments)):
        raise ToolError("Arguments do not match the advertised client schema")


def operation_directory():
    root = os.environ.get("BG_CONFIG_DIR", os.path.expanduser("~/.adp"))
    directory = os.path.join(root, "tool-operations")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        metadata = os.lstat(directory)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ToolError("Operation directory must be private to the current user")
    except OSError as exc:
        raise ToolError("Cannot create private operation directory") from exc
    return directory


def operation_path(request_id):
    try:
        identifier = str(UUID(request_id))
        if identifier != request_id:
            raise ValueError("non-canonical request ID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError("A canonical UUID operation reference is required") from exc
    return os.path.join(operation_directory(), identifier + ".json")


def load_operation(request_id):
    path = operation_path(request_id)
    try:
        if os.stat(path, follow_symlinks=False).st_mode & 0o077:
            raise ToolError("Operation record is not private; fix its permissions before recovery")
        with open(path, encoding="utf-8") as saved:
            record = json.load(saved)
        if record["request_id"] != request_id or not isinstance(record["arguments"], dict):
            raise ValueError("invalid operation record")
        return record
    except FileNotFoundError as exc:
        raise ToolError(f"Unknown operation reference {request_id}") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("Operation record unreadable; do not submit a new ID") from exc


def store_operation(record, *, create=False):
    path = operation_path(record["request_id"])
    descriptor, temporary = tempfile.mkstemp(prefix=".operation-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise ExistingOperationError("Operation reference already exists") from exc
    except OSError as exc:
        raise ToolError("Cannot persist operation safely; tool call not submitted") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def operation_result(receipt, request_id):
    result = {**receipt, "operation_ref": request_id}
    succeeded = receipt.get("status") == "confirmed" and receipt.get("result_status") == "success"
    if not succeeded:
        result["recovery"] = (
            "Check the original operation. Use adp___operation_retry with this operation_ref only "
            "when explicitly requested; never invent a new ID after an uncertain result."
        )
    return {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}], "isError": not succeeded}


def submit_operation(record, *, recover=False):
    if record["tool"] in native_handoff.PRIVATE_TOOLS:
        return private_operation(record, action="status" if recover else "submit")
    if recover and record.get("status") == "rejected":
        return operation_result({"status": "rejected", "message": "Original operation was rejected before dispatch"},
                                record["request_id"])
    try:
        context = gateway_context()
        binding = context["binding"]
        reading_receipt = recover and bool(record.get("execution_id"))
        native = NativeTransport.configured()
        if native is not None and (native.value["version"] == 2 or record["tool"].startswith("atlassian___")):
            # The operator status step quotes the full retained execute payload.
            reading_receipt = False
        if not binding or (recover and record.get("binding") != binding):
            return operation_result({"status": "rejected", "message":
                "Original login, deployment or run binding is missing or changed; restore it before recovery. "
                "Do not submit this operation under a new ID."}, record["request_id"])
        wire_request_id = bound_request_id(record, binding)
        if not recover:
            record["binding"] = binding
            store_operation(record)
        if reading_receipt:
            execution_id = str(UUID(record["execution_id"]))
            query = urlencode({"tool": record["tool"]})
            receipt = gateway_request(
                f"/agent/tools/executions/{execution_id}/operations/{wire_request_id}?{query}", context=context
            )
        else:
            receipt = gateway_request("/agent/tools/operations/recover" if recover else "/agent/tools/call", {
                "tool": record["tool"], "arguments": record["arguments"],
                "execution_id": record.get("execution_id"), "request_id": wire_request_id,
                **({"ttl_seconds": record["ttl_seconds"]} if "ttl_seconds" in record else {}),
            }, context=context)
        if not isinstance(receipt, dict):
            raise ToolError("Gateway returned an invalid operation receipt")
    except ToolError as exc:
        # A failed read cannot prove that the uncertain original never dispatched.
        status = ("rejected" if not recover and isinstance(exc, GatewayHTTPError)
                  and exc.status in {400, 403, 404, 409, 422} else "unknown")
        record["status"] = status
        store_operation(record)
        return operation_result({"status": status, "message": str(exc)}, record["request_id"])
    if receipt.get("request_id") != wire_request_id:
        record["status"] = "unknown"
        store_operation(record)
        return operation_result({"status": "unknown", "message": "Gateway returned a different operation reference"}, record["request_id"])
    record["execution_id"] = receipt.get("execution_id") or record.get("execution_id")
    record["status"] = receipt.get("status", "unknown")
    if record["tool"] in {"code_interpreter___start", "browser___start"} and record["status"] == "confirmed":
        result = receipt.get("result")
        while isinstance(result, dict) and "session_id" not in result:
            result = result.get("result")
        if isinstance(result, dict) and isinstance(result.get("session_id"), str):
            record["session_id"] = result["session_id"]
    if record["tool"] == "code_interpreter___execute" and record["status"] == "confirmed":
        record.pop("provider_id", None)
        result = receipt.get("result")
        for _ in range(4):
            if not isinstance(result, dict) or "provider_id" in result:
                break
            result = result.get("result")
        if (isinstance(result, dict) and result.get("session_id") == record["arguments"].get("session_id")
                and isinstance(result.get("provider_id"), str) and result["provider_id"]):
            record["provider_id"] = result["provider_id"]
    if "result_status" in receipt:
        record["result_status"] = receipt["result_status"]
    store_operation(record)
    return operation_result(receipt, record["request_id"])


def l5_run_operation(record):
    try:
        context = gateway_context()
        if record.get("binding") != context["binding"]:
            return operation_result({"status": "rejected", "message":
                "Original login, deployment or run binding changed; restore it before recovery."}, record["request_id"])
        assessment_ref = record.get("assessment_ref")
        if assessment_ref:
            tool = "l5___get_run"
            arguments = {key: record["arguments"][key] for key in ("product_id", "project_id", "service_id")}
            arguments["assessment_ref"] = assessment_ref
        else:
            tool, arguments = record["tool"], record["arguments"]
        result = gateway_request("/v1/l5/mcp/tools/call", {"tool": tool, "arguments": arguments}, context=context)
        if not isinstance(result, dict) or not isinstance(result.get("structuredContent"), dict):
            raise ToolError("L5 tool result unavailable")
        content = result["structuredContent"]
        if not result.get("isError"):
            data = content.get("data")
            run_id = data.get("run_id") if isinstance(data, dict) else None
            if not isinstance(run_id, str) or not run_id:
                raise ToolError("L5 run identity unavailable")
            if assessment_ref and run_id != assessment_ref:
                raise ToolError("L5 run identity changed; keep the original operation reference")
            record["assessment_ref"] = run_id
            record["status"] = "confirmed"
        else:
            record["status"] = "unknown"
        store_operation(record)
        return result
    except (ToolError, KeyError, TypeError) as error:
        record["status"] = "unknown"
        store_operation(record)
        return operation_result({"status": "unknown", "message": str(error)}, record["request_id"])


def recent_operations(binding=None):
    records = []
    for filename in sorted(os.listdir(operation_directory())):
        if not filename.endswith(".json"):
            continue
        try:
            record = load_operation(filename.removesuffix(".json"))
        except ToolError:
            continue
        if binding is not None and record.get("binding") != binding:
            continue
        records.append({"operation_ref": record["request_id"], "request_id": record["request_id"],
                        "tool": record["tool"],
                        "execution_id": record.get("execution_id"), "status": record.get("status", "pending")})
    return records


def result_execute_reference(arguments, execution_id, binding):
    matches = []
    for summary in recent_operations(binding):
        if summary["tool"] != "code_interpreter___execute" or summary.get("execution_id") != execution_id:
            continue
        original = load_operation(summary["operation_ref"])
        if (original.get("status") == "confirmed" and original.get("binding") == binding
                and original.get("provider_id") == arguments.get("provider_id")
                and original.get("arguments", {}).get("session_id") == arguments.get("session_id")):
            matches.append(original["request_id"])
    if len(matches) != 1:
        raise ToolError("A unique confirmed execute receipt for this task is required; read the original operation status")
    return matches[0]


def bound_request_id(record, binding):
    if record["tool"] != "code_interpreter___result":
        return record["request_id"]
    original = result_execute_reference(record["arguments"], record.get("execution_id"), binding)
    if original != record.get("provider_request_id"):
        raise ToolError("Original execute request binding is missing or changed; do not submit another result")
    return original


def stateful_binding(tool, arguments):
    family, _, action = tool.partition("___")
    if family not in {"code_interpreter", "browser"} or action == "start":
        return arguments, None
    reference = arguments.get("execution_ref")
    if not isinstance(reference, str):
        raise ToolError("An owned execution_ref from the start operation is required")
    record = load_operation(reference)
    if (record["tool"] != family + "___start" or record.get("status") != "confirmed"
            or not record.get("execution_id") or not record.get("session_id")):
        raise ToolError("Execution reference is not a completed start for this tool")
    if record.get("binding") != gateway_context()["binding"]:
        raise ToolError("Execution reference belongs to a different login, deployment or run")
    try:
        execution_id = str(UUID(record["execution_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError("Invalid saved execution reference") from exc
    if arguments.get("session_id") != record["session_id"]:
        raise ToolError("Session handle does not belong to this execution reference")
    return {key: value for key, value in arguments.items() if key != "execution_ref"}, execution_id


def lifecycle_call(name, arguments):
    record = load_operation(arguments.get("operation_ref", arguments.get("request_id")))
    request_id = record["request_id"]
    if record["tool"] in native_handoff.PRIVATE_TOOLS:
        if name == "adp___artifact_read":
            return private_result(record, {"request_id": request_id, "status": "unknown", "code": "native_private_artifact_unavailable"})
        return private_operation(record, action="cancel" if name == "adp___execution_close" else "status")
    try:
        binding = gateway_context()["binding"]
    except ToolError as exc:
        return operation_result({"status": "unknown", "message": str(exc)}, request_id)
    if record.get("binding") != binding:
        return operation_result({"status": "rejected", "message":
            "Original login, deployment or run binding is missing or changed; restore it before recovery."}, request_id)
    if name == "adp___operation_status" and record["tool"] == "l5___submit_run":
        if record.get("assessment_ref"):
            return l5_run_operation(record)
        return operation_result({"status": "unknown", "message":
            "Run identity not received; use explicit retry with the original request."}, request_id)
    execution_id = record.get("execution_id")
    if name == "adp___operation_status" and record["tool"].startswith("atlassian___") and NativeTransport.configured() is not None:
        return submit_operation(record, recover=True)
    if not execution_id:
        return operation_result({"status": "unknown", "message": (
            "Execution ID not received; use explicit adp___operation_retry with the saved request ID"
        )}, request_id)
    try:
        execution_id = str(UUID(execution_id))
        path = f"/agent/tools/executions/{execution_id}"
        query = urlencode({"tool": record["tool"]})
        if name == "adp___operation_status":
            return submit_operation(record, recover=True)
        if name == "adp___execution_close":
            receipt = gateway_request(f"{path}/close?{query}", {})
            if not isinstance(receipt, dict) or receipt.get("execution_id") != execution_id:
                raise ToolError("Gateway returned an invalid close receipt")
            result = {**receipt, "operation_ref": request_id}
            confirmed = receipt.get("state") == "closed" and receipt.get("cleanup_status") == "confirmed"
            if not confirmed:
                result["recovery"] = "Cleanup is not confirmed; check the original execution, do not start another session."
            return {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}], "isError": not confirmed}
        offset = arguments.get("offset", 0)
        length = arguments.get("length", 8192)
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 8192:
            raise ValueError("Artifact offset and length must be within the supported range")
        range_query = urlencode({"offset": offset, "length": length})
        wire_request_id = bound_request_id(record, binding)
        receipt = gateway_request(f"{path}/operations/{wire_request_id}/artifacts/result?{query}&{range_query}")
        if not isinstance(receipt, dict) or receipt.get("artifact_id") != "result" or receipt.get("offset") != offset:
            raise ToolError("Gateway returned an invalid artifact chunk")
        try:
            data = base64.b64decode(receipt["data_base64"], validate=True)
            digest = receipt["sha256"]
            if (len(data) != receipt["length"] or receipt["total_length"] < offset + len(data)
                    or len(digest) != 64 or bytes.fromhex(digest).hex() != digest):
                raise ValueError("invalid artifact metadata")
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ToolError("Gateway returned an invalid artifact chunk") from exc
        if offset == 0 and len(data) == receipt["total_length"] and hashlib.sha256(data).hexdigest() != digest:
            raise ToolError("Artifact SHA256 mismatch; do not trust this result")
        return {"content": [{"type": "text", "text": json.dumps({**receipt, "operation_ref": request_id}, sort_keys=True)}]}
    except ToolError as exc:
        status = "rejected" if isinstance(exc, GatewayHTTPError) and exc.status in {400, 403, 404, 409, 416} else "unknown"
        return operation_result({"status": status, "message": str(exc)}, request_id)


def private_result(record, result):
    result = native_handoff.project_result(result, record["request_id"])
    result = {**result, "operation_ref": record["request_id"],
              "recovery": ("Read adp___operation_status with this original operation_ref. "
                           "Private delivery is never retried or recovered as an artifact.")}
    failed = (result.get("delivery_state") in {"failed", "unavailable", "unknown", "cancelled", "expired"}
              or result.get("status") in {"denied", "expired", "unknown"}
              or (result.get("delivery_state") == "completed" and
                  (result.get("exit_code") != 0 or result.get("cleanup") != "complete"
                   or result.get("acknowledgement") != "confirmed")))
    return {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}], "isError": failed}


def private_operation(record, *, action):
    request_id = record["request_id"]
    try:
        native_handoff.validate_arguments(record["tool"], record["arguments"])
        # Recover with the currently native-authenticated coordinator; it reads
        # issuer metadata with its own JWT. Generic facade authority is unused.
        binding = native_handoff.current_binding()
        if action == "submit":
            readiness = native_handoff.ready()
            if readiness["binding"] != binding or record["tool"] not in readiness["result"]["tools"]:
                raise native_handoff.UnavailableError()
            instance = readiness["instance"]
            record["binding"] = {"mode": "native-private", "binding": binding}
            record["private_host"] = True
            store_operation(record)
        else:
            if record.get("binding") != {"mode": "native-private", "binding": binding}:
                raise native_handoff.UnavailableError("native_host_changed")
            # Recovery requires fresh issuer authority but does not require the
            # original profile/executable/key still to be available for new use.
            instance = native_handoff.rpc(binding, "connect")["instance"]
        response = native_handoff.rpc(binding, action, instance=instance, request_id=request_id,
                                      tool=record["tool"], arguments=record["arguments"])
        result = response["result"]
        if "state" in result:
            raise native_handoff.UnavailableError()
        if any(key in record and key in result and record[key] != result[key] for key in native_handoff.IDS):
            raise native_handoff.UnavailableError("native_request_conflict")
        for key in ("execution_id", "receipt_id", "status"):
            if key in result:
                record[key] = result[key]
        store_operation(record)
        return private_result(record, result)
    except native_handoff.UnavailableError as error:
        record["status"] = "unknown"
        store_operation(record)
        return private_result(record, {"request_id": request_id, "status": "unknown", "code": error.code})


def handle(message):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "adp-tools", "version": "1.0.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        try:
            security_listing = gateway_request("/v1/l5/mcp/tools/list", {})
        except GatewayHTTPError as error:
            if error.status != 404:
                raise
            security_listing = {"tools": []}
        if not isinstance(security_listing, dict) or not isinstance(security_listing.get("tools"), list):
            raise ToolError("L5 tool discovery unavailable")
        security_tools = [tool for tool in security_listing["tools"]
                          if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                          and tool["name"].startswith("l5___")]
        try:
            listing = gateway_request("/agent/tools")
        except GatewayHTTPError as error:
            if error.status != 403 or not security_tools:
                raise
            listing = {"tools": []}
        tools = []
        for tool in listing["tools"]:
            if tool["name"] in native_handoff.PRIVATE_TOOLS:
                continue  # These names can only come from the local native host.
            if tool["name"] in {"browser___start", "code_interpreter___start"}:
                schema = tool["inputSchema"]
                tool = {**tool, "inputSchema": {**schema, "properties": {**schema.get("properties", {}),
                    "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 900}}}}
            tools.append(tool)
        try:
            tools += native_handoff.descriptors(native_handoff.ready()["result"]["tools"])
        except native_handoff.UnavailableError:
            pass
        # The facade also publishes L5 tools. Use the direct L5 descriptor for
        # calls sent to that API, counting each advertised tool only once.
        security_names = {tool["name"] for tool in security_tools}
        definitions = [tool for tool in tools if tool["name"] not in security_names] + security_tools + LOCAL_TOOLS
        size = len(json.dumps(definitions, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        capacity = MAX_EAGER_MCP_SCHEMA_BYTES + (384 * 1024 if security_tools else 0)
        if size > capacity:
            raise ToolError(f"Unsupported eager tool capacity: {len(definitions)} MCP descriptors use {size} UTF-8 bytes "
                            f"(limit {capacity}); native schema deferral is unverified. "
                            "Reduce approved tools; no tools were silently omitted.")
        return {"tools": definitions}
    if method == "tools/call":
        params = message.get("params") or {}
        if (not isinstance(params, dict) or not isinstance(params.get("name"), str)
                or not isinstance(params.get("arguments", {}), dict)
                or set(params) - {"name", "arguments", "_meta"}):
            raise ValueError("Tool name and object arguments required")
        tool = params["name"]
        arguments = params.get("arguments", {})
        if tool.startswith("adp___"):
            validate_local_arguments(tool, arguments)
        if tool == "adp___operations_list":
            operations = recent_operations(gateway_context()["binding"])
            try:
                operations += recent_operations({"mode": "native-private", "binding": native_handoff.current_binding()})
            except (native_handoff.UnavailableError, OSError, ValueError):
                pass
            return {"content": [{"type": "text", "text": json.dumps({"operations": operations})}]}
        if tool == "adp___operation_retry":
            record = load_operation(arguments.get("operation_ref", arguments.get("request_id")))
            if "tool" in arguments and (arguments["tool"] != record["tool"] or
                                        arguments["arguments"] != record.get("client_arguments", record["arguments"])):
                return operation_result({"status": "rejected", "message": "Operation reference conflicts with stored tool or input"},
                                        record["request_id"])
            if record["tool"] == "l5___submit_run":
                return l5_run_operation(record)
            return submit_operation(record, recover=True)
        if tool in {"adp___operation_status", "adp___artifact_read", "adp___execution_close"}:
            return lifecycle_call(tool, arguments)
        validate_arguments(tool, arguments)
        if tool == "l5___submit_run":
            if not isinstance(arguments.get("idempotency_key"), str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{16,128}", arguments["idempotency_key"]):
                raise ToolError("A valid idempotency_key is required for recoverable run submissions")
            record = {"request_id": str(uuid4()), "tool": tool, "arguments": arguments,
                      "binding": gateway_context()["binding"], "status": "unknown"}
            store_operation(record, create=True)
            return l5_run_operation(record)
        if tool.startswith("l5___"):
            result = gateway_request("/v1/l5/mcp/tools/call", {"tool": tool, "arguments": arguments})
            if not isinstance(result, dict) or not isinstance(result.get("structuredContent"), dict):
                raise ToolError("L5 tool result unavailable")
            return result
        if tool in native_handoff.PRIVATE_TOOLS:
            try:
                native_handoff.validate_arguments(tool, arguments)
            except native_handoff.UnavailableError as error:
                raise ToolError(error.code) from None
        provider_arguments, execution_id = stateful_binding(tool, arguments)
        request_id = str(uuid4())
        lifetime = arguments.get("ttl_seconds") if tool in {"browser___start", "code_interpreter___start"} else None
        record = {"request_id": request_id, "tool": tool,
                  "arguments": {key: value for key, value in provider_arguments.items() if key != "ttl_seconds"},
                  "client_arguments": arguments, "status": "pending", "caller_execution_id": execution_id}
        if lifetime is not None:
            record["ttl_seconds"] = lifetime
        if execution_id:
            record["execution_id"] = execution_id
        if tool == "code_interpreter___result":
            # Provider result reads use the original execute key while the
            # local result operation retains its separate recovery reference.
            record["provider_request_id"] = result_execute_reference(
                provider_arguments, execution_id, gateway_context()["binding"])
        try:
            store_operation(record, create=True)
        except ExistingOperationError:
            previous = load_operation(request_id)
            execution_conflict = (record["caller_execution_id"] != previous.get("caller_execution_id")
                                  and record["caller_execution_id"] != previous.get("execution_id"))
            if previous["tool"] != tool or previous.get("client_arguments", previous["arguments"]) != arguments or execution_conflict:
                return operation_result({"status": "rejected", "message": "Request ID conflicts with stored tool or input"}, request_id)
            return submit_operation(previous, recover=True)
        return submit_operation(record)
    raise LookupError("Method not found")


def main():
    parser = argparse.ArgumentParser(description="Run the bundled ADP stdio MCP server")
    parser.add_argument("action", nargs="?", choices=["operations"])
    args = parser.parse_args()
    if args.action == "operations":
        print(json.dumps({"operations": recent_operations()}))
        return
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC request")
            if "id" not in message:
                continue
            response = {"jsonrpc": "2.0", "id": message["id"]}
            try:
                response["result"] = handle(message)
            except (ToolError, ValueError, KeyError, TypeError, LookupError) as exc:
                response["error"] = {"code": -32601 if isinstance(exc, LookupError) else -32603,
                                     "message": str(exc)}
        except (ValueError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
