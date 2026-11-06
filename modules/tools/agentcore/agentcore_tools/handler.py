"""Shared AgentCore gateway Lambda."""

import json
import os
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from agentcore_tools import websearch
from agentcore_tools.gateway_search import GatewaySearch
from agentcore_tools.mcp_adapter import (
    dispatch_stateful,
    verified_call,
    verified_stateful,
)


def lambda_handler(event, context):
    if (isinstance(event, dict) and event == {"action": "reconcile-code-interpreter"}
            and getattr(context, "client_context", None) is None):
        from agentcore_tools.gateway_interpreter import GatewayInterpreterReconciler
        GatewayInterpreterReconciler().run_once()
        return {"status": "ok"}
    if isinstance(event, dict) and "resource" not in event:
        try:
            tool = context.client_context.custom["bedrockAgentCoreToolName"]
            if not tool.startswith("websearch___"):
                claims, arguments, target, operation = verified_stateful(event, context)
                enabled = "ADP_CODE_INTERPRETER_ENABLED" if target == "code-interpreter" else "ADP_TASK_BROWSER_HTTP_ENABLED"
                if operation in {"start", "execute", "file", "step"} and os.environ.get(enabled, "false").lower() != "true":
                    raise HTTPException(503, "Stateful gateway tool unavailable")
                result = dispatch_stateful(target, operation, arguments, claims,
                    revalidate=lambda: verified_stateful(event, context),
                    **({"assertion": event["adp_assertion"]} if target == "browser" else {}))
                return {"result": result, "receipt_id": result.get("receipt_id", claims["receipt_id"]),
                        "tenant_id": claims["principal"]["tenant_id"], "user_id": claims["principal"]["user_id"]}
            if tool not in {"websearch___search", "websearch___operation_get", "websearch___artifact_read"}:
                raise HTTPException(403, "Gateway tool unavailable")
            claims, arguments = verified_call(event, context, tool)
            if tool == "websearch___search":
                if os.environ.get("ADP_WEBSEARCH_ENABLED", "false").lower() != "true":
                    raise HTTPException(503, "Web Search unavailable")
                result = GatewaySearch().search(arguments, claims, websearch.search,
                    revalidate=lambda: verified_call(event, context, tool))
            elif tool == "websearch___operation_get":
                result = GatewaySearch().operation_get(arguments, claims)
            else:
                result = GatewaySearch().artifact_read(arguments, claims)
            return {"result": result, "receipt_id": result.get("receipt_id", claims["receipt_id"]),
                    "tenant_id": claims["principal"]["tenant_id"], "user_id": claims["principal"]["user_id"]}
        except (HTTPException, ValidationError, ValueError, TypeError):
            return {"isError": True, "content": [{"type": "text", "text": "tool_refused"}]}
        except Exception:
            return {"isError": True, "content": [{"type": "text", "text": "outcome_unavailable"}]}
    return {"statusCode": 404, "body": json.dumps({"code": "not_found"})}


def json_value(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError("Unsupported response value")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=json_value, allow_nan=False),
    }
