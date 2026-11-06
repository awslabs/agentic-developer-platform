"""Served browser and installed CLI guidance with run-owned Product cleanup."""

import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.new_ui.product_navigation import open_product_section, product_session_status
from tests.e2e.cli_uplift.product_guidance import (
    capture_guidance_effects, guide_with_installed_cli, verify_direct_save, verify_guided_journey,
)
from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
from tests.e2e.new_ui.test_product_resume import checked_fixture, installed_cli, read_profile, read_session
from tests.e2e.product_onboarding.product_v02_record import CaseEvidence, private_json
from tests.e2e.product_onboarding.test_v07 import (
    verify_guidance_effects_report, verify_guidance_resume_report, verify_guided_case_report,
)


def capture_client_effect_turn(fixture, base_url, provider, session_id, model_id, mode, read_external, perform_turn):
    def read_usage():
        query = urlencode({"org_id": fixture["tenant_id"], "user_id": fixture["actor_id"],
                           "model": model_id, "limit": 100})
        response = provider.request.get(base_url + "/api/usage/logs?" + query,
                                        headers={"Authorization": "Bearer " + provider.usage_control["access_token"]},
                                        timeout=30_000)
        if response.status != 200:
            raise ValueError("Independent authorized usage readback is unavailable")
        return response.json()

    return capture_guidance_effects(
        fixture["tenant_id"], fixture["actor_id"], model_id, mode, read_usage,
        lambda: read_profile(provider, base_url, fixture),
        lambda: read_session(provider, base_url, fixture, session_id), read_external, perform_turn,
    )


def write_client_effect_report(fixture, turns, directory):
    report = verify_guidance_effects_report({
        "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "requirements_revision": fixture["evidence_context"]["identities"]["requirements_revision"],
        "turns": turns,
    })
    directory = private_capture_directory(directory)
    return private_json(directory / f"{fixture['run_id']}-guidance-effects.json", report)


def write_guidance_resume_report(fixture, pending, outage, revocation, attempts, record, directory, screenshot):
    directory = private_capture_directory(directory)
    screenshot = Path(screenshot)
    name = fixture["run_id"] + "-guidance-resume"
    if (screenshot != directory / f"{name}.png" or screenshot.is_symlink()
            or not screenshot.is_file() or screenshot.stat().st_mode & 0o077):
        raise ValueError("Recovery screenshot must be a private run artifact")
    browser_ref = private_json(directory / f"{name}-browser.json", {
        "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "status": record.status, "phases": record.phases, "screenshot": str(screenshot),
    })
    cli_ref = private_json(directory / f"{name}-cli.json", record.cli_events)
    api_ref = private_json(directory / f"{name}-api.json", record.reads)
    cleanup_ref = private_json(directory / f"{name}-cleanup.json", record.cleanup)
    evidence_refs = {"proposal": cli_ref, "interruption": browser_ref, "resume": api_ref,
                     "outage": cli_ref, "revocation": api_ref, "cleanup": cleanup_ref}
    observed_attempts = [{**attempt, "evidence_ref": evidence_refs.get(attempt.get("stage"), browser_ref)}
                         for attempt in attempts]
    report = {
        "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "requirements_revision": fixture["evidence_context"]["identities"]["requirements_revision"],
        "tenant_id": fixture["tenant_id"], "actor_id": fixture["actor_id"],
        "product_id": fixture["product_id"], "project_id": fixture["project_id"],
        "pending": pending, "outage": outage, "revocation": revocation, "attempts": observed_attempts,
        "browser_status": record.status, "cli_events": len(record.cli_events), "api_reads": len(record.reads),
        "cleanup": record.cleanup[-1] if record.cleanup else None,
        "artifacts": {"browser": browser_ref, "installed_cli": cli_ref, "api": api_ref, "cleanup": cleanup_ref},
    }
    report_ref = private_json(directory / f"{name}-report.json", report)
    verify_guidance_resume_report(report)
    if not any(read.get("status") == 200 and read.get("body") == revocation["api_session"]
               for read in record.reads if isinstance(read, dict)):
        raise ValueError("Revocation needs an independently recorded API session readback")
    return report_ref


def save_direct_with_installed_cli(binary, checksums, fixture, base_url, session, record):
    return installed_cli(binary, checksums, fixture["owner"], base_url,
        ["step", "--product", fixture["product_id"], "--project", fixture["project_id"],
         "--session", session["id"], "--section", "information", "--revision", str(session["revision"]),
         "--idempotency-key", "guidance_direct_" + session["id"], "--input", "-"],
        input_body={"action": "save", "product": {"purpose": "Confirmed by direct edit"}}, record=record)


@pytest.mark.preview_on
@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_guided_journeys(page, base_url, deployed_features, product_resume_provider, starting_point):
    from playwright.sync_api import expect

    assert deployed_features.get("product_onboarding") is True
    record = CaseEvidence()
    record.cli_events = []
    case = "guidance_" + starting_point
    directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
    fixture = None
    record.phases["setup"] = "failed"
    with product_resume_provider.fixture(case, record) as fixture:
        fixture["evidence_record"] = record
        checked_fixture(fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"], fixture["source_revision"])
        record.phases["setup"] = "passed"
        record.phases["call"] = "failed"
        profile = read_profile(product_resume_provider, base_url, fixture)
        route = "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(fixture["project_id"], safe="")
        inject_tokens_and_navigate(page, fixture["owner"], path=route)
        open_product_section(page, "Financials")
        page.get_by_role("button", name="Start or resume onboarding").click()
        expect(product_session_status(page)).to_contain_text("Session ")
        session_id = re.search(r"Session ([^ ]+) · Revision", product_session_status(page).inner_text()).group(1)
        session = read_session(product_resume_provider, base_url, fixture, session_id)
        page.get_by_text("Optional guidance", exact=True).click()
        expect(page.get_by_label("Ask for guidance")).to_be_visible()
        expect(page.get_by_role("form", name="Financials")).to_be_visible()
        page.get_by_label("Ask for guidance").fill("What is missing?")
        page.get_by_role("button", name="Suggest next steps").click()
        expect(page.locator("[aria-live='polite']")).not_to_be_empty()
        browser_message = page.locator("[aria-live='polite']").inner_text()
        binary = os.environ["PRODUCT_RESUME_CLI_PATH"]
        checksums = json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"])
        cli_guidance = guide_with_installed_cli(binary, checksums, fixture, base_url, session, record)
        independent_session = read_session(product_resume_provider, base_url, fixture, session_id)
        verify_guided_journey(starting_point, profile, session, browser_message, cli_guidance, independent_session)
        updated = save_direct_with_installed_cli(binary, checksums, fixture, base_url, session, record)
        verify_direct_save(profile, session, updated, read_profile(product_resume_provider, base_url, fixture),
                           read_session(product_resume_provider, base_url, fixture, session_id))
        record.phases["call"] = "passed"
    record.phases["teardown"] = "passed"
    report = verify_guided_case_report({
        "starting_point": starting_point, "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "requirements_revision": fixture["evidence_context"]["identities"]["requirements_revision"],
        "browser_status": record.status, "cli_events": len(record.cli_events), "api_reads": len(record.reads),
        "cleanup": record.cleanup[-1],
    })
    screenshot = directory / f"{fixture['run_id']}-{case}.png"
    page.screenshot(path=str(screenshot))
    screenshot.chmod(0o600)
    browser_ref = private_json(Path(directory) / f"{fixture['run_id']}-{case}-browser.json", {
        "starting_point": starting_point, "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "status": record.status, "screenshot": str(screenshot), "phases": record.phases,
    })
    cli_ref = private_json(Path(directory) / f"{fixture['run_id']}-{case}-cli.json", record.cli_events)
    api_ref = private_json(Path(directory) / f"{fixture['run_id']}-{case}-api.json", record.reads)
    cleanup_ref = private_json(Path(directory) / f"{fixture['run_id']}-{case}-cleanup.json", record.cleanup)
    report["artifacts"] = {"browser": browser_ref, "installed_cli": cli_ref, "api": api_ref, "cleanup": cleanup_ref}
    private_json(Path(directory) / f"{fixture['run_id']}-{case}-report.json", report)
