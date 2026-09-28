"""Cyber-owned Lambda behind the shared API Gateway's AWS_IAM /tools/cyber."""

import base64
import json
import os
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from adp_tools.authority import TaskAuthorityClient, require_worker
from adp_tools.storage import OperationRepository
from cyber_tools.backends import CyberBackends
from cyber_tools.operations import CyberBody, CyberOperations, validate_payload


def lambda_handler(event, context):
    authority = None
    validated = False
    try:
        if event.get("httpMethod") != "POST" or event.get("resource") not in {
            "/tools/cyber",
            "/tools/cyber/common-crawl",
            "/tools/code-interpreter",
            "/tools/websearch",
        }:
            raise HTTPException(404, "Not found")
        require_worker(
            event,
            set(
                filter(None, os.environ.get("CYBER_TOOLS_WORKER_ROLES", "").split(","))
            ),
        )
        raw = event.get("body", "")
        if not isinstance(raw, str) or len(raw) > 90000:
            raise HTTPException(413, "Tool request exceeds bound")
        raw = (
            base64.b64decode(raw, validate=True)
            if event.get("isBase64Encoded")
            else raw.encode()
        )
        if len(raw) > 65536:
            raise HTTPException(413, "Tool request exceeds bound")
        code_route = event.get("resource") == "/tools/code-interpreter"
        if code_route:
            from cyber_tools.code_interpreter import Body

            body = Body.model_validate_json(raw)
        else:
            body = CyberBody.model_validate_json(raw)
            validate_payload(body.operation, body.payload)
        if (body.operation == "search") != (
            event.get("resource") == "/tools/websearch"
        ):
            raise HTTPException(404, "Tool is unavailable on this transport")
        if body.operation.startswith("browser_"):
            raise HTTPException(404, "Browser tools run in the Task worker")
        if (
            event.get("resource") == "/tools/cyber/common-crawl"
            and not body.operation.startswith("common_crawl_")
            and body.operation != "cancel_jobs"
        ):
            raise HTTPException(404, "Tool is unavailable on this transport")
        cleanup = body.operation in (
            {"close", "cancel_jobs"} if code_route else {"cancel_jobs"}
        )
        if (
            code_route
            and not cleanup
            and os.environ.get("ADP_CODE_INTERPRETER_ENABLED", "false").lower()
            != "true"
        ):
            raise HTTPException(503, "Code Interpreter unavailable")
        if (
            not cleanup
            and not code_route
            and os.environ.get(
                "ADP_WEBSEARCH_ENABLED"
                if body.operation == "search"
                else "ADP_TASK_CYBER_ENABLED",
                "false",
            ).lower()
            != "true"
        ):
            raise HTTPException(503, "Cyber tools unavailable")
        authority = TaskAuthorityClient(
            os.environ.get("ADP_TASK_AUTHORITY_ENDPOINT", ""),
            event.get("headers") or {},
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )
        attempt = body.attempt.model_dump()

        def authorize():
            return authority.authorize(
                attempt=attempt,
                tool="websearch.search"
                if body.operation == "search"
                else ("code_interpreter." if code_route else "cyber.") + body.operation,
                cleanup=cleanup,
            )

        validated = True
        verified = authorize()
        backend = CyberBackends()
        if body.operation.startswith("common_crawl_"):
            from cyber_tools.common_crawl_scan import CommonCrawlTools

            backend.url_tools = CommonCrawlTools(
                backend._client("athena"), s3=backend._client("s3")
            )
        table = os.environ.get("CYBER_TOOLS_TABLE")
        if not table:
            raise HTTPException(503, "Cyber operation store unavailable")
        repo = OperationRepository(backend._client("dynamodb"), table, authorize)
        if code_route:
            from cyber_tools.code_interpreter import CodeInterpreter

            identifier = os.environ.get("ADP_CODE_INTERPRETER_ID", "")
            if not identifier:
                raise HTTPException(503, "Code Interpreter resource unavailable")
            service = CodeInterpreter(
                repo, authority, identifier, revalidate=lambda: authorize().identity
            )
            result = service.execute(verified.identity, body)
        else:
            service = CyberOperations(
                repo,
                authority,
                backend,
                revalidate=lambda: authorize().identity,
                remaining_ms=getattr(context, "get_remaining_time_in_millis", None),
            )
            result = service.execute(
                verified.identity, body.operation_id, body.operation, body.payload
            )
        if authorize().identity != verified.identity:
            raise HTTPException(403, "Task authority changed")
        return response(200, result)
    except HTTPException as error:
        return response(
            error.status_code, {"code": "tool_refused", "message": error.detail}
        )
    except (ValidationError, ValueError, TypeError):
        if validated:
            return response(
                503,
                {
                    "code": "outcome_unavailable",
                    "message": "Tool outcome unavailable; retain operation identity",
                },
            )
        return response(
            422, {"code": "invalid_request", "message": "Invalid cyber tool request"}
        )
    except Exception:
        # Uncertain calls retain their durable claim; never include backend URLs,
        # request bodies, tokens or exception strings in client responses/logs.
        return response(
            503,
            {
                "code": "outcome_unavailable",
                "message": "Tool outcome unavailable; retain operation identity",
            },
        )
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
