"""Dedicated IAM-only validation API and private asynchronous Lambda worker.

The deployment role must allow self-invocation only; API callers cannot invoke
Lambda directly. Workload proof is forwarded in the private event, never stored
in job rows or returned to callers.
"""

import base64
from decimal import Decimal
import json
import os

import boto3
from fastapi import HTTPException
from pydantic import ValidationError

from adp_tools.authority import TaskAuthorityClient, require_worker
from adp_tools.contracts import TaskAttemptBody
from adp_tools.storage import OperationRepository
from validation_tools.executor import service_executor
from validation_tools.operations import ValidationBody, execute_job, public_job
from validation_tools.store import ValidationJobs


def response(status, body):
    def value(item):
        if isinstance(item, Decimal):
            return int(item) if item == item.to_integral_value() else float(item)
        raise TypeError("Unsupported response")
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=value, allow_nan=False)}


def lambda_handler(event, context):
    authority = None
    try:
        private = event.get("kind") == "validation-job-v1"
        if private:
            if set(event) != {"kind", "attempt", "operation_id", "headers"}:
                raise HTTPException(422, "Invalid private job")
            attempt = TaskAttemptBody.model_validate(event["attempt"]).model_dump()
            operation, operation_id, cleanup = "run", event["operation_id"], False
        else:
            if event.get("httpMethod") != "POST" or event.get("resource") != "/tools/validation":
                raise HTTPException(404, "Not found")
            require_worker(event, set(filter(None, os.environ.get("ADP_VALIDATION_WORKER_ROLES", "").split(","))))
            raw = event.get("body", "")
            if not isinstance(raw, str) or len(raw) > 12000:
                raise HTTPException(413, "Validation request exceeds bound")
            raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
            if len(raw) > 8192:
                raise HTTPException(413, "Validation request exceeds bound")
            body = ValidationBody.model_validate_json(raw)
            if (body.operation == "run") != (body.payload is not None):
                raise HTTPException(422, "Invalid validation payload")
            attempt, operation, operation_id = body.attempt.model_dump(), body.operation, body.operation_id
            cleanup = operation == "cancel_jobs"
        if not cleanup and os.environ.get("ADP_VALIDATION_SERVICE_ENABLED", "false").lower() != "true":
            raise HTTPException(503, "Validation service unavailable")
        authority = TaskAuthorityClient(os.environ.get("ADP_TASK_AUTHORITY_ENDPOINT", ""), event.get("headers") or {},
                                        region=os.environ.get("AWS_REGION", "us-east-1"))
        def authorize():
            return authority.authorize(attempt=attempt, tool="validation.cancel_jobs" if cleanup else "validation.run", cleanup=cleanup)
        verified = authorize()
        jobs = ValidationJobs(OperationRepository(boto3.client("dynamodb"), os.environ["ADP_VALIDATION_TABLE"], authorize))
        if private:
            execute_job(jobs=jobs, authority=authority, attempt=attempt, identity=verified.identity,
                operation_id=operation_id, executor_factory=service_executor,
                remaining_ms=context.get_remaining_time_in_millis)
            return response(200, {"received": True})
        if operation == "inspect":
            with service_executor(verified.identity.task_id) as executor:
                executor._boundary()
            result = {"schema_version": "1.0", "idle": jobs.idle(verified.identity)}
        elif cleanup:
            pending = jobs.close(verified.identity)
            result = {"schema_version": "1.0", "phase": "pending" if pending else "cancelled", "pending": pending}
        else:
            if operation == "run":
                checks = verified.task.get("repository_binding", {}).get("binding", {}).get("validation_checks", [])
                selected = [check for check in checks if check.get("name") == body.payload.check]
                if (len(selected) != 1 or "@sha256:" not in selected[0].get("image", "")
                        or not 1 <= selected[0].get("timeout_seconds", 120) <= 120):
                    raise HTTPException(403, "Validation check is not admitted for this service")
                row, _ = jobs.admit(verified.identity, operation_id, body.payload.model_dump())
            else:
                row = jobs.read(verified.identity, operation_id)
            if row["phase"] == "pending" and jobs.delivery(verified.identity, operation_id):
                delivered = boto3.client("lambda").invoke(FunctionName=context.invoked_function_arn,
                    InvocationType="Event", Payload=json.dumps({"kind": "validation-job-v1", "attempt": attempt,
                        "operation_id": operation_id, "headers": authority.headers}).encode())
                if delivered.get("StatusCode") != 202:
                    raise HTTPException(503, "Validation delivery unconfirmed; retain operation identity")
            result = public_job(row)
        if authorize().identity != verified.identity:
            raise HTTPException(403, "Validation authority changed")
        return response(200, result)
    except HTTPException as error:
        return response(error.status_code, {"code": "validation_refused", "message": error.detail})
    except (ValidationError, ValueError, TypeError):
        return response(422, {"code": "invalid_request", "message": "Invalid validation request"})
    except Exception:
        return response(503, {"code": "outcome_unavailable", "message": "Validation outcome unavailable; retain operation identity"})
    finally:
        if authority is not None:
            authority.close()
