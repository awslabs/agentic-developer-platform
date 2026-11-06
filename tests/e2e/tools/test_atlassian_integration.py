"""Isolated release-recovery scenarios; these are not deployed acceptance."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from modules.tools.scripts.reconcile_release import reconcile
from modules.tools.scripts.release_scope import operator_sequence
from modules.tools.scripts.release_window import verify_window
from tests.e2e.tools import runner
from tests.e2e.tools import atlassian_client_matrix as matrix
from tests.e2e.tools.atlassian_integration import RecoveryFixture, SetupActionLedger, client_schema_loading
from tests.e2e.tools.test_release_window import (
    ACCOUNT,
    DIGEST,
    IMAGE,
    PATHS,
    SOURCE,
    fixture as base_release_fixture,
    four_route_fixture,
)


CONNECTORS = ("atlassian", "github", "slack", "jira")
CANDIDATE = {"tool-adapters": {"image": DIGEST, "configuration": DIGEST}}


def run_setup(tmp_path, monkeypatch, fixture=None, *, live=False):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None if live else pytest.fail("controlled setup must not call a gateway"))
    result = runner.evaluate(
        "SCN-8454-01", tmp_path / "report", "https://example.invalid", SOURCE, IMAGE, IMAGE,
        command=["unused"], client_factory=lambda *_: pytest.fail("setup fixture must not start a client"),
        atlassian_setup_fixture=fixture, live=live,
    )
    return result, json.loads((tmp_path / "report/evidence.json").read_text())


@pytest.mark.parametrize("kind,policy_required", [("fresh", False), ("legacy", True)])
def test_setup_scenario_uses_private_ledger_without_promoting_controlled_proof(tmp_path, monkeypatch, kind, policy_required):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = SetupActionLedger(private / "actions.json", run_id="run-1", owner_id="owner-1", fixture_kind=kind)
    for action in sorted(SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection", "policy_approval"}):
        ledger.record(action, actor="machine" if action in SetupActionLedger.MACHINE_ACTIONS else "human",
                      status="observed", evidence_sha256="a" * 64)
    restarted = SetupActionLedger(ledger.path, run_id="run-1", owner_id="owner-1", fixture_kind=kind)
    assert [case.identifier for case in runner.select_scenarios("SCN-8454-01")] == ["SCN-8454-01"]
    success, report = run_setup(tmp_path, monkeypatch, {"ledger": restarted, "policy_required": policy_required})
    row = report["scenarios"][0]
    assert success and report["live_attestation"] is False
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["issue_number"] == 8454 and row["local_scenario_id"] == "SCN-01"
    assert row["observations"] == {"scenario": "SCN-01", "status": "pass", "fixture_kind": kind,
                                    "policy_required": policy_required, "unverified_actions": [], "live_status": "unproven"}
    assert "owner-1" not in json.dumps(report)
    live_success, live_report = run_setup(tmp_path, monkeypatch, {"ledger": restarted, "policy_required": policy_required}, live=True)
    assert not live_success and live_report["scenarios"][0]["status"] == "blocked"
    assert live_report["live_attestation"] is False


@pytest.mark.parametrize("missing", ["consent", "site_selection", "policy_approval"])
def test_setup_scenario_blocks_missing_human_action(tmp_path, monkeypatch, missing):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = SetupActionLedger(private / "actions.json", run_id="run-1", owner_id="owner-1", fixture_kind="fresh")
    for action in sorted(SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection", "policy_approval"} - {missing}):
        ledger.record(action, actor="machine" if action in SetupActionLedger.MACHINE_ACTIONS else "human",
                      status="observed", evidence_sha256="a" * 64)
    success, report = run_setup(tmp_path, monkeypatch, {"ledger": ledger, "policy_required": True})
    assert not success and report["scenarios"][0]["observations"]["unverified_actions"] == [missing]
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["live_attestation"] is False


def test_setup_scenario_without_private_fixture_remains_blocked(tmp_path, monkeypatch):
    success, report = run_setup(tmp_path, monkeypatch)
    assert not success and report["scenarios"][0]["status"] == "blocked"
    assert "private setup fixture" in report["scenarios"][0]["reason"]


def test_live_setup_driver_uses_independent_actions_and_reconciles_without_restarting(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = SetupActionLedger(private / "actions.json", run_id="run-1", owner_id="owner-1", fixture_kind="fresh")
    events = [
        {"action": action, "actor": "machine" if action in SetupActionLedger.MACHINE_ACTIONS else "human",
         "status": "observed", "evidence_sha256": "b" * 64,
         "source": {"dcr_registration": "identity", "identity_provider_setup": "identity",
                    "consent": "browser"}.get(action, "gateway"), "run_id": "run-1", "owner_id": "owner-1"}
        for action in sorted(SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection"})
    ]

    class Driver:
        isolated = authorized = deployed_target = True
        starts = 0
        cleanups = 0

        def setup(self, **inputs):
            assert inputs["timeout_seconds"] == 120 and inputs["owner_id"] == "owner-1"
            self.starts += 1

        def cleanup(self, **inputs):
            assert inputs["run_id"] == "run-1"
            self.cleanups += 1

    class Observer:
        independent = True

        def read_actions(self, **inputs):
            assert inputs["owner_id"] == "owner-1"
            return events

        def cleaned(self, **inputs):
            return True

    driver = Driver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    for _ in range(2):
        result = runner.evaluate(
            "SCN-8454-01", tmp_path / "report", "https://example.invalid", SOURCE, IMAGE, IMAGE,
            command=["unused"], client_factory=lambda *_: pytest.fail("setup cannot use an MCP fixture"), live=True,
            atlassian_setup_fixture={"ledger": ledger, "policy_required": False},
            atlassian_setup_driver_factory=lambda *_: driver, atlassian_setup_observer=Observer(),
        )
        row = json.loads((tmp_path / "report/evidence.json").read_text())["scenarios"][0]
        assert result and row["status"] == "pass" and row["cleanup"]["status"] == "confirmed"
    assert driver.starts == 1 and driver.cleanups == 2
    assert len(ledger.state()["actions"]) == 1 + len(events)
    events[0] = {**events[0], "owner_id": "foreign-owner"}
    result = runner.evaluate(
        "SCN-8454-01", tmp_path / "report", "https://example.invalid", SOURCE, IMAGE, IMAGE,
        command=["unused"], live=True, atlassian_setup_fixture={"ledger": ledger, "policy_required": False},
        atlassian_setup_driver_factory=lambda *_: driver, atlassian_setup_observer=Observer(),
    )
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert not result and report["live_attestation"] is False
    assert report["scenarios"][0]["status"] == "blocked" and driver.cleanups == 3
    events[0] = {**events[0], "owner_id": "owner-1"}

    class UnverifiedCleanup(Observer):
        def cleaned(self, **inputs):
            return False

    result = runner.evaluate(
        "SCN-8454-01", tmp_path / "report", "https://example.invalid", SOURCE, IMAGE, IMAGE,
        command=["unused"], live=True, atlassian_setup_fixture={"ledger": ledger, "policy_required": False},
        atlassian_setup_driver_factory=lambda *_: driver, atlassian_setup_observer=UnverifiedCleanup(),
    )
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert not result and report["live_attestation"] is False
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["scenarios"][0]["cleanup"]["status"] == "unverified"
    assert driver.starts == 1 and driver.cleanups == 4


def test_setup_actions_survive_restart_without_claiming_live_acceptance(tmp_path):
    private = tmp_path / "setup"
    private.mkdir(mode=0o700)
    path = private / "actions.json"
    ledger = SetupActionLedger(path, run_id="run-1", owner_id="user-1", fixture_kind="fresh")
    assert ledger.state() == {
        "run_id": "run-1", "owner_id": "user-1", "fixture_kind": "fresh",
        "actions": [], "live_status": "unproven",
    }
    digest = "a" * 64
    ledger.record("dcr_registration", actor="machine", status="started")
    ledger.record("dcr_registration", actor="machine", status="observed", evidence_sha256=digest)
    ledger.record("consent", actor="human", status="blocked")
    resumed = SetupActionLedger(path, run_id="run-1", owner_id="user-1", fixture_kind="fresh")
    actions = resumed.state()["actions"]
    assert [(action["action"], action["actor"], action["status"]) for action in actions] == [
        ("dcr_registration", "machine", "started"),
        ("dcr_registration", "machine", "observed"),
        ("consent", "human", "blocked"),
    ]
    assert resumed.state()["live_status"] == "unproven"
    assert path.stat().st_mode & 0o077 == 0
    assert (private / "actions.json.lock").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("action,actor,status,digest", [
    ("manual_pat", "human", "observed", "a" * 64),
    ("app_secret_entry", "human", "observed", "a" * 64),
    ("mcp_configuration", "human", "observed", "a" * 64),
    ("consent", "machine", "observed", "a" * 64),
    ("consent", "human", "observed", None),
    ("consent", "human", "observed", "raw-token"),
])
def test_setup_ledger_rejects_manual_secrets_and_unattributed_proof(tmp_path, action, actor, status, digest):
    private = tmp_path / "setup"
    private.mkdir(mode=0o700)
    ledger = SetupActionLedger(private / "actions.json", run_id="run-1", owner_id="user-1", fixture_kind="legacy")
    with pytest.raises(ValueError, match="redacted setup actions"):
        ledger.record(action, actor=actor, status=status, evidence_sha256=digest)
    assert ledger.state()["actions"] == []


def test_setup_ledger_rejects_foreign_or_unprotected_state(tmp_path):
    private = tmp_path / "setup"
    private.mkdir(mode=0o700)
    path = private / "actions.json"
    ledger = SetupActionLedger(path, run_id="run-1", owner_id="user-1", fixture_kind="fresh")
    ledger.record("site_selection", actor="human", status="observed", evidence_sha256="b" * 64)
    with pytest.raises(ValueError, match="different run"):
        SetupActionLedger(path, run_id="run-2", owner_id="user-1", fixture_kind="fresh").state()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ledger.state()
    path.unlink()
    path.symlink_to(private / "other")
    with pytest.raises(ValueError, match="symlink|private"):
        ledger.state()


def test_client_schema_loading_keeps_two_separate_unqualified_reports(monkeypatch):
    monkeypatch.setattr("tests.e2e.tools.atlassian_integration.shutil.which",
                        lambda name: None if name == "claude" else "/isolated/bin/codex")
    definitions = [{"name": "jira___get_issue", "inputSchema": {"type": "object"}}]
    reports = [client_schema_loading(client, definitions, "jira___get_issue", {"isError": False})
               for client in ("claude-code", "codex")]
    assert [report["client"] for report in reports] == ["claude-code", "codex"]
    assert all(report["model_facing"]["before"] is None for report in reports)
    assert all(report["installed_client"]["invocation"] == "not-run" for report in reports)
    assert [report["installed_client"]["binary_presence"] for report in reports] == [
        "unavailable", "present-unverified"]
    assert all(report["installed_client"]["native_schema_deferral"] == "unverified" for report in reports)
    assert all(report["loading_mode"] == "bounded-eager-named-tools" for report in reports)


@pytest.mark.parametrize("damage", ("unknown-client", "missing-tool", "failed-call", "oversized", "duplicate"))
def test_client_schema_loading_rejects_invalid_fixture_proof(damage):
    definitions = [{"name": "jira___get_issue", "inputSchema": {"type": "object"}}]
    client = "codex"
    named = "jira___get_issue"
    call = {"isError": False}
    if damage == "unknown-client":
        client = "other"
    elif damage == "missing-tool":
        named = "jira___missing"
    elif damage == "failed-call":
        call = {"isError": True}
    elif damage == "oversized":
        definitions[0]["description"] = "x" * (128 * 1024)
    else:
        definitions.append(definitions[0])
    with pytest.raises(ValueError):
        client_schema_loading(client, definitions, named, call)


def generated(tmp_path):
    previous = four_route_fixture("legacy")
    fixture = RecoveryFixture.generate(
        tmp_path,
        previous,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    return fixture, previous


def decision(previous, fixture, run):
    result = reconcile(
        operator_sequence(["modules/tools/adapters/deploy.py"]),
        SOURCE,
        SOURCE,
        CANDIDATE,
        [run],
        [],
        lambda older, newer: older == newer,
        environment="dev",
        account=ACCOUNT,
        previous={
            "manifest": previous,
            "readback": fixture.readback,
            "gateway_image": IMAGE,
            "facade_image": IMAGE,
        },
    )
    return result["decisions"][0]["action"]


def test_release_recovery(tmp_path):
    fixture, previous = generated(tmp_path)
    original = fixture.state()
    expected_routes = {
        name: {"target_id": name + "-target", "backend": "legacy", "digest": DIGEST}
        for name in CONNECTORS
    }
    assert original["runtime"]["route_group"]["routes"] == expected_routes
    assert set(original["credentials"]) == {"legacy", "v2"}
    assert len(set(original["credentials"].values())) == 2
    assert original["pending"][0]["status"] == "unknown"
    assert fixture.directory.stat().st_mode & 0o077 == 0
    run = {
        "workflow": "tools-platform-deploy.yml",
        "head_sha": SOURCE,
        "id": 18,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "status": "in_progress",
        "conclusion": None,
        "inputs": {
            "mode": "release",
            "stage": "tool-adapters",
            "adp_correlation": "orch:12345678-1234-1234-1234-123456789012",
        },
    }

    fixture.interrupt("jira", "sha256:" + "e" * 64)
    restarted = RecoveryFixture(
        fixture.directory,
        previous,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    interrupted = restarted.state()
    assert interrupted["runtime"]["route_group"]["status"] == "fenced"
    assert interrupted["runtime"]["route_group"]["generation"] == 3
    assert (
        interrupted["runtime"]["route_group"]["routes"]["jira"]["backend"] == "adapter"
    )
    with pytest.raises(ValueError, match="restored and checked"):
        restarted.reopen()
    for field in ("credentials", "pending", "registrations"):
        assert interrupted[field] == original[field]
    with pytest.raises(ValueError, match="independent runtime readback changed"):
        verify_window(
            previous,
            restarted.readback,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    assert decision(previous, restarted, run) == "blocked"
    run.update(status="completed", conclusion="failure")
    assert decision(previous, restarted, run) == "blocked"
    assert restarted.cleanup() == {
        "status": "blocked",
        "reason": "Prior routes or retained authority differ",
    }

    restarted.restore()
    restored = restarted.state()
    assert restored["runtime"]["route_group"]["routes"] == expected_routes
    assert restored["runtime"]["route_group"]["status"] == "fenced"
    assert restored["runtime"]["route_group"]["generation"] == 3
    assert {
        key: restored[key] for key in ("credentials", "pending", "registrations")
    } == {key: original[key] for key in ("credentials", "pending", "registrations")}
    assert decision(previous, restarted, run) == "blocked"
    assert restarted.cleanup()["status"] == "blocked"
    assert restarted.reopen() == 4
    rollback = copy.deepcopy(previous)
    rollback["route_group"]["generation"] = 4
    with pytest.raises(ValueError, match="independent runtime readback changed"):
        verify_window(
            previous,
            restarted.readback,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    report = verify_window(
        rollback,
        restarted.readback,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    assert report["source_revision"] == SOURCE
    assert decision(previous, restarted, run) == "blocked"
    assert decision(rollback, restarted, run) == "protected-apply-required"
    assert restarted.cleanup()["status"] == "blocked"
    assert restarted.directory.exists()
    operation = original["pending"][0]["id"]
    assert restarted.cleanup({operation: 0}) == {
        "status": "cleaned",
        "provider_writes": 0,
        "observed_effects": {operation: 0},
    }
    assert not restarted.directory.exists()


@pytest.mark.parametrize(
    "damage", ("route", "credential", "pending", "registration", "owner")
)
def test_recovery_cleanup_refuses_changed_authority_or_unverified_effects(
    tmp_path, damage
):
    fixture, _ = generated(tmp_path)
    original = fixture.state()
    state = copy.deepcopy(original)
    if damage == "route":
        state["runtime"]["route_group"]["routes"]["slack"]["target_id"] = "other-target"
    elif damage == "credential":
        state["credentials"]["v2"] = "lost"
    elif damage == "pending":
        state["pending"] = []
    elif damage == "registration":
        state["registrations"] += 1
    else:
        state["owner"] = "foreign"
    fixture._write("state", state)
    operation = original["pending"][0]["id"]
    if damage == "owner":
        with pytest.raises(ValueError, match="different fixture"):
            fixture.cleanup({operation: 0})
    else:
        assert fixture.cleanup({operation: 0})["status"] == "blocked"
    assert fixture.directory.exists()
    fixture._write("state", original)
    assert fixture.cleanup({operation: 2})["status"] == "blocked"
    assert fixture.cleanup({operation: 0})["status"] == "cleaned"


def test_existing_ci_runs_generated_recovery_scenarios():
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load(
        (root / ".github/workflows/tools-platform-ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    paths = workflow["on"]["pull_request"]["paths"]
    assert "tests/e2e/tools/atlassian_integration.py" in paths
    assert "tests/e2e/tools/test_atlassian_integration.py" in paths
    assert "tests/e2e/tools/test_release_window.py" in paths
    steps = workflow["jobs"]["plan"]["steps"]
    check = next(
        step for step in steps if step.get("name") == "Test PR planning boundaries"
    )
    assert "tests/e2e/tools/test_atlassian_integration.py" in check["run"]


def test_four_route_fixture_does_not_mutate_shared_release_paths():
    original_paths = tuple(PATHS)
    active = four_route_fixture("active")
    assert tuple(PATHS) == original_paths
    assert "modules/tools/adapters/deploy.py" in active["paths"]
    assert "modules/tools/adapters/deploy.py" not in base_release_fixture()["paths"]


def four_route_candidate():
    candidate = four_route_fixture("active")
    candidate["route_group"]["generation"] = 4
    previous = copy.deepcopy(candidate)
    previous["route_group"].update(status="legacy", generation=2)
    for route in previous["route_group"]["routes"].values():
        route["backend"] = "legacy"
    return previous, candidate


def test_adapter_release_isolation_after_controller_loss_and_group_rollback(tmp_path):
    previous, candidate = four_route_candidate()
    fixture = RecoveryFixture.generate(
        tmp_path,
        previous,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    original = fixture.state()
    expected_adapter_routes = {
        name: {"target_id": name + "-target", "backend": "adapter", "digest": DIGEST}
        for name in CONNECTORS
    }
    expected_effects = {name: 0 for name in CONNECTORS}
    assert original["effects"] == expected_effects
    assert fixture.admits(2)
    for name in CONNECTORS[:2]:
        fixture.interrupt(name, DIGEST)
    restarted = RecoveryFixture(
        fixture.directory,
        previous,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    assert restarted.state()["runtime"]["route_group"]["generation"] == 3
    assert not restarted.admits(2)
    with pytest.raises(ValueError, match="All four prepared routes"):
        restarted.activate(
            candidate,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    with pytest.raises(ValueError, match="independent runtime readback changed"):
        verify_window(
            candidate,
            restarted.readback,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    for name in CONNECTORS[2:]:
        restarted.interrupt(name, DIGEST)
    assert restarted.state()["runtime"]["route_group"]["generation"] == 3
    assert (
        restarted.state()["runtime"]["route_group"]["routes"] == expected_adapter_routes
    )
    assert (
        restarted.activate(
            candidate,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
        == 4
    )
    assert (
        restarted.admits(4) and not restarted.admits(2) and not restarted.admits(True)
    )
    assert (
        verify_window(
            candidate,
            restarted.readback,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )["source_revision"]
        == SOURCE
    )
    activated = restarted.state()
    for name in original["runtime"]:
        if name != "route_group":
            assert activated["runtime"][name] == original["runtime"][name]
    for key in ("credentials", "pending", "registrations", "effects"):
        assert activated[key] == original[key]
    with pytest.raises(ValueError, match="new fence"):
        restarted.interrupt("jira", DIGEST)

    assert restarted.fence() == 5
    assert not restarted.admits(4)
    restarted.restore()
    assert restarted.state()["runtime"]["route_group"]["status"] == "fenced"
    assert not restarted.admits(2)
    assert restarted.reopen() == 6
    assert restarted.admits(6) and not restarted.admits(4) and not restarted.admits(2)
    rollback = copy.deepcopy(previous)
    rollback["route_group"]["generation"] = 6
    assert (
        verify_window(
            rollback,
            restarted.readback,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )["source_revision"]
        == SOURCE
    )
    restored = restarted.state()
    assert all(
        route["backend"] == "legacy"
        for route in restored["runtime"]["route_group"]["routes"].values()
    )
    for key in ("credentials", "pending", "registrations", "effects"):
        assert restored[key] == original[key]
    operation = original["pending"][0]["id"]
    assert restarted.cleanup()["status"] == "blocked"
    assert restarted.cleanup({operation: 0})["status"] == "cleaned"
    assert not restarted.directory.exists()


@pytest.mark.parametrize(
    "damage", ("owner", "generation", "route", "component", "pending", "effects")
)
def test_four_route_activation_refuses_changed_generation_or_authority(
    tmp_path, damage
):
    previous, candidate = four_route_candidate()
    fixture = RecoveryFixture.generate(
        tmp_path,
        previous,
        source=SOURCE,
        account=ACCOUNT,
        gateway_image=IMAGE,
        facade_image=IMAGE,
    )
    for name in CONNECTORS:
        fixture.interrupt(name, DIGEST)
    correct = fixture.state()
    tampered = copy.deepcopy(correct)
    if damage == "owner":
        tampered["owner"] = "foreign"
    elif damage == "generation":
        tampered["runtime"]["route_group"]["generation"] = 2
    elif damage == "route":
        tampered["runtime"]["route_group"]["routes"]["github"]["target_id"] = (
            "foreign-target"
        )
    elif damage == "component":
        tampered["runtime"]["gateway"]["image"] = "sha256:" + "e" * 64
    elif damage == "pending":
        tampered["pending"] = []
    else:
        tampered["effects"]["jira"] = 1
    fixture._write("state", tampered)
    with pytest.raises(ValueError):
        fixture.activate(
            candidate,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    assert fixture.directory.exists()
    fixture._write("state", correct)
    fixture.restore()
    assert fixture.reopen() == 4
    operation = correct["pending"][0]["id"]
    assert fixture.cleanup({operation: 0})["status"] == "cleaned"


def test_cleanup_recovery(tmp_path):
    from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
    from tests.e2e.tools.atlassian_integration import cleanup_recovery
    from tests.e2e.tools.test_atlassian_fixture_cleanup import ProviderFixture

    private = tmp_path / "jira-fixture"
    private.mkdir(mode=0o700)
    inputs = {"tenant_id": "tenant-one", "owner_id": "owner-one", "run_id": "run-one",
              "site_id": "site-one", "project_key": "FIX", "operation_id": "create-one"}
    ledger = JiraFixtureLedger(private / "ledger.json", **inputs)
    reservation = ledger.reserve()
    provider = ProviderFixture()
    provider.add(reservation, "FIX-42")
    provider.add({**reservation, "marker": "unrelated", "owner_id": "someone-else", "run_id": "another-run"},
                 "FOREIGN-1")
    foreign_before = provider.read_issue("FOREIGN-1", site_id="site-one")
    ledger.confirm_created(provider, "FIX-42")
    ledger.track_operation("update-one")
    provider.lose_ack = True
    first = cleanup_recovery(ledger, provider,
                             pending_operations=lambda operation: operation == "update-one",
                             unrelated_issue_id="FOREIGN-1")
    assert first == {"scenario": "SCN-04", "status": "blocked", "cleanup": "pending",
                     "owned_absent": False, "unrelated_unchanged": True}
    assert ledger.state()["cleanup_status"] == "pending"
    assert provider.deletions == []
    restarted = JiraFixtureLedger(private / "ledger.json", **inputs)
    pending = cleanup_recovery(restarted, provider,
                               pending_operations=lambda operation: operation == "update-one",
                               unrelated_issue_id="FOREIGN-1")
    assert pending["status"] == "blocked" and pending["owned_absent"] is False
    assert restarted.state()["cleanup_status"] == "pending"
    failed_first_repair = cleanup_recovery(restarted, provider, pending_operations=lambda operation: False,
                                           unrelated_issue_id="FOREIGN-1")
    assert failed_first_repair["status"] == "blocked" and failed_first_repair["owned_absent"] is True
    assert provider.deletions == ["FIX-42"]
    final = cleanup_recovery(restarted, provider, pending_operations=lambda operation: False,
                             unrelated_issue_id="FOREIGN-1")
    assert final == {"scenario": "SCN-04", "status": "pass", "cleanup": "confirmed",
                     "owned_absent": True, "unrelated_unchanged": True}
    assert provider.read_issue("FIX-42", site_id="site-one") is None
    assert provider.read_issue("FOREIGN-1", site_id="site-one") == foreign_before
    assert provider.deletions == ["FIX-42"]


def test_cleanup_recovery_rejects_foreign_collision_or_incomplete_readback(tmp_path):
    from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
    from tests.e2e.tools.atlassian_integration import cleanup_recovery
    from tests.e2e.tools.test_atlassian_fixture_cleanup import ProviderFixture

    private = tmp_path / "jira-fixture"
    private.mkdir(mode=0o700)
    inputs = {"tenant_id": "tenant-one", "owner_id": "owner-one", "run_id": "run-one",
              "site_id": "site-one", "project_key": "FIX", "operation_id": "create-one"}
    ledger = JiraFixtureLedger(private / "ledger.json", **inputs)
    reservation = ledger.reserve()
    provider = ProviderFixture()
    provider.add({**reservation, "owner_id": "someone-else"}, "COLLISION-1")
    provider.add({**reservation, "marker": "unrelated", "owner_id": "someone-else"}, "FOREIGN-1")
    foreign_before = provider.read_issue("FOREIGN-1", site_id="site-one")
    blocked = cleanup_recovery(ledger, provider, pending_operations=lambda operation: False,
                               unrelated_issue_id="FOREIGN-1")
    assert blocked["status"] == "blocked"
    assert provider.deletions == [] and provider.read_issue("FOREIGN-1", site_id="site-one") == foreign_before
    provider.complete = False
    incomplete = cleanup_recovery(ledger, provider, pending_operations=lambda operation: False,
                                  unrelated_issue_id="FOREIGN-1")
    assert incomplete["status"] == "blocked" and incomplete["owned_absent"] is False
    assert ledger.state()["cleanup_status"] == "pending"


def test_cleanup_recovery_cannot_pass_when_final_provider_readback_fails(tmp_path):
    from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
    from tests.e2e.tools.atlassian_integration import cleanup_recovery
    from tests.e2e.tools.test_atlassian_fixture_cleanup import ProviderFixture

    class OutageProvider(ProviderFixture):
        calls = 0

        def read_issues(self, **kwargs):
            self.calls += 1
            if self.calls == 4:
                raise TimeoutError("final provider readback unavailable")
            return super().read_issues(**kwargs)

    private = tmp_path / "jira-fixture"
    private.mkdir(mode=0o700)
    ledger = JiraFixtureLedger(private / "ledger.json", tenant_id="tenant-one", owner_id="owner-one",
                               run_id="run-one", site_id="site-one", project_key="FIX", operation_id="create-one")
    reservation = ledger.reserve()
    provider = OutageProvider()
    provider.add(reservation, "FIX-42")
    provider.add({**reservation, "marker": "unrelated", "owner_id": "someone-else"}, "FOREIGN-1")
    ledger.confirm_created(provider, "FIX-42")
    result = cleanup_recovery(ledger, provider, pending_operations=lambda operation: False,
                              unrelated_issue_id="FOREIGN-1")
    assert result == {"scenario": "SCN-04", "status": "blocked", "cleanup": "unverified",
                      "owned_absent": False, "unrelated_unchanged": False}
    assert provider.deletions == ["FIX-42"]
    assert cleanup_recovery(ledger, provider, pending_operations=lambda operation: False,
                            unrelated_issue_id="FOREIGN-1")["status"] == "pass"


def test_atlassian_matrix_selects_both_clients_and_keeps_missing_proof_open(tmp_path, monkeypatch):
    from tests.e2e.tools import atlassian_client_matrix as matrix

    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)

    def evaluate(**kwargs):
        return runner.evaluate(
            "SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE, IMAGE, IMAGE,
            command=["unused"], client_factory=lambda *_: pytest.fail("client launched without a protected fixture"),
            **kwargs)
    assert not evaluate()
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert report["selected_scenario_ids"] == ["SCN-8454-02"]
    assert report["live_attestation"] is False and report["matrix_complete"] is True
    assert {(row["client"], row["tool"]) for row in report["scenarios"]} == {
        (client, tool) for client in matrix.CLIENTS for tool in matrix.READS | matrix.WRITES}
    assert len(report["scenarios"]) == 16
    assert {row["status"] for row in report["scenarios"]} == {"blocked"}
    assert all(row["issue_number"] == 8454 and row["local_scenario_id"] == "SCN-02"
               for row in report["scenarios"])
    assert not evaluate(live=True)
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert not report["live_attestation"] and all(row["status"] == "blocked" for row in report["scenarios"])
    assert all(row["evidence_scope"] == "deployed-target" for row in report["scenarios"])


def test_atlassian_matrix_uses_confirmed_per_client_provider_readback(tmp_path, monkeypatch):
    from tests.e2e.tools.test_atlassian_client_matrix import fixture, inputs, attest

    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    for client in ("claude-code", "codex"):
        arguments = inputs(client, "get_jira_issue", client.replace("-", ""))
        expected = reader.readback(run_id="run-one", **arguments)
        ledger.reserve(**arguments, expected=expected, observer=reader)
        result, receipt = attest(expected)
        ledger.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    def evaluate():
        result = runner.evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                                 IMAGE, IMAGE, command=["unused"], atlassian_operation_ledger=ledger,
                                 atlassian_provider_reader=reader,
                                 client_factory=lambda *_: pytest.fail("controlled reader must not start a client"))
        return result, json.loads((tmp_path / "report/evidence.json").read_text())

    success, report = evaluate()
    assert not success and not report["live_attestation"]
    verified = [row for row in report["scenarios"] if row["status"] == "pass"]
    assert {(row["client"], row["tool"]) for row in verified} == {
        ("claude-code", "get_jira_issue"), ("codex", "get_jira_issue")}
    assert all(row["observations"]["effect_count"] == 0 and
               row["observations"]["source"] == "confirmed-client-operation-and-direct-provider" for row in verified)
    assert {row["status"] for row in report["scenarios"] if row not in verified} == {"blocked"}
    assert "000000:account-one" not in json.dumps(report)
    provider.content = "changed-after-confirmation"
    success, report = evaluate()
    assert not success and all(row["status"] == "blocked" for row in report["scenarios"])


def test_atlassian_matrix_uses_protected_workflow_identity_not_generic_user_import():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text())
    steps = workflow["jobs"]["evaluate"]["steps"]
    release_identity = next(step for step in steps if step.get("name") == "Prove the protected tools identity and authorization scope")
    release_window = next(step for step in steps if step.get("name") == "Bind the approved private release window")
    dedicated_session = next(step for step in steps if step.get("name") == "Bind the dedicated installed-client test session")
    independent_reader = next(step for step in steps if step.get("name") == "Retrieve and validate protected client evidence")
    user_import = next(step for step in steps if step.get("name") == "Import the protected test user's refresh session")
    assert "steps.scenario-selection.outputs.atlassian_clients == 'true'" in release_identity["if"]
    assert "steps.scenario-selection.outputs.atlassian_clients == 'true'" in release_window["if"]
    assert "steps.scenario-selection.outputs.atlassian_clients == 'true'" in dedicated_session["if"]
    assert "steps.scenario-selection.outputs.atlassian != 'true'" in user_import["if"]
    assert "steps.scenario-selection.outputs.atlassian == 'true'" in independent_reader["if"]


class ControlledMatrixReader(matrix.IndependentProviderReader):
    def __init__(self):
        self.before = {}
        self.after = {}
        self.committed = set()

    def readback(self, **fields):
        operation_id = fields["operation_id"]
        snapshot = self.after[operation_id] if operation_id in self.committed else self.before[operation_id]
        if any(snapshot[key] != value for key, value in fields.items()
               if key != "provider_id" or value is not None):
            raise ValueError("Requested client operation does not own this provider fixture")
        return dict(snapshot)


def controlled_matrix_fixture(tmp_path, *, clients=None, directory="matrix-private"):
    from tests.e2e.tools.test_atlassian_client_matrix import attest

    private = tmp_path / directory
    private.mkdir(mode=0o700)
    ledger = matrix.ClientOperationLedger(private / "operations.json", run_id="run-one")
    reader = ControlledMatrixReader()
    for number, (client, tool) in enumerate(
            ((client, tool) for client in (sorted(matrix.CLIENTS) if clients is None else clients)
             for tool in sorted(matrix.READS | matrix.WRITES)), 1):
        operation_id = f"operation-{number}"
        marker = f"adp-8454-{number:016x}"
        provider_id = None if tool == "create_jira_issue" else f"owned-{number}"
        baseline = {"source": "direct-atlassian-provider", "run_id": "run-one", "client": client, "tool": tool,
                    "operation_id": operation_id, "marker": marker, "provider_id": provider_id,
                    "site_id": "site-" + client, "account_id": "account-" + client,
                    "content_sha256": None if provider_id is None else matrix.digest({"value": "before-" + marker}),
                    "effect_count": 0}
        after = {**baseline, "provider_id": provider_id or f"owned-{number}",
                 "content_sha256": matrix.digest({"value": "after-" + marker}) if tool in matrix.WRITES
                                   else baseline["content_sha256"],
                 "effect_count": int(tool in matrix.WRITES)}
        reader.before[operation_id] = baseline
        reader.after[operation_id] = after
        expected = {**after, "provider_id": provider_id}
        ledger.reserve(client=client, tool=tool, operation_id=operation_id, marker=marker,
                       provider_id=provider_id, site_id=baseline["site_id"], expected=expected, observer=reader)
        reader.committed.add(operation_id)
        result, receipt = attest(after)
        ledger.confirm(operation_id=operation_id, client_result=result, receipt=receipt, observer=reader)
    return ledger, reader


def test_full_controlled_matrix_checks_each_client_sentinel_and_effect_count(tmp_path, monkeypatch):
    ledger, reader = controlled_matrix_fixture(tmp_path)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                              IMAGE, IMAGE, command=["unused"], atlassian_operation_ledger=ledger,
                              atlassian_provider_reader=reader,
                              client_factory=lambda *_: pytest.fail("controlled matrix must not launch installed clients"))
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert success and report["matrix_complete"] and not report["live_attestation"]
    assert len(report["scenarios"]) == 2 * len(matrix.READS | matrix.WRITES)
    assert {(row["client"], row["tool"]) for row in report["scenarios"]} == {
        (client, tool) for client in matrix.CLIENTS for tool in matrix.READS | matrix.WRITES}
    state = ledger.state()["operations"]
    assert len({row["marker"] for row in state.values()}) == len(state) == 16
    assert {state[row["observations"]["operation_id"]]["expected"]["site_id"] for row in report["scenarios"]
            if row["client"] == "codex"} == {"site-codex"}
    assert all(row["status"] == "pass" and row["evidence_scope"] == "repository-integration" and
               row["observations"]["effect_count"] == int(row["tool"] in matrix.WRITES) and
               row["observations"]["content_sha256"] ==
               reader.after[row["observations"]["operation_id"]]["content_sha256"] for row in report["scenarios"])
    assert "account-codex" not in json.dumps(report) and "owned-1" not in json.dumps(report)


def test_full_matrix_rejects_stale_counts_and_cannot_pass_as_live(tmp_path, monkeypatch):
    ledger, reader = controlled_matrix_fixture(tmp_path)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    reader.after["operation-1"]["effect_count"] = 2
    def run(*, live):
        success = runner.evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                                  IMAGE, IMAGE, command=["unused"], atlassian_operation_ledger=ledger,
                                  atlassian_provider_reader=reader, live=live,
                                  client_factory=lambda *_: pytest.fail("provider fixture must not start a client"))
        return success, json.loads((tmp_path / "report/evidence.json").read_text())

    success, report = run(live=False)
    assert not success and not report["live_attestation"]
    assert [(row["client"], row["tool"]) for row in report["scenarios"] if row["status"] == "blocked"] == [
        (reader.after["operation-1"]["client"], reader.after["operation-1"]["tool"])]
    success, report = run(live=True)
    assert not success and not report["live_attestation"]
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" for row in report["scenarios"])


def run_controlled_matrix(tmp_path, monkeypatch, *, ledger, reader, cell_filter=None):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                              IMAGE, IMAGE, command=["unused"], atlassian_operation_ledger=ledger,
                              atlassian_provider_reader=reader, cell_filter=cell_filter,
                              client_factory=lambda *_: pytest.fail("adapter-only proof cannot launch a client"))
    return success, json.loads((tmp_path / "report/evidence.json").read_text())


def test_direct_adapter_artifact_cannot_qualify_installed_client_matrix(tmp_path, monkeypatch):
    ledger, reader = controlled_matrix_fixture(tmp_path)
    direct_adapter_artifact = {**ledger.state(), "source": "direct-adapter"}
    success, report = run_controlled_matrix(tmp_path, monkeypatch, ledger=direct_adapter_artifact, reader=reader)
    assert not success and not report["live_attestation"]
    assert len(report["scenarios"]) == 16
    assert all(row["status"] == "blocked" and "observations" not in row for row in report["scenarios"])
    assert {row["client"] for row in report["scenarios"]} == matrix.CLIENTS


@pytest.mark.parametrize("single_client", sorted(matrix.CLIENTS))
def test_single_client_matrix_cannot_qualify_or_be_filtered_to_a_pass(tmp_path, monkeypatch, single_client):
    ledger, reader = controlled_matrix_fixture(tmp_path, clients=(single_client,))
    success, report = run_controlled_matrix(tmp_path, monkeypatch, ledger=ledger, reader=reader)
    assert not success and report["matrix_complete"] and not report["live_attestation"]
    assert len(report["scenarios"]) == 16
    assert {row["client"] for row in report["scenarios"] if row["status"] == "pass"} == {single_client}
    assert len([row for row in report["scenarios"] if row["status"] == "blocked"]) == 8
    assert {row["client"] for row in report["scenarios"] if row["status"] == "blocked"} == matrix.CLIENTS - {single_client}
    success, filtered = run_controlled_matrix(tmp_path, monkeypatch, ledger=ledger, reader=reader,
                                               cell_filter=f"{single_client}:get_jira_issue")
    assert not success and not filtered["matrix_complete"] and filtered["error"] == "ValueError"
    assert not filtered["live_attestation"]


def test_client_manifest_binds_two_private_ledgers_without_executable_configuration(tmp_path):
    private = tmp_path / "run"
    private.mkdir(mode=0o700)
    for client in matrix.CLIENTS:
        (private / client).mkdir(mode=0o700)
    manifest = private / "clients.json"
    owners = {client: "owner-" + client for client in matrix.CLIENTS}
    manifest.write_text(json.dumps({"run_id": "run-one", "owners": owners}))
    manifest.chmod(0o600)
    fixtures = matrix.load_isolated_fixtures(manifest, require_readers=False)
    assert set(fixtures) == matrix.CLIENTS
    assert {fixture["ledger"].path.parent.name for fixture in fixtures.values()} == matrix.CLIENTS
    assert all(fixture["ledger"].state()["operations"] == {} and fixture["reader"] is None
               for fixture in fixtures.values())
    assert {fixture["owner_id"] for fixture in fixtures.values()} == set(owners.values())
    manifest.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        matrix.load_isolated_fixtures(manifest)
    manifest.chmod(0o600)
    manifest.write_text(json.dumps({"run_id": "run-one", "owners": owners, "driver": "private_module.py"}))
    with pytest.raises(ValueError, match="exactly two owners"):
        matrix.load_isolated_fixtures(manifest)
    manifest.unlink()
    manifest.symlink_to(private / "other.json")
    with pytest.raises(ValueError, match="private"):
        matrix.load_isolated_fixtures(manifest)


def test_isolated_client_ledgers_keep_provider_readback_bound_to_each_owner(tmp_path, monkeypatch):
    fixtures = {}
    for client in sorted(matrix.CLIENTS):
        ledger, reader = controlled_matrix_fixture(tmp_path, clients=(client,), directory=client)
        reader.owner_id = "owner-" + client
        fixtures[client] = {"ledger": ledger, "reader": reader, "owner_id": reader.owner_id}
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)

    def run(selected):
        success = runner.evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                                  IMAGE, IMAGE, command=["unused"], atlassian_client_fixtures=selected,
                                  client_factory=lambda *_: pytest.fail("controlled fixtures cannot start clients"))
        return success, json.loads((tmp_path / "report/evidence.json").read_text())

    success, report = run(fixtures)
    assert success and not report["live_attestation"]
    assert len(report["scenarios"]) == 16 and all(row["status"] == "pass" for row in report["scenarios"])
    assert {row["client"] for row in report["scenarios"]} == matrix.CLIENTS
    other = "codex"
    success, report = run({**fixtures, other: {**fixtures[other], "ledger": fixtures["claude-code"]["ledger"]}})
    assert not success and report["error"] == "ValueError" and not report["live_attestation"]
    success, report = run({**fixtures, other: {**fixtures[other], "reader": fixtures["claude-code"]["reader"]}})
    assert not success and report["error"] == "ValueError" and not report["live_attestation"]


def test_protected_client_entrypoint_loads_only_run_owned_manifest(tmp_path, monkeypatch):
    private = tmp_path / "run"
    private.mkdir(mode=0o700)
    for client in matrix.CLIENTS:
        (private / client).mkdir(mode=0o700)
    manifest = private / "clients.json"
    manifest.write_text(json.dumps({"run_id": "run-one", "owners": {
        client: "owner-" + client for client in matrix.CLIENTS}}))
    manifest.chmod(0o600)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE", str(manifest))
    observed = []
    actual_evaluate = runner.evaluate
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: observed.append(kwargs) or False)
    arguments = ["--scenario-filter", "SCN-8454-02", "--output", str(tmp_path / "report"),
                 "--gateway-url", "https://example.invalid", "--gateway-revision", SOURCE,
                 "--gateway-image", IMAGE, "--facade-image", IMAGE]
    assert runner.main(arguments) == 1
    assert observed[0]["atlassian_client_fixtures"] is None
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = actual_evaluate("SCN-8454-02", tmp_path / "report", "https://example.invalid", SOURCE,
                              IMAGE, IMAGE, command=["unused"], live=True,
                              atlassian_client_fixtures=observed[0]["atlassian_client_fixtures"],
                              client_factory=lambda *_: pytest.fail("manifest alone cannot start a client"))
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert not success and not report["live_attestation"]
    assert all(row["status"] == "blocked" for row in report["scenarios"])
    manifest.chmod(0o644)
    assert runner.main(arguments) == 1
    assert observed[1]["atlassian_client_fixtures"] is None
