"""Browser and installed-CLI selective financial confirmation on owned fixtures."""

import json
import re
from copy import deepcopy
from urllib.parse import quote

from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.cli_uplift.product_financials import (
    DATE_PATH,
    verify_financial_denial,
    verify_financial_progress,
    verify_selective_date_import,
)
from tests.e2e.new_ui.product_navigation import (
    open_product_section,
    product_session_status,
)
from tests.e2e.new_ui.test_product_resume import (
    installed_cli,
    read_profile,
    read_session,
)


def selective_date_import(page, provider, base_url, fixture, binary, checksums, session_id, proposal_id, source_ref, mode, record):
    if mode not in {"browser", "cli"}:
        raise ValueError("Selective date import needs a real browser or installed CLI")
    before_profile = read_profile(provider, base_url, fixture)
    before_session = read_session(provider, base_url, fixture, session_id)
    proposals = [item for item in before_session["proposals"] if item["id"] == proposal_id]
    if len(proposals) != 1 or DATE_PATH not in proposals[0]["remaining_paths"]:
        raise AssertionError("Authorized source date proposal is unavailable")
    expected_date = proposals[0]["configuration"]["financials"]["release_target_date"]
    if mode == "browser":
        route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
        inject_tokens_and_navigate(page, fixture["owner"], path=route)
        open_product_section(page, "Financials")
        page.get_by_role("button", name=f"Accept suggested date from {proposal_id}").click()
        from playwright.sync_api import expect

        expect(page.get_by_text(f"Release target: {expected_date}")).to_be_visible()
        client_session = read_session(provider, base_url, fixture, session_id)
    else:
        client_session = installed_cli(binary, checksums, fixture["owner"], base_url,
            ["step", "--product", fixture["product_id"], "--project", fixture["project_id"],
             "--session", session_id, "--section", "financials", "--revision", str(before_session["revision"]),
             "--idempotency-key", fixture["confirmation_key"], "--input", "-"],
            input_body={"action": "confirm", "proposal_id": proposal_id, "confirmed_paths": [DATE_PATH]}, record=record)
    after_profile = read_profile(provider, base_url, fixture)
    after_session = read_session(provider, base_url, fixture, session_id)
    independent_session = read_session(provider, base_url, fixture, session_id)
    verify_selective_date_import(before_profile, before_session, client_session, after_profile,
                                 after_session, independent_session, proposal_id=proposal_id,
                                 expected_date=expected_date, source_ref=source_ref)
    return {"mode": mode, "revision": after_session["revision"], "date": expected_date,
            "remaining_paths": after_session["proposals"][0]["remaining_paths"]}


def financial_denial(page, provider, base_url, fixture, binary, checksums, session_id,
                     case, mode, proposal_id, observe_effects, record, source_markers):
    if mode not in {"browser", "cli", "api"} or case not in {"stale", "expired", "revoked", "invalid_period"}:
        raise ValueError("Unknown financial denial client or case")
    if case in {"expired", "revoked"} and mode == "browser":
        raise ValueError("Hidden source proposals cannot be submitted through the browser")
    route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    path = base_url + "/api/v1" + route + "/onboarding-sessions/" + quote(session_id, safe="") + "/steps/financials"

    def snapshot():
        effects = observe_effects()
        if not isinstance(effects, dict) or set(effects) != {"operations", "funding", "actuals", "fx"}:
            raise AssertionError("Independent financial effect readback is unavailable")
        return deepcopy({"profile": read_profile(provider, base_url, fixture),
                         "session": read_session(provider, base_url, fixture, session_id), **effects})

    before = snapshot()
    body = ({"action": "save", "configuration": {"financials": {
        **before["profile"]["configuration"]["financials"],
        "allocations": [{**before["profile"]["configuration"]["financials"]["allocations"][0],
                         "period_start": "2027-01-01", "period_end": "2026-12-31"}]}}}
        if case == "invalid_period" else {"action": "confirm", "proposal_id": proposal_id,
                                           "confirmed_paths": [DATE_PATH]})
    if mode == "browser":
        inject_tokens_and_navigate(page, fixture["owner"], path=route)
        open_product_section(page, "Financials")
        with page.expect_response(lambda response: response.url == path and response.request.method == "PUT") as sent:
            if case == "invalid_period":
                page.get_by_label("Period start").fill("2027-01-01")
                page.get_by_role("button", name="Save proposed allocation").click()
            else:
                page.get_by_role("button", name=f"Accept suggested date from {proposal_id}").click()
        response = {"status": sent.value.status, "body": sent.value.json()}
    elif mode == "cli":
        try:
            installed_cli(binary, checksums, fixture["owner"], base_url,
                ["step", "--product", fixture["product_id"], "--project", fixture["project_id"],
                 "--session", session_id, "--section", "financials", "--revision", str(before["session"]["revision"]),
                 "--idempotency-key", fixture["denial_key"], "--input", "-"], input_body=body, record=record)
        except AssertionError:
            event = record.cli_events[-1]
            result = json.loads(event["stdout"])
            message = result["error"]["message"]
            match = re.search(r"\bHTTP (\d{3})\b", message)
            if event["exit_code"] == 0 or result["status"] != "failed" or match is None:
                raise AssertionError("Installed CLI did not report an HTTP denial") from None
            response = {"status": int(match.group(1)), "body": {"detail": {"code": result["error"]["code"],
                                                                    "message": message}}}
        else:
            raise AssertionError("Installed CLI unexpectedly accepted the denied financial mutation")
    else:
        sent = provider.request.put(path, data=body,
            headers={"Authorization": "Bearer " + fixture["owner"]["access_token"],
                     "If-Profile-Revision": str(before["session"]["revision"]),
                     "Idempotency-Key": fixture["denial_key"]}, timeout=30_000)
        response = {"status": sent.status, "body": sent.json()}
    after = snapshot()
    verify_financial_denial(before, response, after, case=case, source_markers=source_markers)
    return {"case": case, "mode": mode, "status": response["status"], "cli_exit_code":
            record.cli_events[-1]["exit_code"] if mode == "cli" else None}


def recover_financial_confirmation(page, provider, base_url, fixture, binary, checksums, session_id,
                                   proposal_id, source_ref, observe_receipts, observe_source, record):
    from playwright.sync_api import expect

    scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
    route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    step_url = base_url + "/api/v1" + route + "/onboarding-sessions/" + quote(session_id, safe="") + "/steps/financials"
    before_profile = read_profile(provider, base_url, fixture)
    before_session = read_session(provider, base_url, fixture, session_id)
    candidates = [proposal for proposal in before_session["proposals"] if proposal["id"] == proposal_id]
    assert len(candidates) == 1 and DATE_PATH in candidates[0]["remaining_paths"]
    expected_date = candidates[0]["configuration"]["financials"]["release_target_date"]
    before_receipts, source_before = deepcopy(observe_receipts()), deepcopy(observe_source())
    dropped = []

    def lose_acknowledgement(route_request):
        if route_request.request.method != "PUT":
            route_request.continue_()
            return
        response = route_request.fetch(timeout=30_000)
        assert response.status == 200, "Confirmation was not committed before response loss"
        dropped.append(route_request.request.headers.get("idempotency-key"))
        route_request.abort("failed")

    inject_tokens_and_navigate(page, fixture["owner"], path=route)
    open_product_section(page, "Financials")
    page.route(step_url, lose_acknowledgement)
    try:
        page.get_by_role("button", name=f"Accept suggested date from {proposal_id}").click()
        expect(page.get_by_text("Could not confirm the suggested date. Check saved values before retrying.")).to_be_visible(timeout=30_000)
    finally:
        page.unroute(step_url, lose_acknowledgement)
    assert len(dropped) == 1 and isinstance(dropped[0], str) and len(dropped[0]) >= 16
    assert read_session(provider, base_url, fixture, session_id)["revision"] == before_session["revision"] + 1

    body = {"action": "confirm", "proposal_id": proposal_id, "confirmed_paths": [DATE_PATH]}
    replay_session = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", *scope, "--session", session_id, "--section", "financials", "--revision", str(before_session["revision"]),
         "--idempotency-key", dropped[0], "--input", "-"], input_body=body, record=record)
    cli_session = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["resume", *scope, "--session", session_id], record=record)
    after_profile = read_profile(provider, base_url, fixture)
    after_session = read_session(provider, base_url, fixture, session_id)
    after_receipts = deepcopy(observe_receipts())
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_text(f"Release target: {expected_date}")).to_be_visible(timeout=30_000)
    browser_session = read_session(provider, base_url, fixture, session_id)

    page.get_by_role("button", name="Financials Later").click()
    expect(product_session_status(page)).to_contain_text("2/3 checks · 66%")
    later_session = read_session(provider, base_url, fixture, session_id)
    later_profile = read_profile(provider, base_url, fixture)
    excluded = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", *scope, "--session", session_id, "--section", "financials", "--revision", str(later_session["revision"]),
         "--idempotency-key", fixture["exclusion_key"], "--input", "-"],
        input_body={"action": "not_applicable", "reason": "No financial approval requested"}, record=record)
    excluded_session = read_session(provider, base_url, fixture, session_id)
    assert excluded == excluded_session
    excluded_profile = read_profile(provider, base_url, fixture)
    verify_financial_progress(later_session, later_profile, excluded_session, excluded_profile)
    source_after = deepcopy(observe_source())
    return {"revision": after_session["revision"], "confirmation_key": dropped[0],
            "later": later_session["progress"], "not_applicable": excluded_session["progress"],
            "resume_args": [before_profile, before_session, browser_session, cli_session, after_profile,
                            after_session, replay_session, before_receipts, after_receipts, source_before, source_after],
            "resume_expected": {"proposal_id": proposal_id, "expected_date": expected_date,
                                "source_ref": source_ref, "receipt_id": dropped[0], "run_id": fixture["run_id"]}}
