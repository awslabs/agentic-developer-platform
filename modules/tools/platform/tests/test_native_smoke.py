"""Fault-oriented offline acceptance; these tests do not attest a live tool."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.tools.platform.scripts import native_smoke as smoke
from modules.tools.platform.scripts.native_test_session import AdmissionUnavailable


GATEWAY = "https://agentcore.example.invalid/mcp"
GATEWAY_DIGEST = hashlib.sha256(GATEWAY.encode()).hexdigest()
STARTED = 1800000000


@pytest.fixture
def fixture():
    return smoke.SearchFixture(
        str(uuid4()), str(uuid4()), "c" * 64, "explicit approved offline query"
    )


class Session:
    def __init__(
        self,
        fixture,
        *,
        fail_search=False,
        fail_close=False,
        status_change=None,
        after_digest=None,
    ):
        self.fixture = fixture
        self.reference = SimpleNamespace(
            run_id=fixture.run_id,
            approval_revision=fixture.approval_revision,
            source_revision="a" * 40,
            definition_revision="b" * 40,
        )
        self.status_reads = 0
        self.calls = []
        self.execution_id = str(uuid4())
        self.receipt_id = "tools-test-model:" + "d" * 64
        self.fail_search, self.fail_close = fail_search, fail_close
        self.status_change, self.after_digest = status_change, after_digest

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        assert kwargs["gateway_url"] == GATEWAY
        if path == "/api/agent/tools?live=true":
            count = sum(call[1] == path for call in self.calls)
            return {
                "tools": [{"name": smoke.TOOL}],
                "discovery": {
                    "gateway_url_sha256": GATEWAY_DIGEST,
                    "tools_list_sha256": self.after_digest
                    if count == 2 and self.after_digest
                    else "e" * 64,
                },
            }
        if path == "/api/agent/tools/call":
            self.call = kwargs
            if self.fail_search:
                raise AdmissionUnavailable("Private response unavailable")
            return {
                "status": "confirmed",
                "result_status": "success",
                "request_id": kwargs["payload"]["request_id"],
                "execution_id": self.execution_id,
                "receipt_id": self.receipt_id,
                "gateway_url_sha256": GATEWAY_DIGEST,
            }
        assert (
            path
            == f"/api/agent/tools/executions/{self.execution_id}/close?tool={smoke.TOOL}"
        )
        if self.fail_close:
            raise AdmissionUnavailable("Close response unavailable")
        return {
            "execution_id": self.execution_id,
            "state": "closed",
            "cleanup_status": "confirmed",
        }

    def status(self):
        self.status_reads += 1
        assert self.status_reads <= 3
        value = {
            "schema_version": "1.0",
            "run_id": self.fixture.run_id,
            "run_started_at": STARTED,
            "cap_usd": "20",
            "held_usd": "12.10",
            "settled_usd": "0",
            "funding_status": "held",
            "accounting_status": "pending",
            "governance_complete": False,
            "executions": [
                {
                    "execution_id": self.execution_id,
                    "original_operation_id": self.call["operation_id"],
                    "reservation_id": self.receipt_id,
                    "quoted_usd": "0.03",
                    "contract_sha256": self.fixture.contract_sha256,
                    "request_sha256": self.fixture.request_sha256(
                        self.call["payload"]["request_id"]
                    ),
                    "session_deadline_at": STARTED + 300,
                    "cleanup_deadline_at": STARTED + 600,
                    "cleanup_status": "confirmed",
                    "cleanup_confirmed_at": STARTED + 30,
                    "cleanup_evidence_sha256": "f" * 64,
                    "cleanup_attempts_used": 1,
                    "cleanup_attempts_limit": 1,
                }
            ],
        }
        if self.status_change:
            self.status_change(value)
        return value


def test_held_funds_and_timely_cleanup_pass_behavior_without_claiming_settlement(
    fixture,
):
    session = Session(fixture)
    report = smoke.verify(session, fixture, GATEWAY)
    assert report["behavioral_status"] == "passed"
    assert (
        report["accounting_status"] == "pending" and report["funding_status"] == "held"
    )
    assert (
        report["governance_complete"] is False
        and report["cleanup_status"] == "confirmed"
    )
    assert (
        report["cleanup_confirmed_at"]
        <= report["cleanup_deadline_at"]
        <= report["run_started_at"] + 600
    )
    assert [call[0] for call in session.calls] == ["GET", "POST", "POST", "GET"]
    assert len(set(report["native_operations"].values())) == 4
    assert report["request_id"] not in report["native_operations"].values()
    assert session.calls[1][2]["payload"] == {
        "tool": smoke.TOOL,
        "arguments": {"query": fixture.query},
        "request_id": report["request_id"],
        "execution_id": None,
        "ttl_seconds": None,
    }
    assert fixture.query not in json.dumps(report)
    assert report["status_reads"] == 1


def test_trusted_settlement_is_reported_separately(fixture):
    def settled(value):
        value.update(
            held_usd="0",
            settled_usd="0.03",
            funding_status="settled",
            accounting_status="settled",
            governance_complete=True,
        )

    report = smoke.verify(Session(fixture, status_change=settled), fixture, GATEWAY)
    assert (
        report["behavioral_status"] == "passed"
        and report["governance_complete"] is True
    )
    assert report["accounting_status"] == "settled"


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", str(uuid4())),
        ("run_started_at", STARTED + 601),
        ("run_started_at", True),
        ("cap_usd", "21"),
        ("cap_usd", "NaN"),
        ("held_usd", "0"),
        ("held_usd", None),
        ("settled_usd", "20"),
        ("funding_status", "unknown"),
        ("accounting_status", "unknown"),
        ("governance_complete", True),
        ("governance_complete", "false"),
    ],
)
def test_incomplete_or_inconsistent_run_status_cannot_pass(fixture, field, value):
    report = smoke.verify(
        Session(fixture, status_change=lambda status: status.update({field: value})),
        fixture,
        GATEWAY,
    )
    assert (
        report["behavioral_status"] == "failed"
        and report["governance_complete"] is False
    )
    assert "funding_or_cleanup_unconfirmed" in report["failures"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_id", str(uuid4())),
        ("original_operation_id", str(uuid4())),
        ("reservation_id", "another-reservation"),
        ("contract_sha256", "a" * 64),
        ("request_sha256", "b" * 64),
        ("quoted_usd", "19"),
        ("session_deadline_at", STARTED + 301),
        ("cleanup_deadline_at", STARTED + 601),
        ("cleanup_confirmed_at", STARTED + 601),
        ("cleanup_confirmed_at", None),
        ("cleanup_status", "unknown"),
        ("cleanup_evidence_sha256", None),
        ("cleanup_evidence_sha256", ""),
        ("cleanup_attempts_used", 2),
        ("cleanup_attempts_used", 0),
        ("cleanup_attempts_used", True),
        ("cleanup_attempts_limit", 0),
    ],
)
def test_exact_original_execution_funding_and_cleanup_bindings_are_required(
    fixture, field, value
):
    def change(status):
        status["executions"][0][field] = value

    report = smoke.verify(Session(fixture, status_change=change), fixture, GATEWAY)
    assert (
        report["behavioral_status"] == "failed"
        and report["governance_complete"] is False
    )
    assert "funding_or_cleanup_unconfirmed" in report["failures"]


def test_extra_execution_cannot_be_hidden_in_a_stateless_single_effect_run(fixture):
    def change(status):
        status["executions"].append(deepcopy(status["executions"][0]))

    report = smoke.verify(Session(fixture, status_change=change), fixture, GATEWAY)
    assert report["behavioral_status"] == "failed"


def test_unknown_search_uses_owned_status_for_one_cleanup_and_never_replays_effect(
    fixture,
):
    session = Session(fixture, fail_search=True)
    report = smoke.verify(session, fixture, GATEWAY)
    assert (
        report["behavioral_status"] == "failed"
        and report["governance_complete"] is False
    )
    assert report["failures"] == ["search_unconfirmed"]
    assert (
        report["cleanup_status"] == "confirmed" and report["funding_status"] == "held"
    )
    assert report["status_reads"] == 2
    assert [call[1] for call in session.calls].count("/api/agent/tools/call") == 1
    assert sum("/close?" in call[1] for call in session.calls) == 1


def test_unknown_funding_does_not_prevent_original_owned_cleanup(fixture):
    session = Session(
        fixture,
        fail_search=True,
        status_change=lambda status: status.update(funding_status="unknown"),
    )
    report = smoke.verify(session, fixture, GATEWAY)
    assert report["behavioral_status"] == "failed"
    assert sum("/close?" in call[1] for call in session.calls) == 1


def test_changed_original_identity_never_closes_a_different_execution(fixture):
    def change(status):
        status["executions"][0]["original_operation_id"] = str(uuid4())

    session = Session(fixture, fail_search=True, status_change=change)
    report = smoke.verify(session, fixture, GATEWAY)
    assert report["behavioral_status"] == "failed"
    assert not any("/close?" in call[1] for call in session.calls)


def test_close_failure_cannot_retry_or_publish_success(fixture):
    session = Session(fixture, fail_close=True)
    report = smoke.verify(session, fixture, GATEWAY)
    assert report["behavioral_status"] == "failed"
    assert report["failures"] == ["close_unconfirmed"]
    assert sum("/close?" in call[1] for call in session.calls) == 1


def test_changed_discovery_digest_fails_after_retaining_cleanup_evidence(fixture):
    session = Session(fixture, after_digest="a" * 64)
    report = smoke.verify(session, fixture, GATEWAY)
    assert (
        report["behavioral_status"] == "failed"
        and report["cleanup_status"] == "confirmed"
    )
    assert report["failures"] == ["discovery_changed"]


def test_request_digest_uses_gateway_canonical_envelope_with_explicit_nulls(fixture):
    query = replace(fixture, query="approved café")
    request_id = "12345678-1234-4234-8234-123456789012"
    expected = (
        b'{"action":"call","arguments":{"query":"approved caf\\u00e9"},'
        b'"execution_id":null,"request_id":"12345678-1234-4234-8234-123456789012",'
        b'"schema_version":"1.0","tool":"websearch___search","ttl_seconds":null}'
    )
    assert query.request_sha256(request_id) == hashlib.sha256(expected).hexdigest()


def fixture_file(tmp_path, fixture):
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": fixture.run_id,
                "approval_revision": fixture.approval_revision,
                "contract_sha256": fixture.contract_sha256,
                "arguments": {"query": fixture.query},
            }
        )
    )
    path.chmod(0o600)
    return path


def test_exact_private_fixture_has_no_query_default(tmp_path, fixture):
    reference = Session(fixture).reference
    path = fixture_file(tmp_path, fixture)
    assert smoke.SearchFixture.read(path, reference) == fixture
    data = json.loads(path.read_text())
    del data["arguments"]["query"]
    path.write_text(json.dumps(data))
    with pytest.raises(AdmissionUnavailable):
        smoke.SearchFixture.read(path, reference)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("run_id", str(uuid4())),
        ("approval_revision", str(uuid4())),
        ("contract_sha256", ""),
        ("arguments", {"query": ""}),
        ("arguments", {"query": "x" * 201}),
        ("arguments", {"query": "fixed", "maxResults": 1}),
    ],
)
def test_fixture_must_bind_original_approval_and_exact_arguments(
    tmp_path, fixture, field, value
):
    path = fixture_file(tmp_path, fixture)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(AdmissionUnavailable):
        smoke.SearchFixture.read(path, Session(fixture).reference)


def test_retained_report_is_private_bounded_query_free_and_never_overwrites(
    tmp_path, fixture
):
    report = smoke.verify(Session(fixture), fixture, GATEWAY)
    path = tmp_path / "receipt.json"
    smoke.retain(path, report)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size < smoke.MAX_EVIDENCE_BYTES
    assert fixture.query not in path.read_text()
    with pytest.raises(FileExistsError):
        smoke.retain(path, report)
    with pytest.raises(AdmissionUnavailable):
        smoke.retain(
            tmp_path / "oversized.json", {"data": "x" * smoke.MAX_EVIDENCE_BYTES}
        )
