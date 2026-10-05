"""Independent shared AgentCore Lambda; Task authority owns every grant decision."""

import base64
import json
import os
from decimal import Decimal

import boto3
from botocore.config import Config
from fastapi import HTTPException
from pydantic import ValidationError

from adp_tools.authority import TaskAuthorityClient, require_worker
from adp_tools.storage import OperationRepository
from agentcore_tools.code_interpreter import Body, CodeInterpreter
from agentcore_tools.contracts import CyberBody
from agentcore_tools.search_service import WebSearch
from agentcore_tools.websearch import SearchInput


def lambda_handler(event, context):
    if event.get("resource") == "/tools/browser":
        from agentcore_tools.browser_http import lambda_handler as browser_handler
        return browser_handler(event, context)
    authority = None
    validated = False
    try:
        route = event.get("resource")
        if event.get("httpMethod") != "POST" or route not in {"/tools/websearch", "/tools/code-interpreter"}:
            raise HTTPException(404, "Not found")
        require_worker(event, set(filter(None, os.environ.get("ADP_TOOLS_WORKER_ROLES", "").split(","))))
        raw = event.get("body", "")
        if not isinstance(raw, str) or len(raw) > 90000:
            raise HTTPException(413, "Tool request exceeds bound")
        raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
        if len(raw) > 65536:
            raise HTTPException(413, "Tool request exceeds bound")
        code = route == "/tools/code-interpreter"
        body = (Body if code else CyberBody).model_validate_json(raw)
        if not code:
            if body.operation != "search":
                raise HTTPException(404, "Tool is unavailable on this transport")
            SearchInput.model_validate(body.payload)
        cleanup = code and body.operation in {"close", "cancel_jobs"}
        flag = "ADP_CODE_INTERPRETER_ENABLED" if code else "ADP_WEBSEARCH_ENABLED"
        if not cleanup and os.environ.get(flag, "false").lower() != "true":
            raise HTTPException(503, "Code Interpreter unavailable" if code else "Cyber tools unavailable")
        authority = TaskAuthorityClient(
            os.environ.get("ADP_TASK_AUTHORITY_ENDPOINT", ""), event.get("headers") or {},
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )
        attempt = body.attempt.model_dump()
        def authorize():
            return authority.authorize(
                attempt=attempt, tool=("code_interpreter." + body.operation if code else "websearch.search"), cleanup=cleanup,
            )
        validated = True
        verified = authorize()
        table = os.environ.get("ADP_TOOLS_TABLE", "")
        if not table:
            raise HTTPException(503, "Cyber operation store unavailable")
        client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"), config=Config(retries={"total_max_attempts": 1}))
        repo = OperationRepository(client, table, authorize)
        if code:
            identifier = os.environ.get("ADP_CODE_INTERPRETER_ID", "")
            if not identifier:
                raise HTTPException(503, "Code Interpreter resource unavailable")
            result = CodeInterpreter(repo, authority, identifier, revalidate=lambda: authorize().identity).execute(verified.identity, body)
        else:
            result = WebSearch(repo, authority, revalidate=lambda: authorize().identity).execute(verified.identity, body)
        if authorize().identity != verified.identity:
            raise HTTPException(403, "Task authority changed")
        return response(200, result)
    except HTTPException as error:
        return response(error.status_code, {"code": "tool_refused", "message": error.detail})
    except (ValidationError, ValueError, TypeError):
        return response(503 if validated else 422, {
            "code": "outcome_unavailable" if validated else "invalid_request",
            "message": "Tool outcome unavailable; retain operation identity" if validated else "Invalid cyber tool request",
        })
    except Exception:
        return response(503, {"code": "outcome_unavailable", "message": "Tool outcome unavailable; retain operation identity"})
    finally:
        if authority is not None:
            authority.close()


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
