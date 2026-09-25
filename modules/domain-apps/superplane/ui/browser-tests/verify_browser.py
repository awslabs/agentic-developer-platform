"""Remote CI browser/layout evidence; all product HTTP responses are fixtures."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
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


def main(kind="serving"):
    batch = kind == "batch"
    resource = "batch-jobs" if batch else "deployments"
    workload_name = "browser-job" if batch else "browser-model"
    options = {
        "image": IMAGE,
        "command": ["/app/run"],
        "args": ["--input", "/app/data.json"],
        "gpu_count": 1,
        "cpu": "2000m",
        "memory": "8Gi",
    }
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
                except (URLError, ConnectionResetError):
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
                if path == root + (
                    "/batch-profiles" if batch else "/deployment-profiles"
                ):
                    value = {
                        "workspace_id": WORKSPACE,
                        "can_submit": True,
                        "can_cancel": True,
                        "can_observe": True,
                        "can_read_accounting": True,
                        "can_read_results": batch,
                        "can_review_teardown": True,
                        "profiles": [
                            {
                                "profile_id": "fixture-gpu",
                                "image": IMAGE,
                                **(
                                    {"batch_options": options}
                                    if batch
                                    else {"model_options": MODEL}
                                ),
                            }
                        ],
                    }
                elif path == root + "/" + resource and method == "GET":
                    value = {
                        "workspace_id": WORKSPACE,
                        **(
                            {"jobs": rows, "truncated": False}
                            if batch
                            else {"deployments": rows}
                        ),
                    }
                elif (
                    batch
                    and path == root + f"/{resource}/{DEPLOYMENT}/result"
                    and method == "GET"
                ):
                    value = {
                        "workspace_id": WORKSPACE,
                        "job_id": DEPLOYMENT,
                        "operation_id": "fixture-create",
                        "status": "retained",
                        "media_type": "text/plain",
                        "result": {
                            "job_uid": "fixture-original-uid",
                            "pod_uid": "fixture-pod",
                            "content": "accuracy=0.95\n<script>untrusted text only</script>",
                            "sha256": "a" * 64,
                            "redacted": False,
                            "captured_at": "2026-09-24T12:00:00Z",
                        },
                    }
                elif (
                    path == root + f"/{resource}/{DEPLOYMENT}/observation"
                    and method == "GET"
                ):
                    query = parse_qs(parsed.query)
                    logs = query.get("logs") == ["true"]
                    assert not logs or query.get("pod_uid") == ["fixture-pod"]
                    value = {
                        "workspace_id": WORKSPACE,
                        "deployment_id": DEPLOYMENT,
                        "kind": kind,
                        "uid": "fixture-original-uid",
                        "state": "running" if batch else "ready",
                        "checked_at": "2026-09-24T12:00:00Z",
                        "pods": [
                            {
                                "uid": "fixture-pod",
                                "phase": "Running",
                                "ready": True,
                                "restarts": 0,
                                "exit_code": None,
                            }
                        ],
                        "logs": "epoch 1 completed\nrequest token: [redacted]\n"
                        + "bounded long output " * 20
                        if logs
                        else None,
                        "logs_pod_uid": "fixture-pod" if logs else None,
                        "logs_truncated": logs,
                    }
                elif (
                    path == root + f"/{resource}/{DEPLOYMENT}/accounting"
                    and method == "GET"
                ):
                    value = {
                        "workspace_id": WORKSPACE,
                        "deployment_id": DEPLOYMENT,
                        "kind": kind,
                        "checked_at": "2026-09-24T12:00:00Z",
                        "workspace_committed_budget_micros": "2000000",
                        "workspace_reservation_cap_micros": "5000000",
                        "workspace_budget_state": "available",
                        "estimated_cost_micros": None,
                        "observed_cost_micros": None,
                        "cost_reconciliation": "unavailable",
                        "recorded_resources": [
                            {"kind": "compute", "count": 1},
                            {"kind": "storage", "count": 1},
                        ],
                        "operations": [
                            {
                                "action": "provision",
                                "operation_id": "fixture-create",
                                "approved_max_cost_micros": "2000000",
                                "budget_held_micros": "2000000",
                                "budget_state": "retained",
                                "shared_reservation_state": "retained",
                                "accounting_consistent": True,
                                "updated_at": "2026-09-24T11:59:00Z",
                            }
                        ],
                    }
                elif path.endswith(("/" + resource + "/preview", "/teardown-preview")):
                    action = (
                        "teardown" if path.endswith("teardown-preview") else "provision"
                    )
                    plan = {
                        "provider_account_id": "111122223333",
                        "region": "us-east-1",
                        "namespace": "fixture-workspace",
                        "workload": {
                            "kind": kind,
                            "image": IMAGE,
                            **(
                                {**options, "port": None, "auth_secret": None}
                                if batch
                                else {}
                            ),
                        },
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
                        **({"job_id": DEPLOYMENT} if batch else {}),
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
                elif path == root + "/" + resource and method == "POST":
                    assert body["operation_id"] == reviewed["idempotency_key"]
                    assert (
                        body["approval_id"] == "fixture-approval"
                        and body["plan_revision"] == "b" * 64
                    )
                    if batch:
                        assert body["batch_options"] == options
                    else:
                        assert {key: body[key] for key in MODEL} == MODEL
                    rows[:] = [
                        {
                            "name": body["name"],
                            ("job_id" if batch else "deployment_id"): DEPLOYMENT,
                            "status": "Created",
                            "operation_id": "fixture-create",
                            "operation_state": "succeeded",
                        }
                    ]
                    value = rows[0]
                elif (
                    path == root + f"/{resource}/{DEPLOYMENT}/cancellation"
                    and method == "POST"
                ):
                    assert body == {"operation_id": "fixture-queued"}
                    rows[0].update(
                        status="CancelledBeforeDispatch",
                        operation_state="cancelled",
                        cancellation_requested=True,
                        cleanup_status="not-required",
                    )
                    value = {
                        **rows[0],
                        "deployment_id": DEPLOYMENT,
                        "workspace_id": WORKSPACE,
                    }
                elif path == root + f"/{resource}/{DEPLOYMENT}" and method == "DELETE":
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
                    page.goto(ORIGIN + "/serving-browser-check.html?kind=" + kind)
                    name = page.get_by_label(
                        re.compile("Job name" if batch else "Deployment name")
                    )
                    expect(name).to_be_visible()
                    name.fill(workload_name)
                    name.press("Tab")
                    select = page.get_by_label(
                        "Batch profile" if batch else "Serving profile"
                    )
                    expect(select).to_be_focused()
                    select.press("ArrowDown")
                    select.press("Tab")
                    prepare = page.get_by_role("button", name=f"Prepare {kind} review")
                    expect(prepare).to_be_focused()
                    prepare.press("Enter")
                    page.get_by_role(
                        "button", name=f"Review {kind} plan", exact=True
                    ).click()
                    expect(
                        page.get_by_text(
                            "Maximum additional cost: 2 USD. Observed cost: unknown."
                        )
                    ).to_be_visible()
                    page.get_by_role("button", name="Request workload approval").click()
                    submit = page.get_by_role(
                        "button",
                        name="Submit approved batch job"
                        if batch
                        else "Submit approved deployment",
                    )
                    expect(submit).to_be_enabled()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / f"{kind}-mobile-review.png"), full_page=True
                    )
                    submit.focus()
                    submit.press("Enter")
                    budget = page.get_by_role(
                        "button", name=f"View budget for {workload_name}"
                    )
                    expect(budget).to_be_visible()
                    budget.focus()
                    budget.press("Enter")
                    expect(
                        page.get_by_text(
                            "Original workload: approved ceiling 2 USD; budget held 2 USD."
                        )
                    ).to_be_visible()
                    inspect = page.get_by_role(
                        "button", name=f"Inspect status and logs for {workload_name}"
                    )
                    expect(inspect).to_be_visible()
                    inspect.focus()
                    inspect.press("Enter")
                    pod_select = page.get_by_label("Pod log window")
                    expect(pod_select).to_be_visible()
                    pod_select.select_option("fixture-pod")
                    expect(
                        page.get_by_label(f"Logs for {workload_name}")
                    ).to_contain_text("epoch 1 completed")
                    if batch:
                        result_button = page.get_by_role(
                            "button", name=f"View result for {workload_name}"
                        )
                        result_button.focus()
                        result_button.press("Enter")
                        expect(page.get_by_label("Batch result text")).to_contain_text(
                            "accuracy=0.95"
                        )
                        with page.expect_download() as download:
                            page.get_by_role(
                                "button", name="Download text result"
                            ).click()
                        assert (
                            download.value.suggested_filename
                            == f"batch-{DEPLOYMENT}.txt"
                        )
                    for width in [360, 1280]:
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                        page.screenshot(
                            path=str(OUTPUT / f"{kind}-logs-{width}.png"),
                            full_page=True,
                        )
                    page.set_viewport_size({"width": 360, "height": 800})
                    page.get_by_role(
                        "button", name=f"Review stop for {workload_name}"
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
                        path=str(OUTPUT / f"{kind}-mobile-stop.png"), full_page=True
                    )
                    page.set_viewport_size({"width": 1280, "height": 900})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / f"{kind}-desktop.png"), full_page=True
                    )
                    # A separate fixture operation represents queued work. No
                    # request in this browser test can create a real workload.
                    rows[:] = [
                        {
                            "name": "queued-workload",
                            ("job_id" if batch else "deployment_id"): DEPLOYMENT,
                            "status": "Pending",
                            "operation_state": "pending",
                            "operation_id": "fixture-queued",
                        }
                    ]
                    page.get_by_role(
                        "button",
                        name="Refresh batch jobs"
                        if batch
                        else "Refresh serving workloads",
                        exact=True,
                    ).click()
                    cancel = page.get_by_role(
                        "button",
                        name="Cancel pending operation for queued-workload",
                        exact=True,
                    )
                    expect(cancel).to_be_visible()
                    cancel.focus()
                    page.keyboard.press("Enter")
                    expect(
                        page.get_by_text(
                            "Cancelled before dispatch; no workload cleanup is required.",
                            exact=True,
                        )
                    ).to_be_visible()
                    page.set_viewport_size({"width": 360, "height": 800})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(OUTPUT / f"{kind}-mobile-cancel.png"), full_page=True
                    )
                    cancellations = [
                        item
                        for item in requests
                        if item["path"].endswith("/cancellation")
                    ]
                    assert len(cancellations) == 1
                    assert not errors, errors
                    mutations = [
                        item
                        for item in requests
                        if item["method"] == "DELETE"
                        or item["method"] == "POST"
                        and item["path"].endswith("/" + resource)
                    ]
                    assert len(mutations) == 2
                    assert (
                        mutations[0]["body"]["operation_id"]
                        != mutations[1]["body"]["operation_id"]
                    )
                    (OUTPUT / f"{kind}-receipt.json").write_text(
                        json.dumps(
                            {
                                "evidence": "isolated browser with fixture transports; not live acceptance",
                                "keyboard": "pass",
                                "widths": [360, 1280],
                                "mutations": mutations,
                                "cancellations": cancellations,
                            },
                            indent=2,
                        )
                    )
                except BaseException:
                    page.screenshot(
                        path=str(OUTPUT / f"{kind}-failure.png"), full_page=True
                    )
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
    main("serving")
    main("batch")
