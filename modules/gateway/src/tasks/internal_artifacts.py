"""Run-authorized input reads and immutable result uploads on the fixed artifact route."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

from fastapi import APIRouter, Depends, Request

from src.agentauth.routes import require_agent_transport
from src.tasks import errors, http
from src.tasks.read_store import ArtifactCapacityError, SequenceFencedError, TaskStoreError
from src.tasks.routes import get_store

router = APIRouter(prefix="/internal/v1/agent/task", tags=["task-api"], dependencies=[Depends(require_agent_transport)])


@router.post("/artifact")
@http.contract_errors
async def artifact(request: Request):
    http.require_flag(http.FLAG_WORKER)
    from src.agentauth.task_runtime_routes import authenticate_task_attempt

    attempt = await authenticate_task_attempt(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1400000:
            raise errors.payload_too_large("Artifact request exceeds its fixed byte limit.")
    try:
        body = http.parse_json_object(bytes(raw))
    except (ValueError, UnicodeDecodeError):
        raise errors.invalid_request("Artifact request must be JSON.") from None
    if not isinstance(body, dict):
        raise errors.invalid_request("Artifact request must be an object.")
    read = body.get("operation") == "read"
    required = (
        {"schema_version", "operation", "run", "artifact_id"}
        if read
        else {"schema_version", "run", "content_type", "content_sha256", "content_base64"}
    )
    if set(body) != required or body.get("schema_version") != "1.0":
        raise errors.invalid_request("Artifact request does not match the contract.")
    binding = {"task_id": attempt.task_id, "invocation_id": attempt.invocation_id, "generation": attempt.generation}
    run = body["run"]
    if not isinstance(run, dict) or type(run.get("generation")) is not int or run != binding:
        raise errors.state_conflict("Artifact request does not match the verified run.")
    store = get_store()
    try:
        if read:
            if not isinstance(body["artifact_id"], str) or not re.fullmatch(
                r"art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", body["artifact_id"]
            ):
                raise errors.invalid_request("Artifact identifier must be a string.")
            task = store.repository.read_task(attempt.task_id)
            record = store.load_artifact(artifact_id=body["artifact_id"])
            if not task or body["artifact_id"] not in task.get("artifact_ids", []):
                raise errors.not_found()
            if (
                record is None
                or record.task_id != attempt.task_id
                or record.tenant_id != attempt.tenant
                or record.owner_principal_id != attempt.canonical_principal
            ):
                raise errors.not_found()
            if record.content_length > 262144:
                raise errors.payload_too_large("Stored input exceeds its fixed per-artifact limit.")
            content = store.read_artifact(record=record)
            # Recheck live workload, credential and policy after the object read.
            await authenticate_task_attempt(request)
            return http.ok(
                {
                    "schema_version": "1.0",
                    "operation": "read",
                    "run": binding,
                    "artifact_id": record.artifact_id,
                    "content_type": record.content_type,
                    "content_sha256": record.content_sha256,
                    "byte_length": len(content),
                    "content_base64": base64.b64encode(content).decode(),
                },
                status=200,
            )
        try:
            if not isinstance(body["content_base64"], str):
                raise ValueError("base64 must be string")
            content = base64.b64decode(body["content_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise errors.invalid_request("Artifact content is not valid base64.") from None
        if not 0 < len(content) <= 1048576:
            raise errors.payload_too_large("Result artifact exceeds its fixed byte limit.")
        if (
            not isinstance(body["content_type"], str)
            or body["content_type"] not in {"text/plain", "application/json", "text/html"}
            or body["content_sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise errors.invalid_request("Artifact type or digest does not match its content.")
        try:
            text = content.decode("utf-8")
            if body["content_type"] == "application/json":
                json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        except (ValueError, UnicodeDecodeError):
            raise errors.invalid_request("Result artifact must contain valid UTF-8 text or JSON.") from None
        record = store.put_run_artifact(attempt=attempt, content=content, content_type=body["content_type"], digest=body["content_sha256"])
        return http.ok(
            {
                "schema_version": "1.0",
                "artifact_id": record.artifact_id,
                "version": record.version,
                "content_sha256": record.content_sha256,
                "content_type": record.content_type,
                "created_at": record.created_at,
                "expires_at": None,
                "request_id": http.request_id(request),
            },
            status=201,
        )
    except SequenceFencedError:
        raise errors.state_conflict("This run no longer accepts artifact writes.") from None
    except ArtifactCapacityError:
        raise errors.payload_too_large("Task evidence and result artifacts exceed the aggregate storage limit.") from None
    except TaskStoreError:
        raise errors.prerequisite_unavailable("Artifact storage could not confirm this operation.") from None
