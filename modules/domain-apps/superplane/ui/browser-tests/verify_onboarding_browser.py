"""Isolated onboarding keyboard/layout checks with fixture HTTP, never live admission."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[5]
FRONTEND = ROOT / "modules/gateway/frontend"
OUTPUT = ROOT / "test-results/superplane-browser"
ORIGIN = "http://127.0.0.1:4174"
WORKSPACE = "11111111-1111-4111-8111-111111111111"
PREFIX = "/api/superplane/v1"
PROPOSALS = PREFIX + f"/workspaces/{WORKSPACE}/lifecycle-proposals"
PLAN = PROPOSALS + "/fixture-plan"
APPROVAL = "22222222-2222-4222-8222-222222222222"
PHASE = "33333333-3333-4333-8333-333333333333"
EXPIRY_PATHS = {
    "create": PREFIX + "/workspaces/preview",
    "approval": PREFIX + "/operation-approvals/fixture-approval/decision",
    "continuation": PLAN + "/continue",
    "retirement": PREFIX + f"/workspaces/{WORKSPACE}/retirement/preview",
}
PROPOSAL = {
    "status": "awaiting_plan_approval",
    "artifact_id": "fixture-plan",
    "workspace_id": WORKSPACE,
    "source_operation_id": "fixture-phase",
    "request_revision": "a" * 64,
    "phase": "apply-infrastructure",
    "account_id": "000000000000",
    "target": {"region": "example-region-1"},
    "plan_file_sha256": "b" * 64,
    "plan_json_sha256": "c" * 64,
    "inventory": [{"address": "aws_vpc.workspace", "actions": ["create"]}],
    "estimate": None,
}


def phase_approval_request(request_id):
    return {
        "workspace_id": WORKSPACE,
        "action": "provision",
        "idempotency_key": request_id,
        "parameters": {
            "lifecycle_artifact_id": "fixture-plan",
            "lifecycle_source_operation_id": "fixture-phase",
            "terraform_plan_file_sha256": PROPOSAL["plan_file_sha256"],
            "terraform_plan_sha256": PROPOSAL["plan_json_sha256"],
        },
    }


def lifecycle_transport(failure):
    request_id, approval_request, decision, admitted = None, None, "pending", False

    def transport(route, path, method, body):
        nonlocal request_id, approval_request, decision, admitted
        if path == PROPOSALS and method == "GET":
            response = {
                "workspace_id": WORKSPACE,
                "proposals": [] if admitted else [PROPOSAL],
            }
        elif path == PLAN + "/preview" and method == "POST":
            assert set(body) == {"operation_id"} and body["operation_id"]
            if request_id is not None:
                assert body["operation_id"] == request_id
            request_id = body["operation_id"]
            if failure:
                route.fulfill(
                    status=503, json={"detail": "fixture phase preview unavailable"}
                )
                return True
            response = {
                **PROPOSAL,
                "request_id": request_id,
                "revision": "d" * 64,
                "approval_request": phase_approval_request(request_id),
            }
        elif path == PREFIX + "/operation-approvals" and method == "POST":
            assert request_id and body == phase_approval_request(request_id)
            approval_request = body
            response = None
        elif path == PREFIX + f"/operation-approvals/{APPROVAL}" and method == "GET":
            assert approval_request
            response = None
        elif (
            path == PREFIX + f"/operation-approvals/{APPROVAL}/decision"
            and method == "POST"
        ):
            assert approval_request and body == {"result": "allowed-once"}
            decision, response = "allowed-once", None
        elif path == PLAN + "/continue" and method == "POST":
            assert request_id and approval_request and decision == "allowed-once"
            assert body == {"operation_id": request_id, "approval_id": APPROVAL}
            admitted, response = True, {}
        elif (
            method == "GET"
            and request_id
            and path
            in (
                PREFIX + f"/operations/by-idempotency/{request_id}",
                PREFIX + f"/operations/{PHASE}",
            )
        ):
            if not admitted:
                route.fulfill(status=404, json={"detail": "fixture phase not admitted"})
                return True
            response = {}
        else:
            return False
        if response is None:
            response = {
                "approval_id": APPROVAL,
                "workspace_id": WORKSPACE,
                "result": decision,
                "request": approval_request,
                "can_decide": True,
                "expires_at": "2999-01-01T00:00:00Z",
                "revoked": False,
                "plan_digest": "d" * 64,
                "envelope": {
                    "max_resource_units": 2,
                    "max_runtime_seconds": 900,
                    "max_cost_micros": 1000000,
                },
            }
        elif response == {}:
            response = {
                "request_id": request_id,
                "workspace_id": WORKSPACE,
                "provisioning_operation_id": PHASE,
                "state": "running",
                "phase": "apply-infrastructure",
                "retryable": False,
            }
        route.fulfill(json=response)
        return True

    return transport


def fixture_transport(requests, failure, *, continuation=False, expire_action=None):
    assert expire_action is None or expire_action in EXPIRY_PATHS
    expired = False
    lifecycle = lifecycle_transport(failure) if continuation else None

    def transport(route):
        nonlocal expired
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
            route.abort()
            raise AssertionError("unexpected non-fixture origin")
        path = parsed.path
        if path == "/login" and route.request.method == "GET":
            route.fulfill(
                content_type="text/html", body="<h1>Fixture sign-in required</h1>"
            )
            return
        if not path.startswith("/api/"):
            route.continue_()
            return
        method = route.request.method
        body = route.request.post_data_json if method == "POST" else None
        requests.append({"method": method, "path": path, "body": body})
        if (
            not expired
            and expire_action
            and method == "POST"
            and path == EXPIRY_PATHS[expire_action]
        ):
            expired = True
            route.fulfill(status=401, json={"detail": "fixture session expired"})
            return
        if lifecycle and lifecycle(route, path, method, body):
            return
        if path == PREFIX + "/workspaces/preview" and method == "POST":
            if failure:
                route.fulfill(
                    status=503, json={"detail": "fixture preview unavailable"}
                )
            else:
                route.fulfill(
                    json={
                        "revision": "a" * 64,
                        "mode": "managed",
                        "target": {
                            "account": "example-account",
                            "region": "example-region-1",
                            "cluster": None,
                        },
                        "ownership": "adp-managed",
                        "requested_capacity": "2 GPUs",
                        "cost_estimate": None,
                        "approval_required": False,
                    }
                )
        elif path == PREFIX + "/workspaces" and method == "POST":
            assert body["plan_revision"] == "a" * 64
            route.fulfill(
                status=201,
                json={
                    "id": WORKSPACE,
                    "name": "fixture-workspace",
                    "status": "Provisioning",
                    "provisioning_operation_id": "fixture-phase",
                },
            )
        elif path == PREFIX + "/operations/fixture-phase" and method == "GET":
            route.fulfill(
                json={
                    "request_id": next(
                        item["body"]["operation_id"]
                        for item in requests
                        if item["path"] == PREFIX + "/workspaces"
                    ),
                    "workspace_id": WORKSPACE,
                    "provisioning_operation_id": "fixture-phase",
                    "state": "running",
                    "phase": "provisioning",
                    "retryable": False,
                    "observed_at": "2026-10-05T00:00:00Z",
                }
            )
        elif (
            path == PREFIX + f"/workspaces/{WORKSPACE}/lifecycle-proposals"
            and method == "GET"
        ):
            route.fulfill(json={"workspace_id": WORKSPACE, "proposals": []})
        elif (
            path == PREFIX + "/operation-approvals/fixture-approval/decision"
            and method == "POST"
        ):
            assert body == {"result": "allowed-once"}
            route.fulfill(
                json={
                    "approval_id": "fixture-approval",
                    "workspace_id": WORKSPACE,
                    "action": "provision",
                    "result": "allowed-once",
                    "can_decide": True,
                    "expires_at": "2999-01-01T00:00:00Z",
                    "revoked": False,
                    "target": {
                        "account": "example-account",
                        "region": "example-region-1",
                    },
                    "plan_digest": "a" * 64,
                    "envelope": {
                        "max_resource_units": 2,
                        "max_runtime_seconds": 900,
                        "max_cost_micros": 1000000,
                    },
                }
            )
        elif (
            path == PREFIX + f"/workspaces/{WORKSPACE}/retirement/preview"
            and method == "POST"
        ):
            if failure:
                route.fulfill(
                    status=503, json={"detail": "fixture retirement unavailable"}
                )
            else:
                route.fulfill(
                    json={
                        "request_id": body["operation_id"],
                        "workspace_id": WORKSPACE,
                        "source_operation_id": "fixture-phase",
                        "source_payload_digest": "a" * 64,
                        "lifecycle_artifact_id": "fixture-artifact",
                        "account_id": "000000000000",
                        "region": "example-region-1",
                        "inventory_sha256": "b" * 64,
                        "lifecycle_policy_sha256": "c" * 64,
                        "runtime_config_sha256": "d" * 64,
                        "revision": "e" * 64,
                        "steps": [
                            {
                                "step_id": "step-1",
                                "provider": "superplane-kubernetes",
                                "operation_kind": "delete-namespace",
                                "target": "owned-namespace",
                            }
                        ],
                        "preserved": ["Operator-owned cluster survives"],
                        "admission_available": False,
                        "blocked_reason": "staged_cleanup_access_required",
                        "approval_request": None,
                    }
                )
        else:
            route.fulfill(status=500, json={"detail": "unexpected fixture route"})
            raise AssertionError(f"unexpected fixture route: {method} {path}")

    return transport


def exercise(page, requests, failure):
    page.route("**/*", fixture_transport(requests, failure))
    page.goto(ORIGIN + "/onboarding-browser-check.html")
    page.get_by_role("button", name="Begin onboarding").click()
    expect(page.get_by_role("heading", name="Create a workspace")).to_be_focused()
    name = page.get_by_label("Workspace name")
    name.fill("fixture-workspace")
    name.press("Tab")
    expect(page.get_by_label("Isolation mode")).to_be_focused()
    review = page.get_by_role("button", name="Review plan")
    review.focus()
    review.press("Enter")
    if failure:
        expect(
            page.get_by_role("group", name="Workspace submission problem")
        ).to_be_focused()
        assert not any(item["path"] == PREFIX + "/workspaces" for item in requests)
    else:
        expect(page.get_by_role("heading", name="Review this plan")).to_be_focused()
        assert page.get_by_text("not estimated by the server").is_visible()
        submit = page.get_by_role("button", name="Create this workspace")
        submit.focus()
        submit.press("Enter")
        expect(page.get_by_text("Accepted — still being provisioned")).to_be_visible()
        expect(page.get_by_text("No next plan is available yet.")).to_be_visible()
    decision = page.get_by_role("button", name="Approve this operation once")
    decision.focus()
    decision.press("Enter")
    expect(page.get_by_role("status").filter(has_text="allowed-once")).to_be_focused()
    removal = page.get_by_role("button", name="Review removal")
    removal.focus()
    removal.press("Enter")
    if failure:
        expect(page.get_by_role("group", name="Removal review problem")).to_be_focused()
    else:
        expect(page.get_by_role("group", name="Retirement review")).to_be_focused()
        expect(page.get_by_role("button", name="Remove workspace")).to_be_disabled()
    assert not any(item["path"].endswith("/retirement") for item in requests)
    creates = [
        item
        for item in requests
        if item["method"] == "POST" and item["path"] == PREFIX + "/workspaces"
    ]
    assert len(creates) == (0 if failure else 1)
    assert (
        len([item for item in requests if item["path"].endswith("/retirement/preview")])
        == 1
    )
    for width in (360, 1280):
        page.set_viewport_size({"width": width, "height": 800})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        page.screenshot(
            path=str(
                OUTPUT / f"onboarding-{'failure' if failure else 'success'}-{width}.png"
            ),
            full_page=True,
        )


def activate(page, button):
    expect(button).to_be_visible()
    expect(button).to_be_enabled()
    for _attempt in range(60):
        if button.evaluate("element => element === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(button).to_be_focused()
    page.keyboard.press("Enter")


def check_layout(page, scenario):
    for width in (360, 1280):
        page.set_viewport_size({"width": width, "height": 800})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        page.screenshot(path=str(OUTPUT / f"{scenario}-{width}.png"), full_page=True)
    page.set_viewport_size({"width": 360, "height": 800})


def prepare_continuation(page, resuming=False):
    activate(page, page.get_by_role("button", name="Review this lifecycle plan"))
    if not resuming:
        activate(
            page, page.get_by_role("button", name="Request approval for this phase")
        )
        activate(page, page.get_by_role("button", name="Approve this operation once"))
        expect(
            page.get_by_role("status").filter(has_text="allowed-once")
        ).to_be_focused()
    proceed = page.get_by_role("button", name="Continue approved phase")
    expect(proceed).to_be_enabled()
    return proceed


def assert_no_creation_or_removal(requests):
    assert not any(
        item["method"] == "DELETE"
        or item["path"]
        in (PREFIX + "/workspaces", PREFIX + f"/workspaces/{WORKSPACE}/retirement")
        for item in requests
    )


def exercise_continuation(page, requests, failure):
    page.route("**/*", fixture_transport(requests, failure, continuation=True))
    page.goto(ORIGIN + "/onboarding-browser-check.html?continuation")
    if failure:
        activate(page, page.get_by_role("button", name="Review this lifecycle plan"))
        expect(page.get_by_text("Lifecycle continuation unavailable")).to_be_visible()
        expect(
            page.get_by_role("button", name="Request approval for this phase")
        ).to_have_count(0)
        assert not any(item["path"] == PLAN + "/continue" for item in requests)
        check_layout(page, "continuation-refusal")
    else:
        proceed = prepare_continuation(page)
        check_layout(page, "continuation-approved")
        activate(page, proceed)
        expect(
            page.get_by_role("status").filter(has_text="Phase status: running")
        ).to_be_visible()
        admitted = [
            item["body"] for item in requests if item["path"] == PLAN + "/continue"
        ]
        assert len(admitted) == 1
        page.reload()
        recovered = page.get_by_role("article", name="Submitted lifecycle phase")
        expect(recovered.get_by_role("status")).to_contain_text("Phase status: running")
        expect(recovered).to_contain_text(admitted[0]["operation_id"])
        expect(recovered).to_contain_text(PHASE)
        assert (
            len([item for item in requests if item["path"] == PLAN + "/continue"]) == 1
        )
        check_layout(page, "continuation-recovered")
    assert_no_creation_or_removal(requests)


def exercise_expiry(page, requests, action):
    page.route(
        "**/*",
        fixture_transport(
            requests, False, continuation=action == "continuation", expire_action=action
        ),
    )
    entry = (
        ORIGIN
        + "/onboarding-browser-check.html"
        + ("?continuation" if action == "continuation" else "")
    )
    page.goto(entry)
    if action == "create":
        activate(page, page.get_by_role("button", name="Begin onboarding"))
        page.get_by_label("Workspace name").fill("fixture-workspace")
        target = page.get_by_role("button", name="Review plan")
    elif action == "continuation":
        target = prepare_continuation(page)
    else:
        target = page.get_by_role(
            "button",
            name="Approve this operation once"
            if action == "approval"
            else "Review removal",
        )
    check_layout(page, f"before-{action}-expiry")
    activate(page, target)
    expect(page).to_have_url(ORIGIN + "/login")
    expect(page.get_by_role("heading", name="Fixture sign-in required")).to_be_visible()
    assert page.evaluate("sessionStorage.getItem('cognito_access_token')") is None
    receipts = page.evaluate(
        "Object.entries(localStorage).filter(([key]) => key.startsWith('adp.superplane.onboarding.receipt.')).map(([, value]) => JSON.parse(value))"
    )
    attempts = [
        item["body"] for item in requests if item["path"] == EXPIRY_PATHS[action]
    ]
    assert len(attempts) == 1
    if action != "approval":
        assert len(receipts) == 1
        assert receipts[0]["idempotencyKey"] == attempts[0]["operation_id"]
    else:
        assert receipts == []
    assert "browser-fixture" not in json.dumps(receipts)
    if action == "continuation":
        assert receipts[0]["submissionStage"] == "submitted"
        assert receipts[0]["approvalId"] == APPROVAL
        page.goto(entry)
        activate(page, prepare_continuation(page, resuming=True))
        expect(
            page.get_by_role("status").filter(has_text="Phase status: running")
        ).to_be_visible()
        attempts = [
            item["body"] for item in requests if item["path"] == PLAN + "/continue"
        ]
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        check_layout(page, "continuation-after-sign-in")
    assert_no_creation_or_removal(requests)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    entry_path = FRONTEND / "onboarding-browser-entry.tsx"
    page_path = FRONTEND / "onboarding-browser-check.html"
    if entry_path.exists() or page_path.exists():
        raise RuntimeError("refusing to replace an existing browser entry")
    entry_path.write_text(Path(__file__).with_name("onboarding-entry.tsx").read_text())
    page_path.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body><div id="root"></div><script type="module" src="/onboarding-browser-entry.tsx"></script></body></html>'
    )
    process = None
    try:
        with (OUTPUT / "onboarding-vite.log").open("w") as log:
            process = subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4174",
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
                        ORIGIN + "/onboarding-browser-check.html", timeout=2
                    ) as response:
                        assert response.status == 200
                    break
                except (URLError, ConnectionResetError):
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "fixture frontend did not become available"
                        ) from None
                    time.sleep(0.2)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    scenarios = [
                        "success",
                        "refusal",
                        "continuation",
                        "continuation-refusal",
                    ] + [f"expired-{action}" for action in EXPIRY_PATHS]
                    for scenario in scenarios:
                        page = browser.new_page(viewport={"width": 360, "height": 800})
                        errors, requests = [], []
                        page.on(
                            "pageerror",
                            lambda error, failures=errors: failures.append(str(error)),
                        )
                        try:
                            if scenario.startswith("expired-"):
                                exercise_expiry(
                                    page, requests, scenario.removeprefix("expired-")
                                )
                            elif scenario.startswith("continuation"):
                                exercise_continuation(
                                    page, requests, scenario == "continuation-refusal"
                                )
                            else:
                                exercise(page, requests, scenario == "refusal")
                            assert not errors, errors
                        except BaseException:
                            page.screenshot(
                                path=str(OUTPUT / f"onboarding-{scenario}-error.png"),
                                full_page=True,
                            )
                            raise
                        finally:
                            page.close()
                    (OUTPUT / "onboarding-result.json").write_text(
                        json.dumps(
                            {
                                "evidence": "isolated fixture browser; not live acceptance",
                                "paths": scenarios,
                                "widths": [360, 1280],
                                "mutation_claim": "retirement admission never sent",
                            },
                            indent=2,
                        )
                    )
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
