"""V09 selectors require an authorized source, pinned clients and owned cleanup."""

import os
import re

import pytest

from tests.e2e.cli_uplift.product_financials import verify_financial_resume
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.new_ui.test_product_financials import financial_denial, recover_financial_confirmation, selective_date_import


def run_financial_selective_import(run, source_revision, requirements_revision):
    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
                       for revision in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(run.get(name) for name in ("page", "provider", "base_url", "binary", "checksums", "session_id",
                                                     "proposal_id", "source_ref", "record"))
            or not callable(run.get("cleanup")) or run.get("mode") not in ("browser", "cli")):
        raise ValueError("V09 selective import needs pinned deployed clients and an authorized owned source")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        result = selective_date_import(run["page"], run["provider"], run["base_url"], fixture,
                                       run["binary"], run["checksums"], run["session_id"],
                                       run["proposal_id"], run["source_ref"], run["mode"], run["record"])
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision or cleanup.get("status") != "verified"
                or cleanup.get("owned_absent") is not True or cleanup.get("shared_unchanged") is not True
                or not cleanup.get("receipt_ref")):
            raise AssertionError("V09 selective import fixture cleanup was not independently verified")
    return {"result": result, "cleanup": cleanup}


@pytest.mark.live_only
@pytest.mark.parametrize("mode", ("browser", "cli"))
def live_financial_selective_import(request, mode):
    if os.environ.get("PRODUCT_V09_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V09 verification")
    run = request.getfixturevalue("product_v09_selective_fixture")
    assert run["mode"] == mode, "Browser and CLI need separate owned fixtures and reports"
    assert run_financial_selective_import(run, os.environ["PRODUCT_V09_SOURCE_REVISION"],
                                          os.environ["PRODUCT_V09_REQUIREMENTS_REVISION"])


def run_financial_denial(run, source_revision, requirements_revision):
    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
                       for revision in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(run.get(name) for name in ("page", "provider", "base_url", "binary", "checksums", "session_id",
                                                     "proposal_id", "record"))
            or not callable(run.get("observe_effects")) or not callable(run.get("cleanup"))
            or run.get("case") not in ("stale", "expired", "revoked", "invalid_period")
            or run.get("mode") not in ("browser", "cli", "api")
            or not isinstance(run.get("source_markers"), list)
            or not fixture.get("denial_key")):
        raise ValueError("V09 denial needs pinned deployed clients and an authorized owned source")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        result = financial_denial(run["page"], run["provider"], run["base_url"], fixture,
                                  run["binary"], run["checksums"], run["session_id"], run["case"],
                                  run["mode"], run["proposal_id"], run["observe_effects"],
                                  run["record"], run["source_markers"])
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision or cleanup.get("status") != "verified"
                or cleanup.get("owned_absent") is not True or cleanup.get("shared_unchanged") is not True
                or not cleanup.get("receipt_ref")):
            raise AssertionError("V09 denial fixture cleanup was not independently verified")
    return {"result": result, "cleanup": cleanup}


@pytest.mark.live_only
@pytest.mark.parametrize(("case", "mode"), (("stale", "browser"), ("expired", "cli"),
                                              ("revoked", "api"), ("invalid_period", "browser")))
def live_financial_denials(request, case, mode):
    if os.environ.get("PRODUCT_V09_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V09 verification")
    run = request.getfixturevalue("product_v09_denial_fixture")
    assert (run["case"], run["mode"]) == (case, mode), "Denials need separate owned fixtures and attempts"
    assert run_financial_denial(run, os.environ["PRODUCT_V09_SOURCE_REVISION"],
                                os.environ["PRODUCT_V09_REQUIREMENTS_REVISION"])


def run_financial_resume(run, source_revision, requirements_revision):
    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
                       for revision in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(run.get(name) for name in ("page", "provider", "base_url", "binary", "checksums", "session_id",
                                                     "proposal_id", "source_ref", "record"))
            or not fixture.get("exclusion_key") or not callable(run.get("observe_receipts"))
            or not callable(run.get("observe_source")) or not callable(run.get("cleanup"))):
        raise ValueError("V09 resume needs pinned deployed clients and an authorized owned source")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        result = recover_financial_confirmation(run["page"], run["provider"], run["base_url"], fixture,
            run["binary"], run["checksums"], run["session_id"], run["proposal_id"], run["source_ref"],
            run["observe_receipts"], run["observe_source"], run["record"])
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision or cleanup.get("status") != "verified"
                or cleanup.get("owned_absent") is not True or cleanup.get("shared_unchanged") is not True
                or not cleanup.get("receipt_ref")):
            raise AssertionError("V09 resume fixture cleanup was not independently verified")
    verify_financial_resume(*result["resume_args"], cleanup, **result["resume_expected"])
    return {"revision": result["revision"], "later": result["later"],
            "not_applicable": result["not_applicable"], "cleanup": cleanup}


@pytest.mark.live_only
def live_financial_resume(request):
    if os.environ.get("PRODUCT_V09_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V09 verification")
    run = request.getfixturevalue("product_v09_resume_fixture")
    assert run_financial_resume(run, os.environ["PRODUCT_V09_SOURCE_REVISION"],
                                os.environ["PRODUCT_V09_REQUIREMENTS_REVISION"])
