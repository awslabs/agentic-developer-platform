"""Offline transport/recovery tests; these do not claim live acceptance."""

import copy
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

import security_manual_setup as manual
from evidence import AttemptJournal
from clients_setup import CliOperationError, McpOperationError


@pytest.fixture
def driver(tmp_path):
    value = object.__new__(manual.ManualSetupDriver)
    value.case = {"scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"},
                  "max_attempts": 3, "mcp_receipt": "original_mcp_receipt_1", "cli_receipt": "original_cli_receipt_1",
                  "ui_receipt": "original_ui_receipt_1", "cli_settings_file": str(tmp_path / "settings.json")}
    value.path = "/v1/products/product/projects/project/services/security"
    value.deadline = time.monotonic() + 120
    value.references = []
    value.wait_for_execution_consent = lambda: None
    value.retain = lambda **item: value.references.append(item)
    value.journal = AttemptJournal(tmp_path / "attempts.jsonl")
    return value


@pytest.mark.parametrize("delivery", ["normal", "lost_response", "not_delivered"])
def test_initial_mcp_receipt_reconciliation_keeps_same_key_body_and_one_run(driver, delivery):
    submitted = []
    run_input = {"binding_id": "binding", "configuration_revision": 3, "sources": []}
    run = {"run_id": "run", "input": run_input}
    state = {"exists": False}

    def transport(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            submitted.append(payload)
            assert payload["tool"] == "l5___submit_run"
            assert payload["arguments"]["input"] == run_input
            assert payload["arguments"]["idempotency_key"] == driver.case["mcp_receipt"]
            if len(submitted) == 1 and delivery == "not_delivered":
                raise httpx.ConnectError("offline request was not delivered", request=request)
            state["exists"] = True
            if len(submitted) == 1 and delivery == "lost_response":
                raise httpx.ReadError("offline response lost", request=request)
            return httpx.Response(200, json={"structuredContent": {"http_status": 202, "data": run}})
        assert request.url.path == driver.path + "/runs/receipts/" + driver.case["mcp_receipt"]
        return httpx.Response(200, json=run) if state["exists"] else httpx.Response(404)

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(transport)) as client:
        driver.mcp = client
        driver.read_api = lambda path: copy.deepcopy(run)
        result = driver.mcp_submit(run_input, driver.case["mcp_receipt"])
        assert result["run_id"] == "run"
        assert len(submitted) == (2 if delivery == "not_delivered" else 1)
        if len(submitted) == 2:
            assert submitted[0] == submitted[1]
        assert len(driver.journal.read()) == 2
        driver.mcp_submit(run_input, driver.case["mcp_receipt"])
        assert len(submitted) == (2 if delivery == "not_delivered" else 1)


@pytest.mark.parametrize("delivery", ["preparing", "not_delivered", "restart"])
def test_mcp_preparation_polls_only_original_receipt_until_worker_continues(driver, monkeypatch, delivery):
    run_input = {"configuration_revision": 3, "binding_id": "binding"}
    run = {"run_id": "run", "input": run_input}
    calls = []
    reads = []
    if delivery == "restart":
        driver.record(driver.case["mcp_receipt"], "mcp_submit", "prepared")
        driver.references.append({"kind": "mcp_source_preparing", "receipt": driver.case["mcp_receipt"],
                                  "input_sha256": manual.digest(run_input)})

    def transport(request):
        if request.method == "POST":
            calls.append(json.loads(request.content))
            assert calls[-1]["arguments"]["idempotency_key"] == driver.case["mcp_receipt"]
            assert calls[-1]["arguments"]["input"] == run_input
            if len(calls) == 1 and delivery == "not_delivered":
                raise httpx.ConnectError("offline request not delivered", request=request)
            return httpx.Response(200, json={"isError": True, "structuredContent": {
                "http_status": 503, "error": {"code": "source_capture_preparing"}}})
        reads.append(request.url.path)
        assert request.url.path == driver.path + "/runs/receipts/" + driver.case["mcp_receipt"]
        return httpx.Response(200, json=run) if len(reads) >= 4 else httpx.Response(404)

    deadline = driver.deadline
    monkeypatch.setattr(manual.time, "sleep", lambda _: None)
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(transport)) as client:
        driver.mcp = client
        driver.read_api = lambda path: copy.deepcopy(run)
        assert driver.mcp_submit(run_input, driver.case["mcp_receipt"]) == run
        assert len(calls) == {"preparing": 1, "not_delivered": 2, "restart": 0}[delivery]
        if len(calls) == 2:
            assert calls[0] == calls[1]
        assert len(driver.journal.read()) == 2
        assert driver.deadline == deadline
        # A resumed selector reuses the acknowledged run, without a new submit.
        driver.mcp_submit(run_input, driver.case["mcp_receipt"])
        assert len(calls) == {"preparing": 1, "not_delivered": 2, "restart": 0}[delivery]


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (409, "idempotency_conflict"),
                                        (503, "service_unavailable"), (500, "source_capture_preparing")])
def test_mcp_delivered_denial_never_replays_or_polls(driver, status, code):
    calls = []

    def transport(request):
        calls.append(request.method)
        assert request.method == "POST"
        return httpx.Response(200, json={"isError": True, "structuredContent": {
            "http_status": status, "error": {"code": code}}})

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(transport)) as client:
        driver.mcp = client
        with pytest.raises(McpOperationError) as failed:
            driver.mcp_submit({"configuration_revision": 3}, driver.case["mcp_receipt"])
        assert (failed.value.status_code, failed.value.code) == (status, code)
        assert calls == ["POST"]


@pytest.mark.parametrize("stop", ["deadline", "authorization"])
def test_mcp_preparation_polling_preserves_deadline_and_stops_on_denial(driver, monkeypatch, stop):
    calls = []
    deadline = driver.deadline

    def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"isError": True, "structuredContent": {
                "http_status": 503, "error": {"code": "source_capture_preparing"}}})
        return httpx.Response(403 if stop == "authorization" else 404)

    def sleep(_):
        assert driver.deadline == deadline
        monkeypatch.setattr(manual.time, "monotonic", lambda: deadline + 1)

    monkeypatch.setattr(manual.time, "sleep", sleep)
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(transport)) as client:
        driver.mcp = client
        error = AssertionError if stop == "deadline" else httpx.HTTPStatusError
        with pytest.raises(error) as failed:
            driver.mcp_submit({"configuration_revision": 3}, driver.case["mcp_receipt"])
        if stop == "deadline":
            assert "Source preparation receipt did not appear before the original authorized deadline" in str(failed.value)
    assert calls == ["POST", "GET"]
    assert driver.deadline == deadline
    assert len(driver.journal.read()) == 1


@pytest.mark.parametrize("status,replay", [(404, True), (403, False), (503, False)])
def test_cli_replays_only_missing_receipt_with_original_condition(driver, status, replay):
    settings = {"region": "us-west-2"}
    Path(driver.case["cli_settings_file"]).write_text(json.dumps(settings))
    driver.references.append({"kind": "cli_intent", "receipt": driver.case["cli_receipt"], "if_match": '"original"',
                              "request_sha256": manual.digest({"settings": settings, "if_match": '"original"'})})
    calls = []

    def cli(category, *args, **kwargs):
        calls.append((category, args, kwargs))
        if len(calls) == 1:
            raise CliOperationError(status)
        return {"detail": {"configuration": {"revision": 2}}}

    driver.cli = cli
    if replay:
        assert driver.reconcile_cli()["configuration"]["revision"] == 2
        assert calls[1][0:2] == ("config", ("update",))
        assert calls[1][2]["match"] == '"original"'
        assert calls[1][2]["receipt"] == driver.case["cli_receipt"]
    else:
        with pytest.raises(CliOperationError):
            driver.reconcile_cli()
        assert len(calls) == 1


@pytest.mark.parametrize("code", ["security_evidence_pending", "security_evidence_effect_outcome_unknown"])
def test_collect_waits_for_single_inflight_save_without_repeating_it(driver, monkeypatch, code):
    driver.api = object()
    driver.issue = lambda selected: {"request_id": "original", "tenant_id": "tenant"}
    calls = []
    state = {"receipts": [{"kind": "effect_before"}], "events": [], "status": "outcome_unknown", "request": {}}
    driver.remaining = lambda: 1 if len(calls) < 5 else (_ for _ in ()).throw(AssertionError("original deadline"))

    def product_call(client, path, body=None):
        calls.append((path, body))
        if body is None:
            return state
        assert path == "requests/original/reconcile" and body == {}
        response = httpx.Response(409, json={"detail": {"code": code}}, request=httpx.Request("POST", "https://offline.invalid"))
        response.raise_for_status()

    driver.product_call = product_call
    monkeypatch.setattr(manual.time, "sleep", lambda _: None)
    with pytest.raises(AssertionError, match="original deadline"):
        driver.collect({"phase": "edit"})
    assert len([body for _, body in calls if body is not None]) >= 2


def test_mcp_journal_limit_cannot_fall_through_to_receipt_replay(driver):
    driver.case["max_attempts"] = 1
    driver.record(driver.case["cli_receipt"], "cli_update", "prepared")
    driver.mcp = SimpleNamespace(get=lambda *args, **kwargs: pytest.fail("limit denial entered receipt lookup"))
    driver.mcp_call = lambda *args, **kwargs: pytest.fail("limit denial submitted another run")
    with pytest.raises(AssertionError, match="authorized bound"):
        driver.mcp_submit({"configuration_revision": 3}, driver.case["mcp_receipt"])
    assert len(driver.journal.read()) == 1


@pytest.mark.parametrize("changed", ["input", "receipt"])
def test_mcp_recovery_refuses_changed_original_intent(driver, changed):
    original = {"configuration_revision": 3}
    driver.references.append({"kind": "mcp_intent", "receipt": driver.case["mcp_receipt"],
                              "input_sha256": manual.digest(original)})
    driver.mcp = SimpleNamespace(get=lambda *args, **kwargs: pytest.fail("changed intent reached transport"))
    driver.mcp_call = lambda *args, **kwargs: pytest.fail("changed intent submitted another run")
    with pytest.raises(ValueError, match="original"):
        driver.mcp_submit({"configuration_revision": 4} if changed == "input" else original,
                          "different-receipt" if changed == "receipt" else driver.case["mcp_receipt"])


@pytest.mark.parametrize("code", ["delegation_denied", "delegation_scope_changed"])
def test_fresh_native_consent_waits_without_submitting(driver, monkeypatch, code):
    checks = []
    driver.case["provider"] = {"region": "us-east-1"}

    def verify(provider):
        checks.append(provider)
        if len(checks) == 1:
            response = httpx.Response(403, json={"detail": {"code": code}},
                                      request=httpx.Request("GET", "https://offline.invalid/provider"))
            response.raise_for_status()

    driver.verify_provider_authority = verify
    driver.mcp_call = lambda *args, **kwargs: pytest.fail("consent wait submitted a run")
    monkeypatch.setattr(manual.time, "sleep", lambda seconds: None)
    manual.ManualSetupDriver.wait_for_execution_consent(driver)
    assert len(checks) == 2
    assert not driver.journal.path.exists()


def test_native_consent_wait_preserves_deadline_and_unrelated_denials(driver, monkeypatch):
    driver.case["provider"] = {}
    code = "delegation_denied"

    def verify(provider):
        httpx.Response(403, json={"detail": {"code": code}},
                       request=httpx.Request("GET", "https://offline.invalid/provider")).raise_for_status()

    driver.verify_provider_authority = verify
    driver.deadline = time.monotonic() - 1
    monkeypatch.setattr(manual.time, "sleep", lambda seconds: pytest.fail("expired consent wait slept"))
    with pytest.raises(AssertionError, match="authorized deadline"):
        manual.ManualSetupDriver.wait_for_execution_consent(driver)
    code = "source_access_revoked"
    with pytest.raises(httpx.HTTPStatusError):
        manual.ManualSetupDriver.wait_for_execution_consent(driver)


def test_preflight_requires_both_original_tenants_and_real_human_owner():
    one = SimpleNamespace(case={"scope": {"tenant_id": "one"}}, api=object(),
                          product_call=lambda *args: {"user_id": "owner", "owner_id": "owner", "human_owner_eligibility": "designated_automation_account"})
    two = SimpleNamespace(case={"scope": {"tenant_id": "two"}})
    with pytest.raises(ValueError, match="legitimate human owner"):
        manual.preflight([one, two])
    with pytest.raises(ValueError, match="both original distinct"):
        manual.preflight([one, one])


def test_selector_and_independent_readback_cannot_enter_legacy_browser(monkeypatch):
    def poison(*args, **kwargs):
        raise AssertionError("browser execution is forbidden")

    for name in ("playwright", "playwright.sync_api", "playwright.async_api"):
        module = ModuleType(name)
        module.sync_playwright = module.async_playwright = poison
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(manual.LiveSetupDriver, "ui_edit", poison)
    monkeypatch.setattr(manual.LiveSetupDriver, "ui_observations", poison)
    root = Path(__file__).resolve().parents[2]
    selector_spec = importlib.util.spec_from_file_location("security_original_selector", root / "tests/e2e/test_l5_security_acceptance.py")
    selector = importlib.util.module_from_spec(selector_spec)
    selector_spec.loader.exec_module(selector)
    readback_spec = importlib.util.spec_from_file_location("security_original_readback", root / "scripts/qualify-security-readbacks.py")
    readback = importlib.util.module_from_spec(readback_spec)
    readback_spec.loader.exec_module(readback)
    # This entrypoint routing test complements the actual Gateway/SQL evidence
    # tests. It deliberately does not fabricate a passing selector log.
    calls = []
    fixture = {"cases": [{"scope": {"tenant_id": "one"}}, {"scope": {"tenant_id": "two"}}], "limits": {"max_attempts": 3}}

    class Driver:
        def __init__(self, case, *args, **kwargs):
            self.case = case
            calls.append(("created", case["scope"]["tenant_id"], kwargs))

        def run_setup(self, limits):
            calls.append(("setup", self.case["scope"]["tenant_id"]))

        def close(self):
            calls.append(("closed", self.case["scope"]["tenant_id"]))

    monkeypatch.setattr(manual, "ManualSetupDriver", Driver)
    monkeypatch.setattr(manual, "preflight", lambda values: calls.append(("preflight", len(values))))
    monkeypatch.setattr(selector, "load_live_fixture", lambda *args: fixture)
    monkeypatch.setattr(readback, "load_live_fixture", lambda *args: fixture)
    monkeypatch.setattr(readback, "verify_setup_readback", lambda case, limit, driver: calls.append(("readback", case["scope"]["tenant_id"])))
    monkeypatch.setenv("ADP_L5_SECURITY_LIVE_CONFIG", "/offline/fixture.json")
    monkeypatch.setenv("PRIVATE_FIXTURE", "/offline/fixture.json")
    monkeypatch.setenv("ADP_L5_SECURITY_LIVE_ENABLED", "I_ACCEPT_PAID_TESTS")
    selector.test_clients_and_setup()
    for case in fixture["cases"]:
        readback.read_setup(case, fixture["limits"], {})
    assert ("preflight", 2) in calls
    assert all(("setup", tenant) in calls and ("readback", tenant) in calls for tenant in ("one", "two"))
