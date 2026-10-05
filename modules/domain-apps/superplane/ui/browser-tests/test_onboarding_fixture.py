"""Exercise the browser fixture transport without a browser or provider access."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import verify_onboarding_browser as browser_checks
from verify_onboarding_browser import (
    APPROVAL,
    EXPIRY_PATHS,
    ORIGIN,
    PHASE,
    PLAN,
    PREFIX,
    PROPOSALS,
    WORKSPACE,
    fixture_transport,
)


@pytest.fixture
def browser_runtime_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


def test_fixture_module_imports_without_browser_runtime(browser_runtime_unavailable):
    specification = importlib.util.spec_from_file_location(
        "onboarding_fixture_without_browser",
        Path(__file__).with_name("verify_onboarding_browser.py"),
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    route = fixture_route(PROPOSALS)

    module.fixture_transport([], False)(route)

    route.fulfill.assert_called_once_with(
        json={"workspace_id": WORKSPACE, "proposals": []}
    )


def test_browser_runner_requires_runtime_before_effects(
    browser_runtime_unavailable, monkeypatch
):
    output = Mock()
    output.mkdir.side_effect = AssertionError("browser effects before dependency check")
    monkeypatch.setattr(browser_checks, "OUTPUT", output)

    with pytest.raises(ModuleNotFoundError, match="playwright"):
        browser_checks.main()

    output.mkdir.assert_not_called()


def fixture_route(path, method="GET", body=None, origin=ORIGIN):
    return Mock(
        request=SimpleNamespace(url=origin + path, method=method, post_data_json=body)
    )


@pytest.mark.parametrize("failure", [False, True])
def test_lifecycle_proposals_are_readable_after_creation(failure):
    requests = []
    path = PREFIX + f"/workspaces/{WORKSPACE}/lifecycle-proposals"
    route = fixture_route(path)

    fixture_transport(requests, failure)(route)

    route.fulfill.assert_called_once_with(
        json={"workspace_id": WORKSPACE, "proposals": []}
    )
    route.continue_.assert_not_called()
    assert requests == [{"method": "GET", "path": path, "body": None}]


def test_creation_and_observation_preserve_the_request_identity():
    requests = []
    transport = fixture_transport(requests, False)
    preview = fixture_route(PREFIX + "/workspaces/preview", "POST", {})
    transport(preview)
    revision = preview.fulfill.call_args.kwargs["json"]["revision"]
    body = {"plan_revision": revision, "operation_id": "fixture-original-request"}
    create = fixture_route(PREFIX + "/workspaces", "POST", body)
    transport(create)
    workspace = create.fulfill.call_args.kwargs["json"]
    observation = fixture_route(
        PREFIX + "/operations/" + workspace["provisioning_operation_id"]
    )
    transport(observation)

    assert create.fulfill.call_args.kwargs["status"] == 201
    operation = observation.fulfill.call_args.kwargs["json"]
    assert operation["request_id"] == body["operation_id"]
    assert operation["workspace_id"] == workspace["id"] == WORKSPACE
    assert operation["state"] == "running"


@pytest.mark.parametrize(
    "path",
    ["/workspaces/preview", f"/workspaces/{WORKSPACE}/retirement/preview"],
)
def test_refused_previews_return_service_unavailability(path):
    route = fixture_route(PREFIX + path, "POST", {"operation_id": "fixture-request"})

    fixture_transport([], True)(route)

    assert route.fulfill.call_args.kwargs["status"] == 503
    route.continue_.assert_not_called()


def test_retirement_inventory_never_authorizes_admission():
    route = fixture_route(
        PREFIX + f"/workspaces/{WORKSPACE}/retirement/preview",
        "POST",
        {"operation_id": "fixture-retirement-request"},
    )

    fixture_transport([], False)(route)

    review = route.fulfill.call_args.kwargs["json"]
    assert review["request_id"] == "fixture-retirement-request"
    assert review["workspace_id"] == WORKSPACE
    assert review["admission_available"] is False
    assert review["approval_request"] is None
    assert review["blocked_reason"] == "staged_cleanup_access_required"
    assert review["preserved"] == ["Operator-owned cluster survives"]


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/workspaces/{WORKSPACE}/retirement"),
        ("DELETE", f"/workspaces/{WORKSPACE}"),
        ("POST", f"/workspaces/{WORKSPACE}/lifecycle-proposals"),
        ("GET", "/workspaces/other-workspace/lifecycle-proposals"),
        ("GET", "/unexpected"),
    ],
)
def test_unknown_and_destructive_requests_fail_closed(
    continuation, failure, method, path
):
    route = fixture_route(PREFIX + path, method, {})

    with pytest.raises(AssertionError, match="unexpected fixture route"):
        fixture_transport([], failure, continuation=continuation)(route)

    route.fulfill.assert_called_once_with(
        status=500, json={"detail": "unexpected fixture route"}
    )
    route.continue_.assert_not_called()


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("path", [PREFIX + "/workspaces", "/asset.js", "/login"])
def test_non_fixture_origins_are_aborted_before_recording_or_forwarding(
    continuation, path
):
    requests = []
    route = fixture_route(path, origin="https://external.invalid")

    with pytest.raises(AssertionError, match="unexpected non-fixture origin"):
        fixture_transport(requests, False, continuation=continuation)(route)

    route.abort.assert_called_once_with()
    route.fulfill.assert_not_called()
    route.continue_.assert_not_called()
    assert requests == []


def exchange(transport, path, method="GET", body=None):
    route = fixture_route(path, method, body)
    transport(route)
    route.fulfill.assert_called_once()
    route.continue_.assert_not_called()
    return route.fulfill.call_args.kwargs


def approve_phase(transport):
    review = exchange(
        transport, PLAN + "/preview", "POST", {"operation_id": "fixture-request"}
    )["json"]
    approval = exchange(
        transport, PREFIX + "/operation-approvals", "POST", review["approval_request"]
    )["json"]
    assert approval["result"] == "pending"
    assert approval["plan_digest"] == review["revision"]
    decided = exchange(
        transport,
        PREFIX + f"/operation-approvals/{APPROVAL}/decision",
        "POST",
        {"result": "allowed-once"},
    )["json"]
    assert decided["result"] == "allowed-once"
    return {"operation_id": review["request_id"], "approval_id": decided["approval_id"]}


def test_continuation_preserves_plan_approval_and_request_identity_through_recovery():
    transport = fixture_transport([], False, continuation=True)
    proposal = exchange(transport, PROPOSALS)["json"]["proposals"][0]
    review = exchange(
        transport, PLAN + "/preview", "POST", {"operation_id": "fixture-request"}
    )["json"]
    assert review["source_operation_id"] == proposal["source_operation_id"]
    assert (
        review["approval_request"]["parameters"]["terraform_plan_sha256"]
        == proposal["plan_json_sha256"]
    )
    assert (
        exchange(transport, PREFIX + "/operations/by-idempotency/fixture-request")[
            "status"
        ]
        == 404
    )
    body = approve_phase(transport)
    approval = exchange(transport, PREFIX + f"/operation-approvals/{APPROVAL}")["json"]
    assert approval["result"] == "allowed-once"
    operation = exchange(transport, PLAN + "/continue", "POST", body)["json"]
    assert operation["request_id"] == body["operation_id"]
    assert operation["workspace_id"] == WORKSPACE
    assert operation["state"] == "running"
    assert exchange(transport, PROPOSALS)["json"]["proposals"] == []
    assert exchange(transport, PREFIX + f"/operations/{PHASE}")["json"] == operation
    assert (
        exchange(transport, PREFIX + "/operations/by-idempotency/fixture-request")[
            "json"
        ]
        == operation
    )
    assert exchange(transport, PLAN + "/continue", "POST", body)["json"] == operation


def test_continuation_refusal_keeps_the_proposal_readable_without_an_approval():
    transport = fixture_transport([], True, continuation=True)
    assert exchange(transport, PROPOSALS)["json"]["proposals"]
    assert (
        exchange(
            transport, PLAN + "/preview", "POST", {"operation_id": "fixture-request"}
        )["status"]
        == 503
    )
    with pytest.raises(AssertionError):
        exchange(
            transport,
            PLAN + "/continue",
            "POST",
            {"operation_id": "fixture-request", "approval_id": APPROVAL},
        )


@pytest.mark.parametrize("substitution", ["request", "approval"])
def test_continuation_rejects_substituted_identity(substitution):
    transport = fixture_transport([], False, continuation=True)
    body = approve_phase(transport)
    body["operation_id" if substitution == "request" else "approval_id"] = "substituted"
    with pytest.raises(AssertionError):
        exchange(transport, PLAN + "/continue", "POST", body)


def test_continuation_cannot_skip_the_approval_decision():
    transport = fixture_transport([], False, continuation=True)
    review = exchange(
        transport, PLAN + "/preview", "POST", {"operation_id": "fixture-request"}
    )["json"]
    exchange(
        transport, PREFIX + "/operation-approvals", "POST", review["approval_request"]
    )
    with pytest.raises(AssertionError):
        exchange(
            transport,
            PLAN + "/continue",
            "POST",
            {"operation_id": "fixture-request", "approval_id": APPROVAL},
        )


@pytest.mark.parametrize("action", EXPIRY_PATHS)
def test_expiry_returns_one_401_and_preserves_the_same_request_on_reentry(action):
    requests = []
    transport = fixture_transport(
        requests, False, continuation=action == "continuation", expire_action=action
    )
    if action == "continuation":
        body = approve_phase(transport)
    else:
        body = (
            {"result": "allowed-once"}
            if action == "approval"
            else {"operation_id": "fixture-request"}
        )
    response = exchange(transport, EXPIRY_PATHS[action], "POST", body)
    assert response["status"] == 401
    assert exchange(transport, "/login")["body"] == "<h1>Fixture sign-in required</h1>"
    response = exchange(transport, EXPIRY_PATHS[action], "POST", body)
    assert response.get("status", 200) == 200
    if action == "continuation":
        assert response["json"]["request_id"] == body["operation_id"]
    attempts = [
        item["body"] for item in requests if item["path"] == EXPIRY_PATHS[action]
    ]
    assert attempts == [body, body]


def test_local_frontend_assets_are_not_api_fixtures():
    requests = []
    route = fixture_route("/onboarding-browser-entry.tsx")

    fixture_transport(requests, False)(route)

    route.continue_.assert_called_once_with()
    route.fulfill.assert_not_called()
    assert requests == []
