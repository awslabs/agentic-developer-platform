"""Real product buttons; all service responses synthetic and no provider access."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import urlopen

from superplane_acceptance.demo1_browser import PREFIX, PlaywrightBrowserTransport
from superplane_acceptance.demo1_controls import (
    create_workspace,
    recover_removal,
    remove_workspace,
    restore_receipt,
)
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_session import restore_browser_session

ORG = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
REQUEST = "44444444-4444-4444-8444-444444444444"
APPROVAL = "55555555-5555-4555-8555-555555555555"
REMOVAL = "66666666-6666-4666-8666-666666666666"
REMOVAL_APPROVAL = "77777777-7777-4777-8777-777777777777"
REMOVAL_OPERATION = "88888888-8888-4888-8888-888888888888"
PEER = "99999999-9999-4999-8999-999999999999"


def run(browser, state, session, origin, local, release):
    from playwright.sync_api import expect

    calls, sent, errors = [], [], []
    selected = SimpleNamespace(
        org_id=ORG,
        requester_id="fixture-requester",
        workspace_name="demo1-fixture",
        authorized_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    control = {"created": False, "removed": False, "capable": True}
    workspace = {
        "id": WORKSPACE,
        "org_id": ORG,
        "name": selected.workspace_name,
        "display_name": selected.workspace_name,
        "status": "Active",
        "isolation_mode": "dedicated",
        "provisioning_operation_id": OPERATION,
        "cluster_health": "Healthy",
        "last_heartbeat": datetime.now(UTC).isoformat(),
        "is_default": False,
    }
    peer = {
        **workspace,
        "id": PEER,
        "name": "preserved-peer",
        "display_name": "Preserved peer",
        "is_default": True,
    }
    revision = "b" * 64
    removal_revision = "c" * 64
    approval_request = {
        "workspace_id": WORKSPACE,
        "action": "provision",
        "idempotency_key": REQUEST,
        "parameters": {"plan_revision": revision},
    }
    removal_request = {
        "workspace_id": WORKSPACE,
        "action": "teardown",
        "idempotency_key": REMOVAL,
        "parameters": {"plan_revision": "d" * 64},
    }
    review = {
        "request_id": REMOVAL,
        "workspace_id": WORKSPACE,
        "source_operation_id": OPERATION,
        "source_payload_digest": "a" * 64,
        "lifecycle_artifact_id": "a" * 64,
        "account_id": "123456789012",
        "region": "us-east-1",
        "inventory_sha256": "a" * 64,
        "lifecycle_policy_sha256": "a" * 64,
        "runtime_config_sha256": "a" * 64,
        "revision": removal_revision,
        "admission_available": True,
        "blocked_reason": None,
        "steps": [
            {
                "step_id": "remove",
                "provider": "aws",
                "operation_kind": "delete-cluster",
                "target": "owned-cluster",
            }
        ],
        "preserved": ["Preserved peer"],
        "approval_request": removal_request,
    }

    def serve(route):
        request = route.request
        parsed = urlsplit(request.url)
        assert f"{parsed.scheme}://{parsed.netloc}" == origin
        if not parsed.path.startswith("/api/"):
            path = (
                "/demo1-browser-check.html"
                if parsed.path == "/superplane"
                else parsed.path
            )
            with urlopen(
                local + path + ("?" + parsed.query if parsed.query else ""), timeout=10
            ) as response:
                route.fulfill(
                    status=response.status,
                    content_type=response.headers.get("Content-Type", "text/plain"),
                    body=response.read(),
                )
            return
        assert (
            request.headers.get("authorization")
            == "Bearer " + session["cognito_access_token"]
        )
        body = request.post_data_json if request.method == "POST" else None
        calls.append((request.method, parsed.path, body))
        headers = {"X-Superplane-Release": release}
        if parsed.path == "/api/auth/me":
            value = {"user_id": selected.requester_id, "org_id": ORG}
        elif parsed.path.endswith("/capabilities"):
            value = {
                "features": ["create-operation-id-v1"] if control["capable"] else [],
                "modes": ["managed"],
                "providers": ["aws"],
                "isolation_modes": ["dedicated"],
            }
        elif parsed.path == PREFIX + "/workspaces" and request.method == "GET":
            value = {"workspaces": [peer] + ([workspace] if control["created"] else [])}
        elif parsed.path == PREFIX + "/workspaces/preview":
            assert body["operation_id"] == REQUEST
            value = {
                "request_id": REQUEST,
                "workspace_id": WORKSPACE,
                "revision": revision,
                "mode": "managed",
                "target": {"account": "123456789012", "region": "us-east-1"},
                "approval_required": True,
                "approval_request": approval_request,
            }
        elif parsed.path.startswith(PREFIX + "/operation-approvals/"):
            removal = parsed.path.endswith(REMOVAL_APPROVAL)
            value = {
                "approval_id": REMOVAL_APPROVAL if removal else APPROVAL,
                "workspace_id": WORKSPACE,
                "request": removal_request if removal else approval_request,
                "plan_digest": removal_revision if removal else revision,
                "result": "allowed-once",
                "revoked": False,
                "can_decide": False,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            }
        elif parsed.path == PREFIX + "/workspaces" and request.method == "POST":
            assert sent == ["create"], "checkpoint must precede transmission"
            control["created"] = True
            value = workspace
        elif parsed.path == PREFIX + f"/workspaces/{WORKSPACE}/retirement/preview":
            assert body == {"operation_id": REMOVAL}
            value = review
        elif parsed.path == PREFIX + f"/workspaces/{WORKSPACE}/retirement":
            assert sent == ["create", "remove"], "checkpoint must precede removal"
            control["removed"] = True
            value = {
                "request_id": REMOVAL,
                "workspace_id": WORKSPACE,
                "operation_id": REMOVAL_OPERATION,
                "state": "pending",
                "phase": "retire-workspace",
                "retryable": False,
                "retirement_complete": False,
            }
        elif parsed.path == PREFIX + f"/workspaces/{WORKSPACE}":
            value = workspace
        elif parsed.path == PREFIX + "/operations/by-idempotency/" + REMOVAL:
            value = {
                "provisioning_operation_id": REMOVAL_OPERATION,
                "request_id": REMOVAL,
                "workspace_id": WORKSPACE,
                "state": "succeeded",
                "phase": "execution",
                "observed_at": datetime.now(UTC).isoformat(),
                "retryable": False,
            }
        elif "/operations/" in parsed.path:
            value = {
                "provisioning_operation_id": OPERATION,
                "request_id": REQUEST,
                "workspace_id": WORKSPACE,
                "state": "succeeded",
                "phase": "execution",
                "observed_at": datetime.now(UTC).isoformat(),
                "retryable": False,
            }
        elif parsed.path.endswith("/lifecycle-proposals"):
            value = {"workspace_id": WORKSPACE, "proposals": []}
        else:
            route.fulfill(
                status=503,
                json={"detail": "synthetic capability unavailable"},
                headers=headers,
            )
            return
        route.fulfill(json=value, headers=headers)

    context = browser.new_context(storage_state=state, service_workers="block")
    context.route("**/*", serve)
    context.route_web_socket("**/*", lambda socket: None)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_default_timeout(10000)
    restore_browser_session(page, origin, session, timeout=10000)
    page.goto(origin + "/superplane", wait_until="networkidle")
    expect(
        page.get_by_role("button", name="Create a workspace", exact=True)
    ).to_be_enabled()
    expect(
        page.get_by_role("button", name="Preserved peer", exact=False)
    ).to_be_visible()
    control["capable"] = False
    page.reload(wait_until="networkidle")
    expect(
        page.get_by_role("button", name="Create a workspace", exact=True)
    ).to_be_disabled()
    control["capable"] = True
    page.reload(wait_until="networkidle")
    client = PlaywrightBrowserTransport(
        page, origin, release_id=release, selected=selected
    )
    client.creation_workspace_id = WORKSPACE
    body = {
        "operation_id": REQUEST,
        "mode": "managed",
        "name": selected.workspace_name,
        "isolation_mode": "dedicated",
        "cluster_placement": "dedicated",
        "account": "123456789012",
        "region": "us-east-1",
        "budget_max_daily_usd": "10",
        "plan_revision": revision,
        "approval_id": APPROVAL,
    }

    def durable(kind):
        # The real gate refreshes approval through the same browser while the
        # outgoing mutation is held. Exercise nested authenticated reads here.
        status, identity = client.request("GET", "/api/auth/me")
        assert status == 200 and identity["user_id"] == selected.requester_id
        sent.append(kind)

    status, result = create_workspace(client, body, lambda: durable("create"))
    assert status == 200 and result["id"] == WORKSPACE
    # A submitted or foreign receipt cannot be reset to another approval/identity.
    try:
        restore_receipt(
            client,
            request_id=REQUEST,
            workspace_id=WORKSPACE,
            approval_id=REMOVAL_APPROVAL,
            intent="create-workspace",
            payload={},
        )
    except EvidenceError:
        pass
    else:
        raise AssertionError("submitted browser receipt was replaced")
    saved = SimpleNamespace(
        request_id=REMOVAL,
        workspace_id=WORKSPACE,
        approval_id=REMOVAL_APPROVAL,
        source_operation_id=OPERATION,
        revision=removal_revision,
        submitted=False,
    )
    status, result = remove_workspace(client, saved, lambda: durable("remove"))
    assert status == 200 and result["operation_id"] == REMOVAL_OPERATION
    saved.submitted = True
    saved.operation_id = REMOVAL_OPERATION
    page.evaluate("localStorage.clear()")
    recovered = recover_removal(client, saved)
    assert recovered["state"] == "succeeded" and recovered["request_id"] == REMOVAL
    assert (
        sum(
            method == "POST" and path == PREFIX + "/workspaces"
            for method, path, _ in calls
        )
        == 1
    )
    assert (
        sum(
            method == "POST" and path.endswith("/retirement")
            for method, path, _ in calls
        )
        == 1
    )
    assert not any(path.endswith("/decision") for _, path, _ in calls)
    assert control["created"] and control["removed"] and errors == []
    context.close()
    return {
        "actual_create_control": "passed",
        "actual_removal_control": "passed",
        "actual_original_removal_recovery_after_storage_loss": "passed",
        "peer_preserved_in_ui": "passed",
        "receipt_mutation_refused": "passed",
        "no_capability_refused": "passed",
        "live_acceptance": False,
    }
