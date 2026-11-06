"""Offline guards for the manually dispatched Security workflow."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

import run_security_live as live
from l5_services.selector_receipts import SCENARIOS, SELECTOR_NODES


SHA = "a" * 40
NOW = datetime(2026, 10, 28, tzinfo=UTC)


def authorization(tmp_path):
    tmp_path.chmod(0o700)
    fixture = {"authorization": {"source_sha": SHA, "scenarios": list(live.SELECTORS),
                                 "expires_at": (NOW + timedelta(hours=4)).isoformat()},
               "cases": [{"journal_file": str(tmp_path / "one.jsonl"),
                          "assessment_journal_file": str(tmp_path / "assessment-one.jsonl"),
                          "recovery": {"journal_file": str(tmp_path / "recovery.jsonl")},
                          "context_reuse": {"journal_file": str(tmp_path / "context.jsonl")}},
                         {"journal_file": str(tmp_path / "two.jsonl"),
                          "assessment_journal_file": str(tmp_path / "assessment-two.jsonl")}],
               "limits": {"deadline_seconds": 120}}
    return fixture


def test_release_selector_receipts_cover_exactly_the_live_drivers():
    assert tuple(live.SELECTORS) == SCENARIOS


@pytest.mark.parametrize("scenario", list(live.SELECTORS))
def test_bounded_authorized_scenario_accepts_only_fresh_private_journals(tmp_path, scenario):
    fixture = authorization(tmp_path)
    assert live.check_authorization(fixture, scenario, SHA, tmp_path, now=NOW) == fixture


def test_guard_rejects_expired_wrong_revision_and_unselected_scenarios(tmp_path):
    fixture = authorization(tmp_path)
    for change in ({"source_sha": "b" * 40}, {"expires_at": (NOW - timedelta(seconds=1)).isoformat()},
                   {"expires_at": (NOW + timedelta(days=2)).isoformat()}, {"scenarios": ["context"]}):
        changed = {**fixture, "authorization": {**fixture["authorization"], **change}}
        with pytest.raises(ValueError, match="authorization"):
            live.check_authorization(changed, "setup", SHA, tmp_path, now=NOW)
    with pytest.raises(ValueError, match="reviewed source"):
        live.check_authorization(fixture, "setup", "not-a-sha", tmp_path, now=NOW)


def test_guard_rejects_public_reused_shared_or_outside_checkout_journals(tmp_path):
    fixture = authorization(tmp_path)
    previous = tmp_path / "one.jsonl"
    previous.touch(mode=0o600)
    with pytest.raises(ValueError, match="journals"):
        live.check_authorization(fixture, "setup", SHA, tmp_path, now=NOW)
    previous.unlink()
    fixture["cases"][1]["journal_file"] = str(previous)
    with pytest.raises(ValueError, match="share"):
        live.check_authorization(fixture, "setup", SHA, tmp_path, now=NOW)
    fixture["cases"][1]["journal_file"] = str(tmp_path.parent / "outside.jsonl")
    with pytest.raises(ValueError, match="journals"):
        live.check_authorization(fixture, "setup", SHA, tmp_path, now=NOW)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        live.check_authorization(fixture, "setup", SHA, tmp_path, now=NOW)


def test_runner_passes_only_selected_live_node_and_retains_private_logs(tmp_path, monkeypatch):
    fixture = authorization(tmp_path)
    monkeypatch.setattr(live, "load_live_fixture", lambda path: fixture)
    monkeypatch.setattr(live, "check_authorization", lambda *args, **kwargs: fixture)
    captured = []

    def test_command(command, *, env, cwd, capture_output, timeout, check):
        captured.append((command, env.copy(), timeout))
        return SimpleNamespace(returncode=0, stdout=b"1 passed in 0.02s", stderr=b"")

    monkeypatch.setattr(live.subprocess, "run", test_command)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    selected = tmp_path / "fixture.json"
    selected.write_text(json.dumps(fixture))
    assert live.run("setup", SHA, str(selected), str(tmp_path),
                    json.dumps({"TEST_LIVE_TOKEN": "fictional-credential"})) == 0
    assert captured[0][0][-1].endswith("::test_clients_and_setup")
    assert captured[0][1]["ADP_L5_SECURITY_LIVE_ENABLED"] == "I_ACCEPT_PAID_TESTS"
    assert captured[0][2] == 300
    log = tmp_path / "security-123-1-setup.log"
    assert log.exists() and log.stat().st_mode & 0o077 == 0
    assert b"1 passed" in log.read_bytes()
    receipt = tmp_path / "security-123-1-setup.json"
    assert receipt.stat().st_mode & 0o077 == 0
    recorded = json.loads(receipt.read_text())
    assert datetime.fromisoformat(recorded.pop("observed_at")).tzinfo is not None
    assert recorded == {
        "scenario": "setup", "run_id": "123", "run_attempt": "1", "source_revision": SHA,
        "selector_node": SELECTOR_NODES["setup"],
        "fixture_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "result": "passed"}
    assert "TEST_LIVE_TOKEN" not in live.os.environ

def test_runner_rejects_skips_and_does_not_retry_paid_work(tmp_path, monkeypatch):
    fixture = authorization(tmp_path)
    monkeypatch.setattr(live, "load_live_fixture", lambda path: fixture)
    monkeypatch.setattr(live, "check_authorization", lambda *args, **kwargs: fixture)
    calls = []

    def skipped(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"1 skipped in 0.02s", stderr=b"")

    monkeypatch.setattr(live.subprocess, "run", skipped)
    with pytest.raises(RuntimeError, match="did not pass"):
        live.run("setup", SHA, str(tmp_path / "fixture.json"), str(tmp_path),
                 json.dumps({"TEST_LIVE_TOKEN": "fictional-credential"}))
    assert len(calls) == 1
    with pytest.raises(ValueError, match="override"):
        live.run("setup", SHA, "fixture", str(tmp_path), json.dumps({"PATH": "malicious"}))
    assert len(calls) == 1


def test_release_preflight_requires_all_scenarios_and_disjoint_journals(tmp_path, monkeypatch):
    fixture = authorization(tmp_path)
    fixture["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    monkeypatch.setattr(live, "load_live_fixture", lambda path: fixture)
    monkeypatch.setattr(live.subprocess, "run", lambda *args, **kwargs: pytest.fail("preflight ran a selector"))
    credentials = json.dumps({"TEST_LIVE_TOKEN": "fictional-credential"})
    selected, environment = live.preflight(live.SELECTORS, SHA, str(tmp_path / "fixture.json"),
                                           str(tmp_path), credentials)
    assert selected == fixture and environment["TEST_LIVE_TOKEN"] == "fictional-credential"
    assert "TEST_LIVE_TOKEN" not in live.os.environ
    fixture["authorization"]["scenarios"] = ["setup"]
    with pytest.raises(ValueError, match="authorization"):
        live.preflight(live.SELECTORS, SHA, str(tmp_path / "fixture.json"), str(tmp_path), credentials)
    fixture["authorization"]["scenarios"] = list(live.SELECTORS)
    fixture["cases"][0]["recovery"]["journal_file"] = fixture["cases"][0]["journal_file"]
    with pytest.raises(ValueError, match="separate.*journals"):
        live.preflight(live.SELECTORS, SHA, str(tmp_path / "fixture.json"), str(tmp_path), credentials)


def test_workflow_is_manual_main_only_and_checks_protection_before_targets():
    workflow = yaml.load((live.ROOT / ".github/workflows/l5-security-live-acceptance.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert list(workflow["on"]) == ["workflow_dispatch"]
    job = workflow["jobs"]["qualify"]
    assert "refs/heads/main" in job["if"]
    assert job["environment"].startswith("adp-deploy-")
    steps = job["steps"]
    assert "environments/$TARGET_ENV" in steps[0]["run"]
    assert "deployment_branch_policy" in steps[0]["run"]
    assert "run_security_live.py" in steps[-1]["run"]
    assert not any("upload-artifact" in str(step) for step in steps)
