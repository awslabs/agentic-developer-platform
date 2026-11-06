"""Offline fixtures for knowledge discovery and private identity contracts."""

import json

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.knowledge_integration import DENIAL_CONTROLS, KNOWLEDGE_OPERATIONS, READ_ACTIONS, knowledge_ac01, knowledge_ac02, knowledge_ac03, knowledge_ac04
from tests.e2e.tools.scenarios import CLIENTS
from tests.e2e.tools.scenarios import FixtureUnavailable

SHA = "a" * 40
IMAGE = "example.invalid/facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/gateway@sha256:" + "c" * 64
ORACLES = {
    "owner-a": {"source": "independent-knowledge-index", "indexed": True, "owner": "owner-a",
                "tenant": "tenant-a", "fact": "violet-fact", "citation": "fixture/a:7", "source_ref": "fixture/a"},
    "owner-b": {"source": "independent-knowledge-index", "indexed": True, "owner": "owner-b",
                "tenant": "tenant-b", "fact": "copper-fact", "citation": "fixture/b:9", "source_ref": "fixture/b"},
}

BACKEND_OUTPUTS = {
    "owner-a": {"fact": "violet-fact", "citation": "fixture/a:7", "source_ref": "fixture/a"},
    "owner-b": {"fact": "copper-fact", "citation": "fixture/b:9", "source_ref": "fixture/b"},
}


class Observer:
    def __init__(self):
        self.counts = {"owner-a": 0, "owner-b": 0}
        self.finished = False
        self.ready = {operation: True for operation in KNOWLEDGE_OPERATIONS}

    def expected(self, *, fixture_id, attempt_id):
        return dict(ORACLES[fixture_id])

    def provider_calls(self, *, fixture_id, attempt_id):
        return self.counts[fixture_id]

    def backend_ready(self, *, operation, attempt_id):
        return self.ready[operation]

    def cleaned(self, *, attempt_id):
        return self.finished


class Driver:
    isolated = authorized = True

    def __init__(self, observer):
        self.observer = observer
        self.result_override = {}
        self.actions = []

    def discover(self, *, attempt_id):
        return [{"name": "knowledge___" + operation, "inputSchema": {"type": "object"}} for operation in KNOWLEDGE_OPERATIONS]

    def query(self, *, fixture_id, query, attempt_id):
        self.actions.append(("search", None))
        self.observer.counts[fixture_id] += 1
        response = dict(BACKEND_OUTPUTS[fixture_id])
        return {**response, **self.result_override}

    def invoke(self, *, fixture_id, operation, action, attempt_id):
        self.actions.append((operation, action))
        if operation in {"remember", "experience"}:
            return {"isError": True, "code": "knowledge_admission_unavailable"}
        self.observer.counts[fixture_id] += 1
        return dict(BACKEND_OUTPUTS[fixture_id])

    def cleanup(self, *, attempt_id):
        self.observer.finished = True


class DenialDriver(Driver):
    def __init__(self, observer):
        super().__init__(observer)
        self.probes = []

    def probe(self, *, fixture_id, foreign_fixture_id, fault, attempt_id):
        self.probes.append((fixture_id, foreign_fixture_id, fault))
        return {"status": 401 if fault == "unauthenticated_direct" else 403, "content": []}


@pytest.mark.parametrize("distortion", ["fact", "citation", "source_ref", "foreign", "no_provider_call", "no_cleanup", "stale", "missing_tool"])
def test_knowledge_contract_rejects_false_discovery_and_sentinel_evidence(distortion):
    observer = Observer()
    driver = Driver(observer)
    if distortion in {"fact", "citation", "source_ref"}:
        driver.result_override[distortion] = "wrong"
    elif distortion == "foreign":
        driver.result_override["foreign_fact"] = ORACLES["owner-b"]["fact"]
    elif distortion == "no_provider_call":
        driver.query = lambda *, fixture_id, query, attempt_id: {
            key: BACKEND_OUTPUTS[fixture_id][key] for key in ("fact", "citation", "source_ref")}
    elif distortion == "no_cleanup":
        driver.cleanup = lambda *, attempt_id: None
    elif distortion == "stale":
        observer.expected = lambda *, fixture_id, attempt_id: {**ORACLES[fixture_id], "indexed": False}
    else:
        driver.discover = lambda *, attempt_id: []
    result = knowledge_ac01(driver, observer)
    assert result["status"] != "pass"
    assert result["cleanup"]["status"] == ("unknown" if distortion == "no_cleanup" else "confirmed")


def test_knowledge_scenario_records_two_exact_sources_and_cleanup(tmp_path, monkeypatch):
    observer = Observer()
    driver = Driver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    passed = runner.evaluate("SCN-8072-01", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                             client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                               "__exit__": lambda self, *args: None, "version": "fixture"})(),
                             integration_driver_factory=lambda *args: driver, integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    rows = report["scenarios"]
    assert not passed and report["live_attestation"] is False
    assert {row["cell_id"] for row in rows} == {"cli:" + operation for operation in KNOWLEDGE_OPERATIONS}
    for row in rows:
        assert row["scenario_id"] == "SCN-8072-01" and row["evidence_scope"] == "repository-integration"
        assert row["status"] == ("blocked" if row["tool"] in {"remember", "experience"} else "pass")
        assert row["cleanup"] == {"status": "confirmed"}
        if row["status"] == "blocked":
            assert row["reason"] == "knowledge_admission_unavailable"
        else:
            assert row["observations"]["operations"] == ["knowledge___" + row["tool"]]
    assert observer.counts == {"owner-a": 10, "owner-b": 10}


def test_knowledge_scenario_requires_private_driver_and_never_claims_live(tmp_path, monkeypatch):
    with pytest.raises(FixtureUnavailable):
        knowledge_ac01(None, Observer())
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    passed = runner.evaluate("SCN-8072-01", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                             client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                               "__exit__": lambda self, *args: None, "version": "fixture"})(),
                             integration_driver_factory=lambda *args: pytest.fail("local fixture is not deployed evidence"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not passed and len(rows) == len(KNOWLEDGE_OPERATIONS)
    assert all(row["status"] == "blocked" and row["cleanup"]["status"] == "not-run" for row in rows)


@pytest.mark.parametrize("operation,expected_actions", [
    ("search", [None]), ("understand", [None]), ("impact", [None]),
    ("browse", ["ls", "tree", "read", "info"]), ("secure", ["identify", "plan", "verify"]),
    ("remember", [None]), ("experience", ["save", "recall", "list_syntheses"]),
])
def test_every_verb_and_supported_action_has_independent_effect_check(operation, expected_actions):
    observer = Observer()
    driver = Driver(observer)
    result = knowledge_ac01(driver, observer, operation)
    assert result["status"] == ("blocked" if operation in {"remember", "experience"} else "pass")
    assert driver.actions == [(operation, action) for action in expected_actions] * 2
    assert observer.counts == {"owner-a": 0 if operation in {"remember", "experience"} else len(expected_actions),
                               "owner-b": 0 if operation in {"remember", "experience"} else len(expected_actions)}
    assert result["cleanup"] == {"status": "confirmed"}
    assert result["observations"]["actions"] == [action for action in expected_actions if action is not None]


@pytest.mark.parametrize("operation", ("search", "understand", "impact", "browse", "secure"))
def test_missing_backend_prerequisite_cannot_pass_without_query(operation):
    observer = Observer()
    observer.ready[operation] = False
    driver = Driver(observer)
    result = knowledge_ac01(driver, observer, operation)
    assert result["status"] == "blocked" and result["reason"].endswith("unavailable")
    assert driver.actions == [] and observer.counts == {"owner-a": 0, "owner-b": 0}
    assert result["cleanup"] == {"status": "confirmed"}


def test_unadmitted_action_with_provider_effect_is_a_failure():
    observer = Observer()
    driver = Driver(observer)
    original = driver.invoke

    def paid_action(*, fixture_id, operation, action, attempt_id):
        observer.counts[fixture_id] += 1
        return original(fixture_id=fixture_id, operation=operation, action=action, attempt_id=attempt_id)

    driver.invoke = paid_action
    result = knowledge_ac01(driver, observer, "experience")
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_negative_knowledge_scenario_records_isolated_denials_and_cleanup(tmp_path, monkeypatch):
    observer = Observer()
    driver = DenialDriver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    runner.evaluate("SCN-8072-02", tmp_path, "https://example.invalid", SHA,
                    GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                    client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                      "__exit__": lambda self, *args: None, "version": "fixture"})(),
                    integration_driver_factory=lambda *args: driver, integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["live_attestation"] is False
    assert len(report["scenarios"]) == 1
    row = report["scenarios"][0]
    assert row["scenario_id"] == "SCN-8072-02" and row["cell_id"] == "cli:search"
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["observations"]["controls"] == list(DENIAL_CONTROLS)
    assert row["cleanup"] == {"status": "confirmed"}
    assert observer.counts == {"owner-a": 1, "owner-b": 1}
    assert driver.probes == [(source, target, fault)
                             for source, target in (("owner-a", "owner-b"), ("owner-b", "owner-a"))
                             for fault in DENIAL_CONTROLS]


@pytest.mark.parametrize("distortion", ("leak", "allowed", "wrong_status", "provider_call", "foreign_provider_call",
                                          "positive", "missing_cleanup", "unindexed"))
def test_negative_knowledge_scenario_rejects_false_denials(distortion):
    observer = Observer()
    driver = DenialDriver(observer)
    original = driver.probe
    if distortion == "positive":
        driver.query = lambda *, fixture_id, query, attempt_id: {**BACKEND_OUTPUTS[fixture_id], "fact": "wrong"}
    elif distortion == "missing_cleanup":
        driver.cleanup = lambda *, attempt_id: None
    elif distortion == "unindexed":
        observer.expected = lambda *, fixture_id, attempt_id: {**ORACLES[fixture_id], "indexed": False}
    else:
        def distorted(*, fixture_id, foreign_fixture_id, fault, attempt_id):
            answer = original(fixture_id=fixture_id, foreign_fixture_id=foreign_fixture_id,
                              fault=fault, attempt_id=attempt_id)
            if distortion == "leak":
                answer["content"] = [ORACLES[foreign_fixture_id]["fact"]]
            elif distortion == "allowed":
                answer["status"] = 200
            elif distortion == "wrong_status":
                answer["status"] = 403 if fault == "unauthenticated_direct" else 401
            elif distortion == "provider_call":
                observer.counts[fixture_id] += 1
            else:
                observer.counts[foreign_fixture_id] += 1
            return answer
        driver.probe = distorted
    result = knowledge_ac02(driver, observer)
    assert result["status"] != "pass"
    assert result["cleanup"]["status"] == ("unknown" if distortion == "missing_cleanup" else "confirmed")


def test_negative_knowledge_scenario_cannot_claim_deployed_acceptance(tmp_path, monkeypatch):
    observer = Observer()
    with pytest.raises(FixtureUnavailable):
        knowledge_ac02(None, observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    runner.evaluate("SCN-8072-02", tmp_path, "https://example.invalid", SHA,
                    GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                    client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                      "__exit__": lambda self, *args: None, "version": "fixture"})(),
                    integration_driver_factory=lambda *args: pytest.fail("local fixture cannot attest live"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["live_attestation"] is False
    assert report["scenarios"][0]["status"] == "blocked"


class ReadinessObserver(Observer):
    def __init__(self):
        super().__init__()
        self.engine_calls = 0
        self.knowledge_present = True

    def expected_core(self, *, attempt_id):
        return {"source": "independent-engine-fixture", "name": "websearch___search",
                "fact": "public-engine-fact", "citation": "fixture/engine:5"}

    def core_calls(self, *, attempt_id):
        return self.engine_calls

    def target_state(self, *, attempt_id):
        return {"source": "independent-gateway-control", "knowledge": self.knowledge_present, "engine": True}

    def pending_expected(self, *, fixture_id, attempt_id):
        fixture = ORACLES[fixture_id]
        return {"source": "independent-knowledge-index", "indexed": False, "stage": "pending",
                "owner": fixture["owner"], "tenant": fixture["tenant"], "fact": fixture["fact"]}


class ReadinessDriver(Driver):
    def unavailable(self, *, attempt_id):
        return {"tools": ["websearch___search"], "connector_status": "unavailable",
                "query": {"status": 503, "content": []}}

    def core_query(self, *, query, attempt_id):
        assert query == "public-engine-fact"
        self.observer.engine_calls += 1
        return {"fact": "public-engine-fact", "citation": "fixture/engine:5"}

    def pending(self, *, fixture_id, query, attempt_id):
        assert query == ORACLES[fixture_id]["fact"]
        self.actions.append((fixture_id, "pending"))
        return {"isError": True, "code": "knowledge_index_not_ready"}

    def backend_failure(self, *, fixture_id, query, attempt_id):
        assert query == ORACLES[fixture_id]["fact"]
        self.actions.append((fixture_id, "backend-failed"))
        self.observer.counts[fixture_id] += 1
        return {"isError": True, "code": "knowledge_backend_unavailable"}

    def removed(self, *, attempt_id):
        self.observer.knowledge_present = False
        return {"tools": ["websearch___search"], "query": {"status": 403, "code": "tool_undeployed"}}


def test_readiness_scenario_keeps_engine_callable_while_knowledge_fails(tmp_path, monkeypatch):
    observer = ReadinessObserver()
    driver = ReadinessDriver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    runner.evaluate("SCN-8072-03", tmp_path, "https://example.invalid", SHA,
                    GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                    client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                      "__exit__": lambda self, *args: None, "version": "fixture"})(),
                    integration_driver_factory=lambda *args: driver, integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert len(report["scenarios"]) == 1 and report["live_attestation"] is False
    row = report["scenarios"][0]
    assert row["scenario_id"] == "SCN-8072-03" and row["cell_id"] == "cli:search"
    assert row["evidence_scope"] == "repository-integration" and row["status"] == "pass"
    assert row["observations"] == {"source": "independent-engine-fixture", "core_operation": "websearch___search",
                                   "fault": "knowledge_backend_unavailable", "pending_fixtures": 2,
                                   "indexing": "knowledge_index_not_ready", "failed_fixtures": 2,
                                   "backend": "knowledge_backend_unavailable", "target_removed": True,
                                   "engine_calls": 2}
    assert row["cleanup"] == {"status": "confirmed"}
    assert observer.counts == {"owner-a": 1, "owner-b": 1} and observer.engine_calls == 2
    assert observer.knowledge_present is False
    assert driver.actions == [("owner-a", "pending"), ("owner-b", "pending"),
                              ("owner-a", "backend-failed"), ("owner-b", "backend-failed")]


@pytest.mark.parametrize("distortion", ("hidden_engine", "false_success", "fake_core", "core_not_called", "private_dispatched", "missing_cleanup",
                                       "pending_empty_success", "pending_provider_call", "pending_wrong_code", "pending_shared_tenant", "pending_unverified_oracle",
                                       "backend_empty_success", "backend_no_dispatch", "backend_wrong_code", "backend_shared_tenant",
                                       "removed_stale_tool", "removed_false_success", "removed_not_removed", "removed_engine_missing", "removed_private_dispatch"))
def test_readiness_scenario_refuses_incomplete_fault_evidence(distortion):
    observer = ReadinessObserver()
    driver = ReadinessDriver(observer)
    if distortion in {"hidden_engine", "false_success"}:
        driver.unavailable = lambda *, attempt_id: {"tools": [] if distortion == "hidden_engine" else ["websearch___search"],
                                                       "connector_status": "unavailable", "query": {"status": 200, "content": []}}
    elif distortion in {"fake_core", "core_not_called"}:
        driver.core_query = lambda *, query, attempt_id: {"fact": "wrong" if distortion == "fake_core" else query,
                                                           "citation": "fixture/engine:5"}
    elif distortion == "private_dispatched":
        original = driver.unavailable
        def dispatched(*, attempt_id):
            observer.counts["owner-a"] += 1
            return original(attempt_id=attempt_id)
        driver.unavailable = dispatched
    elif distortion in {"pending_empty_success", "pending_provider_call", "pending_wrong_code"}:
        original = driver.pending
        def incorrect_pending(*, fixture_id, query, attempt_id):
            response = original(fixture_id=fixture_id, query=query, attempt_id=attempt_id)
            if distortion == "pending_provider_call":
                observer.counts[fixture_id] += 1
            if distortion == "pending_empty_success":
                return {"isError": False, "results": []}
            if distortion == "pending_wrong_code":
                return {"isError": True, "code": "unknown"}
            return response
        driver.pending = incorrect_pending
    elif distortion in {"pending_shared_tenant", "pending_unverified_oracle"}:
        original = observer.pending_expected
        def incorrect_oracle(*, fixture_id, attempt_id):
            fixture = original(fixture_id=fixture_id, attempt_id=attempt_id)
            if fixture_id == "owner-b":
                fixture["tenant" if distortion == "pending_shared_tenant" else "indexed"] = (
                    "tenant-a" if distortion == "pending_shared_tenant" else True)
            return fixture
        observer.pending_expected = incorrect_oracle
    elif distortion in {"backend_empty_success", "backend_no_dispatch", "backend_wrong_code"}:
        original = driver.backend_failure
        def incorrect_failure(*, fixture_id, query, attempt_id):
            response = original(fixture_id=fixture_id, query=query, attempt_id=attempt_id)
            if distortion == "backend_no_dispatch":
                observer.counts[fixture_id] -= 1
            if distortion == "backend_empty_success":
                return {"isError": False, "results": []}
            if distortion == "backend_wrong_code":
                return {"isError": True, "code": "knowledge_index_not_ready"}
            return response
        driver.backend_failure = incorrect_failure
    elif distortion == "backend_shared_tenant":
        original = observer.expected
        def incorrect_fixture(*, fixture_id, attempt_id):
            fixture = original(fixture_id=fixture_id, attempt_id=attempt_id)
            if fixture_id == "owner-b":
                fixture["tenant"] = "tenant-a"
            return fixture
        observer.expected = incorrect_fixture
    elif distortion in {"removed_stale_tool", "removed_false_success", "removed_private_dispatch"}:
        original = driver.removed
        def faulty_removal(*, attempt_id):
            result = original(attempt_id=attempt_id)
            if distortion == "removed_private_dispatch":
                observer.counts["owner-b"] += 1
            if distortion == "removed_stale_tool":
                return {**result, "tools": ["websearch___search", "knowledge___search"]}
            if distortion == "removed_false_success":
                return {**result, "query": {"status": 200, "content": []}}
            return result
        driver.removed = faulty_removal
    elif distortion == "removed_not_removed":
        original = driver.removed
        def still_present(*, attempt_id):
            result = original(attempt_id=attempt_id)
            observer.knowledge_present = True
            return result
        driver.removed = still_present
    elif distortion == "removed_engine_missing":
        original = observer.target_state
        def missing_engine(*, attempt_id):
            result = original(attempt_id=attempt_id)
            if not result["knowledge"]:
                result["engine"] = False
            return result
        observer.target_state = missing_engine
    else:
        driver.cleanup = lambda *, attempt_id: None
    result = knowledge_ac03(driver, observer)
    assert result["status"] != "pass"
    assert result["cleanup"]["status"] == ("unknown" if distortion == "missing_cleanup" else "confirmed")


def test_readiness_scenario_cannot_claim_live_acceptance(tmp_path, monkeypatch):
    with pytest.raises(FixtureUnavailable):
        knowledge_ac03(None, ReadinessObserver())
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    runner.evaluate("SCN-8072-03", tmp_path, "https://example.invalid", SHA,
                    GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                    client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                      "__exit__": lambda self, *args: None, "version": "fixture"})(),
                    integration_driver_factory=lambda *args: pytest.fail("local readiness cannot attest live"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["live_attestation"] is False and report["scenarios"][0]["status"] == "blocked"


class MatrixObserver(Observer):
    def __init__(self):
        super().__init__()
        self.completed = set()

    def pricing(self, *, attempt_id):
        return {"source": "independent-test-pricing", "per_call_usd": "0.01", "max_cell_usd": "0.50",
                "max_sessions": 1, "ttl_seconds": 300, "cleanup_seconds": 600}

    def client_version(self, *, client_name, attempt_id):
        return {"source": "independent-client-install", "name": client_name,
                "version": "fixture-" + client_name}

    def session_status(self, *, attempt_id):
        return {"source": "independent-session-observer", "count": 0, "ttl_seconds": 300}

    def cleanup_status(self, *, attempt_id):
        return {"source": "independent-cleanup", "attempt_id": attempt_id,
                "status": "confirmed" if attempt_id in self.completed else "missing",
                "owned_only": True, "elapsed_seconds": 1}


class MatrixDriver(Driver):
    def __init__(self, observer, client_name):
        super().__init__(observer)
        self.client_name = client_name
        self.version = "fixture-" + client_name
        self.foreign_calls = []

    def discover(self, *, attempt_id):
        return super().discover(attempt_id=attempt_id) + [
            {"name": "adp___operations_list", "inputSchema": {"type": "object"}}]

    def worker(self, *, fixture_id, attempt_id):
        assert self.client_name == "engine-worker" and fixture_id == "owner-a"
        self.observer.counts[fixture_id] += 1
        return dict(BACKEND_OUTPUTS[fixture_id])

    def foreign(self, *, fixture_id, foreign_fixture_id, attempt_id):
        self.foreign_calls.append((fixture_id, foreign_fixture_id))
        return {"status": 403, "content": []}

    def cleanup(self, *, attempt_id):
        self.observer.completed.add(attempt_id)


@pytest.mark.parametrize("client_name", CLIENTS)
@pytest.mark.parametrize("operation", KNOWLEDGE_OPERATIONS)
def test_knowledge_matrix_operation_and_client_have_isolated_result_and_cleanup(client_name, operation):
    observer = MatrixObserver()
    driver = MatrixDriver(observer, client_name)
    result = knowledge_ac04(driver, observer, client_name, operation)
    expected_actions = READ_ACTIONS.get(operation, (None,))
    expected_calls = 0 if operation in {"remember", "experience"} else len(expected_actions)
    assert result["status"] == ("blocked" if operation in {"remember", "experience"} else "pass")
    assert result["reason"] == "knowledge_admission_unavailable" if operation in {"remember", "experience"} else "reason" not in result
    assert result["client_version"] == "fixture-" + client_name
    assert result["cleanup"] == {"status": "confirmed"} and result["sessions"] == 0
    assert result["observations"]["provider_calls"] == 2 * expected_calls
    assert observer.counts == {"owner-a": expected_calls, "owner-b": expected_calls}
    assert driver.foreign_calls == [("owner-a", "owner-b"), ("owner-b", "owner-a")]


def test_knowledge_matrix_worker_acl_positive_and_foreign_denial():
    observer = MatrixObserver()
    driver = MatrixDriver(observer, "engine-worker")
    result = knowledge_ac04(driver, observer, "engine-worker", "acl")
    assert result["status"] == "pass" and result["observations"]["provider_calls"] == 1
    assert observer.counts == {"owner-a": 1, "owner-b": 0}
    assert driver.foreign_calls == [("owner-a", "owner-b"), ("owner-b", "owner-a")]
    assert result["cleanup"] == {"status": "confirmed"}


def test_knowledge_matrix_rejects_unexpected_knowledge_tool():
    observer = MatrixObserver()
    driver = MatrixDriver(observer, "cli")
    original = driver.discover
    driver.discover = lambda *, attempt_id: original(attempt_id=attempt_id) + [
        {"name": "knowledge___unexpected", "inputSchema": {"type": "object"}}]
    result = knowledge_ac04(driver, observer, "cli", "search")
    assert result["status"] == "fail" and result["reason"] == "ValueError"
    assert observer.counts == {"owner-a": 0, "owner-b": 0}
    assert result["cleanup"] == {"status": "confirmed"}


@pytest.mark.parametrize("fault", ("fact", "citation", "foreign_success", "foreign_call", "cleanup", "price", "version", "session", "discovery", "timeout", "unadmitted_effect"))
def test_knowledge_matrix_rejects_false_client_and_worker_evidence(fault, monkeypatch):
    observer = MatrixObserver()
    driver = MatrixDriver(observer, "cli")
    operation = "experience" if fault == "unadmitted_effect" else "search"
    if fault in {"fact", "citation"}:
        driver.result_override[fault] = "wrong"
    elif fault == "foreign_success":
        driver.foreign = lambda **kwargs: {"status": 200, "content": ["foreign-fact"]}
    elif fault == "foreign_call":
        def leaked(*, fixture_id, foreign_fixture_id, attempt_id):
            observer.counts[fixture_id] += 1
            return {"status": 403, "content": []}
        driver.foreign = leaked
    elif fault == "cleanup":
        driver.cleanup = lambda *, attempt_id: None
    elif fault == "price":
        observer.pricing = lambda *, attempt_id: {**MatrixObserver().pricing(attempt_id=attempt_id), "max_cell_usd": "1"}
    elif fault == "version":
        driver.version = "unverified"
    elif fault == "session":
        observer.session_status = lambda *, attempt_id: {**MatrixObserver().session_status(attempt_id=attempt_id), "count": 2}
    elif fault == "discovery":
        driver.discover = lambda *, attempt_id: []
    elif fault == "timeout":
        from tests.e2e.tools import knowledge_integration
        timestamps = iter((0, 301))
        monkeypatch.setattr(knowledge_integration, "monotonic", lambda: next(timestamps, 301))
    else:
        def paid(*, fixture_id, operation, action, attempt_id):
            observer.counts[fixture_id] += 1
            return {"isError": True, "code": "knowledge_admission_unavailable"}
        driver.invoke = paid
    if fault == "price":
        with pytest.raises(FixtureUnavailable):
            knowledge_ac04(driver, observer, "cli", operation)
        assert observer.counts == {"owner-a": 0, "owner-b": 0}
    else:
        result = knowledge_ac04(driver, observer, "cli", operation)
        assert result["status"] != "pass" and result["cleanup"]["status"] == ("unknown" if fault == "cleanup" else "confirmed")


def test_knowledge_matrix_runner_has_22_individual_non_live_cells(tmp_path, monkeypatch):
    observer = MatrixObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    completed = runner.evaluate("SCN-8072-04", tmp_path, "https://example.invalid", SHA,
                                GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                                client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                  "__exit__": lambda self, *args: None, "version": "fixture"})(),
                                integration_driver_factory=lambda _, name, tool: MatrixDriver(observer, name),
                                integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    rows = report["scenarios"]
    assert not completed and report["live_attestation"] is False and len(rows) == 22
    assert {row["cell_id"] for row in rows} == {client + ":" + verb for client in CLIENTS for verb in KNOWLEDGE_OPERATIONS} | {"engine-worker:acl"}
    assert all(row["scenario_id"] == "SCN-8072-04" and row["evidence_scope"] == "repository-integration" for row in rows)
    assert all(row["fixture"] == {"scope": "run-owned-isolated", "max_sessions": 1, "ttl_seconds": 300,
                                  "cleanup_seconds": 600, "max_cost_usd": "20"} and row["cleanup"] == {"status": "confirmed"} for row in rows)
    assert sum(row["status"] == "blocked" for row in rows) == 6
    assert {row["cell_id"] for row in rows if row["status"] == "blocked"} == {client + ":" + verb for client in CLIENTS for verb in ("remember", "experience")}
    assert observer.counts == {"owner-a": 31, "owner-b": 30}


def test_knowledge_matrix_live_mode_does_not_launch_synthetic_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    completed = runner.evaluate("SCN-8072-04", tmp_path, "https://example.invalid", SHA,
                                GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                                client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                  "__exit__": lambda self, *args: None, "version": "fixture"})(),
                                integration_driver_factory=lambda *args: pytest.fail("synthetic client reached live path"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not completed and report["live_attestation"] is False
    assert len(report["scenarios"]) == 22
    assert all(row["status"] == "blocked" and row["cleanup"] == {"status": "not-run"} for row in report["scenarios"])
