"""Allowlisted failure metadata for SQS workers; never format the exception."""

import json
import re
from pathlib import Path

import httpx
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from starlette.exceptions import HTTPException


_SERVICE_IDS = frozenset({"security", "devops", "well_architected", "transform", "finops"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HTTP_CODES = frozenset({
    "not_found", "setup_intent_changed", "setup_authority_denied",
    "setup_continuation_invalid", "setup_continuation_expired",
    "setup_persistence_failed", "provider_unqualified", "stale_delegation_or_attempt",
})


def authority_failure(error, stage):
    """A retained action and allowlisted cause; never provider exception text."""
    result = {"code": "work_authority_unavailable",
              "action": "Check service consent, connection verification and selected source access. Reconcile the run after correcting the cause."}
    if stage in {"execute", "submit", "poll", "collect", "reconcile", "cancel"}:
        result["stage"] = stage
    for _ in range(4):
        if isinstance(error, httpx.HTTPStatusError):
            result["http_status"] = error.response.status_code
            try:
                payload = error.response.json() if len(error.response.content) <= 16384 else {}
                detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                code = detail.get("code") if isinstance(detail, dict) else None
                if code in _HTTP_CODES | {"delegation_denied", "snapshot_expired", "source_integrity_mismatch"}:
                    result["cause"] = code
            except (ValueError, TypeError):
                pass
            break
        error = error.__cause__
        if error is None:
            break
    return result
# Absolute paths are compared but never emitted. Both file and function must be
# known source locations; dependency frames, dynamic code and locals are omitted.
_SOURCE_FUNCTIONS = {
    "workers/execution.py": frozenset({"process_batch", "execute_once", "execute", "transition", "_authorize"}),
    "workers/bindings.py": frozenset({"execute_setup_work", "block_claim"}),
    "api/bindings.py": frozenset({"operation", "_scope", "finish_create", "finish_link", "finish_control"}),
    "runtime_support.py": frozenset({
        "setup_provider_session", "verify", "run_provider_session", "verify_broker",
        "verify_current", "verify_source_receipts", "require_service",
    }),
    "setup_authority.py": frozenset({"setup_authorization", "continuation_rows"}),
}
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_PATHS = {
    str(_PACKAGE_ROOT / relative): (f"l5_services/{relative}", functions)
    for relative, functions in _SOURCE_FUNCTIONS.items()
}


def _error_metadata(error):
    # Gateway imports the worker entrypoint without loading provider SDKs.
    # AWS exception classification is needed only when a worker logs a failure.
    from botocore.exceptions import ClientError

    if isinstance(error, ClientError):
        result = {"error_code": "provider_error"}
        code = error.response.get("Error", {}).get("Code")
        if code in {"AccessDenied", "AccessDeniedException", "ValidationException", "ResourceNotFoundException",
                    "NoSuchBucket", "NoSuchKey", "NoSuchVersion", "ThrottlingException", "InternalServerException"}:
            result["cause_code"] = code
        if error.operation_name in {"GetObject", "HeadObject", "PutObject", "UpdateAgentSpace", "CreateCodeReview",
                                    "StartCodeReviewJob", "AddArtifact", "UpdateThreatModel", "StartThreatModelJob"}:
            result["provider_operation"] = error.operation_name
        return result
    if isinstance(error, SQLAlchemyError):
        if isinstance(error, DBAPIError):
            # Do not inspect SQL, parameters, messages, diag or exception chains.
            sqlstate = getattr(error.orig, "sqlstate", None)
            if sqlstate is None:
                sqlstate = getattr(error.orig, "pgcode", None)
            if type(sqlstate) is str and sqlstate == "42501":
                return {
                    "error_code": "database_permission_denied",
                    "operator_action": (
                        "Run modules/L5-services/scripts/bootstrap-worker-db.sql as the database owner after L5 migrations. "
                        "Follow docs/l5-services/deployment-authority.md and verify worker database privileges."
                    ),
                }
        return {"error_code": "database_error"}
    if isinstance(error, json.JSONDecodeError):
        return {"error_code": "malformed_json"}
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return {"error_code": "request_timeout"}
    if isinstance(error, httpx.TransportError):
        return {"error_code": "transport_error"}
    if isinstance(error, (HTTPException, httpx.HTTPStatusError)):
        result = {"error_code": "http_error"}
        status = error.status_code if isinstance(error, HTTPException) else error.response.status_code
        if type(status) is int and 100 <= status <= 599:
            result["http_status"] = status
        if isinstance(error, HTTPException) and type(error.detail) is dict:
            code = error.detail.get("code")
            if type(code) is str and code in _HTTP_CODES:
                result["cause_code"] = code
        return result
    # Exception class names can also contain secrets. Unknown classes stay opaque.
    return {"error_code": "unclassified"}


def _source_frames(error):
    frames = []
    traceback = error.__traceback__
    for _ in range(64):
        if traceback is None:
            break
        code = traceback.tb_frame.f_code
        source = _SOURCE_PATHS.get(code.co_filename)
        if source is not None and code.co_name in source[1] and 0 < traceback.tb_lineno <= 1_000_000:
            frames.append({"file": source[0], "function": code.co_name, "line": traceback.tb_lineno})
            frames = frames[-5:]
        traceback = traceback.tb_next
    return frames


def log_record_failure(logger, error, *, record, envelope, service, request_id=None):
    """Diagnostics must not change acknowledgement, retries or durable work state."""
    try:
        metadata = {
            "event": "l5_worker_record_failed",
            "disposition": "partial_batch_failure",
            **_error_metadata(error),
            "frames": _source_frames(error),
        }
        service_id = getattr(service, "service_id", None)
        if type(service_id) is str and service_id in _SERVICE_IDS:
            metadata["service_id"] = service_id
        correlations = {
            "request_id": request_id,
            "message_id": record.get("messageId") if type(record) is dict else None,
            "work_id": envelope.get("work_id") if type(envelope) is dict else None,
        }
        for key, value in correlations.items():
            if type(value) is str and len(value) == 36 and _UUID.fullmatch(value):
                metadata[key] = value
        # Include metadata in the message: Lambda's plain formatter drops extra.
        # Never pass exc_info, stack_info, raw errors or source lines to logging.
        logger.error("L5 work item failed: %s", json.dumps(metadata, sort_keys=True))
    except Exception:
        # Even a broken logging handler must preserve the original batch result.
        pass
