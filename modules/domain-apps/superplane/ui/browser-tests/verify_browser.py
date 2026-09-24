"""Remote CI browser/layout evidence; all product HTTP responses are fixtures."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[5]
FRONTEND = ROOT / "modules/gateway/frontend"
OUTPUT = ROOT / "test-results/superplane-browser"
ORIGIN = "http://127.0.0.1:4173"
WORKSPACE = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT = "22222222-2222-4222-8222-222222222222"
IMAGE = "fixture/serving@sha256:" + "a" * 64
MODEL = {
    "model_name": "fixture/model",
    "precision": "fp16",
    "serving_framework": "vllm",
    "replicas": 1,
    "gpu_per_replica": 1,
    "tensor_parallel_size": 1,
    "max_model_len": None,
}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page_path = FRONTEND / "serving-browser-check.html"
    entry_path = FRONTEND / "serving-browser-entry.tsx"
    if page_path.exists() or entry_path.exists():
        raise RuntimeError("refusing to replace an existing browser entry")
    entry_path.write_text(Path(__file__).with_name("entry.tsx").read_text())
    page_path.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body><div id="root"></div><script type="module" src="/serving-browser-entry.tsx"></script></body></html>'
    )
    process = None
    requests, rows, reviewed = [], [], {}
    errors = []
    try:
        with (OUTPUT / "vite.log").open("w") as log:
            process = subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4173",
                    "--strictPort",
                ],
                cwd=FRONTEND,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(
                        "fixture frontend exited before becoming available"
                    )
                try:
                    with urlopen(
                        ORIGIN + "/serving-browser-check.html", timeout=2
                    ) as response:
                        assert response.status == 200
                    break
                except URLError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "fixture frontend did not become available"
                        ) from None
                    time.sleep(0.2)

            def transport(route):
                parsed = urlsplit(route.request.url)
                if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                    errors.append("unexpected non-fixture origin")
                    route.abort()
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    route.continue_()
                    return
                method = route.request.method
                body = (
                    route.request.post_data_json
                    if method in {"POST", "DELETE"}
                    else None
                )
                requests.append({"method": method, "path": path, "body": body})
                root = f"/api/superplane/v1/workspaces/{WORKSPACE}"
                if path == root + "/deployment-profiles":
                    value = {
                        "workspace_id": WORKSPACE,
                        "can_submit": True,
                        "can_review_teardown": True,
                        "profiles": [
                            {
                                "profile_id": "fixture-gpu",
                                "image": IMAGE,
                                "model_options": MODEL,
                            }
                        ],
                    }
                elif path == root + "/deployments" and method == "GET":
                    value = {"workspace_id": WORKSPACE, "deployments": rows}
                elif path.endswith(("/deployments/preview", "/teardown-preview")):
                    action = (
                        "teardown" if path.endswith("teardown-preview") else "provision"
                    )
                    plan = {
                        "provider_account_id": "111122223333",
                        "region": "us-east-1",
                        "namespace": "fixture-workspace",
                        "workload": {"kind": "serving", "image": IMAGE},
                    }
                    reviewed.clear()
                    reviewed.update(
                        workspace_id=WORKSPACE,
                        action=action,
                        idempotency_key=body["operation_id"],
                        parameters={
                            "controller_deployment_id": DEPLOYMENT,
                            "controller_plan": json.dumps(plan),
                            "max_resource_units": "0" if action == "teardown" else "1",
                            "max_runtime_seconds": "900",
                            "max_cost_micros": "0"
                            if action == "teardown"
                            else "2000000",
                        },
                    )
                    value = {
                        "deployment_id": DEPLOYMENT,
                        "request_id": body["operation_id"],
                        "revision": "b" * 64,
                        "controller_plan": plan,
                        "approval_request": dict(reviewed),
                    }
                elif path.startswith("/api/superplane/v1/operation-approvals"):
                    if method == "POST":
                        assert body == reviewed
                    value = {
                        "approval_id": "fixture-approval",
                        "workspace_id": WORKSPACE,
                        "result": "allowed-once",
                        "can_decide": False,
                        "expires_at": "2099-01-01T00:00:00Z",
                        "revoked": False,
                        "request": dict(reviewed),
                        "plan_digest": "b" * 64,
                        "envelope": {
                            "max_resource_units": 1,
                            "max_runtime_seconds": 900,
                            "max_cost_micros": 2000000,
                        },
                    }
                elif path == root + "/deployments" and method == "POST":
                    assert body["operation_id"] == reviewed["idempotency_key"]
                    assert (
                        body["approval_id"] == "fixture-approval"
                        and body["plan_revision"] == "b" * 64
                    )
                    assert {key: body[key] for key in MODEL} == MODEL
                    rows[:] = [
                        {
                            "name": body["name"],
                            "deployment_id": DEPLOYMENT,
                            "status": "Created",
                            "operation_id": "fixture-create",
                            "operation_state": "succeeded",
                        }
                    ]
                    value = rows[0]
                elif path == root + f"/deployments/{DEPLOYMENT}" and method == "DELETE":
                    assert body["operation_id"] == reviewed["idempotency_key"]
                    rows[0].update(
                        status="Deleting",
                        operation_id="fixture-stop",
                        operation_state="succeeded",
                    )
                    value = rows[0]
                else:
                    errors.append(f"unexpected fixture API: {method} {path}")
                    route.fulfill(
                        status=500, json={"detail": "unconfigured browser fixture"}
                    )
                    return
                route.fulfill(status=200, json=value)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 360, "height": 800})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/*", transport)
                try:
                    page.goto(ORIGIN + "/serving-browser-check.html")
                    name = page.get_by_label(re.compile("Deployment name"))
                    expect(name).to_be_visible()
                    name.fill("browser-model")
                    name.press("Tab")
                    select = page.get_by_label("Serving profile")
                    expect(select).to_be_focused()
                    select.press("ArrowDown")
                    select.press("Tab")
                    prepare = page.get_by_role("button", name="Prepare serving review")
                    expect(prepare).to_be_focused()
                    prepare.press("Enter")
                    page.get_by_role(
                        "button", name="Review serving plan", exact=True
                    ).click()
                    expect(
                        page.get_by_text(
                            "Maximum additional cost: 2 USD. Observed cost: unknown."
                        )
                    ).to_be_visible()
                    page.get_by_role("button", name="Request workload approval").click()
                    submit = page.get_by_role(
                        "button", name="Submit approved deployment"
                    )
                    expect(submit).to_be_enabled()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / "serving-mobile-review.png"), full_page=True
                    )
                    submit.focus()
                    submit.press("Enter")
                    page.get_by_role(
                        "button", name="Review stop for browser-model"
                    ).click()
                    page.get_by_role(
                        "button", name="Review stop plan", exact=True
                    ).click()
                    page.get_by_role("button", name="Request workload approval").click()
                    page.get_by_role("button", name="Submit approved stop").click()
                    expect(
                        page.get_by_text("Status: Deleting; operation: succeeded")
                    ).to_be_visible()
                    expect(
                        page.get_by_text(
                            re.compile(
                                "Existing resources and charges remain unresolved"
                            )
                        )
                    ).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / "serving-mobile-stop.png"), full_page=True
                    )
                    page.set_viewport_size({"width": 1280, "height": 900})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / "serving-desktop.png"), full_page=True
                    )
                    assert not errors, errors
                    mutations = [
                        item
                        for item in requests
                        if item["method"] == "DELETE"
                        or item["method"] == "POST"
                        and item["path"].endswith("/deployments")
                    ]
                    assert len(mutations) == 2
                    assert (
                        mutations[0]["body"]["operation_id"]
                        != mutations[1]["body"]["operation_id"]
                    )
                    (OUTPUT / "receipt.json").write_text(
                        json.dumps(
                            {
                                "evidence": "isolated browser with fixture transports; not live acceptance",
                                "keyboard": "pass",
                                "widths": [360, 1280],
                                "mutations": mutations,
                            },
                            indent=2,
                        )
                    )
                except BaseException:
                    page.screenshot(path=str(OUTPUT / "failure.png"), full_page=True)
                    raise
                finally:
                    browser.close()
    finally:
        page_path.unlink(missing_ok=True)
        entry_path.unlink(missing_ok=True)
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
