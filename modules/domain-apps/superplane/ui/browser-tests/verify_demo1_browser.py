"""Actual Chromium and maintained onboarding UI; all API responses are synthetic."""

import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "modules/domain-apps/superplane"))

from superplane_acceptance.demo1_browser import PREFIX, PlaywrightBrowserTransport
from superplane_acceptance.demo1_c1 import inspect_original_details, inspect_reentry
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_session import (
    browser_state_parts,
    restore_browser_session,
)

FRONTEND = ROOT / "modules/gateway/frontend"
OUTPUT = ROOT / "test-results/superplane-browser"
ORIGIN = "https://demo1.example.invalid"
LOCAL = "http://127.0.0.1:4175"
ORG = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
REQUEST = "44444444-4444-4444-8444-444444444444"
RELEASE = "a" * 64
TOKEN = "synthetic-demo1-requester"


def main():
    from playwright.sync_api import expect, sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)
    entry = FRONTEND / "demo1-browser-entry.tsx"
    html = FRONTEND / "demo1-browser-check.html"
    if entry.exists() or html.exists():
        raise RuntimeError("refusing to replace an existing browser entry")
    entry.write_text(Path(__file__).with_name("demo1-entry.tsx").read_text())
    html.write_text(
        '<!doctype html><html><body><div id="root"></div><script type="module" src="/demo1-browser-entry.tsx"></script></body></html>'
    )
    process = None
    calls, errors = [], []
    controls = {"release": RELEASE, "redirect": False}
    workspace = {
        "id": WORKSPACE,
        "org_id": ORG,
        "name": "demo1-fixture",
        "display_name": "Demo 1 fixture",
        "status": "Provisioning",
        "isolation_mode": "dedicated",
        "provisioning_operation_id": OPERATION,
    }

    def transport(route):
        request = route.request
        parsed = urlsplit(request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
            errors.append("unexpected external origin")
            route.abort()
            return
        if parsed.path.startswith("/api/"):
            calls.append((request.method, parsed.path))
            assert request.headers.get("authorization") == "Bearer " + TOKEN
            assert request.method == "GET", "fixture permits no lifecycle mutations"
            headers = {"X-Superplane-Release": controls["release"]}
            if parsed.path == PREFIX + "/capabilities":
                if controls["redirect"]:
                    route.fulfill(
                        status=302,
                        headers={"Location": "https://foreign.invalid/secret"},
                    )
                    return
                value = {
                    "features": ["create-operation-id-v1"],
                    "modes": ["managed"],
                    "providers": ["aws"],
                    "isolationModes": ["dedicated"],
                }
            elif parsed.path == PREFIX + "/workspaces":
                value = {"workspaces": [workspace]}
            elif parsed.path == PREFIX + f"/workspaces/{WORKSPACE}":
                value = workspace
            elif parsed.path == PREFIX + f"/operations/{OPERATION}":
                value = {
                    "provisioning_operation_id": OPERATION,
                    "request_id": REQUEST,
                    "workspace_id": WORKSPACE,
                    "phase": "bootstrap-workspace",
                    "state": "running",
                    "retryable": False,
                }
            elif parsed.path.endswith("/lifecycle-proposals"):
                value = {"workspace_id": WORKSPACE, "proposals": []}
            else:
                route.fulfill(
                    status=503,
                    json={"detail": "fixture capability unavailable"},
                    headers=headers,
                )
                return
            route.fulfill(json=value, headers=headers)
        else:
            path = (
                "/demo1-browser-check.html"
                if parsed.path == "/superplane"
                else parsed.path
            )
            with urlopen(
                LOCAL + path + ("?" + parsed.query if parsed.query else ""), timeout=10
            ) as response:
                route.fulfill(
                    status=response.status,
                    content_type=response.headers.get("Content-Type", "text/plain"),
                    body=response.read(),
                )

    try:
        with (OUTPUT / "demo1-vite.log").open("w") as log:
            process = subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4175",
                    "--strictPort",
                ],
                cwd=FRONTEND,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("fixture frontend unavailable")
                try:
                    with urlopen(LOCAL + "/demo1-browser-check.html", timeout=2):
                        break
                except (URLError, ConnectionResetError):
                    time.sleep(0.2)
            payload = {
                "sub": "fixture-requester",
                "custom:org_id": ORG,
                "custom:role": "org_admin",
                "auth_time": int(time.time()),
                "exp": int(time.time()) + 3600,
            }
            encoded = (
                base64.urlsafe_b64encode(json.dumps(payload).encode())
                .decode()
                .rstrip("=")
            )
            session = {
                "cognito_access_token": TOKEN,
                "cognito_id_token": "e30." + encoded + ".synthetic",
                "cognito_token_expiry": str(int((time.time() + 3600) * 1000)),
            }
            state, session = browser_state_parts(
                {
                    "cookies": [],
                    "origins": [
                        {
                            "origin": ORIGIN,
                            "localStorage": [],
                            "sessionStorage": [
                                {"name": key, "value": value}
                                for key, value in session.items()
                            ],
                        }
                    ],
                },
                ORIGIN,
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        storage_state=state, service_workers="block"
                    )
                    context.route("**/*", transport)
                    context.route_web_socket("**/*", lambda socket: None)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.set_default_timeout(10000)
                    restore_browser_session(page, ORIGIN, session, timeout=10000)
                    assert calls == []
                    page.goto(ORIGIN + "/superplane", wait_until="networkidle")
                    expect(
                        page.get_by_role("heading", name="Workspaces", exact=True)
                    ).to_be_visible()
                    client = PlaywrightBrowserTransport(
                        page, ORIGIN, release_id=RELEASE
                    )
                    page.evaluate(
                        "() => localStorage.setItem('cognito_access_token', 'stale-synthetic-token')"
                    )
                    assert client.request("GET", PREFIX + "/capabilities")[0] == 200
                    page.evaluate(
                        "() => localStorage.removeItem('cognito_access_token')"
                    )
                    assert (
                        "re-entry visible"
                        in inspect_reentry(page, "Demo 1 fixture")["reason"]
                    )
                    details = inspect_original_details(
                        page, WORKSPACE, REQUEST, OPERATION
                    )
                    assert "original identities visible" in details["reason"], details
                    expect(
                        page.get_by_role("region", name="Workspace details")
                    ).to_contain_text("bootstrap-workspace")
                    assert (
                        page.evaluate(
                            "() => localStorage.getItem('cognito_access_token')"
                        )
                        is None
                    )
                    for failure in ("release", "redirect", "session"):
                        controls.update(
                            release="f" * 64 if failure == "release" else RELEASE,
                            redirect=failure == "redirect",
                        )
                        if failure == "session":
                            page.evaluate("() => sessionStorage.clear()")
                        sent = []
                        try:
                            client.request(
                                "POST",
                                PREFIX + "/workspaces",
                                {},
                                before_send=lambda sent=sent: sent.append(True),
                            )
                        except EvidenceError:
                            pass
                        else:
                            raise AssertionError(
                                "unsafe browser submission was not refused"
                            )
                        assert sent == []
                    page.reload(wait_until="networkidle")
                    assert (
                        page.evaluate(
                            "() => sessionStorage.getItem('cognito_access_token')"
                        )
                        is None
                    )
                    assert errors == []
                    assert all(method == "GET" for method, _ in calls)
                    count = len(calls)
                    for path in (
                        "https://foreign.invalid/api",
                        PREFIX + "/../workspaces",
                        PREFIX + "/workspaces?foreign=true",
                    ):
                        try:
                            client.request("GET", path)
                        except EvidenceError:
                            pass
                        else:
                            raise AssertionError("unapproved path was not refused")
                    page.goto("about:blank")
                    try:
                        client.request("GET", PREFIX + "/capabilities")
                    except EvidenceError:
                        pass
                    else:
                        raise AssertionError("changed origin was not refused")
                    assert len(calls) == count
                    (OUTPUT / "demo1-browser.json").write_text(
                        json.dumps(
                            {
                                "evidence_mode": "offline-synthetic-http",
                                "browser": browser.version,
                                "session_import": "passed",
                                "maintained_ui_reentry": "passed",
                                "continuation_details": "passed",
                                "release_redirect_session_refusals": "passed",
                                "origin_path_refusals": "passed",
                                "live_acceptance": False,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                finally:
                    browser.close()
    finally:
        if process is not None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=15)
        entry.unlink(missing_ok=True)
        html.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
