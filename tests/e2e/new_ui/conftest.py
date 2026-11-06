"""Live NUI-01 fixtures. Shared Cognito session restoration requires PR #5115.

The SPA restores sessionStorage on startup; hosted OAuth is a separate flow.
Failed feature reads and unexpected flag states are errors when enabled.
Ordinary repository runs skip only this package.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests.e2e.chat.helpers import (
    CLOUDFRONT_URL,
    fetch_test_credentials,
    get_cognito_tokens,
    inject_tokens_and_navigate,
    take_failure_screenshot,
)

PACKAGE = Path(__file__).resolve().parent


class SessionTokens(dict):
    """Keep pytest fixture-argument diagnostics from printing credentials."""

    def __repr__(self):
        return "<in-memory Cognito tokens>"


def pytest_configure(config):
    config.addinivalue_line("markers", "preview_on: requires a verified live flag-on deployment")
    config.addinivalue_line("markers", "preview_off: requires a verified live flag-off deployment")
    if (os.environ.get("PRODUCT_EXPECTED_REVISION") and os.environ.get("E2E_NEW_UI_ENABLED") == "1"
            and os.environ.get("PRODUCT_B03_MODE") == "true"):
        from tests.e2e.product_onboarding.product_v04_record import ScopeEvidence

        config.product_scope_evidence = ScopeEvidence(Path("test-results"), os.environ)
    elif os.environ.get("PRODUCT_EXPECTED_REVISION") and os.environ.get("E2E_NEW_UI_ENABLED") == "1":
        from tests.e2e.product_onboarding.product_v02_record import BrowserEvidence

        config.product_evidence = BrowserEvidence(Path("test-results"), os.environ)


def pytest_sessionfinish(session, exitstatus):
    evidence = getattr(session.config, "product_evidence", None)
    if evidence is not None:
        evidence.write()
    scope_evidence = getattr(session.config, "product_scope_evidence", None)
    if scope_evidence is not None:
        scope_evidence.write()
    resume_evidence = getattr(session.config, "product_resume_evidence", None)
    if resume_evidence is not None:
        resume_evidence.write()


@pytest.fixture
def product_case_evidence(request, product_run_fixture):
    evidence = getattr(request.config, "product_evidence", None)
    if evidence is None:
        pytest.fail("Product cases require the pinned browser workflow evidence context")
    evidence.bind(product_run_fixture)
    return evidence.case(request.node.nodeid)


def pytest_collection_modifyitems(config, items):
    enabled = os.environ.get("E2E_NEW_UI_ENABLED", "").lower() in ("1", "true", "yes")
    expected = os.environ.get("E2E_NEW_UI_EXPECTED_FLAG", "")
    own_items = [item for item in items if Path(item.path).resolve().is_relative_to(PACKAGE)]
    if not own_items:
        return
    if not enabled:
        for item in own_items:
            item.add_marker(pytest.mark.skip(reason="Requires E2E_NEW_UI_ENABLED=1 and a deployed environment"))
        return
    if expected not in ("on", "off"):
        raise pytest.UsageError("Set E2E_NEW_UI_EXPECTED_FLAG=on or off explicitly for a live new-UI run")
    opposite = "preview_off" if expected == "on" else "preview_on"
    deselected = [item for item in own_items if item.get_closest_marker(opposite)]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


def pytest_collection_finish(session):
    # Selection plugins and flag filters have finished. Validate only the cases
    # that will actually run, before any browser or credential fixture starts.
    if os.environ.get("E2E_NEW_UI_ENABLED", "").lower() not in ("1", "true", "yes"):
        return
    if any(Path(item.path).resolve().is_relative_to(PACKAGE) and Path(item.path).name.startswith("test_product_")
           for item in session.items):
        from .product_live_release import selection_from_environment

        try:
            selection_from_environment(os.environ)
        except ValueError as error:
            raise pytest.UsageError(
                str(error) + ". Supply the matched Product source, frontend release run/attempt and workflow revision "
                "for the selected Product cases. The full New UI suite includes Product checks."
            ) from None


@pytest.fixture(scope="session")
def base_url():
    return CLOUDFRONT_URL.rstrip("/")


@pytest.fixture(scope="session")
def cognito_tokens():
    return SessionTokens(get_cognito_tokens(fetch_test_credentials()))


@pytest.fixture(scope="package")
def browser_instance():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="package", autouse=True)
def deployed_features(browser_instance, cognito_tokens, base_url):
    """Verify the real deployment independently of later browser simulations."""
    context = browser_instance.new_context()
    try:
        response = context.request.get(
            base_url + "/api/features",
            headers={"Authorization": "Bearer " + cognito_tokens["access_token"]},
            timeout=30_000,
        )
        assert response.status == 200, f"Live features read failed: HTTP {response.status}"
        features = response.json().get("features")
        assert isinstance(features, dict), "Live features response has no features object"
        assert isinstance(features.get("new_ui"), bool), "Live new_ui flag is missing or not boolean"
        expected = os.environ["E2E_NEW_UI_EXPECTED_FLAG"] == "on"
        assert features["new_ui"] is expected, "Live preview flag differs from E2E_NEW_UI_EXPECTED_FLAG"
        return features
    finally:
        context.close()


@pytest.fixture
def page(browser_instance):
    context = browser_instance.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def authed_page(page, cognito_tokens):
    """Real Cognito tokens restored by the shared helper; not hosted OAuth."""
    inject_tokens_and_navigate(page, cognito_tokens, path="/activity")
    yield page


@pytest.fixture(scope="package")
def product_provider(browser_instance, cognito_tokens, base_url, request):
    from .product_fixture_provider import build_provider

    context = browser_instance.new_context(service_workers="block")
    try:
        provider = build_provider(os.environ, base_url, context.request, cognito_tokens,
                                  scope_editor_enabled=os.environ.get("PRODUCT_B03_MODE") == "true")
        try:
            yield provider
        finally:
            receipt = provider.recover()
            if os.environ.get("PRODUCT_SUITE_ID"):
                from .product_suite_cleanup import retain_cleanup

                retain_cleanup(receipt, Path("test-results").resolve(), os.environ)
            scope_evidence = getattr(request.config, "product_scope_evidence", None)
            if scope_evidence is not None:
                from tests.e2e.product_onboarding.product_v04_cli import write_cli_report

                scope_evidence.record_cleanup(provider, receipt)
                write_cli_report(scope_evidence)
    finally:
        context.close()


@pytest.fixture
def product_catalog_provider(browser_instance, cognito_tokens, base_url):
    from .product_fixture_provider import build_provider

    required = ("PRODUCT_CATALOG_LIVE_ENABLED", "PRODUCT_RECOVERY_RUN_ID",
                "PRODUCT_CATALOG_FIXTURE_PATH", "PRODUCT_EXPECTED_REVISION")
    if (os.environ.get(required[0]) != "1"
            or any(not os.environ.get(name) for name in required[1:])):
        pytest.fail("Catalog readback needs a selected, run-owned live fixture")
    fixture_path = Path(os.environ["PRODUCT_CATALOG_FIXTURE_PATH"])
    if not fixture_path.is_absolute() or fixture_path.is_symlink() or not fixture_path.is_file():
        pytest.fail("Catalog readback needs the protected membership fixture")
    metadata = fixture_path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        pytest.fail("Catalog readback needs the protected membership fixture")
    context = browser_instance.new_context(service_workers="block")
    try:
        yield build_provider(
            os.environ, base_url, context.request, cognito_tokens,
            recovery_run_id=os.environ["PRODUCT_RECOVERY_RUN_ID"],
        )
    finally:
        context.close()


@pytest.fixture
def product_run_fixture(request, product_provider):
    evidence = getattr(request.config, "product_evidence", None)
    if evidence is None:
        pytest.fail("Product fixture requires the pinned workflow evidence context")
    case = {"test_product_create": "create", "test_product_conflict_denial": "conflict",
            "test_product_evidence": "evidence"}[request.node.name]
    with product_provider.fixture(case, evidence.case(request.node.nodeid)) as fixture:
        evidence.bind(fixture)
        yield fixture


@pytest.fixture(scope="package")
def product_scope_fixture(product_provider, request):
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence

    from .product_scope_fixture import load_scope_fixture, prepare_scope_fixture, verify_scope_sources

    if os.environ.get("PRODUCT_B03_MODE") != "true" or product_provider.scope_inputs is None:
        pytest.fail("B03 scope setup requires the correlated protected browser workflow")
    record = CaseEvidence()
    with product_provider.fixture("scope", record) as owned:
        path = Path("test-results/product-scope-fixture.json").resolve()
        verify_scope_sources(
            product_provider.request, product_provider.base_url, product_provider.owner,
            {"product_id": owned["product_id"], "project_id": owned["project_id"]},
            product_provider.scope_inputs,
        )
        prepare_scope_fixture(
            product_provider.state, product_provider.request, product_provider.base_url,
            product_provider.owner,
            {"product_id": owned["product_id"], "project_id": owned["project_id"],
             "product_name": "eval-product-" + product_provider.run_id},
            product_provider.scope_inputs, path,
        )
        scope_evidence = getattr(request.config, "product_scope_evidence", None)
        if scope_evidence is None:
            pytest.fail("B03 requires a protected evidence collector")
        scope_evidence.bind(owned)
        fixture = load_scope_fixture(path, product_provider.binding)
        yield fixture
        records = getattr(scope_evidence, "records", {})
        if records and all(record.phases.get("call") == "passed" for record in records.values()):
            from tests.e2e.product_onboarding.product_v04_cli import run_isolation

            run_isolation(product_provider, fixture, scope_evidence)
            from tests.e2e.product_onboarding.product_v04_denials import run_denials

            run_denials(product_provider, fixture, scope_evidence)
            from tests.e2e.product_onboarding.product_v04_history import run_history

            run_history(product_provider, fixture, scope_evidence)


@pytest.fixture
def product_scope_case(request, product_provider, product_scope_fixture):
    evidence = getattr(request.config, "product_scope_evidence", None)
    record = evidence.case(request.node.nodeid) if evidence is not None else None
    if record is None:
        pytest.fail("B03 case is not in the selected scope inventory")
    original = product_provider.request
    product_provider.request = record.request(original)
    page = request.getfixturevalue("page") if "page" in request.fixturenames else None
    if page is not None:
        page.context.tracing.start(screenshots=True, snapshots=True)
    try:
        yield record
    finally:
        product_provider.request = original
        if page is not None:
            directory = Path(os.environ["E2E_SCREENSHOT_DIR"]).resolve()
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            screenshot = directory / ("product-v04-" + request.node.name + ".png")
            trace = directory / ("product-v04-" + request.node.name + ".zip")
            try:
                page.screenshot(path=str(screenshot))
                screenshot.chmod(0o600)
            finally:
                page.context.tracing.stop(path=str(trace))
                trace.chmod(0o600)
            record.viewports.append({"screenshot": str(screenshot), "trace": str(trace)})


@pytest.fixture
def product_scope_editor(product_provider, product_scope_fixture):
    from .product_scope_actors import share_scope_team

    return share_scope_team(product_provider, product_scope_fixture)


@pytest.fixture(scope="package")
def product_resume_provider(browser_instance, cognito_tokens, base_url):
    import json
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from tests.e2e.product_onboarding.product_v04_cli import install_served_cli

    from .product_fixture_provider import build_provider

    context = browser_instance.new_context(service_workers="block")
    try:
        provider = build_provider(os.environ, base_url, context.request, cognito_tokens, onboarding_enabled=True)
        try:
            # Resume/guidance/workspace cases exercise the shipped CLI. Install
            # it once for this owned suite and compare every file with the
            # selected release, using the same installer verifier as scope tests.
            with TemporaryDirectory(prefix="product-resume-cli-") as directory, pytest.MonkeyPatch.context() as environment:
                binary, checksums = install_served_cli(
                    provider, SimpleNamespace(context=provider.evidence_context), Path(directory)
                )
                environment.setenv("PRODUCT_RESUME_CLI_PATH", str(binary))
                environment.setenv("PRODUCT_RESUME_CLI_HASHES", json.dumps(checksums, sort_keys=True))
                yield provider
        finally:
            receipt = provider.recover()
            if os.environ.get("PRODUCT_SUITE_ID"):
                from .product_suite_cleanup import retain_cleanup

                retain_cleanup(receipt, Path("test-results").resolve(), os.environ)
    finally:
        context.close()


@pytest.fixture
def product_resume_fixture(request, product_resume_provider, page):
    from tests.e2e.product_onboarding.product_v05_record import ResumeEvidence

    from .product_release import private_capture_directory

    evidence = getattr(request.config, "product_resume_evidence", None)
    if evidence is None:
        if not os.environ.get("PRODUCT_EXPECTED_REVISION") or not os.environ.get("E2E_NEW_UI_ENABLED") == "1":
            pytest.fail("Resume needs pinned browser workflow evidence")
        evidence = ResumeEvidence(Path("test-results"), os.environ)
        request.config.product_resume_evidence = evidence
    cases = {"test_resume_ui_to_cli": "resume_ui", "test_resume_cli_to_ui": "resume_cli",
             "test_progress_math": "progress", "test_resume_recovery": "recovery"}
    record = evidence.case(request.node.nodeid)
    if record is None:
        pytest.fail("Resume case is missing from the V05 evidence inventory")
    directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
    with product_resume_provider.fixture(cases[request.node.name], record) as fixture:
        evidence.bind(fixture)
        fixture["evidence_record"] = record
        page.context.tracing.start(screenshots=True, snapshots=True)
        try:
            yield fixture
        finally:
            screenshot = directory / f"{fixture['run_id']}-{cases[request.node.name]}.png"
            trace = directory / f"{fixture['run_id']}-{cases[request.node.name]}.zip"
            try:
                page.screenshot(path=str(screenshot))
                screenshot.chmod(0o600)
            finally:
                page.context.tracing.stop(path=str(trace))
                trace.chmod(0o600)
            record.viewports.append({"screenshot": str(screenshot), "trace": str(trace)})


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    evidence = getattr(item.config, "product_evidence", None)
    record = evidence.case(item.nodeid) if evidence is not None else None
    if record is not None:
        record.phases[report.when] = report.outcome
    resume_evidence = getattr(item.config, "product_resume_evidence", None)
    resume_record = resume_evidence.case(item.nodeid) if resume_evidence is not None else None
    if resume_record is not None:
        resume_record.phases[report.when] = report.outcome
    scope_evidence = getattr(item.config, "product_scope_evidence", None)
    scope_record = scope_evidence.case(item.nodeid) if scope_evidence is not None else None
    if scope_record is not None:
        scope_record.phases[report.when] = report.outcome
    if Path(item.path).resolve().is_relative_to(PACKAGE) and report.when == "call" and report.failed:
        page = item.funcargs.get("page")
        if page:
            try:
                path = take_failure_screenshot(page, item.name)
                os.chmod(path, 0o600)
                report.sections.append(("Private failure screenshot", path))
            except Exception:
                pass
