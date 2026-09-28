"""Real browser parity using the current/new UI fixture's navigation assertions."""

import json
from urllib.parse import urlsplit

from tests.e2e.orchestration.config import resolve_secret_ref
from .http import Unsupported


def observe(client, flow_id):
    # Lazy imports keep the offline harness independent of browser/AWS packages.
    try:
        from playwright.sync_api import sync_playwright
        from tests.e2e.new_ui.test_coexistence import _current, _identity, _preview
        from tests.e2e.chat import helpers
    except ImportError:
        raise Unsupported("live Playwright parity dependencies unavailable") from None
    reference = client.config.secret_refs.get("browser_session")
    if not reference:
        raise Unsupported("scoped Cognito browser session reference unavailable")
    tokens = json.loads(resolve_secret_ref(reference))
    origin = client.manifest.api_origin.rstrip("/")
    if helpers.CLOUDFRONT_URL != origin:
        raise Unsupported("set E2E_CLOUDFRONT_URL to the reviewed manifest origin")
    features = client.get("/features")
    if features.get("features", {}).get("new_ui") is not True:
        raise Unsupported("preview is not enabled in the observed environment")
    expected_path = f"/api/orchestration/flows/{flow_id}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, service_workers="block"
        )
        try:
            page = context.new_page()
            helpers.inject_tokens_and_navigate(page, tokens, path="/activity")
            identity = _identity(page)
            owner = client.get("/auth/me")
            assert identity["sub"] == owner["user_id"] == client.config.identity_ref
            assert owner["org_id"] == client.config.org_ref

            def graph():
                with page.expect_response(
                    lambda r: urlsplit(r.url).path == expected_path
                    and r.request.method == "GET"
                ) as response:
                    page.goto(
                        origin + "/flows/" + flow_id, wait_until="domcontentloaded"
                    )
                assert response.value.status == 200
                value = response.value.json()
                page.get_by_role("heading", name=value["title"], exact=True).wait_for(
                    state="visible"
                )
                return value

            current = graph()
            _current(page)
            page.get_by_test_id("try-new-ui").click()
            _preview(page, origin)
            assert _identity(page) == identity
            # Today's preview links into the shared delivery page. Record that
            # route explicitly; do not claim an unimplemented migrated graph.
            links = page.locator('a[href="/flows"]:visible')
            if links.count() == 0:
                raise Unsupported("preview has no live Delivery Flows entry")
            links.first.click()
            preview = graph()
            assert _identity(page) == identity
            api = client.get(f"/orchestration/flows/{flow_id}")

            def normalized(value):
                return {
                    "flow_id": value["flow_id"],
                    "nodes": sorted(
                        (n["id"], n["state"], n["run_id"]) for n in value["nodes"]
                    ),
                }

            assert normalized(current) == normalized(preview) == normalized(api)
            return {
                "current_ui": {
                    "route": "/flows/" + flow_id,
                    "identity": identity,
                    "authenticated_owner": owner,
                    "graph": current,
                },
                "preview_ui": {
                    "route": "/next -> /flows/" + flow_id,
                    "identity": identity,
                    "graph": preview,
                },
                "api": api,
            }
        finally:
            context.close()
            browser.close()
