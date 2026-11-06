"""Synthetic Jira observations cannot establish live user consent or deployment."""

import json

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.jira_integration import jira_ac04
from tests.e2e.tools.scenarios import FixtureUnavailable
from tests.e2e.tools.test_client_matrix import matrix_fixture

REVISION = "a" * 40
GATEWAY_IMAGE = "example.invalid/gateway@sha256:" + "b" * 64
FACADE_IMAGE = "example.invalid/facade@sha256:" + "c" * 64
REVISIONS = {"gateway": REVISION, "gateway_image": GATEWAY_IMAGE, "facade_image": FACADE_IMAGE}
SITE = "00000000-0000-4000-8000-000000000001"
ORACLE = {"source": "independent-jira-fixture", "owner_id": "owner-a", "vendor_user_id": "vendor-user-a",
          "connection_id": "connection-a", "cloud_id": SITE, "issue_id": "10001", "issue_key": "FIX-1",
          "sentinel": "Synthetic issue sentinel", "scope": "read:jira-work"}
FAULT_DISPATCHES = {"other_site": 0, "denied_issue": 3, "missing_scope": 0, "missing_consent": 0, "disconnected": 0}


class Observer:
    deployed_target = False

    def __init__(self, defect=None):
        self.defect = defect
        self.attempts = {}

    def state(self, attempt_id):
        return self.attempts.setdefault(attempt_id, {"calls": 0, "invocations": 0, "cleaned": False, "mutations": 0})

    def pricing(self, client_name, attempt_id):
        quote = matrix_fixture(client_name)["all_in_quote"]
        if client_name != "cli":
            quote["max_model_requests"] = 6
        return {"source": "independent-test-pricing", "max_sessions": 1, "ttl_seconds": 300,
                "cleanup_seconds": 600, "per_call_usd": "0.3",
                "max_cell_usd": "7" if self.defect == "overbudget" else "6",
                "all_in_quote": quote}

    def deployment(self, client_name, attempt_id):
        return {"source": "independent-release-observer", **REVISIONS,
                "connector_revision": "connector-fixture-v1", "oauth_provider_revision": "oauth-fixture-v1"}

    def client_version(self, client_name, attempt_id):
        return {"source": "independent-client-install", "name": client_name, "version": "fixture-v1"}

    def expected(self, fixture_id, attempt_id):
        assert fixture_id == "owner-a"
        return dict(ORACLE)

    def provider_calls(self, fixture_id, attempt_id):
        assert fixture_id == "owner-a"
        return self.state(attempt_id)["calls"]

    def provider_readback(self, fixture_id, attempt_id):
        assert fixture_id == "owner-a"
        return {"source": "independent-jira-provider", "id": ORACLE["issue_id"],
                "key": ORACLE["issue_key"], "summary": (
                    "Modified" if self.state(attempt_id)["mutations"] else ORACLE["sentinel"])}

    def receipt(self, fixture_id, attempt_id):
        assert fixture_id == "owner-a"
        return {"receipt_id": "committed-fixture", "status": "committed",
                "owner_id": "another-owner" if self.defect == "wrong_owner" else ORACLE["owner_id"],
                "vendor_user_id": ORACLE["vendor_user_id"], "connection_id": ORACLE["connection_id"]}

    def mutations(self, attempt_id):
        return self.state(attempt_id)["mutations"]

    def client_invocation(self, client_name, attempt_id):
        return {"source": "independent-client-transcript", "name": client_name,
                "operation": "jira___get_issue", "calls": self.state(attempt_id)["invocations"]}

    def session(self, attempt_id):
        return {"source": "independent-jira-provider", "count": 1,
                "ttl_seconds": 301 if self.defect == "overlong_session" else 300,
                "cleanup_seconds": 601 if self.defect == "overlong_cleanup" else 60}

    def cleaned(self, attempt_id):
        return self.state(attempt_id)["cleaned"]


class Driver:
    isolated = True
    authorized = True
    read_only = True
    identity_qualified = True
    deployed_target = False
    version = "fixture-v1"

    def __init__(self, observer, client_name):
        self.observer = observer
        self.client_name = client_name

    def read(self, fixture_id, cloud_id, issue_id_or_key, attempt_id):
        assert fixture_id == "owner-a" and cloud_id == SITE and issue_id_or_key == "10001"
        self.observer.state(attempt_id)["calls"] += 3
        self.observer.state(attempt_id)["invocations"] += 1
        return {"id": ORACLE["issue_id"], "key": ORACLE["issue_key"],
                "fields": {"summary": "Wrong sentinel" if self.observer.defect == "wrong_issue" else ORACLE["sentinel"]}}

    def deny(self, fixture_id, fault, attempt_id):
        assert fixture_id == "owner-a"
        self.observer.state(attempt_id)["invocations"] += 1
        if self.observer.defect != "extra_dispatch" or fault != "other_site":
            self.observer.state(attempt_id)["calls"] += FAULT_DISPATCHES[fault]
        else:
            self.observer.state(attempt_id)["calls"] += FAULT_DISPATCHES[fault] + 1
        if self.observer.defect == "mutation" and fault == "denied_issue":
            self.observer.state(attempt_id)["mutations"] += 1
        status, reason = {"other_site": (403, "jira_site_denied"), "denied_issue": (404, "jira_not_found"),
                          "missing_scope": (403, "jira_connection_unqualified"), "missing_consent": (403, "tool_not_consented"),
                          "disconnected": (403, "jira_connection_unqualified")}[fault]
        if self.observer.defect == "accepted_denial" and fault == "other_site":
            return {"status": 200, "reason": "ok", "content": ["private"]}
        return {"status": status, "reason": reason, "content": []}

    def cleanup(self, attempt_id):
        self.observer.state(attempt_id)["cleaned"] = self.observer.defect != "unknown_cleanup"


class FixtureClient:
    version = "fixture-v1"

    def __init__(self, command):
        self.command = command

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def evaluate(pattern, tmp_path, monkeypatch, *, observer=None, live=False, cases=runner.SCENARIOS, factory=None):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(pattern, tmp_path, "https://example.invalid", REVISION, GATEWAY_IMAGE,
                              FACADE_IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
                              integration_driver_factory=factory, integration_observer=observer, live=live,
                              scenarios=cases)
    return success, json.loads((tmp_path / "evidence.json").read_text())


def test_jira_ac04_synthetic_three_client_matrix_and_private_evidence(tmp_path, monkeypatch):
    for client in runner.CLIENTS:
        observer = Observer()
        result = jira_ac04(Driver(observer, client), observer, client, REVISIONS)
        assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
        assert result["observations"]["provider_calls"] == 6
        assert ORACLE["sentinel"] not in json.dumps(result)
    observer = Observer()
    success, report = evaluate("SCN-8076-04", tmp_path, monkeypatch, observer=observer,
                               factory=lambda _, client, tool: Driver(observer, client))
    rows = report["scenarios"]
    assert success and len(rows) == 3 and {row["client"] for row in rows} == set(runner.CLIENTS)
    assert all(row["status"] == "pass" and row["evidence_scope"] == "repository-integration" and
               row["issue_number"] == 8076 and row["local_scenario_id"] == "SCN-04" for row in rows)
    assert ORACLE["sentinel"] not in json.dumps(report)


@pytest.mark.parametrize("defect", ("wrong_issue", "wrong_owner", "extra_dispatch", "accepted_denial", "mutation",
                                   "overlong_session", "overlong_cleanup", "unknown_cleanup"))
def test_jira_ac04_rejects_false_success_or_unclean_state(defect):
    observer = Observer(defect)
    result = jira_ac04(Driver(observer, "cli"), observer, "cli", REVISIONS)
    assert result["status"] in {"fail", "blocked"} and result["status"] != "pass"


def test_jira_ac04_unpriced_cell_never_dispatches():
    observer = Observer("overbudget")
    with pytest.raises(FixtureUnavailable, match="pricing"):
        jira_ac04(Driver(observer, "cli"), observer, "cli", REVISIONS)
    assert observer.attempts == {}


def test_jira_ac04_live_requires_private_release_before_client(tmp_path, monkeypatch):
    success, report = evaluate("SCN-8076-04", tmp_path, monkeypatch, live=True,
                               factory=lambda *_: pytest.fail("unverified live driver started"))
    rows = report["scenarios"]
    assert not success and len(rows) == 3 and {row["client"] for row in rows} == set(runner.CLIENTS)
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" for row in rows)


def test_jira_scenarios_remain_blocked_without_qualified_owner(tmp_path, monkeypatch):
    for number in (1, 2, 3):
        success, report = evaluate(f"SCN-8076-0{number}", tmp_path, monkeypatch,
                                   factory=lambda *_: pytest.fail("unqualified Jira call started"))
        assert not success and report["scenarios"][0]["status"] == "blocked"
        assert report["scenarios"][0]["evidence_scope"] == "repository-integration"


def test_jira_ac04_mixed_live_scenarios_refused_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: pytest.fail("mixed selection reached gateway"))
    selection = tuple(case for case in runner.SCENARIOS if case.identifier in {"SCN-8076-01", "SCN-8076-04"})
    success, report = evaluate("SCN-8076-*", tmp_path, monkeypatch, live=True, cases=selection)
    assert not success and report["scenarios"] == []
    assert "SCN-8076-04 must run alone" in report["selection_error"]


class RepositoryObserver(Observer):
    def provider_identity(self, fixture_id, attempt_id):
        assert fixture_id == "owner-a"
        return {"source": "independent-jira-identity", "account_id": "other-user" if self.defect == "wrong_identity" else ORACLE["vendor_user_id"], "scope": "read:me"}

    def shared_token_calls(self, attempt_id):
        return 1 if self.defect == "shared_token" and self.state(attempt_id)["calls"] else 0

    def clock(self, attempt_id):
        return 1000

    def expired_access(self, fixture_id, attempt_id):
        return {"source": "independent-token-lifecycle", "expires_at": 1001 if self.defect == "not_expired" else 999}

    def refresh_count(self, fixture_id, attempt_id):
        return self.state(attempt_id).get("refreshes", 0)

    def connection_removed(self, fixture_id, connection_id, attempt_id):
        assert connection_id == ORACLE["connection_id"]
        return self.state(attempt_id).get("removed", False)


class RepositoryDriver(Driver):
    def refresh_read(self, *, now, **kwargs):
        assert now == 1000
        self.observer.state(kwargs["attempt_id"])["refreshes"] = 0 if self.observer.defect == "refresh_missing" else 1
        return self.read(**kwargs)

    def provider_fault(self, fixture_id, fault, retry_after, attempt_id):
        assert fixture_id == "owner-a" and retry_after == "9"
        status, reason, headers = {
            "unauthorized": (401, "jira_unauthorized", {}), "forbidden": (403, "jira_forbidden", {}),
            "not_found": (404, "jira_not_found", {}), "throttled": (429, "jira_throttled", {"Retry-After": "9"}),
            "api_error": (502, "jira_api_error", {}),
        }[fault]
        self.observer.state(attempt_id)["calls"] += 6 if self.observer.defect == "automatic_retry" else 3
        if fault == "throttled" and self.observer.defect == "lost_retry_guidance":
            headers = {}
        return {"status": status, "reason": reason, "content": [], "headers": headers}

    def disconnect(self, fixture_id, attempt_id):
        self.observer.state(attempt_id)["removed"] = self.observer.defect != "connection_retained"
        return {"status": "disconnected", "revocation_complete": self.observer.defect == "overstated_revocation", "provider_token_revocation": "not_attempted"}


@pytest.mark.parametrize("scenario", ["jira_ac01", "jira_ac02", "jira_ac03"])
def test_jira_repository_selectors_run_and_never_claim_live_qualification(scenario, tmp_path, monkeypatch):
    from tests.e2e.tools import jira_integration

    observer = RepositoryObserver()
    result = getattr(jira_integration, scenario)(RepositoryDriver(observer, "cli"), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert ORACLE["sentinel"] not in json.dumps(result) and ORACLE["vendor_user_id"] not in json.dumps(result)
    selected = tuple(case for case in runner.SCENARIOS if case.action == scenario)
    success, report = evaluate(selected[0].identifier, tmp_path, monkeypatch, observer=observer, cases=selected,
                               factory=lambda *_: RepositoryDriver(observer, "cli"))
    assert success and report["scenarios"][0]["evidence_scope"] == "repository-integration"
    success, report = evaluate(selected[0].identifier, tmp_path / "live", monkeypatch, observer=observer, live=True, cases=selected,
                               factory=lambda *_: pytest.fail("local case must not dispatch in live mode"))
    assert not success and report["scenarios"][0]["status"] == "blocked"


@pytest.mark.parametrize("scenario,defect", [
    ("jira_ac01", "wrong_issue"), ("jira_ac01", "wrong_owner"), ("jira_ac01", "wrong_identity"),
    ("jira_ac01", "shared_token"), ("jira_ac01", "unknown_cleanup"),
    ("jira_ac02", "extra_dispatch"), ("jira_ac02", "accepted_denial"), ("jira_ac02", "mutation"),
    ("jira_ac02", "shared_token"), ("jira_ac03", "not_expired"), ("jira_ac03", "refresh_missing"),
    ("jira_ac03", "lost_retry_guidance"), ("jira_ac03", "automatic_retry"),
    ("jira_ac03", "connection_retained"), ("jira_ac03", "overstated_revocation"),
])
def test_jira_repository_checks_refuse_false_success(scenario, defect):
    from tests.e2e.tools import jira_integration

    observer = RepositoryObserver(defect)
    result = getattr(jira_integration, scenario)(RepositoryDriver(observer, "cli"), observer)
    assert result["status"] in {"fail", "blocked"}


def test_jira_native_matrix_requires_pricing_for_every_model_invocation():
    observer = Observer()
    pricing = observer.pricing

    def underpriced(**kwargs):
        value = pricing(**kwargs)
        value["all_in_quote"]["max_model_requests"] = 1
        return value

    observer.pricing = underpriced
    with pytest.raises(FixtureUnavailable):
        jira_ac04(Driver(observer, "claude-code"), observer, "claude-code", REVISIONS)
    assert not any(state["calls"] for state in observer.attempts.values())


@pytest.mark.parametrize("module_name", ["", "os", "tests.e2e.tools.private_slack_fixture", "tests.e2e.tools.private_jira_../other"])
def test_jira_loader_refuses_unselected_module_without_import(monkeypatch, module_name):
    monkeypatch.setenv("ADP_TOOLS_JIRA_FIXTURE_MODULE", module_name)
    monkeypatch.setattr(runner.importlib, "import_module", lambda *_: pytest.fail("unselected module imported"))
    assert runner.private_jira_bindings() == (None, None)


def test_jira_loader_selects_only_operator_delivered_binding(monkeypatch):
    from types import SimpleNamespace

    driver_factory, observer = object(), object()
    monkeypatch.setenv("ADP_TOOLS_JIRA_FIXTURE_MODULE", "tests.e2e.tools.private_jira_fixture")
    loaded = []

    def load(name):
        loaded.append(name)
        return SimpleNamespace(driver_factory=driver_factory, observer=observer)

    monkeypatch.setattr(runner.importlib, "import_module", load)
    assert runner.private_jira_bindings() == (driver_factory, observer)
    assert loaded == ["tests.e2e.tools.private_jira_fixture"]
