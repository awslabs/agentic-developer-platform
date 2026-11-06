"""Live cross-client onboarding requires a pinned release and run-owned Product."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from modules.gateway.tests.products.product_resume import assert_resume
from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.new_ui.product_navigation import open_product_section, product_session_status
from tests.e2e.new_ui.product_session_recovery import SessionRecovery
from tests.e2e.new_ui.product_release import check_served_assets


def checked_fixture(fixture, source_revision):
    if (fixture.get("source_revision") != source_revision
            or not re.fullmatch(r"[a-f0-9]{40}", source_revision)
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not fixture.get("state_store") or not fixture.get("protected_cleanup")
            or not all(isinstance(fixture.get(key), str) and fixture[key]
                       for key in ("tenant_id", "actor_id", "product_id", "project_id", "run_id"))
            or fixture.get("owner", {}).get("actor_id") != fixture["actor_id"]
            or fixture.get("owner", {}).get("tenant_id") != fixture["tenant_id"]):
        raise ValueError("Resume needs a pinned, run-owned Product and enabled onboarding release")
    return fixture


def installed_cli(binary, checksums, owner, origin, arguments, *, input_body=None, record=None, command="onboard"):
    if command not in ("onboard", "product"):
        raise ValueError("Unknown installed Product command")
    binary = Path(binary)
    if binary.name != "adp" or not binary.is_file() or not isinstance(checksums, dict) or not all(
        re.fullmatch(r"[a-f0-9]{64}", checksums.get(name, ""))
        and (binary.parent / name).is_file()
        and hashlib.sha256((binary.parent / name).read_bytes()).hexdigest() == checksums[name]
        for name in ("adp", "adp-" + command + ".py")
    ):
        raise ValueError("Installed onboarding CLI differs from pinned helper checksums")
    with tempfile.TemporaryDirectory(prefix="product-resume-") as directory:
        home = Path(directory)
        config = home / ".bedrock-gateway"
        config.mkdir(mode=0o700)
        for name, value in (
            ("config.json", {"gateway_url": origin}),
            ("tokens.json", {"access_token": owner["access_token"], "id_token": owner["id_token"],
                             "refresh_token": owner.get("refresh_token", ""),
                             "expires_at": int(time.time()) + int(owner["expires_in"]) - 60}),
        ):
            path = config / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        environment = {"HOME": directory, "BG_CONFIG_DIR": str(config), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "ADP_TENANT": owner["tenant_id"]}
        result = subprocess.run([str(binary), command, *arguments, "--json"],
                                input=json.dumps(input_body) if input_body is not None else None,
                                env=environment, capture_output=True, text=True, timeout=60, check=False)
        if record is not None:
            def scrub(text):
                for token in (owner["access_token"], owner["id_token"], owner.get("refresh_token", "")):
                    if token:
                        text = text.replace(token, "[REDACTED]")
                return re.sub(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", text)

            record.cli_events.append({"command": ["adp", command, *arguments, "--json"],
                                      "exit_code": result.returncode, "stdout": scrub(result.stdout),
                                      "stderr": scrub(result.stderr), "installed_cli_sha256": checksums["adp"]})
        if result.returncode != 0:
            raise AssertionError("Installed onboarding CLI failed; inspect its private attempt report")
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise AssertionError("Installed onboarding CLI did not return JSON") from None
        if payload.get("status") != "ok" or not isinstance(payload.get("detail"), dict):
            raise AssertionError("Installed onboarding CLI did not return an OK session")
        return payload["detail"]


def read_session(provider, base_url, fixture, session_id):
    path = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    evidence = fixture.get("evidence_record")
    request = evidence.request(provider.request) if evidence is not None else provider.request
    response = request.get(base_url + "/api/v1" + path + "/onboarding-sessions/" + quote(session_id, safe=""),
                                    headers={"Authorization": "Bearer " + fixture["observer"]["access_token"]}, timeout=30_000)
    assert response.status == 200, "Independent session readback is unavailable"
    return response.json()


def read_profile(provider, base_url, fixture):
    path = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    evidence = fixture.get("evidence_record")
    request = evidence.request(provider.request) if evidence is not None else provider.request
    response = request.get(base_url + "/api/v1" + path,
                                    headers={"Authorization": "Bearer " + fixture["observer"]["access_token"]}, timeout=30_000)
    assert response.status == 200, "Independent Product progress readback is unavailable"
    return response.json()


def assert_deferred_profile(profile, fixture):
    assert profile["product_id"] == fixture["product_id"]
    assert profile["project_id"] == fixture["project_id"]
    assert profile["tenant_id"] == fixture["tenant_id"]
    assert type(profile["revision"]) is int and profile["revision"] >= 1
    assert profile["context_status"] == "unavailable"
    availability = profile["peer_availability"]
    assert set(availability) == {"context", "l5"}, "Both optional peers must have explicit states"
    for peer, name in (("context", "Product Context"), ("l5", "L5 services")):
        state = availability[peer]
        assert set(state) == {"status", "reason", "message"}, "Deferred peer cannot report a receipt or result"
        assert state["status"] == "unavailable"
        assert state["reason"] == "integration_deferred"
        assert name in state["message"] and "saved Product remains available" in state["message"]
    checks = profile["progress"]["checks"]
    assert isinstance(checks, list)
    assert not any(check["id"] in ("context", "l5") and check.get("required") for check in checks)
    return availability


def assert_progress(session, profile, stage):
    expected = {"initial": (2, 2, 100), "selected": (2, 3, 66),
                "later": (2, 3, 66), "not_applicable": (2, 2, 100)}
    financial_status = {"initial": None, "selected": "missing", "later": "later",
                        "not_applicable": "not_applicable"}
    if stage not in expected:
        raise ValueError("Progress stage is not a reviewed scenario")
    progress = session["progress"]
    assert progress == profile["progress"]
    assert progress["rule_version"] == "profile-1"
    assert (progress["completed"], progress["applicable"], progress["percentage"]) == expected[stage]
    checks = progress["checks"]
    assert [check["id"] for check in checks] == ["information", "project_scope"] + (["financials"] if stage != "initial" else [])
    assert all(check["status"] == "complete" and check["required"] is True for check in checks[:2])
    if stage != "initial":
        assert checks[2]["status"] == financial_status[stage] and checks[2]["required"] is False
    assert session["steps"]["information"]["status"] == "complete"
    assert all(session["steps"][section]["status"] == "not_started" for section in ("basic", "advanced"))
    assert session["steps"]["financials"]["status"] == (
        {"initial": "not_started", "selected": "in_progress", "later": "later",
         "not_applicable": "not_applicable"}[stage]
    )
    assert session["operation_ids"] == []
    configuration = profile["configuration"]
    assert not (configuration.get("financials") or {}).get("allocations")
    assert not (configuration.get("financials") or {}).get("budget_policy_ref")
    assert not (configuration.get("governance") or {}).get("execution_policy_ref")
    if stage == "not_applicable":
        assert checks[2].get("reason") == session["steps"]["financials"].get("reason") == "No financial plan yet"


def exercise_resume(page, base_url, deployed_features, product_resume_fixture, product_resume_provider, cli_first):
    from playwright.sync_api import expect

    fixture = checked_fixture(product_resume_fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
    assert deployed_features.get("product_onboarding") is True
    check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"], fixture["source_revision"])
    binary = os.environ["PRODUCT_RESUME_CLI_PATH"]
    checksums = json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"])
    scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
    route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    if cli_first:
        profile = fixture["evidence_record"].request(product_resume_provider.request).get(base_url + "/api/v1" + route,
            headers={"Authorization": "Bearer " + fixture["observer"]["access_token"]}, timeout=30_000)
        assert profile.status == 200
        availability = assert_deferred_profile(profile.json(), fixture)
        started = installed_cli(binary, checksums, fixture["owner"], base_url,
            ["start", *scope, "--revision", str(profile.json()["revision"]),
             "--idempotency-key", "resume_cli_" + fixture["run_id"].replace("-", "_")], record=fixture["evidence_record"])
        first = read_session(product_resume_provider, base_url, fixture, started["id"])
        assert_resume(first, started, first, product_id=fixture["product_id"], project_id=fixture["project_id"])
    inject_tokens_and_navigate(page, fixture["owner"], path=route)
    if cli_first:
        page.get_by_role("button", name="Context & services", exact=True).click()
        peer_section = page.get_by_role("region", name="Context and Services")
        for peer, name in (("context", "Product Context"), ("l5", "L5 services")):
            expect(peer_section.get_by_role("heading", name=f"{name}: unavailable")).to_be_visible()
            expect(peer_section).to_contain_text(availability[peer]["message"])
    open_product_section(page, "Financials")
    expect(page.get_by_role("button", name="Start or resume onboarding")).to_be_visible()
    page.get_by_role("button", name="Start or resume onboarding").click()
    expect(product_session_status(page)).to_contain_text("Session ")
    if not cli_first:
        session_id = re.search(r"Session ([^ ]+) · Revision", product_session_status(page).inner_text()).group(1)
        first = read_session(product_resume_provider, base_url, fixture, session_id)
    else:
        session_id = started["id"]
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {first['revision']}")
    if not cli_first:
        resumed = installed_cli(binary, checksums, fixture["owner"], base_url,
                                ["resume", *scope, "--session", session_id], record=fixture["evidence_record"])
        assert_resume(first, resumed, read_session(product_resume_provider, base_url, fixture, session_id),
                      product_id=fixture["product_id"], project_id=fixture["project_id"])
    page.reload(wait_until="domcontentloaded")
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {first['revision']}")
    assert_resume(first, read_session(product_resume_provider, base_url, fixture, session_id),
                  read_session(product_resume_provider, base_url, fixture, session_id),
                  product_id=fixture["product_id"], project_id=fixture["project_id"])
    if cli_first:
        page.get_by_role("button", name="Context & services", exact=True).click()
        peer_section = page.get_by_role("region", name="Context and Services")
        for peer, name in (("context", "Product Context"), ("l5", "L5 services")):
            expect(peer_section.get_by_role("heading", name=f"{name}: unavailable")).to_be_visible()
            expect(peer_section).to_contain_text(availability[peer]["message"])
        before = read_profile(product_resume_provider, base_url, fixture)
        assert assert_deferred_profile(before, fixture) == availability
        purpose = before["product"]["purpose"] + " after unavailable peer resume"
        open_product_section(page)
        page.get_by_label("Purpose").fill(purpose)
        page.get_by_role("button", name="Save changes").click()
        expect(page.get_by_text(f"Saved revision {before['revision'] + 1}")).to_be_visible()
        after = read_profile(product_resume_provider, base_url, fixture)
        assert after["revision"] == before["revision"] + 1
        assert after["product"]["purpose"] == purpose
        assert assert_deferred_profile(after, fixture) == availability
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_label("Purpose")).to_have_value(purpose)
        final = read_session(product_resume_provider, base_url, fixture, session_id)
        expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {final['revision']}")
        assert final["id"] == first["id"] and final["resume_path"] == first["resume_path"]
        assert final["operation_ids"] == []
        return {"started": started, "first": first, "final": final, "before": before, "after": after}


@pytest.mark.preview_on
def test_resume_ui_to_cli(page, base_url, deployed_features, product_resume_fixture, product_resume_provider):
    exercise_resume(page, base_url, deployed_features, product_resume_fixture, product_resume_provider, False)


@pytest.mark.preview_on
def test_resume_cli_to_ui(page, base_url, deployed_features, product_resume_fixture, product_resume_provider):
    exercise_resume(page, base_url, deployed_features, product_resume_fixture, product_resume_provider, True)


@pytest.mark.preview_on
def test_v11_cross_client_unavailable(page, base_url, deployed_features, product_resume_provider):
    from tests.e2e.new_ui.product_release import private_capture_directory
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence, private_json
    from tests.e2e.product_onboarding.test_v11 import verify_cross_client_report

    record = CaseEvidence()
    record.cli_events = []
    record.phases = {"setup": "failed", "call": "failed", "teardown": "failed"}
    directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
    with product_resume_provider.fixture("v11_resume", record) as fixture:
        fixture["evidence_record"] = record
        checked_fixture(fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
        record.phases["setup"] = "passed"
        observed = exercise_resume(page, base_url, deployed_features, fixture, product_resume_provider, True)
        screenshot = directory / f"{fixture['run_id']}-v11_resume.png"
        descriptor = os.open(screenshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(page.screenshot(full_page=True))
        record.viewports.append({"screenshot": str(screenshot)})
        record.phases["call"] = "passed"
    record.phases["teardown"] = "passed"
    identities = fixture["evidence_context"]["identities"]
    report = verify_cross_client_report({
        "scenario_id": "SCN-02", "ac_id": "AC-02", "run_id": fixture["run_id"],
        "source_revision": fixture["source_revision"], "requirements_revision": identities["requirements_revision"],
        "observed": fixture["evidence_context"]["deployment"]["observed"],
        "evidence_context": fixture["evidence_context"],
        "frontend_release_ref": identities["frontend_release_ref"],
        "api_image_digest": identities["api_image_digest"], "schema_head": identities["schema_head"],
        "tenant_id": fixture["tenant_id"], "product_id": fixture["product_id"], "project_id": fixture["project_id"],
        "browser_status": record.status, "viewports": record.viewports, "cli_events": record.cli_events,
        "api_reads": record.reads, "cleanup": record.cleanup[-1] if record.cleanup else None,
        "session_started": observed["started"], "session_before": observed["first"],
        "session_after": observed["final"], "profile_before": observed["before"],
        "profile_after": observed["after"],
    }, directory)
    private_json(directory / f"{fixture['run_id']}-v11_resume-report.json", report)


@pytest.mark.preview_on
def test_progress_math(page, base_url, deployed_features, product_resume_fixture, product_resume_provider):
    from playwright.sync_api import expect

    fixture = checked_fixture(product_resume_fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
    assert deployed_features.get("product_onboarding") is True
    check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"], fixture["source_revision"])
    scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
    route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
    binary = os.environ["PRODUCT_RESUME_CLI_PATH"]
    checksums = json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"])
    inject_tokens_and_navigate(page, fixture["owner"], path=route)
    open_product_section(page, "Financials")
    page.get_by_role("button", name="Start or resume onboarding").click()
    expect(product_session_status(page)).to_contain_text("Session ")
    session_id = re.search(r"Session ([^ ]+) · Revision", product_session_status(page).inner_text()).group(1)
    initial = read_session(product_resume_provider, base_url, fixture, session_id)
    assert_progress(initial, read_profile(product_resume_provider, base_url, fixture), "initial")
    assert "2/2 checks · 100%" in product_session_status(page).inner_text()

    selected = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", *scope, "--session", session_id, "--section", "financials", "--revision", str(initial["revision"]),
         "--idempotency-key", "progress_select_" + fixture["run_id"].replace("-", "_"), "--input", "-"],
        input_body={"action": "save", "configuration": {"financials": {}}}, record=fixture["evidence_record"])
    selected_read = read_session(product_resume_provider, base_url, fixture, session_id)
    assert_resume(selected, selected_read, selected_read, product_id=fixture["product_id"], project_id=fixture["project_id"])
    assert_progress(selected_read, read_profile(product_resume_provider, base_url, fixture), "selected")
    page.reload(wait_until="domcontentloaded")
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {selected['revision']}")
    page.get_by_role("button", name="Financials Later").click()
    expect(product_session_status(page)).to_contain_text("2/3 checks · 66%")
    later = read_session(product_resume_provider, base_url, fixture, session_id)
    assert_progress(later, read_profile(product_resume_provider, base_url, fixture), "later")
    cli_later = installed_cli(binary, checksums, fixture["owner"], base_url,
                              ["resume", *scope, "--session", session_id], record=fixture["evidence_record"])
    assert_resume(later, cli_later, read_session(product_resume_provider, base_url, fixture, session_id),
                  product_id=fixture["product_id"], project_id=fixture["project_id"])

    not_applicable = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", *scope, "--session", session_id, "--section", "financials", "--revision", str(later["revision"]),
         "--idempotency-key", "progress_exclude_" + fixture["run_id"].replace("-", "_"), "--input", "-"],
        input_body={"action": "not_applicable", "reason": "No financial plan yet"}, record=fixture["evidence_record"])
    excluded_read = read_session(product_resume_provider, base_url, fixture, session_id)
    assert_resume(not_applicable, excluded_read, excluded_read,
                  product_id=fixture["product_id"], project_id=fixture["project_id"])
    assert_progress(excluded_read, read_profile(product_resume_provider, base_url, fixture), "not_applicable")
    page.reload(wait_until="domcontentloaded")
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {not_applicable['revision']}")
    expect(product_session_status(page)).to_contain_text("2/2 checks · 100%")


@pytest.mark.preview_on
def test_resume_recovery(page, base_url, deployed_features, product_resume_fixture, product_resume_provider):
    from playwright.sync_api import expect

    fixture = checked_fixture(product_resume_fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
    assert deployed_features.get("product_onboarding") is True
    check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"], fixture["source_revision"])
    binary = os.environ["PRODUCT_RESUME_CLI_PATH"]
    checksums = json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"])
    scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
    route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")

    def changes():
        response = fixture["evidence_record"].request(product_resume_provider.request).get(base_url + "/api/v1" + route + "/changes",
            headers={"Authorization": "Bearer " + fixture["observer"]["access_token"]}, timeout=30_000)
        assert response.status == 200, "Independent Product change read failed"
        page_of_changes = response.json()
        items = page_of_changes.get("items")
        assert isinstance(items, list) and 1 <= len(items) <= 100 and page_of_changes.get("next_cursor") is None
        identities = [change["event_id"] for change in items]
        assert len(identities) == len(set(identities))
        return set(identities)

    def session(session_id):
        return read_session(product_resume_provider, base_url, fixture, session_id)

    inject_tokens_and_navigate(page, fixture["owner"], path=route)
    open_product_section(page, "Financials")
    page.get_by_role("button", name="Start or resume onboarding").click()
    expect(product_session_status(page)).to_contain_text("Session ")
    session_id = re.search(r"Session ([^ ]+) · Revision", product_session_status(page).inner_text()).group(1)
    initial = session(session_id)
    selected = installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", *scope, "--session", session_id, "--section", "financials", "--revision", str(initial["revision"]),
         "--idempotency-key", "recovery_select_" + fixture["run_id"].replace("-", "_"), "--input", "-"],
        input_body={"action": "save", "configuration": {"financials": {}}}, record=fixture["evidence_record"])
    assert_progress(session(session_id), read_profile(product_resume_provider, base_url, fixture), "selected")
    page.reload(wait_until="domcontentloaded")
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {selected['revision']}")

    recovery = SessionRecovery(fixture, session, changes)
    intercepted = []

    def lose_acknowledgement(intercepted_route):
        request = intercepted_route.request
        pending = recovery.record(session_id, int(request.headers["if-profile-revision"]),
                                  request.headers["idempotency-key"], request.post_data_json)
        assert pending["revision"] == selected["revision"]
        reply = intercepted_route.fetch(timeout=30_000)
        intercepted.append(reply.status)
        intercepted_route.abort("failed")

    step_route = "**/api/v1" + route + "/onboarding-sessions/" + quote(session_id, safe="") + "/steps/financials"
    page.route(step_route, lose_acknowledgement)
    try:
        page.get_by_role("button", name="Financials Later").click()
        expect(page.get_by_role("alert")).to_contain_text("Could not save applicability")
        assert intercepted == [200], "The intercepted step was not applied exactly once"
    finally:
        page.unroute(step_route, lose_acknowledgement)
    page.reload(wait_until="domcontentloaded")
    restored = SessionRecovery(fixture, session, changes)

    def replay(pending):
        installed_cli(binary, checksums, fixture["owner"], base_url,
            ["step", *scope, "--session", pending["session_id"], "--section", "financials",
             "--revision", str(pending["revision"]), "--idempotency-key", pending["key"], "--input", "-"],
            input_body=pending["body"], record=fixture["evidence_record"])

    result = restored.reconcile(replay)
    assert result["session_id"] == session_id and len(result["attempts"]) == 2
    assert result["attempts"][1]["result"] == "readback_after_lost_ack"
    current = session(session_id)
    assert_progress(current, read_profile(product_resume_provider, base_url, fixture), "later")
    resumed = installed_cli(binary, checksums, fixture["owner"], base_url,
                            ["resume", *scope, "--session", session_id], record=fixture["evidence_record"])
    assert_resume(current, resumed, session(session_id), product_id=fixture["product_id"], project_id=fixture["project_id"])
    expect(product_session_status(page)).to_contain_text(f"Session {session_id} · Revision {result['revision']}")
