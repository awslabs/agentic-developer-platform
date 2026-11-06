"""verify-dev-box.py drives the owner API like the UI and reports bounded, documented exits."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("dev_box_verify", SCRIPTS / "verify-dev-box.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeGateway:
    """Scripted owner API: a box that becomes running after two polls, Crew optional."""

    def __init__(self, *, admission=True, existing=False, crew_ready=False, fail_setup=False, never_running=False, release_stalls=False):
        self.admission = admission
        self.existing = existing
        self.crew_ready = crew_ready
        self.fail_setup = fail_setup
        self.never_running = never_running
        self.release_stalls = release_stalls
        self.calls = []
        self.box_polls = 0
        self.op_polls = {}

    def __call__(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body))
        if path == "/cloud-desktops/policy":
            return 200, {"contract_version": 1, "admission_allowed": self.admission, "reason": None if self.admission else "Dev Box policy is disabled or incomplete"}
        if path == "/cloud-desktops" and method == "GET":
            items = [{"desktop_id": "box-1", "status": "stopped", "profile": "crew"}] if self.existing else []
            return 200, {"contract_version": 1, "items": items}
        if path == "/cloud-desktops/ensure":
            assert headers and headers.get("Idempotency-Key")
            return 202, {"contract_version": 1, "desktop_id": "box-1", "operation_id": "op-setup", "status": "pending", "progress": "admitted"}
        if path.startswith("/cloud-desktops/box-1/operations/"):
            operation = path.rsplit("/", 1)[1]
            self.op_polls[operation] = self.op_polls.get(operation, 0) + 1
            if operation == "op-setup" and self.fail_setup:
                return 200, {"desktop_id": "box-1", "operation_id": operation, "status": "failed", "progress": "launch", "error_class": "provider_denied"}
            if operation == "op-release" and self.release_stalls:
                return 200, {"desktop_id": "box-1", "operation_id": operation, "status": "pending", "progress": "fencing"}
            status = "succeeded" if self.op_polls[operation] >= 2 else "pending"
            return 200, {"desktop_id": "box-1", "operation_id": operation, "status": status, "progress": "launch"}
        if path == "/cloud-desktops/box-1" and method == "GET":
            self.box_polls += 1
            running = self.box_polls >= 2 and not self.never_running
            return 200, {"desktop_id": "box-1", "status": "running" if running else "requested",
                         "observed_state": "running" if running else "unknown", "machine_generation": 1, "profile": "crew"}
        if path == "/crew/target":
            if not self.crew_ready:
                raise MODULE.ApiError(404, {"error": "crew_target_missing"})
            return 200, {"health": "ready", "lease": {"target_kind": "ec2-instance", "target_ref": "box-1", "generation": 1}}
        if path == "/cloud-desktops/box-1/release":
            assert body == {"workspace_retention": "delete"}
            return 202, {"desktop_id": "box-1", "operation_id": "op-release", "status": "pending", "progress": "admitted"}
        raise AssertionError(f"unexpected call {method} {path}")


def fake_aws(online=True):
    def aws(args):
        if args[0] == "ec2":
            return {"Reservations": [{"Instances": [{"InstanceId": "i-0123456789abcdef0"}]}]}
        if args[0] == "ssm":
            return {"InstanceInformationList": [{"InstanceId": "i-0123456789abcdef0", "PingStatus": "Online" if online else "ConnectionLost"}]}
        raise AssertionError(args)
    return aws


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


def run(gateway, aws=None, **overrides):
    clock = Clock()
    logs = []
    verifier = MODULE.Verifier(gateway, aws=aws or fake_aws(), sleep=clock.sleep, clock=clock.monotonic, log=logs.append)
    options = dict(template="assistant", profile="crew", deployment_id="dep", region="us-east-1", ready_timeout=60,
                   release_timeout=60, poll=5, retention="delete", keep=False, require_crew=False, skip_ssm=False)
    options.update(overrides)
    return verifier.run(**options), verifier.summary, logs


def test_happy_path_creates_waits_checks_ssm_and_releases(capsys):
    gateway = FakeGateway(crew_ready=True)
    code, summary, _ = run(gateway)
    assert code == MODULE.OK
    assert summary["desktop_id"] == "box-1" and summary["ssm_online"] == ["i-0123456789abcdef0"]
    assert summary["crew_ready"] is True and summary["released"] is True
    methods = [(m, p) for m, p, _ in gateway.calls]
    assert ("POST", "/cloud-desktops/ensure") in methods and ("POST", "/cloud-desktops/box-1/release") in methods
    assert methods.index(("GET", "/crew/target")) < methods.index(("POST", "/cloud-desktops/box-1/release"))
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["result"] == 0


def test_crew_not_ready_is_reported_but_only_fails_when_required():
    code, summary, _ = run(FakeGateway(crew_ready=False))
    assert code == MODULE.OK and summary["crew_ready"] is False and summary["released"] is True
    code, _, _ = run(FakeGateway(crew_ready=False), require_crew=True)
    assert code == MODULE.CREW_NOT_READY


def test_admission_denied_without_an_existing_box_is_a_prerequisite_failure():
    gateway = FakeGateway(admission=False)
    code, _, logs = run(gateway)
    assert code == MODULE.PREREQ
    assert any("admission denied" in line for line in logs)
    assert not any(path == "/cloud-desktops/ensure" for _, path, _ in gateway.calls)


def test_denied_admission_with_an_existing_box_still_resumes_it():
    code, summary, _ = run(FakeGateway(admission=False, existing=True))
    assert code == MODULE.OK and summary["desktop_id"] == "box-1"


def test_failed_setup_and_timeouts_release_and_exit_one():
    code, summary, _ = run(FakeGateway(fail_setup=True))
    assert code == MODULE.FAILED and summary["released"] is True
    code, _, logs = run(FakeGateway(never_running=True), ready_timeout=20)
    assert code == MODULE.FAILED and any("did not reach running" in line for line in logs)


def test_ssm_offline_fails_unless_skipped():
    code, _, _ = run(FakeGateway(), aws=fake_aws(online=False))
    assert code == MODULE.FAILED
    code, _, _ = run(FakeGateway(), aws=fake_aws(online=False), skip_ssm=True)
    assert code == MODULE.OK


def test_release_that_does_not_complete_exits_four_and_keep_skips_release():
    code, summary, logs = run(FakeGateway(release_stalls=True), release_timeout=15)
    assert code == MODULE.RELEASE_PENDING and summary["released"] is False
    assert any("stays reserved" in line for line in logs)
    gateway = FakeGateway()
    code, _, _ = run(gateway, keep=True)
    assert code == MODULE.OK and not any(path.endswith("/release") for _, path, _ in gateway.calls)


def test_token_and_url_resolution_fail_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("ADP_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="no gateway token"):
        MODULE.resolve_token(None)
    monkeypatch.setenv("ADP_TOKEN", "t")
    assert MODULE.resolve_token(None) == "t"
    assert MODULE.resolve_gateway_url("https://x/api", "dev", "us-east-1", aws=lambda a: None) == "https://x/api"
    with pytest.raises(RuntimeError, match="cloudfront-domain"):
        MODULE.resolve_gateway_url(None, "dev", "us-east-1", aws=lambda a: {"Parameter": {}})
    assert MODULE.resolve_gateway_url(None, "dev", "us-east-1", aws=lambda a: {"Parameter": {"Value": "d.cloudfront.net"}}) == "https://d.cloudfront.net/api"
    with pytest.raises(RuntimeError, match="deployment-id"):
        MODULE.resolve_deployment_id(None, "dev", "us-east-1", aws=lambda a: {"Parameter": {"Value": " "}})
