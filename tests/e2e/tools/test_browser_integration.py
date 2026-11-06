"""Exercise browser denial selection without granting live Browser authority."""

import base64
import hashlib
import json
from uuid import uuid4

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.browser_integration import DENIALS, browser_ac02, browser_ac03, browser_ac04
from tests.e2e.tools.scenarios import FixtureUnavailable

SHA = "a" * 40
IMAGE = "example.invalid/facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/gateway@sha256:" + "c" * 64


class Observer:
    def __init__(self):
        self.counts = {"network_requests": 1, "browser_actions": 0, "mutations": 0}
        self.closed = False

    def snapshot(self, *, attempt_id):
        return {"source": "independent-browser-provider", **self.counts}

    def terminated(self, *, attempt_id):
        return self.closed


class Driver:
    isolated = authorized = inert_https = True

    def __init__(self, observer):
        self.observer = observer
        self.steps = []
        self.revoked = False

    def open_owned(self, *, attempt_id):
        return {"session_id": "owned-session", "view_id": "observed-view", "candidate_id": "owned-candidate"}

    def revoke_owned(self, *, attempt_id):
        self.revoked = True

    def step_denied(self, case, opened, *, attempt_id):
        assert opened["view_id"] == "observed-view"
        assert not self.revoked or case == "revoked_access"
        self.steps.append(case)
        return {"status": "rejected", "error": DENIALS[case]}

    def close_owned(self, *, attempt_id):
        self.observer.closed = True


@pytest.mark.parametrize("distortion", ["error", "network_requests", "browser_actions", "mutations", "cleanup"])
def test_browser_denial_scenario_fails_on_misclassification_or_side_effect(distortion):
    observer = Observer()
    driver = Driver(observer)
    if distortion == "error":
        driver.step_denied = lambda case, opened, *, attempt_id: {"status": "rejected", "error": "unknown"}
    elif distortion == "cleanup":
        driver.close_owned = lambda *, attempt_id: None
    else:
        original = driver.step_denied

        def effect(case, opened, *, attempt_id):
            observer.counts[distortion] += 1
            return original(case, opened, attempt_id=attempt_id)

        driver.step_denied = effect
    evidence = browser_ac02(driver, observer)
    assert evidence["status"] != "pass"
    assert evidence["cleanup"]["status"] == ("unknown" if distortion == "cleanup" else "confirmed")


def test_browser_denial_selector_records_independent_counters_and_cleanup(tmp_path, monkeypatch):
    observer = Observer()
    driver = Driver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "8101")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_SHA", SHA)
    passed = runner.evaluate("SCN-8071-02", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                             client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                               "__exit__": lambda self, *args: None, "version": "test"})(),
                             integration_driver_factory=lambda *args: driver, integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    row, = report["scenarios"]
    assert passed and report["live_attestation"] is False
    assert row["scenario_id"] == "SCN-8071-02" and row["tags"] == [
        "@scenario SCN-8071-02", "@story 8071", "@acceptance AC-02"]
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["denials"] == DENIALS and driver.steps == list(DENIALS)
    assert row["cleanup"] == {"status": "confirmed", "provider_termination": True}
    assert report["github_run_id"] == "8101" and report["github_run_attempt"] == "2"
    assert report["harness_revision"] == SHA


def test_browser_denial_selector_does_not_run_fixture_in_live_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    success = runner.evaluate("SCN-8071-02", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("live fixture must not run"))
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"
    assert row["evidence_scope"] == "repository-integration"
    assert row["cleanup"]["status"] == "not-run"


class RecoveryObserver(Observer):
    def __init__(self):
        super().__init__()
        self.provider_id = "private-provider"
        self.elapsed_seconds = 42

    def snapshot(self, *, attempt_id):
        return {**super().snapshot(attempt_id=attempt_id), "provider_id": self.provider_id}

    def termination(self, *, attempt_id):
        return {"source": "independent-browser-provider", "provider_id": self.provider_id,
                "provider_status": "TERMINATED" if self.closed else "READY",
                "elapsed_seconds": self.elapsed_seconds}


class RecoveryDriver:
    isolated = authorized = inert_https = True

    def __init__(self, observer):
        self.observer = observer
        self.content = json.dumps({"view_id": "owned-view", "manifest": {"session_id": "private-provider"},
            "observations": [{"page_title": "Inert fixture", "visible_text": "x" * 9000}]}).encode()
        self.operation_ref = str(uuid4())
        self.recovery_calls = 0
        self.termination_confirmed = True

    def start_owned(self, *, attempt_id):
        return {"session_id": "owned-session", "view_id": "owned-view", "provider_id": "private-provider",
                "ttl_seconds": 300, "artifact_size": len(self.content),
                "artifact_sha256": hashlib.sha256(self.content).hexdigest()}

    def read_artifact(self, opened, *, offset, length, attempt_id):
        data = self.content[offset:offset + length]
        return {"sha256": opened["artifact_sha256"], "total_length": len(self.content),
                "offset": offset, "length": len(data), "data_base64": base64.b64encode(data).decode()}

    def lose_reply(self, opened, *, attempt_id):
        self.observer.counts["browser_actions"] += 1
        return {"status": "unknown", "operation_ref": self.operation_ref}

    def recover_original(self, operation_ref, *, attempt_id):
        self.recovery_calls += 1
        return {"status": "unknown", "operation_ref": operation_ref}

    def close_owned(self, *, attempt_id):
        self.observer.closed = True
        return {"status": "confirmed", "termination_confirmed": self.termination_confirmed}


def test_browser_recovery_selector_checks_paged_bytes_single_action_and_close(tmp_path, monkeypatch):
    observer = RecoveryObserver()
    driver = RecoveryDriver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "8102")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    monkeypatch.setenv("GITHUB_SHA", SHA)
    success = runner.evaluate("SCN-8071-03", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: driver, integration_observer=observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    row, = report["scenarios"]
    assert success and report["live_attestation"] is False
    assert row["scenario_id"] == "SCN-8071-03" and row["tags"] == [
        "@scenario SCN-8071-03", "@story 8071", "@acceptance AC-03"]
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["artifact_sha256"] == hashlib.sha256(driver.content).hexdigest()
    assert row["provider_actions"] == 1 and driver.recovery_calls == 1
    assert row["cleanup"] == {"status": "confirmed", "provider_termination": "TERMINATED"}
    assert report["github_run_id"] == "8102" and report["github_run_attempt"] == "3"
    assert report["harness_revision"] == SHA


@pytest.mark.parametrize("distortion", ["artifact", "replay", "wrong_provider", "late_cleanup", "unconfirmed_close"])
def test_browser_recovery_selector_refuses_corrupt_artifacts_replays_or_cleanup(distortion):
    observer = RecoveryObserver()
    driver = RecoveryDriver(observer)
    if distortion == "artifact":
        original = driver.read_artifact

        def corrupt(opened, *, offset, length, attempt_id):
            chunk = original(opened, offset=offset, length=length, attempt_id=attempt_id)
            chunk["data_base64"] = base64.b64encode(b"bad" + base64.b64decode(chunk["data_base64"])[3:]).decode()
            return chunk

        driver.read_artifact = corrupt
    elif distortion == "replay":
        original = driver.recover_original

        def replay(operation_ref, *, attempt_id):
            observer.counts["browser_actions"] += 1
            return original(operation_ref, attempt_id=attempt_id)

        driver.recover_original = replay
    elif distortion == "wrong_provider":
        observer.provider_id = "foreign-provider"
    elif distortion == "late_cleanup":
        observer.elapsed_seconds = 601
    else:
        driver.termination_confirmed = False
    evidence = browser_ac03(driver, observer)
    assert evidence["status"] != "pass"
    assert observer.closed


def test_browser_recovery_selector_never_runs_local_fixture_as_live(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    success = runner.evaluate("SCN-8071-03", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("live fixture must not run"))
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"
    assert row["evidence_scope"] == "repository-integration"
    assert row["cleanup"]["status"] == "not-run"


class LifecycleObserver:
    def __init__(self):
        self.counts = {"network_requests": 1, "browser_actions": 0, "mutations": 0}
        self.view = "first"
        self.closed = False
        self.elapsed_seconds = 25

    def snapshot(self, *, attempt_id):
        return {"source": "independent-browser-provider", "provider_id": "private-provider",
                "url": "https://example.com/" if self.view == "first" else "https://example.com/linked",
                "page_title": "Inert fixture" if self.view == "first" else "Linked fixture",
                "visible_text": "Known fixture" if self.view == "first" else "Changed page marker",
                **self.counts}

    def termination(self, *, attempt_id):
        return {"source": "independent-browser-provider", "provider_id": "private-provider",
                "provider_status": "TERMINATED" if self.closed else "READY",
                "elapsed_seconds": self.elapsed_seconds}


class LifecycleDriver:
    isolated = authorized = inert_https = True
    fixture = {"url": "https://example.com/", "title": "Inert fixture",
               "link_text": "Follow", "destination_url": "https://example.com/linked",
               "destination_title": "Linked fixture", "marker": "Changed page marker", "form_inert": True}

    def __init__(self, client_name, observer):
        self.client_name = client_name
        self.observer = observer
        self.actions = []
        self.artifact_bytes = b"Inert inspected page artifact"

    def start_owned(self, *, attempt_id):
        self.actions.append("start")
        return {"status": "confirmed", "session_id": "owned-session", "view_id": "first",
                "provider_id": "private-provider", "ttl_seconds": 300,
                "client_name": self.client_name, "client_version": "test-pinned-client",
                "source_revision": SHA}

    def inspect_owned(self, current, *, attempt_id):
        self.actions.append("inspect")
        result = {"status": "confirmed", "session_id": current["session_id"],
                  "view_id": current["view_id"]}
        if current["view_id"] == "first":
            result["choices"] = [{"id": "observed-link", "action": "follow", "text": "Follow",
                                  "url": "https://example.com/linked"}]
        else:
            result["artifact"] = {"byte_length": len(self.artifact_bytes),
                                  "content_sha256": hashlib.sha256(self.artifact_bytes).hexdigest()}
        return result

    def follow_owned(self, inspected, *, candidate_id, attempt_id):
        assert inspected["view_id"] == "first" and candidate_id == "observed-link"
        self.actions.append("step")
        self.observer.view = "linked"
        self.observer.counts["browser_actions"] += 1
        self.observer.counts["network_requests"] += 1
        return {"status": "confirmed", "session_id": inspected["session_id"], "view_id": "second"}

    def read_artifact(self, inspected, *, offset, length, attempt_id):
        self.actions.append("artifact_read")
        data = self.artifact_bytes[offset:offset + length]
        return {"data_base64": base64.b64encode(data).decode(), "offset": offset, "length": len(data),
                "total_length": len(self.artifact_bytes), "sha256": hashlib.sha256(self.artifact_bytes).hexdigest()}

    def close_owned(self, *, attempt_id):
        self.actions.append("close")
        self.observer.closed = True
        return {"status": "confirmed", "termination_confirmed": True}


@pytest.mark.parametrize("client_name", ("cli", "claude-code", "codex"))
def test_browser_lifecycle_selector_routes_all_client_drivers_and_checks_observed_cleanup(tmp_path, monkeypatch, client_name):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    observer = LifecycleObserver()
    driver = LifecycleDriver(client_name, observer)
    selected = []

    def driver_factory(case, name, tool):
        assert case.identifier == "SCN-8071-04" and tool == "browser"
        selected.append(name)
        return driver if name == client_name else None

    passed = runner.evaluate("SCN-8071-04", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                             client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                               "__exit__": lambda self, *args: None, "version": "test"})(),
                             integration_driver_factory=driver_factory, integration_observer=observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not passed and selected == ["cli", "claude-code", "codex"]
    assert {row["client"] for row in rows} == set(selected)
    owner, = (row for row in rows if row["client"] == client_name)
    assert owner["status"] == "pass" and owner["evidence_scope"] == "repository-integration"
    assert owner["client_version"] == "test-pinned-client" and owner["view_ids"] == ["first", "second"]
    assert owner["cleanup"] == {"status": "confirmed", "provider_termination": "TERMINATED"}
    assert driver.actions == ["start", "inspect", "step", "inspect", "artifact_read", "close"]
    assert all(row["status"] == "not-run" for row in rows if row is not owner)


@pytest.mark.parametrize("fault", ("wrong_client", "missing_marker", "mutation", "unchanged_view", "corrupt_artifact", "unconfirmed_close", "slow_cleanup"))
def test_browser_lifecycle_contract_refuses_synthetic_false_positive(fault):
    observer = LifecycleObserver()
    driver = LifecycleDriver("cli", observer)
    if fault == "wrong_client":
        driver.client_name = "codex"
    elif fault == "missing_marker":
        driver.fixture = {**driver.fixture, "marker": "Absent marker"}
    elif fault == "mutation":
        observer.counts["mutations"] = 1
    elif fault == "unchanged_view":
        original = driver.follow_owned

        def unchanged(*args, **kwargs):
            return {**original(*args, **kwargs), "view_id": "first"}

        driver.follow_owned = unchanged
    elif fault == "corrupt_artifact":
        driver.artifact_bytes = b"altered bytes"
        original = driver.read_artifact
        driver.read_artifact = lambda inspected, **kwargs: {**original(inspected, **kwargs), "sha256": "0" * 64}
    elif fault == "unconfirmed_close":
        driver.close_owned = lambda *, attempt_id: {"status": "confirmed", "termination_confirmed": False}
    else:
        observer.elapsed_seconds = 601
    if fault == "wrong_client":
        with pytest.raises(FixtureUnavailable, match="fixture and independent observer required"):
            browser_ac04(driver, observer, "cli", SHA)
        return
    evidence = browser_ac04(driver, observer, "cli", SHA)
    assert evidence["status"] != "pass"
    assert evidence["cleanup"]["status"] == ("unknown" if fault in {"unconfirmed_close", "slow_cleanup"}
                                            else "confirmed")


def test_browser_lifecycle_live_refuses_before_start_or_fixture_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    success = runner.evaluate("SCN-8071-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("unadmitted fixture must not run"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3
    assert {row["client"] for row in rows} == {"cli", "claude-code", "codex"}
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" and
               row["cleanup"]["status"] == "not-run" for row in rows)


def test_browser_lifecycle_live_requires_authoritative_admission_before_private_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    priced_clients = []

    def priced_fixture(fixture, *, client_name):
        assert fixture == {}
        assert client_name in {"cli", "claude-code", "codex"}
        priced_clients.append(client_name)
        return {"reserved_usd": 1}

    monkeypatch.setattr(runner, "validate_fixture", priced_fixture)
    monkeypatch.setattr(runner, "client_runtime_window", lambda *args, **kwargs:
                        pytest.fail("release window must not open before admission"))
    success = runner.evaluate("SCN-8071-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("paid client must not start"),
                              integration_observer=LifecycleObserver(),
                              release_manifest={}, release_observer=lambda phase: {},
                              release_source_revision=SHA, release_account="fictional", client_refresh_file="private",
                              recovery_root="private", client_fixture={"browser_lifecycle": {}})
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert sorted(priced_clients) == ["claude-code", "cli", "codex"]
    assert not success and len(rows) == 3
    assert all(row["status"] == "blocked" and "admission unavailable" in row["reason"] and
               row["cleanup"]["status"] == "not-run" for row in rows)


def test_browser_lifecycle_missing_pricing_blocks_before_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setattr(runner, "require_paid_admission", lambda: pytest.fail("pricing must be checked first"))
    success = runner.evaluate("SCN-8071-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("unpriced client must not start"),
                              integration_observer=LifecycleObserver(),
                              release_manifest={}, release_observer=lambda phase: {},
                              release_source_revision=SHA, release_account="fictional", client_refresh_file="private",
                              recovery_root="private", client_fixture={"browser_lifecycle": {}})
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3
    assert all(row["status"] == "blocked" and "priced authorized" in row["reason"] and
               row["cleanup"]["status"] == "not-run" for row in rows)


def test_browser_navigation_selector_verifies_inert_fixture_receipt_and_independent_view(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    observer = LifecycleObserver()
    driver = LifecycleDriver("cli", observer)
    passed = runner.evaluate("SCN-8071-01", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                             client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                               "__exit__": lambda self, *args: None, "version": "test"})(),
                             integration_driver_factory=lambda case, name, tool: driver,
                             integration_observer=observer)
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert passed and row["scenario_id"] == "SCN-8071-01" and row["status"] == "pass"
    assert row["evidence_scope"] == "repository-integration" and row["client"] == "cli"
    assert row["view_ids"] == ["first", "second"] and row["provider_id"] == "private-provider"
    assert row["cleanup"] == {"status": "confirmed", "provider_termination": "TERMINATED"}
    assert driver.actions == ["start", "inspect", "step", "inspect", "artifact_read", "close"]


def test_browser_navigation_selector_does_not_use_synthetic_fixture_as_live_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    success = runner.evaluate("SCN-8071-01", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=lambda _: type("Client", (), {"__enter__": lambda self: self,
                                "__exit__": lambda self, *args: None, "version": "test"})(),
                              integration_driver_factory=lambda *args: pytest.fail("local fixture must not run"))
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and row["status"] == "blocked"
    assert row["evidence_scope"] == "repository-integration" and row["cleanup"]["status"] == "not-run"
