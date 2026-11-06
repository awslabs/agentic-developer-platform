"""Offline guards for the real V09 selective import selector."""

import json
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

import pytest

from tests.e2e.new_ui import test_product_financials as finance_client
from tests.e2e.product_onboarding import test_v09

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def make_run():
    calls = []
    fixture = {"owned_fixture": True, "run_id": "owned-run", "source_revision": SOURCE,
               "frontend_release": {}, "evidence_context": {"deployment": {"observed": {"product_onboarding_enabled": True}}}}
    run = {"fixture": fixture, "requirements_revision": REQUIREMENTS, "deployment_source_revision": SOURCE,
           "page": object(), "provider": SimpleNamespace(request=object()), "base_url": "https://example.invalid",
           "binary": "/private/bin/adp", "checksums": {"adp": "c" * 64}, "session_id": "session-1",
           "proposal_id": "suggestion-1", "source_ref": {"record_id": "business-case"}, "record": object(),
           "mode": "browser", "cleanup": lambda: calls.append("cleanup") or {"run_id": "owned-run",
               "source_revision": SOURCE, "status": "verified", "owned_absent": True,
               "shared_unchanged": True, "receipt_ref": "private:cleanup"}}
    return run, calls


def test_selector_invokes_real_client_driver_and_owned_cleanup(monkeypatch):
    run, calls = make_run()
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: calls.append("release"))
    monkeypatch.setattr(test_v09, "selective_date_import", lambda *args: calls.append("client") or {"mode": args[-2]})
    assert test_v09.run_financial_selective_import(run, SOURCE, REQUIREMENTS)["result"] == {"mode": "browser"}
    assert calls == ["release", "client", "cleanup"]


@pytest.mark.parametrize("change", [
    {"deployment_source_revision": "c" * 40}, {"requirements_revision": "d" * 40},
    {"proposal_id": None}, {"source_ref": None}, {"mode": "fake"},
])
def test_selector_rejects_unpinned_or_incomplete_fixture(change):
    run, calls = make_run()
    run.update(change)
    with pytest.raises(ValueError, match="pinned deployed"):
        test_v09.run_financial_selective_import(run, SOURCE, REQUIREMENTS)
    assert calls == []


def test_selector_fails_when_cleanup_is_unverified(monkeypatch):
    run, _calls = make_run()
    run["cleanup"] = lambda: {"status": "failed"}
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: None)
    monkeypatch.setattr(test_v09, "selective_date_import", lambda *_args: {})
    with pytest.raises(AssertionError, match="cleanup"):
        test_v09.run_financial_selective_import(run, SOURCE, REQUIREMENTS)


def test_live_selector_does_not_synthesize_missing_deployment(monkeypatch):
    monkeypatch.setenv("PRODUCT_V09_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V09_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V09_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No authorized deployed fixture"))
    with pytest.raises(pytest.fail.Exception, match="No authorized deployed fixture"):
        test_v09.live_financial_selective_import(request, "browser")


def test_denial_selector_requires_effect_observer_and_reconciled_cleanup(monkeypatch):
    run, calls = make_run()
    run.update(case="expired", mode="cli", observe_effects=dict, source_markers=["private-source-id"])
    run["fixture"]["denial_key"] = "denial-example-key-123"
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: calls.append("release"))
    monkeypatch.setattr(test_v09, "financial_denial", lambda *args: calls.append("client") or {"case": args[7]})
    assert test_v09.run_financial_denial(run, SOURCE, REQUIREMENTS)["result"] == {"case": "expired"}
    assert calls == ["release", "client", "cleanup"]


def test_denial_selector_rejects_missing_observer_and_unverified_cleanup(monkeypatch):
    run, calls = make_run()
    run.update(case="revoked", mode="api", source_markers=[])
    run["fixture"]["denial_key"] = "denial-example-key-123"
    with pytest.raises(ValueError, match="pinned deployed"):
        test_v09.run_financial_denial(run, SOURCE, REQUIREMENTS)
    assert calls == []
    run["observe_effects"] = dict
    run["cleanup"] = lambda: {"status": "failed"}
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: None)
    monkeypatch.setattr(test_v09, "financial_denial", lambda *_args: {})
    with pytest.raises(AssertionError, match="cleanup"):
        test_v09.run_financial_denial(run, SOURCE, REQUIREMENTS)


@pytest.mark.parametrize(("case", "mode", "status"), (("invalid_period", "browser", 422),
                                                       ("expired", "cli", 410), ("revoked", "api", 403)))
def test_denial_driver_uses_real_client_surface_and_independent_observations(monkeypatch, case, mode, status):
    calls = []
    fixture = {"product_id": "product-1", "project_id": "project-1", "owner": {"access_token": "synthetic-token"},
               "denial_key": "denial-example-key-123"}
    profile = {"configuration": {"financials": {"allocations": [{
        "amount": {"amount": "75.00", "currency": "USD"}, "approval_status": "proposed",
        "period_start": "2026-10-01", "period_end": "2026-12-31"}]}}, "provenance": []}
    session = {"id": "session-1", "revision": 2, "proposals": []}
    monkeypatch.setattr(finance_client, "read_profile", lambda *_args: calls.append("profile") or profile)
    monkeypatch.setattr(finance_client, "read_session", lambda *_args: calls.append("session") or session)
    monkeypatch.setattr(finance_client, "inject_tokens_and_navigate", lambda *_args, **_kwargs: calls.append("navigate"))
    monkeypatch.setattr(finance_client, "open_product_section", lambda _page, section: calls.append("section:" + section))
    message = {"detail": {"code": "denied"}}
    reply = SimpleNamespace(status=status, json=lambda: message)
    page = SimpleNamespace(
        expect_response=lambda _predicate: nullcontext(SimpleNamespace(value=reply)),
        get_by_label=lambda _label: SimpleNamespace(fill=lambda _value: calls.append("fill")),
        get_by_role=lambda *_args, **_kwargs: SimpleNamespace(click=lambda: calls.append("click")),
    )
    def cli_denial(_binary, _checksums, _owner, _url, arguments, *, input_body, record):
        calls.append("cli")
        assert arguments[:2] == ["step", "--product"] and input_body["confirmed_paths"] == [finance_client.DATE_PATH]
        record.cli_events.append({"exit_code": 5, "stdout": json.dumps({"status": "failed",
            "error": {"code": "proposal_expired", "message": "ADP returned HTTP 410 (proposal_expired)."}})})
        raise AssertionError("Expected CLI rejection")
    monkeypatch.setattr(finance_client, "installed_cli", cli_denial)
    provider = SimpleNamespace(request=SimpleNamespace(put=lambda *_args, **_kwargs: calls.append("api") or reply))
    record = SimpleNamespace(cli_events=[])
    result = finance_client.financial_denial(page, provider, "https://example.invalid", fixture, "adp", {}, "session-1",
        case, mode, "suggestion-1", lambda: calls.append("effects") or {"operations": [],
            "funding": {"approvals": [], "limits": []}, "actuals": [], "fx": []}, record, ["private-source-id"])
    assert result["status"] == status
    assert result["cli_exit_code"] == (5 if mode == "cli" else None)
    assert calls.count("effects") == 2 and calls.count("profile") == 2 and calls.count("session") == 2
    assert ("click" if mode == "browser" else mode) in calls
    if mode == "browser":
        assert calls.index("section:Financials") < calls.index("click")


def test_resume_selector_reconciles_after_verified_cleanup(monkeypatch):
    run, calls = make_run()
    run["fixture"]["exclusion_key"] = "exclude-example-key-123"
    run.update(observe_receipts=list, observe_source=lambda: {"revision": "source-1"})
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: calls.append("release"))
    monkeypatch.setattr(test_v09, "recover_financial_confirmation", lambda *_args: calls.append("clients") or {
        "revision": 3, "later": {"percentage": 66}, "not_applicable": {"percentage": 100},
        "resume_args": ["independent-readback"], "resume_expected": {"receipt_id": "same-key"}})
    def verify(*args, **kwargs):
        assert args[0] == "independent-readback"
        assert args[1]["status"] == "verified" and args[1]["owned_absent"] is True
        assert kwargs == {"receipt_id": "same-key"}
        calls.append("oracle")

    monkeypatch.setattr(test_v09, "verify_financial_resume", verify)
    assert test_v09.run_financial_resume(run, SOURCE, REQUIREMENTS)["not_applicable"]["percentage"] == 100
    assert calls == ["release", "clients", "cleanup", "oracle"]


def test_resume_selector_rejects_missing_observer_or_cleanup(monkeypatch):
    run, calls = make_run()
    run["fixture"]["exclusion_key"] = "exclude-example-key-123"
    with pytest.raises(ValueError, match="pinned deployed"):
        test_v09.run_financial_resume(run, SOURCE, REQUIREMENTS)
    assert calls == []
    run.update(observe_receipts=list, observe_source=dict, cleanup=lambda: {"status": "failed"})
    monkeypatch.setattr(test_v09, "check_served_assets", lambda *_args: None)
    monkeypatch.setattr(test_v09, "recover_financial_confirmation", lambda *_args: {})
    with pytest.raises(AssertionError, match="cleanup"):
        test_v09.run_financial_resume(run, SOURCE, REQUIREMENTS)


def test_recovery_driver_drops_committed_response_then_replays_same_key(monkeypatch):
    calls = []
    state = {"stage": "initial", "route": None}
    browser = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.expect = lambda _element: SimpleNamespace(to_be_visible=lambda **_kwargs: None,
                                                       to_contain_text=lambda *_args: None)
    monkeypatch.setitem(sys.modules, "playwright", browser)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(finance_client, "inject_tokens_and_navigate", lambda *_args, **_kwargs: calls.append("navigate"))
    monkeypatch.setattr(finance_client, "open_product_section", lambda _page, section: calls.append("section:" + section))
    monkeypatch.setattr(finance_client, "product_session_status", lambda _page: object())
    fixture = {"product_id": "product-1", "project_id": "project-1", "run_id": "owned-run",
               "owner": {"access_token": "synthetic-token"}, "exclusion_key": "exclude-owned-key-123"}
    proposal = {"id": "business-case-1", "remaining_paths": [finance_client.DATE_PATH, "/configuration/financials/allocations"],
                "configuration": {"financials": {"release_target_date": "2026-12-01"}}}

    def session(*_args):
        calls.append("session")
        return {"id": "session-1", "revision": {"initial": 2, "confirmed": 3, "later": 4, "excluded": 5}[state["stage"]],
                "proposals": [proposal] if state["stage"] == "initial" else [],
                "progress": {"percentage": 66 if state["stage"] == "later" else 100}}

    monkeypatch.setattr(finance_client, "read_session", session)
    monkeypatch.setattr(finance_client, "read_profile", lambda *_args: calls.append("profile") or {
        "configuration": {"financials": {"allocations": [{"amount": {"amount": "75.00", "currency": "USD"}}]}}})
    monkeypatch.setattr(finance_client, "verify_financial_progress", lambda *args:
        calls.append("progress") if (args[0]["revision"], args[2]["revision"]) == (4, 5)
        else pytest.fail("Wrong progress states"))

    def installed(_binary, _checksums, _owner, _url, arguments, *, input_body=None, record=None):
        if arguments[0] == "step" and input_body and input_body["action"] == "confirm":
            assert arguments[arguments.index("--idempotency-key") + 1] == "same-receipt-key-123"
            calls.append("replay")
        elif arguments[0] == "step":
            assert input_body["action"] == "not_applicable"
            state["stage"] = "excluded"
            calls.append("excluded")
        else:
            calls.append("cli-resume")
        return session()

    monkeypatch.setattr(finance_client, "installed_cli", installed)

    def click(name):
        if name.startswith("Accept suggested date"):
            def fetch(**_kwargs):
                state["stage"] = "confirmed"
                return SimpleNamespace(status=200)
            route_request = SimpleNamespace(request=SimpleNamespace(method="PUT", headers={
                "idempotency-key": "same-receipt-key-123"}), fetch=fetch,
                abort=lambda reason: calls.append("dropped:" + reason))
            state["route"](route_request)
        elif name == "Financials Later":
            state["stage"] = "later"
        calls.append("click:" + name)

    page = SimpleNamespace(route=lambda _url, handler: state.update(route=handler),
                           unroute=lambda _url, _handler: state.update(route=None),
                           get_by_role=lambda _role, name=None: SimpleNamespace(click=lambda: click(name)),
                           get_by_text=lambda _text: object(), reload=lambda **_kwargs: calls.append("reload"))
    reads = iter(([], [{"idempotency_key": "same-receipt-key-123", "action": "confirm"}]))
    result = finance_client.recover_financial_confirmation(page, SimpleNamespace(), "https://example.invalid",
        fixture, "adp", {}, "session-1", "business-case-1", {"record_id": "business-case"},
        lambda: next(reads), lambda: {"revision": "source-1"}, SimpleNamespace(cli_events=[]))
    assert result["confirmation_key"] == "same-receipt-key-123"
    assert result["resume_args"][8] == [{"idempotency_key": "same-receipt-key-123", "action": "confirm"}]
    assert state["route"] is None
    assert calls.index("dropped:failed") < calls.index("replay") < calls.index("cli-resume")
    assert calls.index("section:Financials") < calls.index("dropped:failed")
    assert "progress" in calls and "excluded" in calls
