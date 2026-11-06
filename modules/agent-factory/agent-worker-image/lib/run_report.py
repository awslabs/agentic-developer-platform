"""Acknowledged reports using one dispatcher-issued, reporting-only capability."""

from __future__ import annotations

import atexit
import hashlib
import json
import tempfile
from pathlib import Path
import os
import secrets
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import botocore.auth
import botocore.awsrequest
import botocore.session
import requests

from lib.reviewer_errors import review_refusal

_assignment: dict | None = None


class RunReportError(Exception):
    def __init__(self, code: str, *, retryable: bool = True):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class SupersededDelivery(RunReportError):
    """An authenticated report explicitly retired this delivery permanently."""

    def __init__(self):
        super().__init__("execution_assignment_superseded", retryable=False)


def configure(envelope: dict) -> None:
    global _assignment
    report = envelope.get("run_report")
    if not report:
        _assignment = None
        return
    if report.get("contract_version") != 1 or not isinstance(report.get("credential"), str):
        raise RunReportError("unsupported_run_report", retryable=False)
    # A protected worker identity is denied direct S3 by its permissions boundary;
    # the dispatcher then tells it to keep the pending-report marker in the gateway.
    spool = report.get("spool", "s3")
    if spool not in {"s3", "gateway"}:
        raise RunReportError("unsupported_run_report", retryable=False)
    # File handoff follows the existing protected-run convention. Child clients
    # read this file rather than embedding the capability in command arguments.
    fd, path = tempfile.mkstemp(prefix="adp-run-report-")
    with os.fdopen(fd, "w") as target:
        target.write(report["credential"])
    os.environ["ADP_RUN_REPORT_CREDENTIAL_FILE"] = path
    os.environ["ADP_RUN_ATTEMPT"] = "1"
    os.environ["ADP_ORCHESTRATION_ATTEMPT"] = str(envelope["orchestration"]["attempt"])
    atexit.register(lambda: Path(path).unlink(missing_ok=True))
    _assignment = {
        "credential": report["credential"],
        "ownership_nonce": secrets.token_hex(16),
        "run_id": envelope["message_id"],
        "attempt": envelope["orchestration"]["attempt"],
        "bound_pull_request": envelope.get("bound_pull_request"),
        "tenant_id": envelope["tenant_id"],
        "repo": envelope["source_ref"]["repo"],
        "spool": spool,
        "reviewer_owned_delivery": (envelope.get("review_cycle_input") or {}).get("reviewer_owned_delivery") is True,
    }
    os.environ["ADP_RUN_REPORT_OWNERSHIP_NONCE"] = _assignment["ownership_nonce"]


def assigned_pull_request(repo: str) -> str:
    candidate = (_assignment or {}).get("bound_pull_request") or {}
    if (
        candidate.get("repo", "").lower() == repo.lower()
        and type(candidate.get("pr_number")) is int
    ):
        return str(candidate["pr_number"])
    return ""


def enabled() -> bool:
    return _assignment is not None


def request(path: str = "", body: dict | None = None, *, timeout: int = 15, max_bytes: int = 32768) -> dict:
    if _assignment is None:
        raise RunReportError("run_report_unconfigured", retryable=False)
    base = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RunReportError("run_report_endpoint_unconfigured", retryable=False)
    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    credentials = worker_credentials(botocore.session.get_session())
    if credentials is None:
        raise RunReportError("worker_transport_unavailable")
    method = "GET" if body is None else "POST"
    data = b"" if body is None else json.dumps(body).encode()
    url = base + "/report" + path
    signed = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Adp-Report-Credential": _assignment["credential"],
        },
    )
    botocore.auth.SigV4Auth(
        credentials.get_frozen_credentials(), "execute-api", gateway_signing_region(url)
    ).add_auth(signed)
    try:
        with requests.Session() as http:
            http.trust_env = False
            with http.request(
                method,
                url,
                data=data,
                headers=dict(signed.headers),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code not in (200, 201):
                    code, retryable = review_refusal(response, path, f"run_report_http_{response.status_code}")
                    raise RunReportError(code, retryable=retryable)
                raw = response.raw.read(max_bytes + 1, decode_content=True)
                if len(raw) > max_bytes:
                    raise RunReportError("invalid_run_report_receipt")
                result = json.loads(raw)
    except RunReportError:
        raise
    except (requests.RequestException, ValueError, OSError):
        raise RunReportError("run_report_unavailable") from None
    if (
        not isinstance(result, dict)
        or result.get("contract_version") != 1
        or result.get("run_id") != _assignment["run_id"]
        or result.get("attempt") != _assignment["attempt"]
    ):
        raise RunReportError("invalid_run_report_receipt", retryable=False)
    return result


def operations_report(report: str) -> dict:
    result = request("/operations", {"report": report})
    receipt = result.get("operations_report_receipt") or {}
    if (receipt.get("recorded") is not True or receipt.get("run_id") != result["run_id"]
            or receipt.get("attempt") != result["attempt"]
            or receipt.get("sha256") != hashlib.sha256(report.encode("utf-8")).hexdigest()):
        raise RunReportError("operations_report_unacknowledged")
    return result


def terminal(outcome: str, *, failure: dict | None = None) -> dict:
    result = request("/terminal", {"outcome": outcome, **({"failure": failure} if failure is not None else {})})
    receipt = result.get("terminal_receipt") or {}
    if (
        receipt.get("outcome") != outcome
        or receipt.get("run_id") != result["run_id"]
        or receipt.get("attempt") != result["attempt"]
    ):
        raise RunReportError("terminal_report_unacknowledged")
    return result


def _spool_location() -> tuple[str, str]:
    bucket = os.environ.get("AGENT_RUN_LOGS_BUCKET", "")
    if not _assignment or not bucket:
        raise RunReportError("report_spool_unconfigured", retryable=False)
    org = hashlib.sha256(_assignment["tenant_id"].encode()).hexdigest()
    run = hashlib.sha256(_assignment["run_id"].encode()).hexdigest()
    return bucket, f"run-reports/{org}/{run}/attempt-{_assignment['attempt']}.json"


def _spool_client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


SPOOL_LIMIT = 2 * 1024 * 1024


def _gateway_spool() -> bool:
    if _assignment is None:
        raise RunReportError("report_spool_unconfigured", retryable=False)
    return _assignment.get("spool") == "gateway"


def _read_gateway_spool() -> dict | None:
    """The gateway holds the marker verbatim; it is still validated here as untrusted."""
    try:
        return request("/spool", max_bytes=SPOOL_LIMIT).get("spool_document")
    except RunReportError:
        raise RunReportError("report_spool_unavailable") from None


def _write_gateway_spool(body: dict, *, create: bool) -> None:
    try:
        request("/spool", {"document": body, "create": create}, max_bytes=SPOOL_LIMIT)
    except RunReportError as exc:
        if exc.code == "run_report_http_409":
            # Same meaning as the S3 IfNoneMatch precondition: another creator exists.
            raise RunReportError("delivery_recovery_required", retryable=False) from None
        if read_spool() != body:
            raise RunReportError("report_spool_unavailable") from None


def read_spool() -> dict | None:
    """Read untrusted recovery material, checking its exact dispatch attribution."""
    if _gateway_spool():
        body = _read_gateway_spool()
        if body is None:
            return None
    else:
        bucket, key = _spool_location()
        try:
            response = _spool_client().get_object(Bucket=bucket, Key=key)
            with response["Body"] as stream:
                raw = stream.read(SPOOL_LIMIT + 1)
            if len(raw) > SPOOL_LIMIT:
                raise RunReportError("invalid_report_spool", retryable=False)
            body = json.loads(raw)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise RunReportError("report_spool_unavailable") from None
        except (BotoCoreError, OSError, ValueError, KeyError, TypeError):
            raise RunReportError("report_spool_unavailable") from None
    if (
        not isinstance(body, dict)
        or body.get("contract_version") != 1
        or any(body.get(field) != _assignment[field] for field in ("run_id", "attempt", "repo"))
        or body.get("phase") not in {"executing", "candidate", "failed", "review"}
    ):
        raise RunReportError("report_spool_scope_mismatch", retryable=False)
    if body["phase"] == "review" and (
        not isinstance(body.get("review_content"), str)
        or not 0 < len(body["review_content"].encode()) <= 256 * 1024
        or not isinstance(body.get("ownership_nonce"), str)
        or len(body["ownership_nonce"]) != 32
    ):
        raise RunReportError("report_spool_scope_mismatch", retryable=False)
    if body["phase"] == "failed" and (
        set(body) - {"failure"} != {"contract_version", "run_id", "attempt", "repo", "phase", "candidate_pr", "ownership_nonce"}
        or body.get("candidate_pr") is not None
        or not isinstance(body.get("ownership_nonce"), str)
        or len(body["ownership_nonce"]) != 32
        or any(char not in "0123456789abcdef" for char in body["ownership_nonce"])
    ):
        raise RunReportError("report_spool_scope_mismatch", retryable=False)
    if "failure" in body:
        failure = body["failure"]
        if (not isinstance(failure, dict) or set(failure) != {"category", "exit_code"}
                or failure.get("category") not in {"unknown", "cancelled", "deadline", "signal", "policy", "provider_refusal", "authentication", "transport", "stale_head", "git_validation", "inspection", "contract"}
                or type(failure.get("exit_code")) is not int or not -255 <= failure["exit_code"] <= 255):
            raise RunReportError("report_spool_scope_mismatch", retryable=False)
    return body


def _spool_document(phase: str, candidate: dict | None = None) -> dict:
    return {
        "contract_version": 1,
        "run_id": _assignment["run_id"],
        "attempt": _assignment["attempt"],
        "repo": _assignment["repo"],
        "phase": phase,
        "candidate_pr": candidate,
    }


def can_retry_start(spool: dict, snapshot: dict) -> bool:
    """An exact pre-start marker is reusable only before any acknowledged work.

    The spool records intent before `/started`, so it also survives an explicit
    refusal or transport failure. It is not execution authority. The gateway's
    locked ownership receipt still chooses the sole worker allowed to execute.
    Missing snapshot fields are unknown evidence, not an absent receipt.
    """
    return spool == _spool_document("executing") and all(
        key in snapshot and snapshot[key] is None
        for key in (
            "worker_receipt", "candidate_pr", "binding_receipt", "terminal_receipt", "review_receipt"
        )
    )


def _write_spool(phase: str, candidate: dict | None = None, *, create: bool = False, review_content: str | None = None, failure: dict | None = None) -> None:
    body = _spool_document(phase, candidate)
    if phase in {"failed", "review"}:
        body["ownership_nonce"] = _assignment["ownership_nonce"]
    if phase == "review":
        body["review_content"] = review_content
    if failure is not None:
        body["failure"] = failure
    if _gateway_spool():
        _write_gateway_spool(body, create=create)
        if read_spool() != body:
            raise RunReportError("report_spool_unacknowledged")
        return
    bucket, key = _spool_location()
    options = {"IfNoneMatch": "*"} if create else {}
    try:
        _spool_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
            **options,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
            raise RunReportError("delivery_recovery_required", retryable=False) from None
        if read_spool() != body:
            raise RunReportError("report_spool_unavailable") from None
    except BotoCoreError:
        # A lost acknowledgement may mean the write committed. Read it back;
        # incomplete uploads never masquerade as a durable candidate.
        if read_spool() != body:
            raise RunReportError("report_spool_unavailable") from None
    if read_spool() != body:
        raise RunReportError("report_spool_unacknowledged")


def begin_delivery() -> None:
    if not enabled():
        return
    spool = read_spool()
    if spool is None:
        _write_spool("executing", create=True)
    else:
        # Recheck after bootstrap: another worker may have won ownership since
        # resume_handoff read the same marker. Never rewrite or delete the spool.
        if not can_retry_start(spool, request()) or read_spool() != spool:
            raise RunReportError("delivery_recovery_required", retryable=False)
    # Concurrent retries (including a still-in-flight first request) use distinct
    # ownership nonces. /started locks the row and acknowledges only its winner;
    # losers return before the entrypoint can exec the agent subprocess.
    snapshot = request("/started", {"ownership_nonce": _assignment["ownership_nonce"]})
    if (snapshot.get("worker_receipt") or {}).get("ownership_nonce") != _assignment[
        "ownership_nonce"
    ]:
        raise RunReportError("delivery_start_unacknowledged")


def spool_undelivered_failure(*, failure: dict | None = None) -> None:
    """Keep a failed owner's report retryable without starting another worker.

    Delivered PR candidates retain their existing handoff recovery. Only the
    exact executing marker can become a failure marker, and replay must match
    the gateway's acknowledged ownership nonce before reporting failure.
    """
    spool = read_spool()
    if spool is None:
        raise RunReportError("delivery_recovery_required", retryable=False)
    if spool["phase"] == "candidate":
        return
    if spool["phase"] == "review":
        if spool.get("ownership_nonce") != _assignment["ownership_nonce"]:
            raise RunReportError("delivery_recovery_required", retryable=False)
        return  # Preserve produced evidence alongside the actual failed exit.
    if spool["phase"] == "failed":
        if spool["ownership_nonce"] != _assignment["ownership_nonce"]:
            raise RunReportError("delivery_recovery_required", retryable=False)
        return
    if spool != _spool_document("executing"):
        raise RunReportError("delivery_recovery_required", retryable=False)
    _write_spool("failed", failure=failure)


def spool_candidate(candidate: dict) -> None:
    _write_spool("candidate", candidate)


def spool_review(data: bytes) -> None:
    """Retain exact completed review bytes before upload or merge, in our spool."""
    if not enabled() or not _assignment.get("reviewer_owned_delivery"):
        return
    if not 0 < len(data) <= 256 * 1024:
        raise RunReportError("invalid_review_spool", retryable=False)
    spool = read_spool()
    if spool is None or spool["phase"] not in {"executing", "review"}:
        raise RunReportError("delivery_recovery_required", retryable=False)
    if spool["phase"] == "review" and spool.get("ownership_nonce") != _assignment["ownership_nonce"]:
        raise RunReportError("delivery_recovery_required", retryable=False)
    _write_spool("review", review_content=data.decode("utf-8"))


def report_block(code: str) -> None:
    if code not in {
        "delivery_recovery_required",
        "report_spool_unavailable",
        "report_spool_unconfigured",
        "report_spool_scope_mismatch",
        "pr_candidate_missing",
    }:
        return
    try:
        request("/block", {"code": code})
    except RunReportError:
        pass  # Preserve the original failure; the durable spool still survives.
