"""Synthetic GitHub evidence; no provider connection is made by these tests."""

import json

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.github_integration import github_ac01
from tests.e2e.tools.scenarios import FixtureUnavailable
from tests.e2e.tools.test_runner import FixtureClient, GATEWAY_IMAGE, IMAGE, SHA


class Observer:
    def __init__(self):
        self.calls = 0
        self.identity = None
        self.receipt_record = None
        self.was_cleaned = False

    def expected(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "github-connection-a", "account_id": "213", "scope": "read:user"}

    def provider_calls(self, fixture_id, attempt_id):
        return self.calls

    def provider_identity(self, fixture_id, attempt_id):
        return self.identity

    def receipt(self, fixture_id, attempt_id):
        return self.receipt_record

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
        self.observer.calls += 2 if self.defect == "duplicate_call" else 1
        self.observer.identity = {"source": "independent-oauth-provider",
                                  "account_id": "999" if self.defect == "wrong_provider" else expected["account_id"],
                                  "scope": expected["scope"]}
        self.observer.receipt_record = {"receipt_id": "receipt-a", "status": "pending" if self.defect == "pending_receipt" else "committed"}
        return {"id": True if self.defect == "boolean_id" else 999 if self.defect == "wrong_user" else 213,
                "connection_id": expected["connection_id"], "receipt_id": "receipt-a"}

    def cleanup(self, attempt_id):
        self.observer.was_cleaned = True


def test_github_ac01_matches_independent_numeric_identity_and_committed_receipt(tmp_path, monkeypatch):
    observer = Observer()
    assert github_ac01(Driver(observer), observer)["status"] == "pass"
    assert observer.calls == 1 and observer.was_cleaned
    observer = Observer()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8075-01", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: Driver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and row["status"] == "pass" and row["issue_number"] == 8075
    assert row["local_scenario_id"] == "SCN-01" and row["evidence_scope"] == "repository-integration"
    assert row["observations"] == {"source": "independent-oauth-provider", "provider_calls": 1,
                                   "receipt": "committed", "user_id_match": True}


@pytest.mark.parametrize("defect", ("duplicate_call", "wrong_provider", "pending_receipt", "boolean_id", "wrong_user"))
def test_github_ac01_rejects_false_identity_receipt_or_provider_counts(defect):
    observer = Observer()
    result = github_ac01(Driver(observer, defect), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_github_ac01_requires_isolated_fixture_and_numeric_provider_id(tmp_path, monkeypatch):
    with pytest.raises(FixtureUnavailable):
        github_ac01(None, Observer())
    observer = Observer()
    observer.expected = lambda **kwargs: {**Observer().expected(**kwargs), "account_id": "username"}
    assert github_ac01(Driver(observer), observer)["status"] == "fail"
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8075-01", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"


class DenialObserver(Observer):
    def __init__(self):
        super().__init__()
        self.provider_counts = {"owner-a": 0, "owner-b": 0}
        self.broker_counts = {"owner-a": 0, "owner-b": 0}
        self.pat_counts = {"owner-a": 0, "owner-b": 0}

    def expected(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "connection-" + fixture_id,
                "account_id": "213" if fixture_id == "owner-a" else "214", "scope": "read:user"}

    def provider_calls(self, fixture_id, attempt_id):
        return self.provider_counts[fixture_id]

    def broker_calls(self, fixture_id, attempt_id):
        return self.broker_counts[fixture_id]

    def pat_calls(self, fixture_id, attempt_id):
        return self.pat_counts[fixture_id]


class DenialDriver(Driver):
    def probe(self, fixture_id, foreign_fixture_id, fault, attempt_id):
        if self.defect == "provider:" + fault:
            self.observer.provider_counts[fixture_id] += 1
        if self.defect == "broker:" + fault:
            self.observer.broker_counts[fixture_id] += 1
        if self.defect == "pat:" + fault:
            self.observer.pat_counts[fixture_id] += 1
        if self.defect == "foreign:" + fault:
            self.observer.provider_counts[foreign_fixture_id] += 1
        reason = {
            "wrong_user": "GitHub connection unqualified",
            "missing_scope": "scope_not_granted",
            "withdrawn_oauth_consent": "GitHub connection unqualified",
            "withdrawn_tool_consent": "tool_not_consented",
            "disconnected_with_pat": "GitHub connection unqualified",
        }[fault]
        if self.defect == "reason:" + fault:
            reason = "tool_undeployed"
        return {"status": 200 if self.defect == "accepted:" + fault else 403,
                "reason": reason, "content": []}


def test_github_ac02_denies_isolated_owners_without_provider_broker_or_pat_dispatch(tmp_path, monkeypatch):
    from tests.e2e.tools.github_integration import github_ac02

    observer = DenialObserver()
    result = github_ac02(DenialDriver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert result["observations"] == {"source": "independent-oauth-provider", "owners": 2,
                                      "denials": 10, "provider_calls": 0, "broker_calls": 0, "pat_calls": 0}
    observer = DenialObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8075-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: DenialDriver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and row["status"] == "pass" and row["issue_number"] == 8075
    assert row["local_scenario_id"] == "SCN-02" and row["evidence_scope"] == "repository-integration"


@pytest.mark.parametrize("fault", ("wrong_user", "missing_scope", "withdrawn_oauth_consent",
                                   "withdrawn_tool_consent", "disconnected_with_pat"))
@pytest.mark.parametrize("defect", ("provider", "broker", "pat", "foreign", "reason", "accepted"))
def test_github_ac02_rejects_each_dispatch_or_false_denial(defect, fault):
    from tests.e2e.tools.github_integration import github_ac02

    observer = DenialObserver()
    result = github_ac02(DenialDriver(observer, defect + ":" + fault), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_github_ac02_missing_private_fixture_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8075-02", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked" and row["cleanup"] == {"status": "not-run"}


class LifecycleObserver(DenialObserver):
    def __init__(self):
        super().__init__()
        self.refreshes = {"owner-a": 0, "owner-b": 0}
        self.identities = {}
        self.receipts = {}
        self.removed = set()

    def expected(self, fixture_id, attempt_id):
        return {**super().expected(fixture_id, attempt_id), "expired_at": 1000}

    def clock(self, attempt_id):
        return 1001

    def provider_identity(self, fixture_id, attempt_id):
        return self.identities.get(fixture_id)

    def receipt(self, fixture_id, attempt_id):
        return self.receipts.get(fixture_id)

    def refresh_count(self, fixture_id, attempt_id):
        return self.refreshes[fixture_id]

    def connection_removed(self, fixture_id, connection_id, attempt_id):
        return (fixture_id, connection_id) in self.removed

    def pat_grace(self, fixture_id, attempt_id):
        return {"source": "independent-adp-audit", "started_at": 1000, "grace_seconds": 30 * 86400}

    def pat_denial_audited(self, fixture_id, at, attempt_id):
        return at == 1000 + 30 * 86400


class LifecycleDriver(DenialDriver):
    def refresh_read(self, fixture_id, now, attempt_id):
        assert now == self.observer.clock(attempt_id)
        expected = self.observer.expected(fixture_id, attempt_id)
        self.observer.provider_counts[fixture_id] += 0 if self.defect == "no_provider_read" else 1
        self.observer.refreshes[fixture_id] += 0 if self.defect == "skipped_refresh" else 1
        self.observer.identities[fixture_id] = {
            "source": "independent-oauth-provider",
            "account_id": "999" if self.defect == "foreign_profile" else expected["account_id"],
            "scope": "read:user",
        }
        self.observer.receipts[fixture_id] = {"receipt_id": "receipt-" + fixture_id, "status": "committed"}
        return {"id": 999 if self.defect == "wrong_refresh_id" else int(expected["account_id"]),
                "connection_id": expected["connection_id"], "receipt_id": "receipt-" + fixture_id}

    def disconnect(self, fixture_id, attempt_id):
        expected = self.observer.expected(fixture_id, attempt_id)
        self.observer.removed.add((fixture_id, expected["connection_id"]))
        return {"status": "disconnected", "revocation_complete": self.defect == "revocation_overstated",
                "provider_token_revocation": "not_attempted"}

    def read_after_disconnect(self, fixture_id, attempt_id):
        if self.defect == "continued_after_disconnect":
            self.observer.provider_counts[fixture_id] += 1
        return {"status": 403, "reason": "GitHub connection unqualified", "content": []}

    def explicit_legacy_pat_read(self, fixture_id, at, attempt_id):
        deadline = 1000 + 30 * 86400
        if at < deadline:
            if self.defect != "early_pat_denial":
                self.observer.pat_counts[fixture_id] += 1
            return ({"status": 403, "reason": "github_pat_retired"} if self.defect == "early_pat_denial"
                    else {"status": 200, "channel": "explicit-legacy-pat"})
        if self.defect == "extra_pat_call":
            self.observer.pat_counts[fixture_id] += 1
        return ({"status": 200, "channel": "explicit-legacy-pat"} if self.defect == "late_pat_acceptance"
                else {"status": 403, "reason": "github_pat_retired"})


def test_github_ac03_refresh_disconnect_and_explicit_pat_boundary(tmp_path, monkeypatch):
    from tests.e2e.tools.github_integration import github_ac03

    observer = LifecycleObserver()
    result = github_ac03(LifecycleDriver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert result["observations"]["revocation_complete"] is False
    assert observer.refreshes == observer.provider_counts == observer.pat_counts == {"owner-a": 1, "owner-b": 1}
    observer = LifecycleObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(
        "SCN-8075-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: LifecycleDriver(observer), integration_observer=observer,
    )
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and row["status"] == "pass" and row["issue_number"] == 8075
    assert row["local_scenario_id"] == "SCN-03" and row["evidence_scope"] == "repository-integration"
    assert row["observations"]["pat_expiry"] == "denied-and-audited"


@pytest.mark.parametrize("defect", ("wrong_refresh_id", "foreign_profile", "skipped_refresh",
                                   "no_provider_read", "continued_after_disconnect", "revocation_overstated",
                                   "early_pat_denial", "late_pat_acceptance", "extra_pat_call"))
def test_github_ac03_rejects_refresh_disconnect_or_pat_boundary_defects(defect):
    from tests.e2e.tools.github_integration import github_ac03

    observer = LifecycleObserver()
    result = github_ac03(LifecycleDriver(observer, defect), observer)
    assert result["status"] == "fail" and result["cleanup"] == {"status": "confirmed"}


def test_github_ac03_requires_private_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8075-03", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"


class MatrixObserver:
    deployed_target = False

    def __init__(self, defect=None):
        self.defect = defect
        self.attempts = {}

    def state(self, attempt_id):
        return self.attempts.setdefault(attempt_id, {"calls": 0, "refreshes": 0, "mutations": 0, "invocations": 0,
                                                      "identity": None, "receipt": None,
                                                      "removed": False, "cleaned": False})

    def pricing(self, client_name, attempt_id):
        from datetime import UTC, datetime, timedelta

        from tests.e2e.tools.test_client_matrix import matrix_fixture

        quote = matrix_fixture(client_name)["all_in_quote"]
        if client_name != "cli":
            quote["max_model_requests"] = 1 if self.defect == "underpriced_model" else 9
        if self.defect == "stale_quote":
            quote["effective_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        return {"source": "independent-test-pricing", "max_sessions": 1, "ttl_seconds": 300,
                "cleanup_seconds": 600, "per_call_usd": "0.3",
                "max_cell_usd": "7" if self.defect == "overbudget" else "6", "all_in_quote": quote}

    def deployment(self, client_name, attempt_id):
        return {"source": "independent-release-observer",
                "gateway": "wrong-gateway" if self.defect == "wrong_release" else SHA,
                "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE,
                "connector_revision": "connector-fixture-v1", "oauth_provider_revision": "provider-fixture-v1"}

    def client_version(self, client_name, attempt_id):
        return {"source": "independent-client-install", "name": client_name,
                "version": "wrong-client" if self.defect == "wrong_client" else "fixture-v1"}

    def expected(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "github-connection-a", "account_id": "213", "scope": "read:user"}

    def provider_calls(self, fixture_id, attempt_id):
        return self.state(attempt_id)["calls"]

    def provider_identity(self, fixture_id, attempt_id):
        return self.state(attempt_id)["identity"]

    def receipt(self, fixture_id, attempt_id):
        return self.state(attempt_id)["receipt"]

    def refresh_count(self, fixture_id, attempt_id):
        return self.state(attempt_id)["refreshes"]

    def repo_mutations(self, attempt_id):
        return self.state(attempt_id)["mutations"]

    def client_invocation(self, client_name, attempt_id):
        return {"source": "independent-client-transcript", "name": client_name,
                "operation": "github___get_user", "calls": self.state(attempt_id)["invocations"]}

    def connection_removed(self, fixture_id, connection_id, attempt_id):
        return self.state(attempt_id)["removed"] and connection_id == "github-connection-a"

    def session(self, attempt_id):
        return {"source": "independent-oauth-provider", "count": 1,
                "ttl_seconds": 301 if self.defect == "overlong_session" else 300,
                "cleanup_seconds": 601 if self.defect == "overlong_cleanup" else 60}

    def cleaned(self, attempt_id):
        return self.state(attempt_id)["cleaned"]


class MatrixDriver:
    isolated = True
    authorized = True
    read_only = True
    deployed_target = False
    version = "fixture-v1"

    def __init__(self, observer, client_name):
        self.observer = observer
        self.client_name = client_name

    def call(self, fixture_id, operation, attempt_id):
        assert fixture_id == "owner-a" and operation == "github___get_user"
        state = self.observer.state(attempt_id)
        state["calls"] += 1
        state["invocations"] += int(self.observer.defect != "direct_gateway_call")
        state["identity"] = {"source": "independent-oauth-provider", "account_id": "213", "scope": "read:user"}
        state["receipt"] = {"receipt_id": "receipt-fixture", "status": "committed"}
        state["mutations"] += int(self.observer.defect == "repo_mutation")
        return {"id": 999 if self.observer.defect == "wrong_user" else 213,
                "connection_id": "github-connection-a", "receipt_id": "receipt-fixture"}

    def refresh_read(self, fixture_id, operation, attempt_id):
        assert fixture_id == "owner-a" and operation == "github___get_user"
        state = self.observer.state(attempt_id)
        state["calls"] += 1
        state["invocations"] += 1
        state["refreshes"] += int(self.observer.defect != "no_refresh")
        if self.observer.defect == "changed_provider":
            state["identity"] = {"source": "independent-oauth-provider", "account_id": "999", "scope": "read:user"}
        return {"id": 999 if self.observer.defect == "wrong_refresh_id" else 213}

    def probe_wrong_user(self, fixture_id, attempt_id):
        self.observer.state(attempt_id)["invocations"] += 1
        if self.observer.defect == "wrong_user_dispatch":
            self.observer.state(attempt_id)["calls"] += 1
        return {"status": 200 if self.observer.defect == "accepted_wrong_user" else 403,
                "reason": "GitHub connection unqualified", "content": []}

    def disconnect(self, fixture_id, attempt_id):
        self.observer.state(attempt_id)["removed"] = True
        return {"status": "disconnected", "revocation_complete": False,
                "provider_token_revocation": "not_attempted"}

    def read_after_disconnect(self, fixture_id, attempt_id):
        self.observer.state(attempt_id)["invocations"] += 1
        if self.observer.defect == "post_disconnect_dispatch":
            self.observer.state(attempt_id)["calls"] += 1
        return {"status": 403, "reason": "GitHub connection unqualified", "content": []}

    def cleanup(self, attempt_id):
        self.observer.state(attempt_id)["cleaned"] = self.observer.defect != "unknown_cleanup"


def test_github_ac04_three_client_read_only_matrix(tmp_path, monkeypatch):
    from tests.e2e.tools.github_integration import github_ac04

    revisions = {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE}
    for client_name in runner.CLIENTS:
        observer = MatrixObserver()
        result = github_ac04(MatrixDriver(observer, client_name), observer, client_name, revisions)
        assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
        assert result["observations"]["repo_mutations"] == 0
        assert result["observations"]["provider_calls"] == 2
    observer = MatrixObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8075-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
                              integration_driver_factory=lambda _, client_name, tool: MatrixDriver(observer, client_name),
                              integration_observer=observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert success and len(rows) == 3 and {row["client"] for row in rows} == set(runner.CLIENTS)
    assert all(row["status"] == "pass" and row["issue_number"] == 8075 and
               row["local_scenario_id"] == "SCN-04" and row["evidence_scope"] == "repository-integration"
               for row in rows)
    assert len(observer.attempts) == 3


@pytest.mark.parametrize("defect", ("wrong_release", "wrong_client", "wrong_user", "repo_mutation",
                                   "no_refresh", "wrong_refresh_id", "changed_provider", "wrong_user_dispatch",
                                   "accepted_wrong_user", "post_disconnect_dispatch", "direct_gateway_call", "overlong_session",
                                   "overlong_cleanup", "unknown_cleanup"))
def test_github_ac04_rejects_client_lifecycle_or_repository_mutation(defect):
    from tests.e2e.tools.github_integration import github_ac04

    observer = MatrixObserver(defect)
    result = github_ac04(MatrixDriver(observer, "cli"), observer, "cli",
                         {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE})
    assert result["status"] in {"fail", "blocked"} and result["status"] != "pass"


@pytest.mark.parametrize("defect", ("stale_quote", "overbudget", "underpriced_model"))
def test_github_ac04_unpriced_cell_does_not_dispatch(defect):
    from tests.e2e.tools.github_integration import github_ac04

    client_name = "codex" if defect == "underpriced_model" else "cli"
    observer = MatrixObserver(defect)
    with pytest.raises(FixtureUnavailable, match="pricing"):
        github_ac04(MatrixDriver(observer, client_name), observer, client_name,
                    {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE})
    assert not observer.attempts


def test_github_ac04_live_requires_protected_release_and_private_observer(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8075-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=FixtureClient,
                              integration_driver_factory=lambda *_: pytest.fail("no observer; do not launch a client"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3 and {row["client"] for row in rows} == set(runner.CLIENTS)
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" for row in rows)


def test_github_ac04_live_mixed_scenarios_refused_before_any_network(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: pytest.fail("mixed live selection reached the gateway"))
    selection = tuple(case for case in runner.SCENARIOS
                      if case.identifier in {"SCN-8075-01", "SCN-8075-04"})
    success = runner.evaluate("SCN-8075-*", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              scenarios=selection, client_factory=FixtureClient)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not success and not report["scenarios"]
    assert "SCN-8075-04 must run alone" in report["selection_error"]
