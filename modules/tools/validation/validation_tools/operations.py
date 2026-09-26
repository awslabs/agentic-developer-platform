"""Bound artifact admission and asynchronous, owner-fenced validation execution."""

import base64
import hashlib
import json
import threading
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from adp_tools.contracts import TaskAttemptBody, UUID4
from lib.codex_validation_service_runner import run_service_validation


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    artifact_id: str = Field(pattern=r"^art_" + UUID4[1:])
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_length: int = Field(strict=True, ge=1, le=262144)


class ValidationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody
    operation: Literal["run", "status", "inspect", "cancel_jobs"]
    operation_id: str = Field(pattern=UUID4)
    payload: ValidationRequest | None = None


def read_manifest(authority, attempt, request):
    receipt = authority.post("artifact", {"schema_version": "1.0", "operation": "read",
        "run": attempt["run"], "artifact_id": request.artifact_id})
    if any(receipt.get(key) != value for key, value in {
        "schema_version": "1.0", "operation": "read", "run": attempt["run"],
        "artifact_id": request.artifact_id, "content_type": "application/json",
        "content_sha256": request.content_sha256, "byte_length": request.byte_length,
    }.items()):
        raise HTTPException(403, "Validation artifact binding differs")
    try:
        content = base64.b64decode(receipt["content_base64"], validate=True)
        if len(content) != request.byte_length or hashlib.sha256(content).hexdigest() != request.content_sha256:
            raise ValueError("Artifact integrity differs")
        manifest = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(manifest, dict):
            raise ValueError("Manifest is not an object")
        return manifest
    except (KeyError, ValueError, TypeError):
        raise HTTPException(403, "Validation artifact integrity differs") from None


def public_job(row):
    return {"schema_version": "1.0", "operation_id": row["operation_id"],
            "phase": row["phase"], "result": row.get("result")}


def execute_job(*, jobs, authority, attempt, identity, operation_id, executor_factory,
                remaining_ms, runner=run_service_validation):
    """Only a fresh durable claim permits work. Unknown effects never replay."""
    claimed = jobs.claim(identity, operation_id)
    if claimed is None:
        return
    cancelled, finished = threading.Event(), threading.Event()

    def monitor():
        while not finished.wait(1):
            try:
                if remaining_ms() < 20000 or jobs.stopping(identity):
                    cancelled.set()
                    return
                if authority.authorize(attempt=attempt, tool="validation.run").identity != identity:
                    cancelled.set()
                    return
            except Exception:
                cancelled.set()
                return

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    phase, result = "unknown", None
    try:
        with executor_factory(identity.task_id) as executor:
            try:
                if remaining_ms() < 170000 or jobs.stopping(identity):
                    cancelled.set()
                    raise HTTPException(409, "Validation execution stopped")
                stored = claimed["request"]
                length = stored["byte_length"]
                if int(length) != length:
                    raise ValueError("Stored artifact length is not integral")
                request = ValidationRequest.model_validate({**stored, "byte_length": int(length)})
                manifest = read_manifest(authority, attempt, request)
                result = runner(authority=authority, attempt=attempt, manifest=manifest,
                    check_name=request.check, executor=executor, cancelled=cancelled)
                if cancelled.is_set() or jobs.stopping(identity):
                    raise HTTPException(409, "Validation execution stopped")
                phase = "completed"
            except Exception:
                cancelled.set()
                # This invocation owns the only claim. Recovery must observe
                # termination; an empty Pod list alone does not clear intent.
                executor.recover()
                phase, result = "cancelled", None
    except Exception:
        phase, result = "unknown", None
    finally:
        finished.set()
        watcher.join(timeout=4)
    try:
        jobs.settle(identity, operation_id, claimed["owner_token"], phase=phase, result=result)
    except Exception:
        # Completion may lose to a stop fence after execution returned. Its
        # runner has returned, so this owner can confirm cleanup before settling.
        if phase == "completed":
            try:
                with executor_factory(identity.task_id) as executor:
                    executor.recover()
                jobs.settle(identity, operation_id, claimed["owner_token"], phase="cancelled")
            except Exception:
                jobs.settle(identity, operation_id, claimed["owner_token"], phase="unknown")
        else:
            raise
