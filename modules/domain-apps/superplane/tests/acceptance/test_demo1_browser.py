"""Source-bound browser journey tests with no live requests."""

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_demo1_cli import fixture_documents

from superplane_acceptance import demo1_browser
from superplane_acceptance.demo1_browser import advance_creation
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError


def identity(number):
    return str(UUID(int=number))


class Transport:
    origin = "https://example.invalid"

    def __init__(self, selected):
        self.selected = selected
        self.calls = []
        self.approved = False
        self.lost = False

    def browser_page(self):
        return SimpleNamespace(
            get_by_role=lambda *args, **kwargs: SimpleNamespace(is_visible=lambda: True)
        )

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/auth/me":
            return 200, {
                "user_id": self.selected.requester_id,
                "org_id": self.selected.org_id,
            }
        if path.endswith("/capabilities"):
            return 200, {"features": ["create-operation-id-v1"]}
        if path.endswith("/workspaces/preview"):
            return 200, {
                "request_id": self.selected.request_id,
                "revision": self.selected.plan_revision,
                "workspace_id": identity(10),
                "mode": "managed",
                "target": {
                    "account": self.selected.account,
                    "region": self.selected.region,
                },
                "approval_request": {
                    "workspace_id": identity(10),
                    "idempotency_key": self.selected.request_id,
                    "action": "provision",
                    "parameters": {"plan_revision": self.selected.plan_revision},
                },
            }
        if path.endswith(("/operation-approvals", "/" + identity(11))):
            return 200, {
                "approval_id": identity(11),
                "workspace_id": identity(10),
                "requester": self.selected.requester_id,
                "approvers": [self.selected.approver_id],
                "request": {
                    "idempotency_key": self.selected.request_id,
                    "action": "provision",
                    "parameters": {"plan_revision": self.selected.plan_revision},
                },
                "expires_at": "2026-10-05T11:59:00+00:00",
                "result": "allowed-once" if self.approved else "pending",
                "decided_by": self.selected.approver_id if self.approved else None,
                "decided_at": "2026-10-05T11:01:00+00:00" if self.approved else None,
                "revoked": False,
            }
        if path.endswith("/workspaces") and method == "POST":
            if self.lost:
                raise ConnectionError("private lost response")
            return 201, {"id": identity(10), "org_id": self.selected.org_id}
        if path.endswith("/operations/by-idempotency/" + self.selected.request_id):
            return 200, {
                "request_id": self.selected.request_id,
                "provisioning_operation_id": identity(12),
                "workspace_id": identity(10),
                "state": "pending",
                "phase": "execution",
                "reason": None,
                "observed_at": "2026-10-05T11:02:00+00:00",
                "retryable": False,
            }
        if path.endswith("/workspaces/" + identity(10)) and method == "GET":
            return 200, {
                "id": identity(10),
                "org_id": self.selected.org_id,
                "name": self.selected.workspace_name,
                "provisioning_operation_id": identity(12),
            }
        if path.endswith("/retirement/preview"):
            return 503, {"detail": "unavailable"}
        raise AssertionError("unexpected request")


@pytest.fixture
def selected():
    return DemoInput.parse(fixture_documents()[0])


def advance(selected, transport, checkpoint=None):
    saved = []
    return advance_creation(
        selected,
        transport,
        origin="https://example.invalid",
        checkpoint=checkpoint,
        persist=saved.append,
        effects_authorized=True,
        now=datetime(2026, 10, 5, 11, 2, tzinfo=UTC),
    )


def test_real_routes_wait_for_approver_and_recover_without_repeating_post(
    selected, monkeypatch
):
    transport = Transport(selected)
    checkpoint, result = advance(selected, transport)
    assert result["reason"] == "awaiting independent human approval"
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)
    assert advance(selected, transport, checkpoint)[1]["status"] == "BLOCKED"
    transport.approved = True
    transport.lost = True
    checkpoint, result = advance(selected, transport, checkpoint)
    assert checkpoint.submitted and "uncertain" in result["reason"]
    transport.lost = False
    monkeypatch.setattr(
        demo1_browser,
        "inspect_reentry",
        lambda *args: {
            "reason": "read-only re-entry visible; sign-in, authority and Ready not independently proved"
        },
    )
    monkeypatch.setattr(
        demo1_browser,
        "inspect_original_details",
        lambda *args: {
            "reason": "original identities visible; session and provider authority unverified"
        },
    )
    checkpoint, result = advance(selected, transport, checkpoint)
    assert result["status"] == "BLOCKED" and result["readiness"] == "UNKNOWN"
    assert (
        len([call for call in transport.calls if call[1].endswith("/workspaces")]) == 1
    )
    assert not any(call[1].endswith("/retirement") for call in transport.calls)


@pytest.fixture
def uncertain_creation(selected):
    transport = Transport(selected)
    checkpoint, _ = advance(selected, transport)
    transport.approved = True
    transport.lost = True
    checkpoint, report = advance(selected, transport, checkpoint)
    assert checkpoint.submitted and "uncertain" in report["reason"]
    transport.lost = False
    transport.calls.clear()
    return transport, checkpoint


def replace_response(monkeypatch, transport, suffix, changes, missing=()):
    original = transport.request

    def request(method, path, body=None):
        status, response = original(method, path, body)
        if path.endswith(suffix):
            response = {**response, **changes}
            for field in missing:
                response.pop(field, None)
        return status, response

    monkeypatch.setattr(transport, "request", request)


@pytest.mark.parametrize("phase", ["issued", "resumed"])
@pytest.mark.parametrize(
    "invalid",
    [
        "foreign_plan",
        "missing_plan",
        "missing_parameters",
        "null_parameters",
        "list_parameters",
        "nonstring_parameter",
        "null_approvers",
        "string_approvers",
        "mapping_approvers",
        "mixed_approvers",
    ],
)
def test_approval_rejects_foreign_plan_and_malformed_scope_before_creation(
    selected, monkeypatch, phase, invalid
):
    transport = Transport(selected)
    checkpoint = None
    suffix = "/operation-approvals"
    if phase == "resumed":
        checkpoint, _ = advance(selected, transport)
        transport.approved = True
        transport.calls.clear()
        suffix = "/" + checkpoint.approval_id
    request = {
        "idempotency_key": selected.request_id,
        "action": "provision",
        "parameters": {"plan_revision": selected.plan_revision},
    }
    changes = {"request": request}
    if invalid == "foreign_plan":
        request["parameters"]["plan_revision"] = "d" * 64
    elif invalid == "missing_plan":
        request["parameters"] = {}
    elif invalid == "missing_parameters":
        del request["parameters"]
    elif invalid == "null_parameters":
        request["parameters"] = None
    elif invalid == "list_parameters":
        request["parameters"] = []
    elif invalid == "nonstring_parameter":
        request["parameters"]["max_runtime_seconds"] = 900
    elif invalid == "null_approvers":
        changes["approvers"] = None
    elif invalid == "string_approvers":
        changes["approvers"] = selected.approver_id
    elif invalid == "mapping_approvers":
        changes["approvers"] = {selected.approver_id: True}
    elif invalid == "mixed_approvers":
        changes["approvers"] = [selected.approver_id, None]
    replace_response(monkeypatch, transport, suffix, changes)
    saved = []
    with pytest.raises(EvidenceError, match="approval identity or scope mismatch"):
        advance_creation(
            selected,
            transport,
            origin=transport.origin,
            checkpoint=checkpoint,
            persist=saved.append,
            effects_authorized=True,
            now=datetime(2026, 10, 5, 11, 2, tzinfo=UTC),
        )
    assert saved == []
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


@pytest.mark.parametrize("parameters", [None, [], "unverified"])
def test_preview_rejects_malformed_approval_parameters(
    selected, monkeypatch, parameters
):
    transport = Transport(selected)
    replace_response(
        monkeypatch,
        transport,
        "/workspaces/preview",
        {
            "approval_request": {
                "workspace_id": identity(10),
                "idempotency_key": selected.request_id,
                "action": "provision",
                "parameters": parameters,
            }
        },
    )
    with pytest.raises(EvidenceError, match="reviewed plan or selected target differs"):
        advance(selected, transport)
    assert not any(
        path.endswith("/operation-approvals") for _, path, _ in transport.calls
    )
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"request_id": identity(99)}, ()),
        ({}, ("request_id",)),
        ({"workspace_id": identity(99)}, ()),
        ({}, ("workspace_id",)),
        ({"workspace_id": None}, ()),
        ({"provisioning_operation_id": "unverified"}, ()),
        ({}, ("provisioning_operation_id",)),
    ],
)
def test_recovery_rejects_foreign_or_incomplete_operation_without_effects(
    selected, uncertain_creation, monkeypatch, changes, missing
):
    transport, checkpoint = uncertain_creation
    replace_response(
        monkeypatch,
        transport,
        "/operations/by-idempotency/" + selected.request_id,
        changes,
        missing,
    )
    with pytest.raises(EvidenceError):
        advance(selected, transport, checkpoint)
    assert all(method == "GET" for method, _, _ in transport.calls)
    assert not any(
        path.endswith("/workspaces/" + identity(10)) for _, path, _ in transport.calls
    )


def test_recovery_preserves_pending_workspace_registration(
    selected, uncertain_creation, monkeypatch
):
    transport, checkpoint = uncertain_creation
    replace_response(
        monkeypatch,
        transport,
        "/operations/by-idempotency/" + selected.request_id,
        {"workspace_id": None, "phase": "workspace_registration"},
    )
    recovered, report = advance(selected, transport, checkpoint)
    assert recovered == checkpoint
    assert report == {
        "status": "BLOCKED",
        "reason": "original operation found; workspace registration incomplete",
    }
    assert all(method == "GET" for method, _, _ in transport.calls)
    assert not any(
        path.endswith("/workspaces/" + identity(10)) for _, path, _ in transport.calls
    )


@pytest.mark.parametrize("operation_id", [identity(99), None])
def test_reentry_requires_the_recovered_operation_identity(
    selected, uncertain_creation, monkeypatch, operation_id
):
    transport, checkpoint = uncertain_creation
    replace_response(
        monkeypatch,
        transport,
        "/workspaces/" + identity(10),
        {"provisioning_operation_id": operation_id},
    )
    with pytest.raises(EvidenceError, match="workspace re-entry differs"):
        advance(selected, transport, checkpoint)
    assert all(method == "GET" for method, _, _ in transport.calls)


@pytest.mark.parametrize(
    "invalid", ["origin", "identity", "plan", "approver", "expired"]
)
def test_mismatched_authority_refuses_before_create(selected, invalid):
    transport = Transport(selected)
    if invalid == "origin":
        transport.origin = "https://another.invalid"
    if invalid == "identity":
        transport.selected = SimpleNamespace(
            **{**vars(selected), "requester_id": identity(99)}
        )
    if invalid == "plan":
        transport.selected = SimpleNamespace(
            **{**vars(selected), "plan_revision": "d" * 64}
        )
    if invalid == "approver":
        transport.selected = SimpleNamespace(
            **{**vars(selected), "approver_id": identity(99)}
        )
    if invalid == "expired":
        selected = SimpleNamespace(
            **{
                **vars(selected),
                "authorized_at": datetime(2026, 10, 5, 12, tzinfo=UTC),
            }
        )
    with pytest.raises(EvidenceError):
        advance(selected, transport)
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


def test_replayed_checkpoint_cannot_submit(selected):
    transport = Transport(selected)
    checkpoint, _ = advance(selected, transport)
    replay = demo1_browser.CreationCheckpoint(
        **{**vars(checkpoint), "request_id": identity(99)}
    )
    with pytest.raises(EvidenceError):
        advance(selected, transport, replay)
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.invalid",
        "https://example.invalid/path",
        "https://user:pass@example.invalid",
    ],
)
def test_invalid_origin_refuses_without_transport(origin):
    with pytest.raises(EvidenceError):
        demo1_browser.checked_origin(origin)


def test_live_effects_are_disabled_without_explicit_authorization(selected):
    transport = Transport(selected)
    with pytest.raises(EvidenceError, match="explicit live authorization"):
        advance_creation(
            selected,
            transport,
            origin=transport.origin,
            now=datetime(2026, 10, 5, 11, 2, tzinfo=UTC),
        )
    assert transport.calls == []


def test_requires_durable_checkpoint_writer_before_effect(selected):
    transport = Transport(selected)
    with pytest.raises(EvidenceError, match="checkpoint writer"):
        advance_creation(
            selected,
            transport,
            origin=transport.origin,
            effects_authorized=True,
            now=datetime(2026, 10, 5, 11, 2, tzinfo=UTC),
        )
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


def test_workspace_reading_requires_fresh_healthy_registration():
    now = datetime(2026, 10, 5, 11, 2, tzinfo=UTC)
    reading = {
        "status": "Ready",
        "cluster_health": "Healthy",
        "last_heartbeat": "2026-10-05T11:01:00+00:00",
    }
    assert demo1_browser.workspace_reading(reading, now) == "FRESH_WORKSPACE_ONLY"
    for change in (
        {"cluster_health": "Degraded"},
        {"last_heartbeat": "2026-10-05T10:50:00+00:00"},
        {"status": "Provisioning"},
        {"last_heartbeat": None},
    ):
        assert demo1_browser.workspace_reading({**reading, **change}, now) == "UNKNOWN"


def test_saved_approval_can_expire_before_creation(selected):
    transport = Transport(selected)
    checkpoint, _ = advance(selected, transport)
    transport.approved = True
    with pytest.raises(EvidenceError, match="approval expired"):
        advance_creation(
            selected,
            transport,
            origin=transport.origin,
            checkpoint=checkpoint,
            effects_authorized=True,
            persist=lambda state: None,
            now=datetime(2026, 10, 5, 11, 59, tzinfo=UTC),
        )
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize("ticket_state", ["expired", "revoked", "unavailable"])
def test_submitted_operation_resumes_after_approval_expiry_without_recreation(
    selected, monkeypatch, lost_response, ticket_state
):
    transport = Transport(selected)
    monkeypatch.setattr(
        demo1_browser,
        "inspect_reentry",
        lambda *args: {
            "reason": "read-only re-entry visible; sign-in, authority and Ready not independently proved"
        },
    )
    monkeypatch.setattr(
        demo1_browser,
        "inspect_original_details",
        lambda *args: {
            "reason": "original identities visible; session and provider authority unverified"
        },
    )
    checkpoint, _ = advance(selected, transport)
    transport.approved = True
    transport.lost = lost_response
    saved = []
    checkpoint, report = advance_creation(
        selected,
        transport,
        origin=transport.origin,
        checkpoint=checkpoint,
        persist=saved.append,
        effects_authorized=True,
        now=datetime(2026, 10, 5, 11, 2, tzinfo=UTC),
    )
    assert checkpoint.submitted and saved == [checkpoint]
    assert report["status"] == "BLOCKED"
    assert sum(path.endswith("/workspaces") for _, path, _ in transport.calls) == 1
    transport.lost = False
    transport.calls.clear()
    original = transport.request

    def request(method, path, body=None):
        status, response = original(method, path, body)
        if path.endswith("/operation-approvals/" + checkpoint.approval_id):
            if ticket_state == "unavailable":
                return 503, None
            if ticket_state == "revoked":
                response["revoked"] = True
        return status, response

    monkeypatch.setattr(transport, "request", request)
    for _attempt in range(2):
        resumed, report = advance_creation(
            selected,
            transport,
            origin=transport.origin,
            checkpoint=checkpoint,
            effects_authorized=True,
            now=datetime(2026, 10, 5, 11, 59, 30, tzinfo=UTC),
        )
        assert resumed == checkpoint
        assert report["status"] == "BLOCKED" and report["creation_observed"] is True
    assert (
        sum(
            path.endswith("/operations/by-idempotency/" + selected.request_id)
            for _, path, _ in transport.calls
        )
        == 2
    )
    assert (
        sum(
            path.endswith("/workspaces/" + checkpoint.workspace_id)
            for _, path, _ in transport.calls
        )
        == 2
    )
    assert not any("/operation-approvals/" in path for _, path, _ in transport.calls)
    assert not any(path.endswith("/workspaces") for _, path, _ in transport.calls)


@pytest.mark.parametrize("field", ["user_id", "org_id"])
def test_submitted_recovery_still_requires_selected_authenticated_principal(
    selected, uncertain_creation, monkeypatch, field
):
    transport, checkpoint = uncertain_creation
    replace_response(monkeypatch, transport, "/api/auth/me", {field: identity(99)})
    with pytest.raises(EvidenceError, match="requester or organization differs"):
        advance_creation(
            selected,
            transport,
            origin=transport.origin,
            checkpoint=checkpoint,
            effects_authorized=True,
            now=datetime(2026, 10, 5, 11, 59, 30, tzinfo=UTC),
        )
    assert transport.calls == [("GET", "/api/auth/me", None)]


def test_browser_adapter_keeps_authentication_in_same_origin_page(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(Error=RuntimeError)
    )
    calls = []

    def evaluate(script, arguments):
        calls.append((script, arguments))
        return [200, {"version": 1}]

    page = SimpleNamespace(url="https://example.invalid/workspaces", evaluate=evaluate)
    transport = demo1_browser.PlaywrightBrowserTransport(
        page, "https://example.invalid"
    )
    assert transport.request("GET", "/api/superplane/v1/capabilities") == (
        200,
        {"version": 1},
    )
    assert len(calls) == 1
    assert "cognito_access_token" in calls[0][0]
    assert "Authorization" not in calls[0][1]
    page.url = "https://another.invalid/workspaces"
    with pytest.raises(EvidenceError, match="origin changed"):
        transport.request("GET", "/api/superplane/v1/capabilities")
    with pytest.raises(EvidenceError, match="unapproved request path"):
        transport.request(
            "POST", "https://example.invalid/api/superplane/v1/workspaces", {}
        )
    assert len(calls) == 1
