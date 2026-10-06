"""Exercise CLI/runtime/browser phase wiring without credentials or network access."""

import json
import re
import shlex
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_demo1_browser import Transport, identity
from test_demo1_cli import write_private
from test_demo1_runtime import Producer, documents

from superplane_acceptance import demo1_cli, demo1_journey
from superplane_acceptance.demo1_browser import (
    CreationCheckpoint,
    PlaywrightBrowserTransport,
)
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_live import LiveEnvelope, PrivateCheckpoint
from superplane_acceptance.demo1_runtime import RuntimeReader


class Page:
    def __init__(self, selected, release):
        self.url = "about:blank"
        self.service = Transport(selected)
        self.release = release
        self.visible = True
        self.reloads = 0
        self.contexts = []
        self.closed = 0

    def goto(self, url, **options):
        assert url.endswith(("/superplane", "/.well-known/adp-demo1-session"))
        assert options["timeout"] > 0
        self.url = url

    def route(self, url, handler):
        assert url.endswith("/.well-known/adp-demo1-session")

    unroute = route

    def set_default_timeout(self, value):
        assert value > 0

    set_default_navigation_timeout = set_default_timeout

    def get_by_role(self, *args, **kwargs):
        return self

    def get_by_text(self, *args, **kwargs):
        return self

    def wait_for(self, **kwargs):
        if not self.visible:
            raise RuntimeError("private browser exception")

    def is_visible(self):
        return self.visible

    def click(self):
        pass

    def reload(self):
        self.reloads += 1

    def evaluate(self, script, arguments):
        if "sessionStorage.setItem" in script:
            assert arguments["cognito_access_token"]
            return
        assert (
            "redirect: 'error'" in script and "AbortSignal.timeout(timeout)" in script
        )
        assert 0 < arguments["timeout"] <= 30_000
        status, body = self.service.request(
            arguments["method"], arguments["path"], arguments["body"]
        )
        if isinstance(body, dict) and "approval_id" in body:
            body["expires_at"] = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
            if self.service.approved:
                body["decided_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
        return [status, body, self.release]

    def new_context(self, **options):
        self.url = "about:blank"
        assert options["service_workers"] == "block"
        self.contexts.append(options)
        return SimpleNamespace(new_page=lambda: self)

    def close(self):
        self.closed += 1


@pytest.mark.parametrize(
    "state",
    ["pending", "running", "succeeded", "failed", "cancelled", "unknown", "Ready"],
)
def test_cli_reports_preparation_state_without_claiming_later_phases(
    driver, monkeypatch, state
):
    driver.run()
    driver.page.service.approved = True
    assert driver.run()["browser"]["creation_observed"]
    saved = (driver.path / "checkpoint.json").read_bytes()
    before = len(driver.page.service.calls)
    request = driver.page.service.request

    def changed_state(method, path, body=None):
        status, response = request(method, path, body)
        if "/operations/by-idempotency/" in path:
            response["state"] = state
        return status, response

    monkeypatch.setattr(driver.page.service, "request", changed_state)
    report = driver.run()
    assert not report["live_acceptance"] and report["status"] == "BLOCKED"
    assert (driver.path / "checkpoint.json").read_bytes() == saved
    if state == "Ready":
        assert "operation state unverified" in report["reason"]
        assert all(
            method == "GET" for method, _, _ in driver.page.service.calls[before:]
        )
        return
    progress = report["browser"]["lifecycle"]
    assert progress["phases"]["prepare-infrastructure"]["state"] == state
    for phase in ("apply-infrastructure", "bootstrap-workspace"):
        assert progress["phases"][phase] == {"status": "NOT RUN", "state": "unobserved"}
    assert all(check["status"] == "BLOCKED" for check in progress["checks"].values())
    assert progress["status"] == (
        "FAIL" if state in ("failed", "cancelled") else "BLOCKED"
    )


def test_replaced_checkpoint_lock_refuses_before_creation_submission(
    driver, monkeypatch
):
    driver.run()
    driver.page.service.approved = True
    checkpoint = driver.path / "checkpoint.json"
    original = checkpoint.read_bytes()
    save = PrivateCheckpoint.save
    before = len(driver.page.service.calls)

    def replace_lock(store, state):
        if state.submitted:
            lock = checkpoint.with_name(checkpoint.name + ".lock")
            lock.unlink()
            lock.touch(mode=0o600)
        save(store, state)

    monkeypatch.setattr(PrivateCheckpoint, "save", replace_lock)
    assert driver.run() is None
    assert checkpoint.read_bytes() == original
    assert not any(
        method == "POST" and path.endswith("/workspaces")
        for method, path, _ in driver.page.service.calls[before:]
    )


class Playwright:
    def __init__(self, page, producer):
        self.page, self.producer = page, producer
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        pass

    def launch(self, **options):
        assert self.producer.calls and options["headless"] is True
        return self.page


@pytest.fixture
def driver(tmp_path, monkeypatch):
    selected, authority, session = documents()
    parsed = DemoInput.parse(selected)
    producer = Producer(selected, authority["runtime_target"])
    page = Page(parsed, authority["runtime_target"]["release_id"])
    monkeypatch.setattr(
        demo1_journey,
        "RuntimeReader",
        lambda selected, target: RuntimeReader(selected, target, runner=producer),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(
            Error=RuntimeError, sync_playwright=lambda: Playwright(page, producer)
        ),
    )
    tmp_path.chmod(0o700)
    for name, value in (
        ("selection.json", selected),
        ("authority.json", authority),
        ("requester-state.json", session),
    ):
        write_private(tmp_path / name, value)

    def run():
        index = len(list(tmp_path.glob("result-*.json")))
        report = tmp_path / f"result-{index}.json"
        arguments = [
            "--mode",
            "live",
            "--advance-creation",
            "--private-input",
            str(tmp_path / "selection.json"),
            "--authority",
            str(tmp_path / "authority.json"),
            "--browser-state",
            str(tmp_path / "requester-state.json"),
            "--checkpoint",
            str(tmp_path / "checkpoint.json"),
            "--report",
            str(report),
        ]
        assert demo1_cli.main(arguments) == 2
        return json.loads(report.read_text()) if report.exists() else None

    return SimpleNamespace(
        run=run,
        path=tmp_path,
        selected=parsed,
        envelope=LiveEnvelope.parse(authority, parsed),
        session=session,
        page=page,
        producer=producer,
    )


@pytest.mark.parametrize("lost_reply", [False, True])
def test_cli_waits_for_independent_approval_and_recovers_lost_reply(driver, lost_reply):
    result = driver.run()
    assert result["browser"]["reason"] == "awaiting independent human approval"
    assert result["public_release"]["status"] == "OBSERVED"
    assert result["checkpoint"]["submitted"] is False
    checkpoint = driver.path / "checkpoint.json"
    saved = checkpoint.read_bytes()
    assert json.loads(saved)["version"] == "demo1-checkpoint-v3"
    assert driver.run()["browser"]["reason"] == "awaiting independent human approval"
    assert checkpoint.read_bytes() == saved
    driver.page.service.approved = True
    driver.page.service.lost = lost_reply
    result = driver.run()
    assert result["checkpoint"]["submitted"] is True
    if lost_reply:
        assert "uncertain" in result["browser"]["reason"]
    else:
        assert result["browser"]["creation_observed"] is True
    driver.page.service.lost = False
    for _attempt in range(2):
        result = driver.run()
        assert result["browser"]["creation_observed"] is True
        assert result["browser"]["retirement"] == "BLOCKED"
        assert result["browser"]["readiness"] == "UNKNOWN"
        assert result["status"] == "BLOCKED" and result["live_acceptance"] is False
    assert driver.page.reloads == (2 if lost_reply else 3)
    calls = driver.page.service.calls
    assert (
        sum(
            method == "POST" and path.endswith("/workspaces")
            for method, path, _ in calls
        )
        == 1
    )
    assert not any(path.endswith(("/retirement", "/decide")) for _, path, _ in calls)
    assert driver.page.closed == 5
    assert all(
        context["storage_state"]["origins"][0]["localStorage"] == []
        for context in driver.page.contexts
    )


@pytest.mark.parametrize("release", [None, "f" * 64])
def test_foreign_public_release_refuses_before_preview_or_creation(driver, release):
    driver.page.release = release
    result = driver.run()
    assert "public route release differs" in result["reason"]
    assert not (driver.path / "checkpoint.json").exists()
    assert all(method == "GET" for method, _, _ in driver.page.service.calls)


def test_runtime_failure_prevents_browser_launch(driver):
    driver.producer.runtime["source_revision"] = "f" * 40
    assert "runtime:" in driver.run()["reason"]
    assert not driver.page.contexts and not driver.page.service.calls


def test_wrong_requester_refuses_before_effects(driver, monkeypatch):
    original = driver.page.service.request

    def request(method, path, body=None):
        status, result = original(method, path, body)
        return status, {
            **result,
            "user_id": identity(99),
        } if path == "/api/auth/me" else result

    monkeypatch.setattr(driver.page.service, "request", request)
    assert "requester or organization differs" in driver.run()["reason"]
    assert all(method == "GET" for method, _, _ in driver.page.service.calls)


def test_browser_failure_is_sanitized_and_closes_context(driver):
    driver.page.visible = False
    result = driver.run()
    assert "private browser exception" not in json.dumps(result)
    assert "browser response unavailable" in result["reason"]
    assert driver.page.closed == 1


def test_documented_browser_command_executes_the_guarded_phase(driver):
    document = (Path(__file__).parent / "README.md").read_text()
    match = re.search(
        r"### Guarded browser creation and recovery.*?```sh\n(.*?)\n```",
        document,
        re.DOTALL,
    )
    assert match
    command = shlex.split(
        match[1].replace("\\\n", "").replace("$DEMO1_PRIVATE_DIR", str(driver.path))
    )
    assert demo1_cli.main(command[4:]) == 2
    result = json.loads((driver.path / "browser-report.json").read_text())
    assert result["browser"]["reason"] == "awaiting independent human approval"
    assert result["live_acceptance"] is False
    previous_calls = len(driver.producer.calls)
    assert demo1_cli.main(command[4:]) == 2
    assert len(driver.producer.calls) == previous_calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("broker_label", "other-label"),
        ("authority_ref", identity(90)),
        ("max_runtime_seconds", 899),
        ("cleanup_deadline", datetime(2026, 10, 7, tzinfo=UTC)),
    ],
)
def test_execution_checkpoint_binds_authority_fields(driver, field, value):
    driver.run()
    path = driver.path / "checkpoint.json"
    previous = path.read_bytes()
    envelope = replace(driver.envelope, **{field: value})
    with (
        PrivateCheckpoint(
            str(path), driver.selected, envelope.origin, envelope=envelope
        ) as store,
        pytest.raises(EvidenceError, match="selection differs"),
    ):
        store.load()
    assert path.read_bytes() == previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "foreign"),
        ("cluster_name", "foreign"),
        ("release_id", "f" * 64),
        ("role", "ForeignRole"),
        ("connection_id", identity(90)),
    ],
)
def test_execution_checkpoint_binds_runtime_target(driver, field, value):
    driver.run()
    target = replace(driver.envelope.runtime_target, **{field: value})
    envelope = replace(driver.envelope, runtime_target=target)
    with (
        PrivateCheckpoint(
            str(driver.path / "checkpoint.json"),
            driver.selected,
            envelope.origin,
            envelope=envelope,
        ) as store,
        pytest.raises(EvidenceError, match="selection differs"),
    ):
        store.load()


def test_legacy_checkpoint_is_not_rebound_or_used_for_creation(driver):
    path = driver.path / "checkpoint.json"
    with PrivateCheckpoint(str(path), driver.selected, driver.envelope.origin) as store:
        store.save(
            CreationCheckpoint(
                driver.selected.request_id,
                identity(10),
                driver.selected.plan_revision,
                identity(11),
                identity(13),
                submitted=True,
            )
        )
    previous = path.read_bytes()
    assert driver.run() is None
    assert path.read_bytes() == previous
    assert not driver.producer.calls and not driver.page.service.calls


def test_deadline_refuses_before_runtime_or_browser(driver):
    with (
        PrivateCheckpoint(
            str(driver.path / "checkpoint.json"),
            driver.selected,
            driver.envelope.origin,
            envelope=driver.envelope,
        ) as store,
        pytest.raises(EvidenceError, match="runtime exhausted"),
    ):
        demo1_journey.advance_browser(
            driver.selected,
            driver.envelope,
            driver.session,
            store,
            clock=lambda: driver.selected.deadline,
        )
    assert not driver.producer.calls and not driver.page.service.calls


@pytest.mark.parametrize(
    "path",
    [
        "/api/superplane/v1/../workspaces",
        "/api/superplane/v1/%2e/workspaces",
        "/api/superplane/v1/workspaces#foreign",
        "/api/superplane/v1//workspaces",
        "/api/foreign",
    ],
)
def test_transport_rejects_unapproved_paths_before_evaluation(driver, path):
    driver.page.url = driver.envelope.origin + "/superplane"
    transport = PlaywrightBrowserTransport(
        driver.page,
        driver.envelope.origin,
        release_id=driver.envelope.runtime_target.release_id,
    )
    with pytest.raises(EvidenceError, match="unapproved request path"):
        transport.request("POST", path, {})
    assert not driver.page.service.calls


def test_release_change_is_rechecked_before_mutation(driver):
    driver.run()
    driver.page.service.calls.clear()
    driver.page.release = "f" * 64
    transport = PlaywrightBrowserTransport(
        driver.page,
        driver.envelope.origin,
        release_id=driver.envelope.runtime_target.release_id,
    )
    with pytest.raises(EvidenceError, match="public route release differs"):
        transport.request("POST", "/api/superplane/v1/workspaces", {})
    assert all(method == "GET" for method, _, _ in driver.page.service.calls)


def test_missing_browser_dependency_refuses_before_remote_reads(driver, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert "Playwright" in driver.run()["reason"]
    assert not driver.producer.calls and not driver.page.service.calls


def test_changed_release_in_creation_reply_retains_submitted_identity(
    driver, monkeypatch
):
    driver.run()
    driver.page.service.approved = True
    evaluate = driver.page.evaluate

    def changed_reply(script, arguments):
        if "path" not in arguments:
            return evaluate(script, arguments)
        result = evaluate(script, arguments)
        if arguments["method"] == "POST" and arguments["path"].endswith("/workspaces"):
            result[2] = "f" * 64
        return result

    monkeypatch.setattr(driver.page, "evaluate", changed_reply)
    result = driver.run()
    assert result["checkpoint"]["submitted"] is True
    assert "uncertain" in result["browser"]["reason"]
    assert driver.run()["browser"]["creation_observed"] is True
    assert (
        sum(
            method == "POST" and path.endswith("/workspaces")
            for method, path, _ in driver.page.service.calls
        )
        == 1
    )


@pytest.mark.parametrize("failure", ["release", "denied", "unavailable"])
def test_final_presend_refusal_keeps_checkpoint_retryable(driver, monkeypatch, failure):
    driver.run()
    driver.page.service.approved = True
    checkpoint = driver.path / "checkpoint.json"
    original = checkpoint.read_bytes()
    evaluate = driver.page.evaluate
    approval_checked = False

    def refuse_final_probe(script, arguments):
        nonlocal approval_checked
        if "path" not in arguments:
            return evaluate(script, arguments)
        result = evaluate(script, arguments)
        if arguments["path"].endswith("/operation-approvals/" + identity(11)):
            approval_checked = True
        elif approval_checked and arguments["path"].endswith("/capabilities"):
            assert checkpoint.read_bytes() == original
            if failure == "unavailable":
                raise RuntimeError("private probe failure")
            if failure == "release":
                result[2] = "f" * 64
            else:
                result[0] = 503
        return result

    monkeypatch.setattr(driver.page, "evaluate", refuse_final_probe)
    result = driver.run()
    assert approval_checked
    assert result["checkpoint"]["submitted"] is False
    assert checkpoint.read_bytes() == original
    assert "not sent" in result["browser"]["reason"]
    assert "private probe failure" not in json.dumps(result)
    assert not any(
        method == "POST" and path.endswith("/workspaces")
        for method, path, _ in driver.page.service.calls
    )

    monkeypatch.setattr(driver.page, "evaluate", evaluate)
    assert driver.run()["browser"]["creation_observed"] is True
    assert driver.run()["browser"]["creation_observed"] is True
    persisted = json.loads(checkpoint.read_bytes())["checkpoint"]
    original_state = json.loads(original)["checkpoint"]
    assert persisted == {**original_state, "submitted": True}
    assert (
        sum(
            method == "POST" and path.endswith("/workspaces")
            for method, path, _ in driver.page.service.calls
        )
        == 1
    )


@pytest.mark.parametrize("written", [False, True])
def test_checkpoint_write_failure_never_sends_creation(driver, monkeypatch, written):
    driver.run()
    driver.page.service.approved = True
    checkpoint = driver.path / "checkpoint.json"
    save = PrivateCheckpoint.save

    def fail_save(store, state):
        assert state.submitted
        if written:
            save(store, state)
        raise EvidenceError("private persistence failure")

    monkeypatch.setattr(PrivateCheckpoint, "save", fail_save)
    result = driver.run()
    assert "checkpoint persistence failed" in result["reason"]
    assert "private persistence failure" not in json.dumps(result)
    assert json.loads(checkpoint.read_bytes())["checkpoint"]["submitted"] is written
    assert not any(
        method == "POST" and path.endswith("/workspaces")
        for method, path, _ in driver.page.service.calls
    )

    monkeypatch.setattr(PrivateCheckpoint, "save", save)
    if written:
        request = driver.page.service.request

        def missing_operation(method, path, body=None):
            result = request(method, path, body)
            if "/operations/by-idempotency/" in path:
                return 404, {"detail": "not found"}
            return result

        monkeypatch.setattr(driver.page.service, "request", missing_operation)
        prior = checkpoint.read_bytes()
        assert "response" in driver.run()["reason"]
        assert checkpoint.read_bytes() == prior
    else:
        assert driver.run()["browser"]["creation_observed"] is True
    assert sum(
        method == "POST" and path.endswith("/workspaces")
        for method, path, _ in driver.page.service.calls
    ) == (0 if written else 1)


def test_uncertain_evaluation_is_durably_marked_and_recovery_404_never_replays(
    driver, monkeypatch
):
    driver.run()
    driver.page.service.approved = True
    checkpoint = driver.path / "checkpoint.json"
    evaluate = driver.page.evaluate

    def uncertain_evaluation(script, arguments):
        if "path" not in arguments:
            return evaluate(script, arguments)
        if arguments["method"] == "POST" and arguments["path"].endswith("/workspaces"):
            assert (
                json.loads(checkpoint.read_bytes())["checkpoint"]["submitted"] is True
            )
            raise RuntimeError("uncertain browser evaluation")
        result = evaluate(script, arguments)
        if "/operations/by-idempotency/" in arguments["path"]:
            result[0] = 404
        return result

    monkeypatch.setattr(driver.page, "evaluate", uncertain_evaluation)
    assert "uncertain" in driver.run()["browser"]["reason"]
    saved = checkpoint.read_bytes()
    assert "response" in driver.run()["reason"]
    assert checkpoint.read_bytes() == saved
    assert not any(
        method == "POST" and path.endswith("/workspaces")
        for method, path, _ in driver.page.service.calls
    )
