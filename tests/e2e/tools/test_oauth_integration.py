"""Independent synthetic observations for the private OAuth scenario."""

import json

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.oauth_integration import oauth_ac01
from tests.e2e.tools.scenarios import FixtureUnavailable
from tests.e2e.tools.test_runner import FixtureClient, GATEWAY_IMAGE, IMAGE, SHA


class Observer:
    def __init__(self):
        self.calls = {"owner-a": 0, "owner-b": 0}
        self.identities = {}
        self.was_cleaned = False

    def expected(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "connection-" + fixture_id, "account_id": "account-" + fixture_id,
                "scope": "read:user"}

    def provider_calls(self, fixture_id, attempt_id):
        return self.calls[fixture_id]

    def provider_identity(self, fixture_id, attempt_id):
        return self.identities[fixture_id]

    def cleaned(self, attempt_id):
        return self.was_cleaned


class Driver:
    isolated = True
    authorized = True

    def __init__(self, observer, defect=None):
        self.observer = observer
        self.defect = defect

    def connect(self, fixture_id, attempt_id):
        expected = self.observer.expected(fixture_id, attempt_id)
        return {"connection_id": expected["connection_id"], "owner_id": expected["owner_id"]}

    def call(self, fixture_id, attempt_id):
        expected = self.observer.expected(fixture_id, attempt_id)
        other = "owner-b" if fixture_id == "owner-a" else "owner-a"
        observed = self.observer.expected(other if self.defect == "shared_token" else fixture_id, attempt_id)
        self.observer.calls[fixture_id] += 1
        self.observer.identities[fixture_id] = {
            "source": "independent-oauth-provider", "account_id": observed["account_id"],
            "scope": "write:user" if self.defect == "scope_drift" else observed["scope"],
        }
        return {"account_id": expected["account_id"], "connection_id": expected["connection_id"]}

    def probe(self, fixture_id, foreign_fixture_id, fault, attempt_id):
        if self.defect == "dispatched_denial" or self.defect == "dispatched_" + fault:
            self.observer.calls[fixture_id] += 1
        if self.defect == "cross_owner_" + fault:
            self.observer.calls[foreign_fixture_id] += 1
        if self.defect == "accepted_" + fault:
            return {"status": 200, "content": []}
        return {"status": 403, "content": []}

    def cleanup(self, attempt_id):
        self.observer.was_cleaned = True


def test_oauth_ac01_two_owners_and_independent_provider_oracle(tmp_path, monkeypatch):
    observer = Observer()
    result = oauth_ac01(Driver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert result["observations"]["denial_controls"] == 8
    assert observer.calls == {"owner-a": 1, "owner-b": 1}
    observer = Observer()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8074-01", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: Driver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["issue_number"] == 8074 and row["local_scenario_id"] == "SCN-01"


@pytest.mark.parametrize("defect", ("shared_token", "scope_drift", "dispatched_denial"))
def test_oauth_ac01_rejects_provider_drift_and_denial_dispatch(defect):
    observer = Observer()
    result = oauth_ac01(Driver(observer, defect), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


@pytest.mark.parametrize("fault", ("foreign_state", "foreign_session", "other_user_token", "replayed_receipt"))
@pytest.mark.parametrize("defect", ("accepted", "dispatched", "cross_owner"))
def test_oauth_ac01_rejects_each_fault_even_when_the_other_owner_is_dispatched(fault, defect):
    observer = Observer()
    result = oauth_ac01(Driver(observer, defect + "_" + fault), observer)
    assert result["status"] == "fail"
    assert result["cleanup"] == {"status": "confirmed"}


def test_oauth_ac01_missing_consent_or_observer_blocks(tmp_path, monkeypatch):
    with pytest.raises(FixtureUnavailable):
        oauth_ac01(None, Observer())
    observer = Observer()
    driver = Driver(observer)
    driver.authorized = False
    with pytest.raises(FixtureUnavailable):
        oauth_ac01(driver, observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8074-01", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked" and row["cleanup"]["status"] == "not-run"


class RefreshObserver(Observer):
    def __init__(self):
        super().__init__()
        self.refreshes = {"owner-a": 0, "owner-b": 0}

    def clock(self, attempt_id):
        return 1001

    def expected(self, fixture_id, attempt_id):
        return {**super().expected(fixture_id, attempt_id), "expires_at": 1000}

    def refresh_count(self, fixture_id, attempt_id):
        return self.refreshes[fixture_id]


class RefreshDriver(Driver):
    def call_expired(self, fixture_id, attempt_id, now):
        assert now == self.observer.clock(attempt_id)
        if self.defect != "missing_refresh":
            self.observer.refreshes[fixture_id] += 1
        result = super().call(fixture_id, attempt_id)
        return {"connection_id": result["connection_id"], "account_id": result["account_id"]}

    def probe_scope_change(self, fixture_id, attempt_id, scope):
        assert scope == "repo"
        if self.defect == "scope_change_dispatch":
            foreign = "owner-b" if fixture_id == "owner-a" else "owner-a"
            self.observer.calls[foreign] += 1
        if self.defect == "scope_change_refresh":
            self.observer.refreshes[fixture_id] += 1
        if self.defect == "scope_change_accepted":
            return {"status": 200, "content": []}
        return {"status": 403, "content": []}


def test_oauth_ac02_expired_token_refresh_and_scope_change(tmp_path, monkeypatch):
    from tests.e2e.tools.oauth_integration import oauth_ac02

    observer = RefreshObserver()
    result = oauth_ac02(RefreshDriver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert result["observations"] == {"source": "independent-oauth-provider", "owners": 2,
                                      "refreshes": 2, "provider_calls": 2, "scope_change_denials": 2}
    assert observer.calls == observer.refreshes == {"owner-a": 1, "owner-b": 1}
    observer = RefreshObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8074-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: RefreshDriver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["issue_number"] == 8074 and row["local_scenario_id"] == "SCN-02"


@pytest.mark.parametrize("defect", (
    "shared_token", "scope_drift", "missing_refresh", "scope_change_dispatch",
    "scope_change_refresh", "scope_change_accepted",
))
def test_oauth_ac02_rejects_wrong_refresh_or_scope_change(defect):
    from tests.e2e.tools.oauth_integration import oauth_ac02

    observer = RefreshObserver()
    result = oauth_ac02(RefreshDriver(observer, defect), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_oauth_ac02_requires_qualified_fixture_and_cannot_qualify_live(tmp_path, monkeypatch):
    from tests.e2e.tools.oauth_integration import oauth_ac02

    with pytest.raises(FixtureUnavailable):
        oauth_ac02(None, RefreshObserver())
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8074-02", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked" and row["cleanup"]["status"] == "not-run"
    runner.evaluate("SCN-8074-02", tmp_path, "https://example.invalid", SHA,
                    GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
                    integration_driver_factory=lambda *_: pytest.fail("local driver must not run as live"), live=True)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert row["status"] == "blocked" and row["evidence_scope"] == "repository-integration"


class RevocationObserver(Observer):
    def __init__(self):
        super().__init__()
        self.tokens = {"owner-a": True, "owner-b": True}
        self.connections = {owner: "connection-" + owner for owner in self.tokens}

    def token_active(self, fixture_id, attempt_id):
        return self.tokens[fixture_id]

    def connection_removed(self, fixture_id, connection_id, attempt_id):
        return self.connections[fixture_id] is None and connection_id == "connection-" + fixture_id


class RevocationDriver(Driver):
    def disconnect(self, fixture_id, attempt_id):
        if self.defect != "pointer_retained":
            self.observer.connections[fixture_id] = None
        if self.defect == "vendor_revoked":
            self.observer.tokens[fixture_id] = False
        response = {"status": "disconnected", "identity_token_revoked": False,
                    "provider_token_revocation": "not_attempted", "revocation_complete": False,
                    "provider_access_token_expires_at": None, "private_assertion_max_seconds": 30}
        if self.defect == "claimed_revocation":
            response["revocation_complete"] = True
        return response

    def call_after_disconnect(self, fixture_id, attempt_id):
        if self.defect == "post_disconnect_dispatch":
            self.observer.calls[fixture_id] += 1
        return {"status": 403, "content": []}

    def cleanup(self, attempt_id):
        if self.defect != "cleanup_unknown":
            super().cleanup(attempt_id)


def test_oauth_ac03_reports_independently_active_vendor_token_not_false_revocation(tmp_path, monkeypatch):
    from tests.e2e.tools.oauth_integration import oauth_ac03

    observer = RevocationObserver()
    result = oauth_ac03(RevocationDriver(observer), observer)
    assert result["status"] == "blocked"
    assert result["cleanup"] == {"status": "confirmed"}
    assert result["observations"] == {"source": "independent-oauth-provider", "owners": 2,
                                      "new_adp_calls_denied": 2, "new_provider_calls": 0,
                                      "provider_token_active": True, "revocation_complete": False}
    assert observer.calls == {"owner-a": 0, "owner-b": 0}
    assert observer.tokens == {"owner-a": True, "owner-b": True}
    observer = RevocationObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8074-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: RevocationDriver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"
    assert row["evidence_scope"] == "repository-integration" and row["local_scenario_id"] == "SCN-03"
    assert row["issue_number"] == 8074 and "remains active" in row["reason"]


@pytest.mark.parametrize("defect", ("claimed_revocation", "post_disconnect_dispatch", "vendor_revoked", "pointer_retained"))
def test_oauth_ac03_refuses_false_revocation_and_vendor_dispatch(defect):
    from tests.e2e.tools.oauth_integration import oauth_ac03

    observer = RevocationObserver()
    result = oauth_ac03(RevocationDriver(observer, defect), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_oauth_ac03_missing_fixture_or_unconfirmed_cleanup_remains_blocked(tmp_path, monkeypatch):
    from tests.e2e.tools.oauth_integration import oauth_ac03

    with pytest.raises(FixtureUnavailable):
        oauth_ac03(None, RevocationObserver())
    observer = RevocationObserver()
    result = oauth_ac03(RevocationDriver(observer, "cleanup_unknown"), observer)
    assert result["status"] == "blocked" and result["cleanup"]["status"] == "unknown"
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8074-03", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked" and row["cleanup"]["status"] == "not-run"


class LifecycleObserver(RefreshObserver):
    def __init__(self, defect=None):
        super().__init__()
        self.defect = defect
        self.tokens = {"owner-a": True, "owner-b": True}

    def pricing(self, client_name, attempt_id):
        from datetime import UTC, datetime, timedelta

        from tests.e2e.tools.test_client_matrix import matrix_fixture

        quote = matrix_fixture(client_name)["all_in_quote"]
        if client_name != "cli":
            quote["max_model_requests"] = 8 if self.defect == "underpriced_model" else 9
        if self.defect == "stale_quote":
            quote["effective_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        return {"source": "independent-test-pricing", "max_sessions": 1, "ttl_seconds": 300,
                "cleanup_seconds": 600, "per_call_usd": "0.3",
                "max_cell_usd": "7" if self.defect == "exceeds_run_budget" else "6", "all_in_quote": quote}

    def deployment(self, client_name, attempt_id):
        return {"source": "independent-release-observer",
                "gateway": "different" if self.defect == "changed_gateway" else SHA,
                "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE,
                "app_revisions": {vendor: "fixture-revision-" + vendor for vendor in ("github", "atlassian", "slack")}}

    def client_version(self, client_name, attempt_id):
        return {"source": "independent-client-install", "name": client_name,
                "version": "changed" if self.defect == "changed_client" else "fixture-v1"}

    def token_active(self, fixture_id, attempt_id):
        return self.tokens[fixture_id]

    def session(self, attempt_id):
        return {"source": "independent-oauth-provider", "count": 1,
                "ttl_seconds": 301 if self.defect == "overlong_session" else 300,
                "cleanup_seconds": 30}


class LifecycleDriver(RefreshDriver):
    version = "fixture-v1"
    deployed_target = False

    def __init__(self, observer, client_name):
        super().__init__(observer, observer.defect)
        self.client_name = client_name

    def connect(self, fixture_id, attempt_id):
        self.observer.calls[fixture_id] += 1
        return super().connect(fixture_id, attempt_id)

    def disconnect(self, fixture_id, attempt_id):
        response = {"status": "disconnected", "identity_token_revoked": False,
                    "provider_token_revocation": "not_attempted", "revocation_complete": False,
                    "provider_access_token_expires_at": None, "private_assertion_max_seconds": 30}
        if self.defect == "false_revocation":
            response["revocation_complete"] = True
        return response

    def call_after_disconnect(self, fixture_id, attempt_id):
        if self.defect == "post_disconnect_dispatch":
            self.observer.calls[fixture_id] += 1
        return {"status": 403, "content": []}

    def cleanup(self, attempt_id):
        if self.defect != "unconfirmed_cleanup":
            super().cleanup(attempt_id)


def test_oauth_ac04_synthetic_clients_remain_blocked_on_unrevoked_provider_token(tmp_path, monkeypatch):
    from tests.e2e.tools.oauth_integration import oauth_ac04

    revisions = {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE}
    for client_name in runner.CLIENTS:
        observer = LifecycleObserver()
        result = oauth_ac04(LifecycleDriver(observer, client_name), observer, client_name, revisions)
        assert result["status"] == "blocked" and result["cleanup"]["status"] == "confirmed"
        assert "provider token remains active" in result["reason"]
        assert result["observations"]["provider_calls"] == 5
        assert result["observations"]["new_provider_calls_after_disconnect"] == 0
        assert observer.calls == {"owner-a": 3, "owner-b": 2}
        assert "fixture-revision-" not in str(result)
    observer = LifecycleObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8074-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
                              integration_driver_factory=lambda _, client_name, tool: LifecycleDriver(observer, client_name),
                              integration_observer=observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == len(runner.CLIENTS)
    assert {row["client"] for row in rows} == set(runner.CLIENTS)
    assert all(row["status"] == "blocked" and row["cleanup"]["status"] == "confirmed" and
               row["evidence_scope"] == "repository-integration" and row["issue_number"] == 8074 and
               row["local_scenario_id"] == "SCN-04" for row in rows)


@pytest.mark.parametrize("defect", ("changed_gateway", "changed_client", "missing_refresh", "shared_token",
                                    "overlong_session", "false_revocation", "post_disconnect_dispatch"))
def test_oauth_ac04_rejects_unverified_lifecycle(defect):
    from tests.e2e.tools.oauth_integration import oauth_ac04

    observer = LifecycleObserver(defect)
    result = oauth_ac04(LifecycleDriver(observer, "cli"), observer, "cli",
                        {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE})
    assert result["status"] == "fail" and result["cleanup"]["status"] == "confirmed"


@pytest.mark.parametrize("defect", ("exceeds_run_budget", "stale_quote", "underpriced_model"))
def test_oauth_ac04_refuses_unpriced_work_before_provider_dispatch(defect):
    from tests.e2e.tools.oauth_integration import oauth_ac04

    observer = LifecycleObserver(defect)
    client_name = "codex" if defect == "underpriced_model" else "cli"
    with pytest.raises(FixtureUnavailable, match="pricing"):
        oauth_ac04(LifecycleDriver(observer, client_name), observer, client_name,
                  {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE})
    assert observer.calls == {"owner-a": 0, "owner-b": 0}


def test_oauth_ac04_live_requires_private_deployment_and_isolated_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8074-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=FixtureClient,
                              integration_driver_factory=lambda *_: pytest.fail("no protected observer; do not start a driver"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" and
               row["cleanup"]["status"] == "not-run" for row in rows)
    success = runner.evaluate("SCN-8074-*", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=FixtureClient)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not success and report["scenarios"] == []
    assert "SCN-8074-04 must run alone" in report["selection_error"]
