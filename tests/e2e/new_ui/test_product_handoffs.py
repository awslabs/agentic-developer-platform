"""Browser/CLI observation of the same administrator handoff operation."""

from tests.e2e.new_ui.product_navigation import open_product_section, product_session_status
from tests.e2e.cli_uplift.product_handoffs import finish_admin_handoff, start_admin_handoff


def browser_admin_resume(page, provider, base_url, fixture, session_id, key, *, navigate,
                         complete_admin, receipt_observer, resume_cli):
    """The browser resumes the API handoff; the admin workflow remains external."""
    navigate()
    open_product_section(page, "Financials")
    page.get_by_role("button", name="Start or resume onboarding").click()
    product_session_status(page).filter(has_text="Session " + session_id).wait_for(timeout=30_000)
    started = start_admin_handoff(provider.request, base_url, fixture, session_id, key)
    page.reload(wait_until="domcontentloaded")
    product_session_status(page).filter(has_text="Session " + session_id).wait_for(timeout=30_000)
    complete_admin(started["operation_id"])
    completed = finish_admin_handoff(provider.request, fixture, started, receipt_observer)
    cli_session = resume_cli(session_id)
    observer = fixture["observer"]["access_token"]
    response = provider.request.get(started["session_url"],
                                    headers={"Authorization": "Bearer " + observer}, timeout=30_000)
    if response.status != 200:
        raise AssertionError("Independent observer could not read the resumed session")
    observed = response.json()
    if (observed.get("id") != session_id or observed.get("tenant_id") != fixture["tenant_id"]
            or cli_session.get("id") != session_id or cli_session.get("steps") != observed.get("steps")
            or observed.get("steps", {}).get("financials") != started["before"].get("steps", {}).get("financials")):
        raise AssertionError("Browser, CLI and independent observer differ on saved progress")
    page.reload(wait_until="domcontentloaded")
    product_session_status(page).filter(has_text="Session " + session_id).wait_for(timeout=30_000)
    visible = page.locator("body").inner_text()
    if any(token and token in visible for token in (fixture["owner"]["access_token"],
                                                 fixture["observer"]["access_token"])):
        raise AssertionError("Browser displayed credentials during the handoff")
    return {**completed, "cli_session": cli_session, "observer_session": observed}


def browser_repository_recovery(page, provider, base_url, fixture, session_id, plan, keys,
                                provider_observer, *, navigate):
    """Confirm through the served UI, lose its reply, then reconcile that key."""
    from tests.e2e.cli_uplift.product_handoffs import recover_repository

    def preview_action(expected):
        navigate()
        open_product_section(page, "Connections & people")
        source = fixture["source_label"] + " · " + fixture["source_provider"]
        page.get_by_role("button", name="Inspect " + source).click()
        page.get_by_label("New repository name").fill(expected["name"])
        page.get_by_label("Visibility").select_option(value=expected["visibility"])
        template = "repository: " + fixture["template_name"]
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith("/repository-plans"), timeout=30_000) as captured:
            page.get_by_text(template).locator("..").get_by_role(
                "button", name="Preview creation from this template").click()
        response = captured.value
        if response.status not in (200, 201) or response.request.post_data_json != {"repository_plan": expected}:
            raise AssertionError("Browser did not preview the owned repository plan")
        preview = response.json()
        displayed = page.get_by_label("Repository creation preview").inner_text()
        if any(value not in displayed for value in (expected["name"], expected["visibility"],
                  expected["template_ref"]["record_id"], expected["protections_ref"]["record_revision"])):
            raise AssertionError("Browser preview did not show the confirmed plan and protections")
        return preview, response.request.headers.get("idempotency-key")

    def confirm_action(expected):
        observed = {}
        path = ("/api/v1/products/" + fixture["product_id"] + "/projects/" + fixture["project_id"]
                + "/onboarding-sessions/" + session_id + "/handoffs")

        def lose_reply(route):
            if route.request.post_data_json != expected:
                route.abort("failed")
                raise AssertionError("Browser attempted a different repository plan")
            observed["key"] = route.request.headers.get("idempotency-key")
            response = route.fetch(timeout=30_000)
            if response.status not in (200, 202):
                route.fulfill(response=response)
                raise AssertionError(f"Browser confirmation returned {response.status}")
            observed["response"] = response.json()
            route.abort("failed")

        page.route("**" + path, lose_reply)
        try:
            page.get_by_role("button", name="Confirm and create this repository").click()
            page.get_by_role("status").filter(has_text="Repository outcome is uncertain").wait_for(timeout=30_000)
        finally:
            page.unroute("**" + path, lose_reply)
        if set(observed) != {"key", "response"}:
            raise AssertionError("Browser never sent an owned repository confirmation")
        return observed["response"], observed["key"]

    return recover_repository(provider.request, base_url, fixture, session_id, plan, keys,
                              provider_observer, browser_preview=preview_action, browser_confirm=confirm_action)
