"""Deployed producers for issue #8258 AC-02 (client effects) and AC-03 (revocation).

These are the executing live cases of the V07 guidance inventory: they drive the
served browser, the installed CLI and the deployed API against run-owned Product
fixtures, and write the private evidence reports that the reviewed SCN report
checks validate. The report-only checks in test_v07.py are not producers.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_v07_live import run_guidance_effects, run_guidance_resume

OWNED_EFFECTS_CASE = "evidence"
OWNED_RESUME_CASE = "recovery"


def selected_environment(*names):
    if os.environ.get("PRODUCT_V07_LIVE_ENABLED") != "1":
        pytest.skip("Issue-8258 AC-02 and AC-03 need the authorized deployed Product guidance fixtures")
    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR", *names):
        if not os.environ.get(variable):
            raise ValueError("The deployed guidance producer needs " + variable + " from the pinned workflow")


@contextmanager
def owned_guidance_run(request, case, *, with_page=False):
    """Claim one run-owned deployed Product fixture for a guidance producer."""
    from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
    from tests.e2e.new_ui.test_product_resume import checked_fixture
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence

    provider = request.getfixturevalue("product_onboarding_live_provider")
    page = request.getfixturevalue("product_onboarding_live_page") if with_page else None
    record = CaseEvidence()
    record.cli_events = []
    record.phases["setup"] = "failed"
    directory = Path(private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"]))
    with provider.fixture(case, record) as fixture:
        fixture["evidence_record"] = record
        checked_fixture(fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(
            provider.request, provider.base_url, fixture["frontend_release"], fixture["source_revision"]
        )
        record.phases["setup"] = "passed"
        record.phases["call"] = "failed"
        yield provider, page, fixture, record, directory
        record.phases["call"] = "passed"
    record.phases["teardown"] = "passed"


@pytest.mark.live_only
def live_guidance_effects(request):
    """Metered, profile and external effects of every deployed guidance turn."""
    selected_environment()
    with owned_guidance_run(request, OWNED_EFFECTS_CASE) as (provider, _page, fixture, record, directory):
        observed = run_guidance_effects(provider, fixture, provider.base_url, record, directory)
    if record.status != "pass" or not observed["report"]:
        raise AssertionError("Deployed guidance effect capture produced no private evidence report")


@pytest.mark.live_only
def live_guidance_resume(request):
    """Cross-client resume of pending guidance and revocation of its source."""
    from tests.e2e.new_ui.test_product_guidance import write_guidance_resume_report

    selected_environment("GITHUB_REPOSITORY_ID")
    with owned_guidance_run(request, OWNED_RESUME_CASE, with_page=True) as (
        provider, page, fixture, record, directory,
    ):
        pending, outage, revocation, attempts = run_guidance_resume(
            provider, page, fixture, provider.base_url, record
        )
    screenshot = directory / (fixture["run_id"] + "-guidance-resume.png")
    page.screenshot(path=str(screenshot))
    screenshot.chmod(0o600)
    report = write_guidance_resume_report(
        fixture, pending, outage, revocation, attempts, record, directory, screenshot
    )
    if record.status != "pass" or not report:
        raise AssertionError("Deployed guidance recovery produced no private evidence report")
