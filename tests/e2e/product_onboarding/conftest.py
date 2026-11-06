"""Collect Product scenario names used by the deployment acceptance inventory."""

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("python_functions", "deploy_core_receipt")
    config.addinivalue_line("python_functions", "reject_false_green")
    config.addinivalue_line("python_functions", "rollout_recovery")
    config.addinivalue_line("python_functions", "automatic_release_handoff")
    config.addinivalue_line("python_functions", "api_product_lifecycle")
    config.addinivalue_line("python_functions", "api_denial_oracle")
    config.addinivalue_line("python_functions", "api_recovery_cleanup")
    config.addinivalue_line(
        "markers", "live_only: requires protected deployed Product fixtures"
    )
    config.addinivalue_line("python_functions", "live_browser_create")
    config.addinivalue_line("python_functions", "live_browser_conflict_denial")
    config.addinivalue_line("python_functions", "live_browser_evidence")
    config.addinivalue_line("python_functions", "live_scope_isolation")
    config.addinivalue_line("python_functions", "live_scope_denials")
    config.addinivalue_line("python_functions", "live_scope_history_cleanup")
    config.addinivalue_line("python_functions", "live_bidirectional_resume")
    config.addinivalue_line("python_functions", "live_progress_math")
    config.addinivalue_line("python_functions", "live_resume_recovery")
    config.addinivalue_line("python_functions", "live_sources_reuse")
    config.addinivalue_line("python_functions", "live_source_status")
    config.addinivalue_line("python_functions", "live_source_recovery")
    config.addinivalue_line("python_functions", "live_guided_journeys")
    config.addinivalue_line("python_functions", "live_guidance_effects")
    config.addinivalue_line("python_functions", "live_guidance_resume")
    config.addinivalue_line("python_functions", "portfolio_many_to_many")
    config.addinivalue_line("python_functions", "portfolio_history")
    config.addinivalue_line("python_functions", "portfolio_denials")
    config.addinivalue_line("python_functions", "initiative_cross_product")
    config.addinivalue_line("python_functions", "contribution_versioning")
    config.addinivalue_line("python_functions", "contribution_optional_access")
    config.addinivalue_line("python_functions", "optional_portfolio_ui")
    config.addinivalue_line("python_functions", "initiative_selection_ui")
    config.addinivalue_line("python_functions", "catalog_ui_failure_states")
    config.addinivalue_line("python_functions", "live_catalog_membership")
    config.addinivalue_line("python_functions", "live_catalog_history_access")
    config.addinivalue_line("python_functions", "live_catalog_recovery")
    config.addinivalue_line("python_functions", "live_admin_handoff")
    config.addinivalue_line("python_functions", "live_repository_recovery")
    config.addinivalue_line("python_functions", "live_provider_cleanup")
    config.addinivalue_line("python_functions", "live_financial_selective_import")
    config.addinivalue_line("python_functions", "live_financial_denials")
    config.addinivalue_line("python_functions", "live_financial_resume")
    config.addinivalue_line("python_functions", "live_cli_full_journeys")
    config.addinivalue_line("python_functions", "live_cli_peer_unavailable")
    config.addinivalue_line("python_functions", "live_cli_release_evidence")
    config.addinivalue_line("python_functions", "live_insights_cli_checkpoint")


@pytest.fixture(scope="session")
def product_onboarding_live_tokens():
    """Real Cognito tokens for the run-owned deployed Product fixtures."""
    from tests.e2e.chat.helpers import fetch_test_credentials, get_cognito_tokens

    return get_cognito_tokens(fetch_test_credentials())


@pytest.fixture(scope="package")
def product_onboarding_live_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def product_onboarding_live_page(product_onboarding_live_browser):
    context = product_onboarding_live_browser.new_context(
        viewport={"width": 1440, "height": 1000}, service_workers="block"
    )
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture(scope="package")
def product_onboarding_live_provider(product_onboarding_live_browser, product_onboarding_live_tokens):
    """Run-owned deployed Product provider with the served CLI installed for this suite.

    Mirrors the maintained new-UI resume provider: the live flag state is read
    back from the deployment itself before any producer runs, and the suite
    recovers and retains its own cleanup receipt afterwards.
    """
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from tests.e2e.chat.helpers import CLOUDFRONT_URL
    from tests.e2e.new_ui.product_fixture_provider import build_provider
    from tests.e2e.product_onboarding.product_v04_cli import install_served_cli

    base_url = CLOUDFRONT_URL.rstrip("/")
    context = product_onboarding_live_browser.new_context(service_workers="block")
    try:
        response = context.request.get(
            base_url + "/api/features",
            headers={"Authorization": "Bearer " + product_onboarding_live_tokens["access_token"]},
            timeout=30_000,
        )
        assert response.status == 200, f"Live features read failed: HTTP {response.status}"
        features = response.json().get("features")
        assert isinstance(features, dict), "Live features response has no features object"
        assert features.get("new_ui") is (os.environ["E2E_NEW_UI_EXPECTED_FLAG"] == "on"), (
            "Live preview flag differs from E2E_NEW_UI_EXPECTED_FLAG"
        )
        assert features.get("product_onboarding") is True, "Product onboarding is not enabled in the deployment"
        provider = build_provider(os.environ, base_url, context.request, product_onboarding_live_tokens,
                                  onboarding_enabled=True)
        try:
            with TemporaryDirectory(prefix="product-v06-cli-") as directory, pytest.MonkeyPatch.context() as env:
                binary, checksums = install_served_cli(
                    provider, SimpleNamespace(context=provider.evidence_context), Path(directory)
                )
                env.setenv("PRODUCT_RESUME_CLI_PATH", str(binary))
                env.setenv("PRODUCT_RESUME_CLI_HASHES", json.dumps(checksums, sort_keys=True))
                yield provider
        finally:
            receipt = provider.recover()
            if os.environ.get("PRODUCT_SUITE_ID"):
                from tests.e2e.new_ui.product_suite_cleanup import retain_cleanup

                retain_cleanup(receipt, Path("test-results").resolve(), os.environ)
    finally:
        context.close()
