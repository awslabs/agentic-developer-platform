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

_assignment: dict | None = None


class RunReportError(Exception):
    def __init__(self, code: str, *, retryable: bool = True):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def configure(envelope: dict) -> None:
    global _assignment
    report = envelope.get("run_report")
    if not report:
        _assignment = None
        return
    if report.get("contract_version") != 1 or not isinstance(report.get("credential"), str):
        raise RunReportError("unsupported_run_report", retryable=False)
    # File handoff follows the existing protected-run convention. Child clients
    # read this file rather than embedding the capability in command arguments.
    fd, path = tempfile.mkstemp(prefix="adp-run-report-")
    with os.fdopen(fd, "w") as target:
        target.write(report["credential"])
    os.environ["ADP_RUN_REPORT_CREDENTIAL_FILE"] = path
    os.environ["ADP_RUN_ATTEMPT"] = "1"
    atexit.register(lambda: Path(path).unlink(missing_ok=True))
    _assignment = {
        "credential": report["credential"],
        "ownership_nonce": secrets.token_hex(16),
        "run_id": envelope["message_id"],
        "attempt": envelope["orchestration"]["attempt"],
        "bound_pull_request": envelope.get("bound_pull_request"),
        "tenant_id": envelope["tenant_id"],
        "repo": envelope["source_ref"]["repo"],
    }


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


def request(path: str = "", body: dict | None = None) -> dict:
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
                timeout=15,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code not in (200, 201):
                    raise RunReportError(
                        f"run_report_http_{response.status_code}",
                        retryable=response.status_code >= 500 or response.status_code == 429,
                    )
                raw = response.raw.read(32769, decode_content=True)
                if len(raw) > 32768:
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


def terminal(outcome: str) -> dict:
    result = request("/terminal", {"outcome": outcome})
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


def read_spool() -> dict | None:
    """Read untrusted recovery material, checking its exact dispatch attribution."""
    bucket, key = _spool_location()
    try:
        response = _spool_client().get_object(Bucket=bucket, Key=key)
        with response["Body"] as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
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
        or body.get("phase") not in {"executing", "candidate", "failed"}
    ):
        raise RunReportError("report_spool_scope_mismatch", retryable=False)
    if body["phase"] == "failed" and (
        set(body) != {"contract_version", "run_id", "attempt", "repo", "phase", "candidate_pr", "ownership_nonce"}
        or body.get("candidate_pr") is not None
        or not isinstance(body.get("ownership_nonce"), str)
        or len(body["ownership_nonce"]) != 32
        or any(char not in "0123456789abcdef" for char in body["ownership_nonce"])
    ):
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


def _write_spool(phase: str, candidate: dict | None = None, *, create: bool = False) -> None:
    bucket, key = _spool_location()
    body = _spool_document(phase, candidate)
    if phase == "failed":
        body["ownership_nonce"] = _assignment["ownership_nonce"]
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


def spool_undelivered_failure() -> None:
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
    if spool["phase"] == "failed":
        if spool["ownership_nonce"] != _assignment["ownership_nonce"]:
            raise RunReportError("delivery_recovery_required", retryable=False)
        return
    if spool != _spool_document("executing"):
        raise RunReportError("delivery_recovery_required", retryable=False)
    _write_spool("failed")


def spool_candidate(candidate: dict) -> None:
    _write_spool("candidate", candidate)


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
