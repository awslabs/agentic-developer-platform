"""Verify the facade's signed authority before a Gateway Lambda tool executes."""

import base64
import hashlib
import hmac
import json
import os
import re
import time

import boto3
from adp_tools.contracts import UUID4
from botocore.config import Config
from fastapi import HTTPException

from agentcore_tools.code_interpreter import PATH
from agentcore_tools.url_contract import checked_url, validate_url_payload


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _decode(value):
    if not isinstance(value, str) or not 0 < len(value) <= 16384:
        raise ValueError("Invalid authority assertion")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def verified_call(event, context, expected_tool):
    if not isinstance(event, dict) or not isinstance(event.get("adp_assertion"), str):
        raise HTTPException(403, "Gateway authority unavailable")
    try:
        tool = context.client_context.custom["bedrockAgentCoreToolName"]
        if tool != expected_tool:
            raise ValueError("Tool identity differs")
        envelope = json.loads(_decode(event["adp_assertion"]))
        claims, signature = envelope["claims"], _decode(envelope["signature"])
        if not isinstance(claims, dict) or not signature:
            raise ValueError("Unsigned assertion")
        issued, expires = claims["issued_at"], claims["expires_at"]
        now = int(time.time())
        principal = claims["principal"]
        arguments = {key: value for key, value in event.items() if key != "adp_assertion"}
        cleanup = claims.get("cleanup_reconcile") is True
        starting = tool in {"browser___start", "code-interpreter___start"}
        cleanup_role = os.environ.get("ADP_TOOLS_RECONCILER_ROLE_ARN")
        cleanup_key = os.environ.get("ADP_TOOLS_RECONCILER_KEY_ARN")
        if claims.get("cleanup_reconcile") not in (None, True) or (
            cleanup and (not cleanup_role or not cleanup_key or
                         claims.get("caller_role_arn") != cleanup_role or
                         tool not in {"browser___execution_close", "code-interpreter___execution_close"})
        ):
            raise ValueError("Untrusted cleanup assertion")
        if (type(issued) is not int or type(expires) is not int or not issued <= now < expires
                or expires - issued > 60 or (not cleanup and
                    claims.get("caller_role_arn") != os.environ["ADP_TOOLS_FACADE_ROLE_ARN"])
                or claims.get("method") != "tools/call" or claims.get("tool") != tool
                or not isinstance(principal, dict)
                or not all(isinstance(principal.get(key), str) and principal[key]
                           for key in ("user_id", "tenant_id", "team_id"))
                or (claims.get("run_id") is None) != (claims.get("workload_id") is None)
                or (claims.get("run_id") is None and claims.get("run_expires_at") is not None)
                or (claims.get("run_id") is not None and (
                    not isinstance(claims["run_id"], str) or not claims["run_id"]
                    or not isinstance(claims["workload_id"], str) or not claims["workload_id"]
                    or (not cleanup and (type(claims.get("run_expires_at")) is not int or
                                         claims["run_expires_at"] <= now))))
                or (cleanup and (claims.get("run_expires_at") is not None or
                                 set(arguments) != {"execution_id"}))
                or (starting and (type(claims.get("execution_deadline_at")) is not int or
                    not now < claims["execution_deadline_at"] <= issued + 900 or
                    (claims.get("run_expires_at") is not None and
                     claims["execution_deadline_at"] > claims["run_expires_at"])))
                or (not starting and "execution_deadline_at" in claims)
                or principal["user_id"] != claims.get("root_human_id")
                or not isinstance(claims.get("receipt_id"), str) or not claims["receipt_id"]
                or not hmac.compare_digest(claims["arguments_sha256"], hashlib.sha256(_encoded(arguments)).hexdigest())):
            raise ValueError("Authority binding differs")
        verified = boto3.client("kms", region_name=os.environ["AWS_REGION"], config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})).verify(
            KeyId=cleanup_key if cleanup else os.environ["ADP_TOOLS_ASSERTION_KEY_ARN"], Message=_encoded(claims),
            MessageType="RAW", Signature=signature, SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if verified.get("SignatureValid") is not True:
            raise ValueError("Untrusted signature")
        return claims, arguments
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, base64.binascii.Error):
        raise HTTPException(403, "Gateway authority unavailable") from None

STATEFUL_FIELDS = {
    "code-interpreter": {
        "start": {"execution_id", "request_id"},
        "execute": {"execution_id", "request_id", "session_id", "code", "language"},
        "result": {"execution_id", "request_id", "session_id", "provider_id"},
        "file": {"execution_id", "request_id", "session_id", "path"},
        "close": {"execution_id", "session_id"},
        "operation_get": {"execution_id", "request_id"},
        "artifact_read": {"execution_id", "artifact_id"},
        "execution_close": {"execution_id"},
    },
    "browser": {
        "start": {"execution_id", "request_id", "url"},
        "step": {"execution_id", "request_id", "session_id", "view_id", "action"},
        "inspect": {"execution_id", "request_id", "session_id", "view_id"},
        "close": {"execution_id", "request_id", "session_id"},
        "operation_get": {"execution_id", "request_id"},
        "artifact_read": {"execution_id", "artifact_id"},
        "execution_close": {"execution_id"},
    },
}


def verified_stateful(event, context):
    try:
        tool = context.client_context.custom["bedrockAgentCoreToolName"]
        target, operation = tool.split("___")
        fields = STATEFUL_FIELDS[target][operation]
        claims, arguments = verified_call(event, context, tool)
        optional = ({"offset"} if operation == "artifact_read" else
                    {"candidate_id", "url", "seconds"} if target == "browser" and operation == "step" else set())
        if not fields <= arguments.keys() or arguments.keys() - fields - optional:
            raise ValueError("Invalid tool fields")
        if any(not isinstance(arguments.get(field), str) or not re.fullmatch(UUID4, arguments[field])
               for field in ("execution_id", "request_id") if field in arguments):
            raise ValueError("Invalid operation identity")
        if "session_id" in arguments and (not isinstance(arguments["session_id"], str)
                                       or not re.fullmatch(r"[a-f0-9]{64}", arguments["session_id"])):
            raise ValueError("Invalid session identity")
        if "offset" in arguments and (type(arguments["offset"]) is not int or not 0 <= arguments["offset"] <= 1000000):
            raise ValueError("Invalid artifact offset")
        for field in ("artifact_id", "provider_id", "view_id"):
            if field in arguments and (not isinstance(arguments[field], str) or not 0 < len(arguments[field]) <= 256):
                raise ValueError("Invalid provider reference")
        if target == "code-interpreter":
            if operation == "execute" and (arguments["language"] != "python" or not isinstance(arguments["code"], str)
                                           or not 0 < len(arguments["code"].encode()) <= 8192):
                raise ValueError("Only bounded Python is supported")
            if operation == "file" and (not isinstance(arguments["path"], str) or not PATH.fullmatch(arguments["path"])):
                raise ValueError("Invalid interpreter path")
        elif operation in {"start", "step", "inspect", "close"}:
            browser_operation = "browser_" + operation
            payload = {key: value for key, value in arguments.items()
                       if key not in {"execution_id", "request_id", "view_id"}}
            if operation == "step":
                payload["view_id"] = arguments["view_id"]
            validate_url_payload(browser_operation, payload)
            if operation == "start" or (operation == "step" and arguments["action"] == "navigate"):
                checked_url(arguments["url"])
        return claims, arguments, target, operation
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise HTTPException(403, "Gateway tool unavailable") from None


def dispatch_stateful(target, operation, arguments, claims, revalidate, assertion=None):
    if target == "browser":
        if os.environ.get("ADP_GATEWAY_BROWSER_READY") != "true":
            raise HTTPException(503, "Browser Gateway consumer unavailable")
        from agentcore_tools.gateway_browser import GatewayBrowser
        browser = GatewayBrowser()
        if operation in {"start", "step", "inspect", "close"}:
            return browser.submit(operation, arguments, claims, revalidate, assertion)
        if operation in {"operation_get", "artifact_read"}:
            return getattr(browser, operation)(arguments, claims)
        if operation == "execution_close":
            return browser.execution_close(arguments, claims, revalidate)
        raise HTTPException(503, "Browser cleanup unavailable")
    if target != "code-interpreter":
        raise HTTPException(503, "Stateful gateway provider unavailable")
    from agentcore_tools.gateway_interpreter import GatewayInterpreter

    interpreter = GatewayInterpreter()
    if operation in {"start", "execute", "file", "close", "execution_close"}:
        return getattr(interpreter, operation)(arguments, claims,
            revalidate=lambda: revalidate()[:2])
    if operation in {"result", "operation_get", "artifact_read"}:
        return getattr(interpreter, operation)(arguments, claims)
    raise HTTPException(503, "Interpreter operation unavailable")
