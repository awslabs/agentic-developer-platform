"""V08 live selectors fail closed unless protected deployed fixtures actually run."""

import os
import re

import pytest

from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.new_ui.test_product_handoffs import browser_admin_resume


def run_admin_handoff(run, source_revision, requirements_revision):
    """Exercise a real owned browser/CLI/admin handoff, then clean its fixtures."""
    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
                       for value in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or not isinstance(run.get("cli_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", run["cli_sha256"])
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(callable(run.get(name)) for name in
                       ("navigate", "complete_admin", "receipt_observer", "resume_cli", "cleanup"))
            or not all(run.get(name) for name in ("page", "provider", "base_url", "session_id", "key"))):
        raise ValueError("V08 handoff needs a pinned deployed release and authorized owned fixture")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        result = browser_admin_resume(
            run["page"], run["provider"], run["base_url"], fixture, run["session_id"], run["key"],
            navigate=run["navigate"], complete_admin=run["complete_admin"],
            receipt_observer=run["receipt_observer"], resume_cli=run["resume_cli"],
        )
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision or cleanup.get("status") != "verified"
                or cleanup.get("owned_absent") is not True or cleanup.get("shared_unchanged") is not True
                or not cleanup.get("receipt_ref")):
            raise AssertionError("V08 handoff fixture cleanup was not independently verified")
    return {"result": result, "cleanup": cleanup}


@pytest.mark.live_only
def live_admin_handoff(request):
    """A missing deployed fixture is never replaced with a local or synthetic success."""
    if os.environ.get("PRODUCT_V08_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V08 verification")
    source_revision = os.environ["PRODUCT_V08_SOURCE_REVISION"]
    requirements_revision = os.environ["PRODUCT_V08_REQUIREMENTS_REVISION"]
    run = request.getfixturevalue("product_v08_handoff_fixture")
    assert run_admin_handoff(run, source_revision, requirements_revision)


def run_repository_recovery(run, source_revision, requirements_revision):
    """Run UI confirmation, installed-CLI resume and owned provider cleanup."""
    from urllib.parse import quote

    from tests.e2e.new_ui.test_product_handoffs import browser_repository_recovery

    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
                       for value in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or not isinstance(run.get("cli_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", run["cli_sha256"])
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(callable(run.get(name)) for name in
                       ("navigate", "provider_observer", "resume_cli", "cleanup"))
            or not all(run.get(name) for name in
                       ("page", "provider", "base_url", "session_id", "plan", "keys"))):
        raise ValueError("V08 repository needs a pinned deployed release and authorized owned provider fixture")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        result = browser_repository_recovery(
            run["page"], run["provider"], run["base_url"], fixture, run["session_id"],
            run["plan"], run["keys"], run["provider_observer"], navigate=run["navigate"],
        )
        cli_session = run["resume_cli"](run["session_id"])
        root = (run["base_url"].rstrip("/") + "/api/v1/products/" + quote(fixture["product_id"], safe="")
                + "/projects/" + quote(fixture["project_id"], safe="") + "/operations/"
                + quote(result["operation"]["id"], safe=""))
        observer = run["provider"].request.get(root,
                                                headers={"Authorization": "Bearer " + fixture["observer"]["access_token"]},
                                                timeout=30_000)
        if (observer.status != 200 or observer.json().get("repository") != result["operation"].get("repository")
                or cli_session.get("id") != run["session_id"]
                or cli_session.get("tenant_id") != fixture["tenant_id"]):
            raise AssertionError("Installed CLI or independent operation readback disagrees with provider")
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision or cleanup.get("status") != "verified"
                or cleanup.get("owned_absent") is not True or cleanup.get("shared_unchanged") is not True
                or not cleanup.get("receipt_ref")):
            raise AssertionError("V08 repository cleanup was not independently verified")
    return {"result": result, "cli_session": cli_session, "cleanup": cleanup}


@pytest.mark.live_only
def live_repository_recovery(request):
    """No provider fixture or deployed pin means no live repository acceptance."""
    if os.environ.get("PRODUCT_V08_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V08 verification")
    run = request.getfixturevalue("product_v08_repository_fixture")
    assert run_repository_recovery(run, os.environ["PRODUCT_V08_SOURCE_REVISION"],
                                   os.environ["PRODUCT_V08_REQUIREMENTS_REVISION"])


def run_provider_cleanup(run, source_revision, requirements_revision):
    """Observe adopted provider assets and recover run-owned cleanup on failure."""
    from tests.e2e.cli_uplift.product_handoffs import adopt_and_cleanup

    fixture = run.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
                       for value in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or run.get("deployment_source_revision") != source_revision
            or not isinstance(run.get("cli_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", run["cli_sha256"])
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or not all(callable(run.get(name)) for name in
                       ("navigate", "cli_read_product", "existing_observer", "owned_observer",
                        "delete_owned", "history_reader", "recover_cleanup"))
            or not all(run.get(name) for name in
                       ("page", "provider", "base_url", "existing_product_name"))):
        raise ValueError("V08 provider cleanup needs a pinned deployed release and durable owned fixture")
    try:
        check_served_assets(run["provider"].request, run["base_url"], fixture["frontend_release"], source_revision)
        before_cli = run["cli_read_product"]()
        if (before_cli.get("product_id") != fixture["product_id"]
                or before_cli.get("tenant_id") != fixture["tenant_id"]):
            raise AssertionError("Installed CLI did not read the owned existing Product")
        run["navigate"]()
        run["page"].get_by_role("heading", name=run["existing_product_name"]).wait_for(timeout=30_000)
        result = adopt_and_cleanup(
            run["provider"].request, run["base_url"], fixture, run["existing_observer"],
            run["owned_observer"], run["delete_owned"], run["history_reader"],
        )
        after_cli = run["cli_read_product"]()
        if (after_cli.get("product_id") != before_cli["product_id"]
                or after_cli.get("tenant_id") != before_cli["tenant_id"]):
            raise AssertionError("Installed CLI lost the existing Product after unlink")
    finally:
        recovery = run["recover_cleanup"]()
        if (not isinstance(recovery, dict) or recovery.get("run_id") != fixture["run_id"]
                or recovery.get("source_revision") != source_revision
                or recovery.get("owned_absent") is not True
                or recovery.get("existing_unchanged") is not True
                or recovery.get("history_retained") is not True or not recovery.get("receipt_ref")):
            raise AssertionError("Durable owned cleanup and existing provider baseline were not verified")
    return {"result": result, "cli_before": before_cli, "cli_after": after_cli, "recovery": recovery}


@pytest.mark.live_only
def live_provider_cleanup(request):
    """Only authorized deployed readbacks may close the V08 cleanup scenario."""
    if os.environ.get("PRODUCT_V08_LIVE_ENABLED") != "1":
        pytest.skip("Requires explicitly authorized deployed V08 verification")
    run = request.getfixturevalue("product_v08_provider_cleanup_fixture")
    assert run_provider_cleanup(run, os.environ["PRODUCT_V08_SOURCE_REVISION"],
                                os.environ["PRODUCT_V08_REQUIREMENTS_REVISION"])
