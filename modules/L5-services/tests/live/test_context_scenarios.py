"""Offline Context selector checks; simulated receipts are not live acceptance."""

import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from context_live import LiveContextDriver, validate_context_fixture
from context_scenarios import context_reuse
from evidence import AttemptJournal
from test_context import context_evidence


specification = importlib.util.spec_from_file_location(
    "security_context_readback", Path(__file__).resolve().parents[2] / "scripts/qualify-security-readbacks.py")
readback = importlib.util.module_from_spec(specification)
specification.loader.exec_module(readback)


@pytest.fixture
def reuse_case(context_evidence):
    run, result, publication, receipt, agent, visibility, denied, repeats, audit = deepcopy(context_evidence)
    run["state"] = "succeeded"
    run["provider_state"] = "completed"
    run["native_job_ref"] = result["native_job_ref"]
    result["coverage"] = "complete"
    run["input"].update(capability="threat_model", binding_id="binding-example")
    result["finding_count"] = 0
    selection_plan = {"input": deepcopy(run["input"])}
    publication.update(destination="product-context", contribution_hash="a" * 64)
    publication["payload"].update(contribution_id=publication["publication_id"], claims=[], evidence=[],
                                  schema_version="1.0")
    selection = {"scope": run["scope"], "run_id": run["run_id"], "result_id": result["result_id"],
                 "publication_id": publication["publication_id"], "sources": run["input"]["sources"],
                 "max_attempts": 3, "query_receipt": "query-receipt-example",
                 "assessment_plan": selection_plan, "delegation_id": "delegation-example",
                 "foreign_product_id": "foreign-product-example"}
    case = {"scope": run["scope"], "context_reuse": selection, "max_attempts": 3}
    foreign = {"scope": {**run["scope"], "tenant_id": "other-tenant-example"}}
    return case, foreign, [run, result, publication, receipt, agent, visibility, denied, repeats, audit]


class RecordingContext:
    def __init__(self, evidence):
        self.run, self.result, self.publication, self.receipt, self.agent, self.visibility_packet, self.denials, self.repeats, self.audit = evidence
        self.calls = []

    def read_product(self, selection):
        self.calls.append("product")
        return self.run, self.result, []

    def verify_result(self, run, result, findings, plan):
        self.calls.append("native_source_review")

    def read_publication(self, selection):
        self.calls.append("publication")
        return self.publication

    def read_receipt(self, selection):
        self.calls.append("receipt")
        return self.receipt

    def replay_publication(self, publication, selection):
        self.calls.append("replay")
        return self.repeats

    def later_agent_read(self, selection):
        self.calls.append("agent")
        return self.agent

    def visibility(self, selection):
        self.calls.append("visibility")
        return self.visibility_packet

    def denied_reads(self, selection, foreign):
        self.calls.append("denied")
        return self.denials

    def event_audit(self, selection):
        self.calls.append("audit")
        return self.audit


@pytest.mark.parametrize("fault", ["accepted", "latest", "foreign_scope", "missing_result",
                                     "wrong_contribution", "foreign_receipt", "missing_citation",
                                     "duplicate_count", "visibility_changed", "feedback_loop", "stale_agent",
                                     "wrong_payload_schema"])
def test_read_only_context_snapshot_checks_current_native_links_without_replay(reuse_case, fault):
    case, foreign, evidence = reuse_case
    evidence[4]["observed_at"] = datetime.now(UTC).isoformat()
    selection = case["context_reuse"]
    selection.update(agent_read_url="https://consumer.example.invalid/read",
                     agent_read_token_env="CONSUMER_TOKEN",
                     context_count_url="https://context.example.invalid/count",
                     context_token_env="CONTEXT_TOKEN")
    driver = RecordingContext(evidence)
    count = {"scope": case["scope"], "contribution_id": selection["publication_id"], "count": 1}

    def request(method, url, token_env):
        driver.calls.append("consumer-get" if url.endswith("/read") else "count-get")
        assert method == "GET"
        packet = evidence[4] if url.endswith("/read") else count
        return httpx.Response(200, json=packet, request=httpx.Request(method, url))

    driver.request = request
    if fault == "latest":
        selection["sources"] = [{**selection["sources"][0], "record_revision": "latest"}]
    elif fault == "foreign_scope":
        foreign["scope"]["tenant_id"] = case["scope"]["tenant_id"]
    elif fault == "missing_result":
        evidence[0]["state"] = "running"
    elif fault == "wrong_contribution":
        evidence[2]["payload"]["scope"] = {**case["scope"], "product_id": "foreign"}
    elif fault == "foreign_receipt":
        evidence[3]["consumer_revision"] = "foreign"
    elif fault == "missing_citation":
        evidence[4]["citations"] = []
    elif fault == "duplicate_count":
        count["count"] = 2
    elif fault == "visibility_changed":
        evidence[5]["revision"] = "changed"
    elif fault == "feedback_loop":
        evidence[8]["items"] = [{"causal_origin": selection["run_id"]}]
    elif fault == "stale_agent":
        evidence[4]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "wrong_payload_schema":
        evidence[2]["payload"]["schema_version"] = "2.0"
    if fault == "accepted":
        result = readback.verify_context_snapshot(case, foreign, driver)
        assert result["schema_version"] == result["admitted_schema"] == "1.0"
        assert result["agent_run_id"] == evidence[4]["agent_run_id"]
        assert driver.calls == ["product", "native_source_review", "publication", "receipt",
                                "consumer-get", "count-get", "visibility", "denied", "audit"]
    else:
        with pytest.raises((ValueError, AssertionError)):
            readback.verify_context_snapshot(case, foreign, driver)
        assert "replay" not in driver.calls and "agent" not in driver.calls


@pytest.mark.parametrize("fault", ["accepted", "missing_ack", "wrong_key", "wrong_run",
                                     "foreign_agent", "stale", "public", "extra_attempt", "wrong_pages"])
def test_context_replay_journal_is_bounded_by_one_native_contribution(reuse_case, tmp_path, fault):
    case, foreign, evidence = reuse_case
    selection = case["context_reuse"]
    selection.update(journal_file=str(tmp_path / "context.jsonl"), agent_read_url="https://consumer.example/read",
                     agent_read_token_env="CONSUMER_TOKEN", context_count_url="https://context.example/count",
                     context_token_env="CONTEXT_TOKEN")
    driver = RecordingContext(evidence)
    journal = object.__new__(LiveContextDriver)
    journal.case = case
    journal.journal = AttemptJournal(selection["journal_file"])
    for repeat in range(2):
        for status in ("prepared", "acknowledged"):
            journal.record(f"replay-{repeat}-0", "context_replay", status, selection["publication_id"],
                           f"{case['scope']['tenant_id']}/{selection['result_id']}/1/product-context/1.0")
    for status, work_id in (("prepared", selection["publication_id"]),
                            ("acknowledged", evidence[4]["agent_run_id"])):
        journal.record("later-query", "later_agent_query", status, work_id, selection["query_receipt"])
    path = Path(selection["journal_file"])
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == "missing_ack":
        entries.pop(3)
    elif fault == "wrong_key":
        entries[2]["idempotency_key"] = "foreign-key"
    elif fault == "wrong_run":
        entries[0]["run_id"] = "foreign-run"
    elif fault == "foreign_agent":
        entries[-1]["work_id"] = "other-agent"
    elif fault == "stale":
        entries[0]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "extra_attempt":
        entries.append(entries[-1])
    elif fault == "wrong_pages":
        evidence[2]["payload"]["claims"] = [{"evidence": []} for _ in range(101)]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    if fault == "public":
        path.chmod(0o644)
    evidence[4]["observed_at"] = datetime.now(UTC).isoformat()

    def request(method, url, token_env):
        driver.calls.append("consumer-get" if url.endswith("/read") else "count-get")
        packet = (evidence[4] if url.endswith("/read") else
                  {"scope": case["scope"], "contribution_id": selection["publication_id"], "count": 1})
        return httpx.Response(200, json=packet, request=httpx.Request(method, url))

    driver.request = request
    if fault == "accepted":
        observed = readback.verify_context_snapshot(case, foreign, driver)
        assert observed["replay"]["attempts"] == 2
        assert len(observed["replay"]["sha256"]) == 64
        assert "replay" not in driver.calls and "agent" not in driver.calls
    else:
        with pytest.raises((ValueError, KeyError)):
            readback.verify_context_snapshot(case, foreign, driver)


def test_context_selector_reads_product_then_receipt_before_replay_and_later_agent(reuse_case):
    case, foreign, evidence = reuse_case
    driver = RecordingContext(evidence)
    assert context_reuse(case, foreign, driver)["agent_run_id"] == "later-agent-example"
    assert driver.calls == ["product", "native_source_review", "publication", "receipt", "replay", "agent", "visibility", "denied", "audit"]


@pytest.mark.parametrize("change,expected_calls", [
    (lambda case, foreign, evidence: case["context_reuse"].update(max_attempts=4), []),
    (lambda case, foreign, evidence: case["context_reuse"]["sources"][0].update(record_revision="latest"), []),
    (lambda case, foreign, evidence: evidence[0].update(state="running"), ["product"]),
    (lambda case, foreign, evidence: evidence[2]["payload"].update(scope={"tenant_id": "wrong"}), ["product", "native_source_review", "publication"]),
    (lambda case, foreign, evidence: evidence[3].update(consumer_revision="wrong"), ["product", "native_source_review", "publication", "receipt"]),
])
def test_context_selector_refuses_unsafe_replay(reuse_case, change, expected_calls):
    case, foreign, evidence = reuse_case
    change(case, foreign, evidence)
    driver = RecordingContext(evidence)
    with pytest.raises(AssertionError):
        context_reuse(case, foreign, driver)
    assert driver.calls == expected_calls


def test_context_selector_refuses_missing_later_citation_or_wrong_scope(reuse_case):
    case, foreign, evidence = reuse_case
    evidence[4]["citations"] = []
    with pytest.raises(AssertionError, match="citation|uncertainty"):
        context_reuse(case, foreign, RecordingContext(evidence))
    evidence[4]["citations"] = [{"source_ref": "wrong"}]
    foreign["scope"]["tenant_id"] = case["scope"]["tenant_id"]
    driver = RecordingContext(evidence)
    with pytest.raises(AssertionError, match="bounded"):
        context_reuse(case, foreign, driver)
    assert driver.calls == []


def test_context_live_fixture_requires_fresh_private_journal_and_distinct_authorities(reuse_case, tmp_path, monkeypatch):
    case, foreign, _ = reuse_case
    selection = case["context_reuse"]
    selection["authorized_replay"] = True
    selection["journal_file"] = str(tmp_path / "attempts.jsonl")
    for name in ("publication_read_url", "context_url", "context_count_url", "agent_query_url",
                 "agent_read_url", "visibility_url", "event_audit_url"):
        selection[name] = ("https://private.example" if name == "context_url"
                           else "https://private.example/" + name)
    names = ("publication_token_env", "context_token_env", "agent_token_env", "agent_read_token_env",
             "visibility_token_env", "audit_token_env", "foreign_product_token_env", "revoked_token_env")
    for index, name in enumerate(names):
        selection[name] = "TEST_" + name.upper()
        monkeypatch.setenv(selection[name], "synthetic-" + str(index))
    tmp_path.chmod(0o700)
    validate_context_fixture(case, foreign)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        validate_context_fixture(case, foreign)
    tmp_path.chmod(0o700)
    monkeypatch.setenv(selection["agent_read_token_env"], "synthetic-2")
    with pytest.raises(ValueError, match="separate"):
        validate_context_fixture(case, foreign)


def test_context_replay_uses_same_idempotency_key_and_private_journal(reuse_case, tmp_path):
    case, foreign, evidence = reuse_case
    selection = case["context_reuse"]
    driver = object.__new__(LiveContextDriver)
    from evidence import AttemptJournal
    driver.case = case
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    calls = []

    def request(method, url, token_env, *, body=None, headers=None):
        calls.append((method, headers["Idempotency-Key"], body))
        return httpx.Response(201, json=evidence[3], request=httpx.Request(method, url))

    driver.request = request
    selection.update(context_url="https://context.example", context_token_env="TEST_CONTEXT")
    receipts = driver.replay_publication(evidence[2], selection)
    assert receipts == [evidence[3], evidence[3]]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert [row["status"] for row in driver.journal.read()] == [
        "prepared", "acknowledged", "prepared", "acknowledged"]
    selection["max_attempts"] = 2
    with pytest.raises(AssertionError, match="attempt bound"):
        driver.replay_publication(evidence[2], selection)
    assert len(calls) == 2


def test_context_live_denials_reject_disclosure_even_when_status_is_404(reuse_case):
    case, foreign, _ = reuse_case
    driver = object.__new__(LiveContextDriver)
    driver.case = case
    selection = case["context_reuse"]
    selection.update(context_url="https://context.example", foreign_product_token_env="TEST_FOREIGN",
                     revoked_token_env="TEST_REVOKED")
    driver.request = lambda method, url, token_env: httpx.Response(404, json={"detail": "Not Found",
        "secret": selection["result_id"]}, request=httpx.Request(method, url))
    assert all(probe["disclosed_resources"] for probe in driver.denied_reads(selection, foreign))


def test_context_replay_refuses_changed_receipt_before_second_request(reuse_case, tmp_path):
    case, foreign, evidence = reuse_case
    selection = case["context_reuse"]
    selection.update(context_url="https://context.example", context_token_env="TEST_CONTEXT")
    driver = object.__new__(LiveContextDriver)
    from evidence import AttemptJournal
    driver.case = case
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    calls = []

    def request(method, url, token_env, *, body=None, headers=None):
        calls.append(method)
        return httpx.Response(201, json={**evidence[3], "consumer_revision": "wrong"},
                              request=httpx.Request(method, url))

    driver.request = request
    with pytest.raises(AssertionError, match="changed"):
        driver.replay_publication(evidence[2], selection)
    assert calls == ["POST"]
    assert [row["status"] for row in driver.journal.read()] == ["prepared"]


def test_context_live_verifies_native_result_before_reading_publication(reuse_case):
    case, foreign, evidence = reuse_case
    run, result = evidence[:2]
    driver = object.__new__(LiveContextDriver)
    class NativeObservation:
        def __init__(self):
            self.calls = []

        def native_job(self, run):
            self.calls.append("native")
            return {"native_job_ref": run["native_job_ref"], "status": "COMPLETED",
                    "findings_coverage": "complete"}

        def read_sources(self, run, plan):
            self.calls.append("source")
            return []

        def reviews(self, run, result, findings, native, plan):
            self.calls.append("review")
            return {}

    driver.assessment = NativeObservation()
    with pytest.raises(AssertionError, match="source"):
        driver.verify_result(run, result, [], case["context_reuse"]["assessment_plan"])
    assert driver.assessment.calls == ["native", "source", "review"]
    driver.assessment.calls.clear()
    case["context_reuse"]["assessment_plan"]["input"]["binding_id"] = "wrong"
    with pytest.raises(AssertionError, match="assessment plan"):
        driver.verify_result(run, result, [], case["context_reuse"]["assessment_plan"])
    assert driver.assessment.calls == []


def test_context_later_agent_count_must_come_from_independent_context_read(reuse_case, tmp_path):
    case, foreign, evidence = reuse_case
    selection = case["context_reuse"]
    selection.update(agent_query_url="https://consumer.example/query", agent_read_url="https://consumer.example/read",
                     context_count_url="https://context.example/count", agent_token_env="TEST_AGENT",
                     agent_read_token_env="TEST_READ", context_token_env="TEST_CONTEXT")
    driver = object.__new__(LiveContextDriver)
    from evidence import AttemptJournal
    driver.case = case
    driver.journal = AttemptJournal(tmp_path / "attempts.jsonl")

    def request(method, url, token_env, *, body=None, headers=None):
        if url.endswith("/query"):
            data = {"agent_run_id": evidence[4]["agent_run_id"]}
        elif url.endswith("/read"):
            data = evidence[4]
        else:
            data = {"scope": case["scope"], "contribution_id": selection["publication_id"], "count": 2}
        return httpx.Response(200, json=data, request=httpx.Request(method, url))

    driver.request = request
    with pytest.raises(AssertionError, match="count"):
        driver.later_agent_read(selection)
    assert [row["status"] for row in driver.journal.read()] == ["prepared", "acknowledged"]


def test_context_publication_uses_installed_product_route_without_custom_credentials(reuse_case):
    case, _, evidence = reuse_case
    driver = object.__new__(LiveContextDriver)
    driver.case = case
    driver.remaining = lambda: 20
    selection = case["context_reuse"]
    observed = []

    def request(request):
        observed.append(request)
        assert request.method == "GET"
        assert request.url.path == (
            f"/v1/products/{case['scope']['product_id']}/projects/{case['scope']['project_id']}"
            f"/services/security/runs/{selection['run_id']}/publications/{selection['publication_id']}")
        assert request.headers["Authorization"] == "Bearer fictional-product-session"
        return httpx.Response(200, json={"run_id": selection["run_id"], "publication": evidence[2]})

    with httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(request),
                      headers={"Authorization": "Bearer fictional-product-session"}) as driver.api:
        assert driver.read_publication(selection) == evidence[2]
    assert len(observed) == 1
