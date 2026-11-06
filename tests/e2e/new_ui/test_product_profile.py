"""Served Product workspace checks; live execution requires a matched deployment."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

import pytest

from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.new_ui.product_navigation import open_product_section
from tests.e2e.new_ui.product_release import check_served_assets, capture_product_viewports, private_capture_directory
from tests.e2e.new_ui.product_recovery import ProductRun, dispatch_owned_product


def product_api(request, base_url, tokens, path):
    return request.get(
        base_url + "/api/v1" + path,
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        timeout=30_000,
    )


def assert_saved_profile(response, product_id, project_id, name, purpose, users):
    assert response.status == 200, f"Independent Product read failed: HTTP {response.status}"
    profile = response.json()
    assert profile["product_id"] == product_id
    assert profile["project_id"] == project_id
    assert profile["product"]["name"] == name
    assert profile["product"]["purpose"] == purpose
    assert profile["product"]["intended_users"] == users
    return profile


def assert_journey_readback(response, fixture, starting_point, name, purpose, users, previous_revision):
    profile = assert_saved_profile(response, fixture["product_id"], fixture["project_id"], name, purpose, users)
    assert profile["product"]["starting_point"] == starting_point, "Saved starting point changed during browser editing"
    assert profile["revision"] == previous_revision + 1, "Browser save did not advance the saved revision once"
    return profile


def assert_offline_unchanged(response, fixture, baseline):
    saved = assert_saved_profile(response, fixture["product_id"], fixture["project_id"],
                                 baseline["product"]["name"], baseline["product"]["purpose"],
                                 baseline["product"]["intended_users"])
    assert saved["revision"] == baseline["revision"], "Offline attempt committed or duplicated a Profile edit"
    assert saved["product"]["starting_point"] == baseline["product"]["starting_point"]
    return saved


@pytest.mark.preview_on
@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_product_full_journey(page, base_url, deployed_features, product_resume_provider, starting_point):
    from playwright.sync_api import expect

    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence, private_json
    from tests.e2e.product_onboarding.test_v11 import verify_full_journey_report

    assert deployed_features.get("product_profile") is True
    assert deployed_features.get("product_onboarding") is True
    record = CaseEvidence()
    case = "v11_" + starting_point
    record.phases = {"setup": "failed", "call": "failed", "teardown": "failed"}
    with product_resume_provider.fixture(case, record) as fixture:
        check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"],
                            os.environ["PRODUCT_EXPECTED_REVISION"])
        assert fixture["source_revision"] == os.environ["PRODUCT_EXPECTED_REVISION"]
        assert fixture["evidence_context"]["deployment"]["observed"]["product_onboarding_enabled"] is True
        owner = fixture["owner"]
        request = record.request(page.context.request)
        path = product_path(fixture["product_id"], fixture["project_id"])
        name = "eval-product-" + fixture["run_id"] + "-" + case
        purpose = "Verify saved Product workspace information"
        users = ["Maintainers"]
        initial = assert_saved_profile(product_api(request, base_url, owner, path),
                                       fixture["product_id"], fixture["project_id"], name, purpose, users)
        assert initial["product"]["starting_point"] == starting_point
        assert initial["revision"] >= 1
        record.phases["setup"] = "passed"
        directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
        for width in (390, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            inject_tokens_and_navigate(page, owner, path=path)
            expect(page.get_by_role("heading", name=name)).to_be_visible()
            open_product_section(page)
            expect(page.get_by_label("Name")).to_have_value(name)
            expect(page.get_by_label("Purpose")).to_have_value(purpose)
            expect(page.get_by_label("Intended users")).to_have_value(users[0])
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_label("Name").focus()
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "product-edit-purpose"
            edited_purpose = purpose + f" at {width}px"
            page.context.set_offline(True)
            try:
                page.get_by_label("Purpose").fill(edited_purpose)
                page.get_by_role("button", name="Save changes").click()
                expect(page.get_by_role("status")).to_contain_text("couldn't confirm the changes")
                expect(page.get_by_label("Purpose")).to_have_value(edited_purpose)
            finally:
                page.context.set_offline(False)
            assert_offline_unchanged(product_api(request, base_url, owner, path), fixture, initial)
            page.get_by_role("button", name="Check save status").click()
            expect(page.get_by_text(f"Saved revision {initial['revision'] + 1}")).to_be_visible()
            updated = assert_journey_readback(product_api(request, base_url, owner, path), fixture,
                                              starting_point, name, edited_purpose, users, initial["revision"])
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_label("Purpose")).to_have_value(edited_purpose)
            screenshot = directory / f"{fixture['run_id']}-{case}-{width}.png"
            descriptor = os.open(screenshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(page.screenshot(full_page=True))
            record.viewports.append({"width": width, "screenshot": str(screenshot),
                                     "revision": updated["revision"], "purpose": edited_purpose,
                                     "keyboard": "pass", "offline_retry": "pass"})
            purpose, initial = edited_purpose, updated
        record.phases["call"] = "passed"
    record.phases["teardown"] = "passed"
    identities = fixture["evidence_context"]["identities"]
    report = verify_full_journey_report({
        "scenario_id": "SCN-01", "ac_id": "AC-01", "starting_point": starting_point,
        "run_id": fixture["run_id"], "source_revision": fixture["source_revision"],
        "requirements_revision": identities["requirements_revision"],
        "observed": fixture["evidence_context"]["deployment"]["observed"],
        "evidence_context": fixture["evidence_context"],
        "frontend_release_ref": identities["frontend_release_ref"],
        "api_image_digest": identities["api_image_digest"], "schema_head": identities["schema_head"],
        "product_id": fixture["product_id"], "project_id": fixture["project_id"],
        "browser_status": record.status, "viewports": record.viewports,
        "api_reads": record.reads, "cleanup": record.cleanup[-1] if record.cleanup else None,
    }, directory)
    private_json(directory / f"{fixture['run_id']}-{case}-report.json", report)


@pytest.mark.preview_on
def test_v11_release_regressions(page, base_url, deployed_features, product_resume_provider):
    from playwright.sync_api import expect

    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence, private_json
    from tests.e2e.product_onboarding.test_v11 import verify_release_report

    assert deployed_features.get("product_profile") is True
    assert deployed_features.get("product_onboarding") is True
    record = CaseEvidence()
    record.phases = {"setup": "failed", "call": "failed", "teardown": "failed"}
    with product_resume_provider.fixture("v11_release", record) as fixture:
        validate_conflict_fixture(fixture, fixture["owner"], os.environ["PRODUCT_EXPECTED_REVISION"], name_suffix="-v11_release")
        check_served_assets(product_resume_provider.request, base_url, fixture["frontend_release"], fixture["source_revision"])
        request = record.request(product_resume_provider.request)
        path = product_path(fixture["product_id"], fixture["project_id"])
        owner = fixture["owner"]
        current = product_api(request, base_url, owner, path)
        assert current.status == 200
        baseline = current.json()
        name, purpose, users = (baseline["product"][key] for key in ("name", "purpose", "intended_users"))
        assert_denied_read(product_api(request, base_url, fixture["nonmember"], path), (name, purpose))
        record.phases["setup"] = "passed"

        inject_tokens_and_navigate(page, owner, path=path)
        expect(page.get_by_role("heading", name=name)).to_be_visible()
        people_response = product_api(request, base_url, owner, path + "/people")
        assert people_response.status == 200
        people = people_response.json()
        assert people["owner"]["id"] == baseline["product"]["owner_ref"]["id"]
        assert not any(people["permissions"].values()), "Ordinary owners must not gain organization administration"
        open_product_section(page, "Connections & people")
        people_panel = page.get_by_role("region", name="People and access", exact=True)
        expect(people_panel).to_contain_text(people["owner"]["name"])
        expect(people_panel).to_contain_text(people["team"]["name"] if people["team"] else "No team assigned")
        for member in people["members"]:
            expect(people_panel).to_contain_text(member["name"])
            if member["github_username"]:
                expect(people_panel).to_contain_text("@" + member["github_username"])
        expect(people_panel.get_by_role("button", name="Manage people", exact=True)).to_have_count(0)
        drafted_purpose = purpose + " from V11 browser"
        open_product_section(page)
        page.get_by_label("Purpose").fill(drafted_purpose)
        other_purpose = purpose + " from another client"
        other = patch_product(request, base_url, owner, path, baseline["revision"], other_purpose)
        assert other.status == 200
        assert other.json()["revision"] == baseline["revision"] + 1
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Review conflicting changes")).to_be_visible()
        expect(page.get_by_label("Purpose")).to_have_value(drafted_purpose)

        page.context.set_offline(True)
        try:
            page.get_by_role("button", name="Replace saved fields with my edits").click()
            expect(page.get_by_role("status")).to_contain_text("couldn't confirm the changes")
            expect(page.get_by_label("Purpose")).to_have_value(drafted_purpose)
        finally:
            page.context.set_offline(False)
        assert_offline_unchanged(product_api(request, base_url, owner, path), fixture, other.json())
        page.get_by_role("button", name="Check save status").click()
        expect(page.get_by_text(f"Saved revision {other.json()['revision'] + 1}")).to_be_visible()
        saved = assert_saved_profile(product_api(request, base_url, owner, path),
                                     fixture["product_id"], fixture["project_id"], name, drafted_purpose, users)
        assert saved["revision"] == other.json()["revision"] + 1
        directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
        record.viewports = capture_product_viewports(page, directory, fixture["run_id"] + "-v11-release", name)
        fixture["revoke"]()
        assert_revoked_read(product_api(request, base_url, fixture["revoked"], path), (name, drafted_purpose))
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("alert")).to_contain_text("Product unavailable or you do not have access")
        record.phases["call"] = "passed"
    record.phases["teardown"] = "passed"
    identities = fixture["evidence_context"]["identities"]
    report = verify_release_report({
        "scenario_id": "SCN-03", "ac_id": "AC-03", "run_id": fixture["run_id"],
        "source_revision": fixture["source_revision"],
        "requirements_revision": identities["requirements_revision"],
        "observed": fixture["evidence_context"]["deployment"]["observed"],
        "evidence_context": fixture["evidence_context"],
        "frontend_release_ref": identities["frontend_release_ref"],
        "api_image_digest": identities["api_image_digest"], "schema_head": identities["schema_head"],
        "tenant_id": fixture["tenant_id"], "product_id": fixture["product_id"],
        "project_id": fixture["project_id"], "baseline": baseline,
        "concurrent": other.json(), "saved": saved,
        "browser_status": record.status, "viewports": record.viewports,
        "api_reads": record.reads, "cleanup": record.cleanup[-1] if record.cleanup else None,
    }, directory)
    private_json(directory / f"{fixture['run_id']}-v11-release-report.json", report)


@pytest.mark.preview_on
def test_product_create(page, base_url, deployed_features, product_case_evidence, product_run_fixture):
    cognito_tokens = product_run_fixture["owner"]
    assert deployed_features.get("product_profile") is True, "Product API admission is not enabled"
    check_served_assets(page.context.request, base_url, product_run_fixture["frontend_release"],
                        os.environ["PRODUCT_EXPECTED_REVISION"])
    directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
    source_revision = os.environ["PRODUCT_EXPECTED_REVISION"]
    if product_run_fixture["source_revision"] != source_revision or product_run_fixture["owner"]["access_token"] != cognito_tokens["access_token"]:
        raise ValueError("Browser actor or fixture source differs from the protected run")
    run_name = "eval-product-" + product_run_fixture["run_id"]
    purpose = "Verify saved Product workspace information"
    users = ["Maintainers"]
    body = {"name": run_name, "purpose": purpose, "intended_users": users,
            "starting_point": "idea", "project": {"mode": "create"}}
    run = ProductRun(
        product_run_fixture["state_store"], product_run_fixture["protected_cleanup"],
        run_id=product_run_fixture["run_id"], tenant_id=product_run_fixture["tenant_id"],
        actor_id=product_run_fixture["actor_id"], source_revision=source_revision,
        owner_token=cognito_tokens["access_token"], request_body=body,
    )
    request = product_case_evidence.request(page.context.request)
    create_attempted = False
    dispatch_failures = []

    def intercept(route):
        if not dispatch_owned_product(route, run):
            dispatch_failures.append("intent_not_durable")

    page.route("**/api/v1/products", intercept)
    try:
        inject_tokens_and_navigate(page, cognito_tokens, path="/products/new")
        from playwright.sync_api import expect

        expect(page.get_by_role("heading", name="Create product")).to_be_visible()
        assert page.get_by_label(re.compile("project", re.I)).count() == 0
        page.get_by_label("Name").fill(run_name)
        page.get_by_label("Purpose").fill(purpose)
        page.get_by_label("Intended users").fill(users[0])
        create_attempted = True
        page.get_by_role("button", name="Save product").click()
        assert not dispatch_failures, "Product intent could not be persisted before dispatch"
        page.wait_for_url(re.compile(r"/products/[^/]+/projects/[^/]+$"), timeout=30_000)
        parts = urlsplit(page.url).path.split("/")
        product_id, project_id = parts[2], parts[4]
        receipt = run.reconcile(request, base_url)
        assert receipt == {"product_id": product_id, "project_id": project_id}, "Browser destination differs from persisted Product receipt"
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name=run_name)).to_be_visible()
        information = page.get_by_role("region", name="Information", exact=True)
        expect(information).to_contain_text(purpose)
        expect(information).to_contain_text(users[0])
        expect(page.get_by_role("combobox", name="Delivery scope", exact=True)).to_have_count(0)
        profile = assert_saved_profile(
            product_api(request, base_url, cognito_tokens, "/products/" + product_id + "/projects/" + project_id),
            product_id, project_id, run_name, purpose, users,
        )
        assert profile["revision"] >= 1
        product_case_evidence.viewports = capture_product_viewports(page, directory, product_run_fixture["run_id"], run_name)
    finally:
        try:
            if create_attempted:
                product_case_evidence.cleanup.append(run.retire(request, base_url, product_run_fixture["observer"]))
        finally:
            page.unroute("**/api/v1/products", intercept)


def product_path(product_id, project_id):
    return "/products/" + quote(product_id, safe="") + "/projects/" + quote(project_id, safe="")


def patch_product(request, base_url, tokens, path, revision, purpose):
    return request.patch(
        base_url + "/api/v1" + path,
        headers={
            "Authorization": "Bearer " + tokens["access_token"],
            "Idempotency-Key": uuid4().hex,
            "If-Profile-Revision": str(revision),
        },
        data={"purpose": purpose},
        timeout=30_000,
    )


def assert_denied_read(response, fields):
    assert response.status == 404, f"Product scope read did not deny access: HTTP {response.status}"
    for field in fields:
        assert field not in response.text(), "Denied Product response disclosed a saved field"


def assert_revoked_read(response, fields):
    assert response.status == 403, f"Revoked tenant read did not deny access: HTTP {response.status}"
    assert response.json().get("detail", {}).get("error") == "tenant_membership_revoked", "Expected canonical tenant membership revocation"
    for field in fields:
        assert field not in response.text(), "Denied Product response disclosed a saved field"



def validate_conflict_fixture(fixture, browser_tokens, source_revision, *, name_suffix=""):
    assert isinstance(fixture, dict), "Owned editor fixture must be a protected record"
    for key in ("run_id", "tenant_id", "actor_id", "source_revision", "product_id", "project_id", "intent_key"):
        assert isinstance(fixture.get(key), str) and fixture[key], "Editor ownership record is incomplete"
    assert re.fullmatch(r"[a-f0-9]{40}", source_revision), "A pinned source revision is required"
    assert fixture["source_revision"] == source_revision, "Editor fixture differs from the pinned release"
    for key in ("owner", "nonmember", "revoked"):
        assert isinstance(fixture.get(key), dict) and fixture[key].get("access_token"), "Peer session is missing"
    owner, nonmember, revoked = (fixture[key] for key in ("owner", "nonmember", "revoked"))
    assert owner.get("actor_id") == fixture["actor_id"], "Browser actor differs from owned intent"
    assert owner.get("tenant_id") == fixture["tenant_id"], "Browser tenant differs from owned intent"
    if browser_tokens["access_token"] != owner["access_token"]:
        raise AssertionError("Browser session differs from owned actor")
    assert nonmember.get("tenant_id") != fixture["tenant_id"], "Nonmember must be in another tenant"
    if nonmember["access_token"] == owner["access_token"]:
        raise AssertionError("Nonmember must have a separate session")
    assert revoked.get("actor_id") == fixture["actor_id"], "Revoked session differs from owned actor"
    assert callable(fixture.get("revoke")) and callable(fixture.get("protected_cleanup")), "Protected revocation and cleanup are required"
    observer = fixture.get("observer")
    assert isinstance(observer, dict) and observer.get("tenant_id") == fixture["tenant_id"] and observer.get("access_token"), "Independent owning-tenant observer is missing"
    store = fixture.get("state_store")
    assert all(callable(getattr(store, method, None)) for method in ("load", "save")), "Durable Product ownership is missing"
    document, manifest = store.load(fixture["run_id"])
    assert isinstance(document, dict) and isinstance(manifest, dict), "Protected Product intent was not persisted"
    run = ProductRun(store, fixture["protected_cleanup"], run_id=fixture["run_id"],
                     tenant_id=fixture["tenant_id"], actor_id=fixture["actor_id"],
                     source_revision=source_revision, owner_token=owner["access_token"],
                     request_body=document["request"], name_suffix=name_suffix)
    run._read()
    assert document["idempotency_key"] == fixture["intent_key"] and document.get("receipt") == {
        "product_id": fixture["product_id"], "project_id": fixture["project_id"]
    }, "Editor fixture differs from the protected Product receipt"

@pytest.mark.preview_on
def test_product_conflict_denial(page, base_url, deployed_features, product_case_evidence, product_run_fixture):
    cognito_tokens = product_run_fixture["owner"]
    from playwright.sync_api import expect

    assert deployed_features.get("product_profile") is True, "Product API admission is not enabled"
    validate_conflict_fixture(product_run_fixture, cognito_tokens, os.environ.get("PRODUCT_EXPECTED_REVISION", ""))
    check_served_assets(page.context.request, base_url, product_run_fixture["frontend_release"],
                        os.environ["PRODUCT_EXPECTED_REVISION"])
    directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
    owner = product_run_fixture["owner"]
    product_id, project_id = product_run_fixture["product_id"], product_run_fixture["project_id"]
    path = product_path(product_id, project_id)
    request = product_case_evidence.request(page.context.request)
    original = product_api(request, base_url, owner, path)
    assert original.status == 200, f"Owned edit fixture unavailable: HTTP {original.status}"
    profile = original.json()
    name = profile["product"]["name"]
    purpose = profile["product"]["purpose"]
    users = profile["product"]["intended_users"]
    assert_denied_read(product_api(request, base_url, product_run_fixture["nonmember"], path), (name, purpose))

    inject_tokens_and_navigate(page, cognito_tokens, path=path)
    expect(page.get_by_role("heading", name=name)).to_be_visible()
    drafted_purpose = purpose + " from browser"
    open_product_section(page)
    page.get_by_label("Purpose").fill(drafted_purpose)
    other_purpose = purpose + " from another client"
    other = patch_product(request, base_url, owner, path, profile["revision"], other_purpose)
    assert other.status == 200, f"Concurrent edit did not commit: HTTP {other.status}"
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Review conflicting changes")).to_be_visible()
    expect(page.get_by_label("Purpose")).to_have_value(drafted_purpose)

    page.context.set_offline(True)
    try:
        page.get_by_role("button", name="Replace saved fields with my edits").click()
        expect(page.get_by_role("status")).to_contain_text("couldn't confirm the changes")
        expect(page.get_by_label("Purpose")).to_have_value(drafted_purpose)
    finally:
        page.context.set_offline(False)
    assert_saved_profile(product_api(request, base_url, owner, path), product_id, project_id, name, other_purpose, users)
    page.get_by_role("button", name="Check save status").click()
    expect(page.get_by_role("status")).to_contain_text("Saved revision")
    assert_saved_profile(product_api(request, base_url, owner, path), product_id, project_id, name, drafted_purpose, users)
    product_case_evidence.viewports = capture_product_viewports(page, directory, product_run_fixture["run_id"] + "-edit", name)
    product_run_fixture["revoke"]()
    assert_revoked_read(product_api(request, base_url, product_run_fixture["revoked"], path), (name, drafted_purpose))
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("alert")).to_contain_text("Product unavailable or you do not have access")


@pytest.mark.preview_on
def test_product_evidence(page, base_url, deployed_features, product_case_evidence, product_run_fixture):
    """SCN-03: live viewport evidence and a synthetic wrong-digest refusal for an owned fixture."""
    from copy import deepcopy

    from playwright.sync_api import expect

    cognito_tokens = product_run_fixture["owner"]
    assert deployed_features.get("product_profile") is True, "Product API admission is not enabled"
    source_revision = os.environ["PRODUCT_EXPECTED_REVISION"]
    validate_conflict_fixture(product_run_fixture, cognito_tokens, source_revision)
    fixture = product_run_fixture
    owner = fixture["owner"]
    request = product_case_evidence.request(page.context.request)
    path = product_path(fixture["product_id"], fixture["project_id"])
    document, _ = fixture["state_store"].load(fixture["run_id"])
    run = ProductRun(
        fixture["state_store"], fixture["protected_cleanup"], run_id=fixture["run_id"],
        tenant_id=fixture["tenant_id"], actor_id=fixture["actor_id"],
        source_revision=source_revision, owner_token=owner["access_token"],
        request_body=document["request"],
    )
    try:
        observed = check_served_assets(request, base_url, fixture["frontend_release"], source_revision)
        assert observed["status"] == "matched"
        wrong = deepcopy(fixture["frontend_release"])
        wrong["release"]["assets"]["index.html"] = "0" * 64
        with pytest.raises(AssertionError, match="Wrong frontend bundle served"):
            check_served_assets(request, base_url, wrong, source_revision)

        response = product_api(request, base_url, owner, path)
        assert response.status == 200, f"Owned Product read unavailable: HTTP {response.status}"
        profile = response.json()
        name = profile["product"]["name"]
        purpose = profile["product"]["purpose"]
        users = profile["product"]["intended_users"]
        assert_saved_profile(response, fixture["product_id"], fixture["project_id"], name, purpose, users)
        inject_tokens_and_navigate(page, cognito_tokens, path=path)
        expect(page.get_by_role("heading", name=name)).to_be_visible()
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name=name)).to_be_visible()
        expect(page.get_by_role("region", name="Information", exact=True)).to_contain_text(purpose)
        assert_saved_profile(product_api(request, base_url, owner, path),
                             fixture["product_id"], fixture["project_id"], name, purpose, users)
        directory = private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"])
        viewports = capture_product_viewports(page, directory, fixture["run_id"] + "-evidence", name, trace=True)
        product_case_evidence.viewports = viewports
        assert [entry["width"] for entry in viewports] == [390, 1440]
    finally:
        cleanup = run.retire(request, base_url, fixture["observer"])
        product_case_evidence.cleanup.append(cleanup)
        assert cleanup["fixture_removed"] is True
