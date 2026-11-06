"""Fail-closed MCP interception after AgentCore's IAM resource-policy check."""

import base64
import hashlib
import json
import logging
import os
import re
import time

from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config

LOGGER = logging.getLogger(__name__)
# Lambda's root logger defaults to WARNING; the allow/deny lines below are the only
# operational trace of the interceptor and never include payloads or assertions.
LOGGER.setLevel(logging.INFO)
MAX_ASSERTION_SECONDS = 60
MAX_AUDIT_UTF8_BYTES = 512
AUDIT_FIELDS = frozenset({
    "receipt_id", "method", "reason", "error_type", "status", "streaming",
    "body_type", "body_key_count", "response_key_count",
})
VENDOR_TOOL_SCOPES = {
    "github___get_user": "read:user",
    "jira___get_issue": "read:jira-work",
    "atlassian___get_project_context": "read:jira:agent-interface",
    "atlassian___search_jira": "search:jira:agent-interface",
    "atlassian___get_jira_issue": "read:jira:agent-interface",
    "atlassian___search_confluence": "search:confluence:agent-interface",
    "atlassian___get_confluence_page": "read:confluence:agent-interface",
    "slack___get_user": "users:read",
    "slack___post_message": "chat:write",
}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _audit(event, **fields):
    """Emit one bounded JSON message; never log request/response key names.

    A handler path emits at most two messages. The cap covers application
    message bytes, separately from Lambda's runtime prefix and platform logs.
    """
    record = {"event": event}
    for key, value in fields.items():
        if key not in AUDIT_FIELDS:
            continue
        if type(value) is bool or value is None or type(value) is int and -10**12 <= value <= 10**12:
            record[key] = value
        else:
            text = value if isinstance(value, str) else type(value).__name__
            raw = text.encode("utf-8", errors="replace")
            # Preserve a correlatable digest if an unusual receipt needs a cap.
            if key == "receipt_id" and len(raw) > 128:
                record["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
            limit = 128 if key == "receipt_id" else 64
            record[key] = raw[:limit].decode("utf-8", errors="ignore")
            if len(raw) > limit:
                record["truncated"] = True
    # JSON escaping can expand even a byte-limited string. Shorten values, not
    # serialized JSON, so Unicode and the audit field names remain valid.
    encoded = _encoded(record)
    while len(encoded) > MAX_AUDIT_UTF8_BYTES:
        candidates = [key for key in AUDIT_FIELDS if isinstance(record.get(key), str) and record[key]]
        if not candidates:
            raise ValueError("Audit schema exceeds its byte budget")
        key = max(candidates, key=lambda name: len(_encoded(record[name])))
        raw = record[key].encode("utf-8")
        if key == "receipt_id" and "receipt_sha256" not in record:
            record["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
        record[key] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
        record["truncated"] = True
        encoded = _encoded(record)
    LOGGER.info("%s", encoded.decode("utf-8"))


def _decode(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError("Invalid assertion encoding")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _headers(request):
    headers = request.get("headers")
    if not isinstance(headers, dict):
        raise TypeError("Request headers unavailable")
    normalized = {key.lower(): value for key, value in headers.items() if isinstance(key, str)}
    if len(normalized) != len(headers):
        raise ValueError("Ambiguous request headers")
    return normalized


def _verify_assertion(headers, now, response=False):
    envelope = json.loads(_decode(headers.get("x-adp-assertion")))
    claims, signature = envelope["claims"], _decode(envelope["signature"])
    if not isinstance(claims, dict) or not signature:
        raise ValueError("Invalid assertion")
    key_arn = os.environ["ADP_TOOLS_ASSERTION_KEY_ARN"]
    expected_role = os.environ["ADP_TOOLS_FACADE_ROLE_ARN"]
    if not key_arn or not expected_role or claims.get("caller_role_arn") != expected_role:
        raise ValueError("Untrusted assertion role")
    issued, expires = claims["issued_at"], claims["expires_at"]
    if (type(issued) is not int or type(expires) is not int
            or expires <= issued or expires - issued > MAX_ASSERTION_SECONDS
            or (not response and not issued <= now < expires)):
        raise ValueError("Expired assertion")
    import boto3

    verified = boto3.client("kms", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})).verify(
        KeyId=key_arn, Message=_encoded(claims), MessageType="RAW",
        Signature=signature, SigningAlgorithm="RSASSA_PSS_SHA_256",
    )
    if verified.get("SignatureValid") is not True:
        raise ValueError("Invalid signature")
    principal = claims.get("principal")
    if (not isinstance(principal, dict)
            or not all(isinstance(principal.get(field), str) and principal[field]
                       for field in ("user_id", "tenant_id", "team_id"))
            or not isinstance(claims.get("receipt_id"), str) or not claims["receipt_id"]):
        raise ValueError("Missing principal or receipt")
    return claims


def _check_binding(claims, request, headers):
    body = request.get("body")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise ValueError("Malformed MCP request")
    method = body.get("method")
    if method == "tools/list":
        tool, arguments = "tools/list", body.get("params", {})
    elif method == "tools/call" and isinstance(body.get("params"), dict):
        tool = body["params"].get("name")
        arguments = body["params"].get("arguments", {})
    else:
        return "tool_mismatch"
    if not isinstance(tool, str) or not tool or not isinstance(arguments, dict):
        return "tool_mismatch"
    if (not isinstance(headers.get("x-adp-tenant"), str)
            or headers["x-adp-tenant"] != claims["principal"]["tenant_id"]):
        return "tenant_mismatch"
    if method == "tools/call" and "adp_assertion" in arguments:
        if not isinstance(arguments["adp_assertion"], str) or arguments["adp_assertion"] != headers.get("x-adp-assertion"):
            return "tool_mismatch"
        arguments = {key: value for key, value in arguments.items() if key != "adp_assertion"}
    if method == "tools/call" and tool.startswith(("knowledge___", "github___", "jira___", "slack___", "atlassian___")) and (
        type(body.get("id")) not in {str, int} or not str(body["id"])
        or type(claims.get("rpc_id")) is not type(body["id"]) or claims["rpc_id"] != body["id"]
    ):
        return "tool_mismatch"
    if (claims.get("method") != method or claims.get("tool") != tool
            or claims.get("arguments_sha256") != hashlib.sha256(_encoded(arguments)).hexdigest()):
        return "tool_mismatch"
    return None


def _deny(request, reason):
    body = request.get("body") if isinstance(request, dict) else None
    request_id = body.get("id") if isinstance(body, dict) else None
    _audit("denied", reason=reason)
    return {"interceptorOutputVersion": "1.0", "mcp": {
        "transformedGatewayResponse": {"statusCode": 403, "body": {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32000, "message": reason},
        }}
    }}


def handler(event, context):
    mcp = event.get("mcp", {}) if isinstance(event, dict) else {}
    if not isinstance(mcp, dict):
        return _deny({}, "untrusted_assertion")
    request = mcp.get("gatewayRequest")
    if not isinstance(request, dict):
        return _deny({}, "untrusted_assertion")
    try:
        headers = _headers(request)
        # The gateway sends the request-phase event with "gatewayResponse": null
        # (the field exists but no response has been generated yet); only a
        # populated object means the RESPONSE interception point.
        response = isinstance(mcp.get("gatewayResponse"), dict)
        claims = _verify_assertion(headers, int(time.time()), response=response)
        reason = _check_binding(claims, request, headers)
        if reason:
            return _deny(request, reason)
        if claims["method"] == "tools/call":
            tool = claims["tool"]
            if tool.startswith("atlassian___"):
                scopes = claims.get("connection_scopes")
                connection = claims.get("connection")
                binding = claims.get("binding")
                if (
                    tool not in VENDOR_TOOL_SCOPES
                    or not isinstance(scopes, list)
                    or VENDOR_TOOL_SCOPES[tool] not in scopes
                ):
                    return _deny(request, "scope_not_granted")
                if (
                    not isinstance(connection, dict)
                    or set(connection) != {"id", "revision", "site_id"}
                    or any(not isinstance(value, str) or not value for value in connection.values())
                    or connection["id"] != claims.get("connection_id")
                    or not isinstance(binding, dict)
                    or binding.get("release") != claims.get("adapter_release")
                    or binding.get("operation_id") != str(claims.get("rpc_id"))
                    or binding.get("receipt_id") != claims.get("receipt_id")
                    or binding.get("payload_sha256") != claims.get("arguments_sha256")
                ):
                    return _deny(request, "connection_binding_required")
            if tool.split("___", 1)[0] in {"github", "jira", "slack"}:
                scopes = claims.get("connection_scopes")
                if (tool not in VENDOR_TOOL_SCOPES or not isinstance(scopes, list)
                        or any(not isinstance(scope, str) for scope in scopes)
                        or VENDOR_TOOL_SCOPES[tool] not in scopes):
                    return _deny(request, "scope_not_granted")
                if tool not in {"github___get_user", "jira___get_issue", "slack___get_user", "slack___post_message"}:
                    return _deny(request, "user_bound_target_unavailable")
                binding = claims.get(tool.split("___", 1)[0] + "_connection")
                fields = {"connection_id", "revision", "account_id", "scopes_sha256", "credential_sha256"}
                if tool == "jira___get_issue":
                    fields.add("resources_sha256")
                    if not {"read:jira-work", "read:me"} <= set(scopes):
                        return _deny(request, "scope_not_granted")
                if (
                    not isinstance(binding, dict)
                    or set(binding) != fields
                    or binding["connection_id"] != claims.get("connection_id")
                    or not isinstance(binding["account_id"], str)
                    or not re.fullmatch(
                        r"[1-9][0-9]{0,18}" if tool == "github___get_user" else r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}" if tool == "jira___get_issue" else r"T[A-Z0-9]{8,63}:U[A-Z0-9]{8,63}",
                        binding["account_id"],
                    )
                    or any(not isinstance(binding[key], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[key]) for key in fields if key.endswith("sha256"))
                    or any(not isinstance(binding[key], str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", binding[key]) for key in ("connection_id", "revision"))
                ):
                    return _deny(request, "connection_binding_required")
        if response:
            gateway_response = mcp["gatewayResponse"]
            if gateway_response.get("isStreamingResponse") is True and gateway_response.get("body") is None:
                # Streaming: the first response event carries only the HTTP
                # envelope (statusCode, headers); every JSON-RPC frame follows in
                # later invocations, each bound and stamped below. Pass the
                # envelope through unchanged — there is no payload to inspect.
                _audit("response_envelope_allowed", receipt_id=claims["receipt_id"], method=claims.get("method"))
                transformed = {field: gateway_response[field] for field in ("statusCode", "headers") if field in gateway_response}
                return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": transformed}}
            body = gateway_response["body"]
            if (gateway_response.get("statusCode") == 403
                    and claims["tool"].startswith("knowledge___")
                    and (not isinstance(body, dict) or not isinstance(body.get("error"), dict))):
                body = {"jsonrpc": "2.0", "id": request["body"].get("id"), "error": {
                    "code": -32003, "message": "knowledge_access_denied",
                    "data": {"adp_receipt_id": claims["receipt_id"]},
                }}
            elif "result" in body and isinstance(body["result"], dict):
                result = body["result"]
                body = {**body, "result": {**result, "_meta": {
                    **result.get("_meta", {}), "adp_receipt_id": claims["receipt_id"]
                }}}
            elif "error" in body and isinstance(body["error"], dict):
                error = body["error"]
                data = error.get("data", {})
                if not isinstance(data, dict):
                    data = {"provider_data": data}
                body = {**body, "error": {**error, "data": {**data, "adp_receipt_id": claims["receipt_id"]}}}
            elif gateway_response.get("isStreamingResponse") is True and isinstance(body.get("params"), dict):
                params = body["params"]
                body = {**body, "params": {**params, "_meta": {
                    **params.get("_meta", {}), "adp_receipt_id": claims["receipt_id"]
                }}}
            else:
                raise ValueError("Unrecognized MCP response")
            # The gateway accepts only the documented output fields; echoing the
            # input's own fields (isStreamingResponse, ...) is rejected as an
            # invalid interceptor response.
            transformed = {"body": body}
            for field in ("statusCode", "headers"):
                if field in gateway_response:
                    transformed[field] = gateway_response[field]
            _audit("response_allowed", receipt_id=claims["receipt_id"], method=claims.get("method"))
            return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": transformed}}
        body = request["body"]
        if body.get("method") == "tools/call" and body["params"]["name"].split("___", 1)[0] in {"websearch", "code-interpreter", "browser", "knowledge", "github", "jira", "slack", "atlassian"}:
            arguments = body["params"]["arguments"]
            if "adp_assertion" in arguments:
                return _deny(request, "tool_mismatch")
            body = {**body, "params": {**body["params"], "arguments": {**arguments, "adp_assertion": headers["x-adp-assertion"]}}}
        _audit("request_allowed", receipt_id=claims["receipt_id"])
        return {"interceptorOutputVersion": "1.0", "mcp": {
            "transformedGatewayRequest": {"body": body}
        }}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RuntimeError, BotoCoreError, ClientError) as exc:
        gateway_response = mcp.get("gatewayResponse")
        if isinstance(gateway_response, dict):
            # Shape only — never the payload: lets an operator see which response
            # contract the gateway used when a refusal is unexpected.
            body = gateway_response.get("body")
            _audit("refused_response", error_type=type(exc).__name__, status=gateway_response.get("statusCode"),
                   streaming=gateway_response.get("isStreamingResponse"), response_key_count=len(gateway_response),
                   body_type=type(body).__name__, body_key_count=len(body) if isinstance(body, dict) else None)
        else:
            _audit("refused_request", error_type=type(exc).__name__)
        return _deny(request, "untrusted_assertion")
