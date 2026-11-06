"""Bounded stateless smoke with original-run cleanup and accounting evidence.

The private fixture supplies an explicitly approved query and effect contract.
Only the gateway's immutable approval and protected quote authorize dispatch.
Behavioral success with retained funding is distinct from settled governance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .native_test_session import (
    AdmissionUnavailable,
    canonical,
    identifier,
    private_file,
    unique_object,
)

TOOL = "websearch___search"
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_EVIDENCE_BYTES = 16384
CAP_USD = Decimal("20")


def refused():
    raise AdmissionUnavailable("Native smoke evidence is invalid or incomplete")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def opaque(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
        refused()
    return value


def checksum(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        refused()
    return value


def money(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,8}(?:\.[0-9]{1,18})?", value
    ):
        refused()
    try:
        result = Decimal(value)
    except InvalidOperation:
        refused()
    if not result.is_finite() or not Decimal("0") <= result <= CAP_USD:
        refused()
    return result


def epoch(value):
    if type(value) is not int or not 0 < value < 2**63:
        refused()
    return value


@dataclass(frozen=True)
class SearchFixture:
    run_id: str
    approval_revision: str
    contract_sha256: str
    query: str

    @classmethod
    def read(cls, path, reference):
        value = json.loads(private_file(path, 16384), object_pairs_hook=unique_object)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "run_id",
                "approval_revision",
                "contract_sha256",
                "arguments",
            }
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value["run_id"] != reference.run_id
            or value["approval_revision"] != reference.approval_revision
            or not isinstance(value["arguments"], dict)
            or set(value["arguments"]) != {"query"}
        ):
            refused()
        query = value["arguments"]["query"]
        if (
            not isinstance(query, str)
            or not 1 <= len(query) <= 200
            or not query.strip()
        ):
            refused()
        return cls(
            identifier(value["run_id"]),
            identifier(value["approval_revision"]),
            checksum(value["contract_sha256"]),
            query,
        )

    def payload(self, request_id):
        return {
            "tool": TOOL,
            "arguments": {"query": self.query},
            "request_id": identifier(request_id),
            "execution_id": None,
            "ttl_seconds": None,
        }

    def request_sha256(self, request_id):
        return digest(
            {"schema_version": "1.0", "action": "call", **self.payload(request_id)}
        )


def discovery(session, gateway_url, operation_id):
    result = session.request(
        "GET",
        "/api/agent/tools?live=true",
        operation_id=operation_id,
        gateway_url=gateway_url,
    )
    evidence = result.get("discovery")
    tools = result.get("tools")
    if (
        not isinstance(evidence, dict)
        or evidence.get("gateway_url_sha256")
        != hashlib.sha256(gateway_url.encode()).hexdigest()
        or not isinstance(tools, list)
        or sum(isinstance(tool, dict) and tool.get("name") == TOOL for tool in tools)
        != 1
    ):
        refused()
    return checksum(evidence.get("tools_list_sha256"))


def search_receipt(result, request_id, gateway_url):
    if (
        not isinstance(result, dict)
        or result.get("status") != "confirmed"
        or result.get("result_status") != "success"
        or result.get("request_id") != request_id
        or result.get("gateway_url_sha256")
        != hashlib.sha256(gateway_url.encode()).hexdigest()
    ):
        refused()
    return identifier(result.get("execution_id")), opaque(result.get("receipt_id"))


def owned_execution(status, fixture, operation_id, request_id, *, execution_id=None):
    """Authenticate ownership separately so unknown funding never blocks cleanup."""
    if (
        not isinstance(status, dict)
        or status.get("schema_version") != "1.0"
        or status.get("run_id") != fixture.run_id
        or not isinstance(status.get("executions"), list)
        or len(status["executions"]) != 1
    ):
        refused()
    row = status["executions"][0]
    if (
        not isinstance(row, dict)
        or row.get("original_operation_id") != operation_id
        or row.get("contract_sha256") != fixture.contract_sha256
        or row.get("request_sha256") != fixture.request_sha256(request_id)
        or (execution_id is not None and row.get("execution_id") != execution_id)
    ):
        refused()
    identifier(row.get("execution_id"))
    opaque(row.get("reservation_id"))
    return row


def status_evidence(
    status, fixture, operation_id, request_id, execution_id, receipt_id
):
    row = owned_execution(
        status, fixture, operation_id, request_id, execution_id=execution_id
    )
    started = epoch(status.get("run_started_at"))
    deadline, cleanup_deadline = (
        epoch(row.get(name)) for name in ("session_deadline_at", "cleanup_deadline_at")
    )
    confirmed = epoch(row.get("cleanup_confirmed_at"))
    attempts, limit = (
        row.get("cleanup_attempts_used"),
        row.get("cleanup_attempts_limit"),
    )
    if (
        row["reservation_id"] != receipt_id
        or not started <= deadline <= started + 300
        or not deadline <= cleanup_deadline <= started + 600
        or not started <= confirmed <= cleanup_deadline
        or row.get("cleanup_status") != "confirmed"
        or type(attempts) is not int
        or type(limit) is not int
        or not 1 <= attempts <= limit
        or money(status.get("cap_usd")) != CAP_USD
        or status.get("funding_status") not in {"held", "settled"}
        or status.get("accounting_status") not in {"pending", "settled"}
        or type(status.get("governance_complete")) is not bool
    ):
        refused()
    cleanup_digest = checksum(row.get("cleanup_evidence_sha256"))
    quoted, held, settled = (
        money(value)
        for value in (
            row.get("quoted_usd"),
            status.get("held_usd"),
            status.get("settled_usd"),
        )
    )
    if quoted <= 0 or held + settled > CAP_USD:
        refused()
    if status["funding_status"] == "held":
        if (
            held < quoted
            or status["accounting_status"] != "pending"
            or status["governance_complete"]
        ):
            refused()
    elif status["accounting_status"] != "settled" or held != 0 or settled <= 0:
        refused()
    if status["governance_complete"] and (
        status["funding_status"] != "settled"
        or status["accounting_status"] != "settled"
    ):
        refused()
    return {
        "run_started_at": started,
        "cap_usd": str(CAP_USD),
        "funding_status": status["funding_status"],
        "accounting_status": status["accounting_status"],
        "governance_complete": status["governance_complete"],
        "held_usd": str(held),
        "settled_usd": str(settled),
        "quoted_usd": str(quoted),
        "reservation_id": row["reservation_id"],
        "session_deadline_at": deadline,
        "cleanup_deadline_at": cleanup_deadline,
        "cleanup_confirmed_at": confirmed,
        "cleanup_status": "confirmed",
        "cleanup_evidence_sha256": cleanup_digest,
        "cleanup_attempts_used": attempts,
        "cleanup_attempts_limit": limit,
    }


def verify(session, fixture, gateway_url):
    """Attempt one exact search and one owned close; never replay paid work."""
    if (fixture.run_id, fixture.approval_revision) != (
        session.reference.run_id,
        session.reference.approval_revision,
    ):
        refused()
    operations = {
        name: str(uuid4())
        for name in ("discovery_before", "search", "close", "discovery_after")
    }
    request_id = str(uuid4())
    report = {
        "schema_version": "1.0",
        "run_id": fixture.run_id,
        "approval_revision": fixture.approval_revision,
        "source_revision": session.reference.source_revision,
        "definition_revision": session.reference.definition_revision,
        "gateway_url_sha256": hashlib.sha256(gateway_url.encode()).hexdigest(),
        "contract_sha256": fixture.contract_sha256,
        "request_sha256": fixture.request_sha256(request_id),
        "request_id": request_id,
        "native_operations": {},
        "behavioral_status": "failed",
        "governance_complete": False,
        "funding_status": "unknown",
        "accounting_status": "unknown",
        "cleanup_status": "unknown",
        "failures": [],
    }
    execution_id = receipt_id = None
    search_attempted = False
    try:
        report["native_operations"]["discovery_before"] = operations["discovery_before"]
        report["tools_list_sha256"] = discovery(
            session, gateway_url, operations["discovery_before"]
        )
        search_attempted = True
        report["native_operations"]["search"] = operations["search"]
        result = session.request(
            "POST",
            "/api/agent/tools/call",
            payload=fixture.payload(request_id),
            operation_id=operations["search"],
            gateway_url=gateway_url,
        )
        execution_id, receipt_id = search_receipt(result, request_id, gateway_url)
        report["execution_id"] = execution_id
        report["receipt_id"] = receipt_id
    except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
        report["failures"].append(
            "search_unconfirmed" if search_attempted else "discovery_unconfirmed"
        )

    if search_attempted and execution_id is None:
        try:
            row = owned_execution(
                session.status(), fixture, operations["search"], request_id
            )
            execution_id = row["execution_id"]
            receipt_id = row["reservation_id"]
            report["execution_id"] = execution_id
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("owned_execution_unconfirmed")
    if execution_id is not None:
        report["native_operations"]["close"] = operations["close"]
        try:
            close = session.request(
                "POST",
                f"/api/agent/tools/executions/{execution_id}/close?tool={TOOL}",
                operation_id=operations["close"],
                gateway_url=gateway_url,
            )
            if (
                close.get("execution_id") != execution_id
                or close.get("state") != "closed"
                or close.get("cleanup_status") != "confirmed"
            ):
                refused()
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("close_unconfirmed")
        if not report["failures"]:
            report["native_operations"]["discovery_after"] = operations[
                "discovery_after"
            ]
            try:
                if (
                    discovery(session, gateway_url, operations["discovery_after"])
                    != report["tools_list_sha256"]
                ):
                    refused()
            except (
                AdmissionUnavailable,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ):
                report["failures"].append("discovery_changed")
        try:
            report.update(
                status_evidence(
                    session.status(),
                    fixture,
                    operations["search"],
                    request_id,
                    execution_id,
                    receipt_id,
                )
            )
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("funding_or_cleanup_unconfirmed")
    if not report["failures"]:
        report["behavioral_status"] = "passed"
    # An authenticated settled run does not make a failed functional smoke pass.
    report["governance_complete"] = (
        report["governance_complete"] and not report["failures"]
    )
    report["status_reads"] = session.status_reads
    return report


def retain(path, report):
    """One bounded private JSON artifact, with no query, token or provider body."""
    data = canonical(report) + b"\n"
    if len(data) > MAX_EVIDENCE_BYTES:
        refused()
    descriptor = os.open(
        Path(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
