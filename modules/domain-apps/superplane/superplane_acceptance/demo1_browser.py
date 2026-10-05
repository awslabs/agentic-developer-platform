"""Authenticated, same-origin Demo 1 creation and recovery; no approval impersonation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .demo1_c1 import inspect_original_details, inspect_reentry, retirement_preview
from .demo1_evidence import DemoInput, EvidenceError, digest, identifier, instant

PREFIX = "/api/superplane/v1"


class BrowserTransport(Protocol):
    origin: str

    def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, object]: ...

    def browser_page(self): ...


@dataclass(frozen=True)
class CreationCheckpoint:
    request_id: str
    workspace_id: str
    plan_revision: str
    approval_id: str
    retirement_request_id: str
    submitted: bool = False


class PlaywrightBrowserTransport:
    """Keep the bearer token inside an already signed-in requester page."""

    def __init__(self, page, origin: str):
        self.page = page
        self.origin = checked_origin(origin)

    def browser_page(self):
        return self.page

    def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, object]:
        from playwright.sync_api import Error as PlaywrightError

        if method not in ("GET", "POST") or not path.startswith("/api/") or "?" in path:
            raise EvidenceError("browser: unapproved request path")
        if (
            urlsplit(self.page.url).scheme + "://" + urlsplit(self.page.url).netloc
            != self.origin
        ):
            raise EvidenceError("browser: session origin changed")
        try:
            result = self.page.evaluate(
                """async ({method, path, body}) => {
                  const token = localStorage.getItem('cognito_access_token');
                  if (!token) return [401, null];
                  const response = await fetch(path, {
                    method, credentials: 'same-origin',
                    headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'},
                    ...(body === null ? {} : {body: JSON.stringify(body)})
                  });
                  let data = null;
                  try { data = await response.json(); } catch { data = null; }
                  return [response.status, data];
                }""",
                {"method": method, "path": path, "body": body},
            )
        except (PlaywrightError, OSError, RuntimeError, ValueError):
            raise EvidenceError(
                "browser: response unavailable; retain original request"
            ) from None
        if (
            not isinstance(result, list)
            or len(result) != 2
            or type(result[0]) is not int
        ):
            raise EvidenceError("browser: invalid response; retain original request")
        return result[0], result[1]


def checked_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise EvidenceError("browser: exact HTTPS deployment origin required")
    return origin


def _response(
    transport: BrowserTransport, method: str, path: str, body: dict | None = None
) -> dict:
    try:
        status, value = transport.request(method, path, body)
    except (OSError, RuntimeError, ValueError):
        raise EvidenceError(
            "browser: response unavailable; retain original request"
        ) from None
    if status not in ((200, 201) if method == "POST" else (200,)) or not isinstance(
        value, dict
    ):
        raise EvidenceError(
            "browser: denied, unavailable or malformed response; retain original request"
        )
    return value


def _approval(
    ticket: dict, selected: DemoInput, checkpoint: CreationCheckpoint, now: datetime
) -> str:
    if (
        ticket.get("approval_id") != checkpoint.approval_id
        or ticket.get("workspace_id") != checkpoint.workspace_id
        or ticket.get("requester") != selected.requester_id
        or not isinstance(ticket.get("request"), dict)
        or ticket.get("request", {}).get("idempotency_key") != selected.request_id
        or ticket.get("request", {}).get("action") != "provision"
        or ticket.get("revoked") is not False
        or selected.approver_id not in ticket.get("approvers", [])
    ):
        raise EvidenceError("browser: approval identity or scope mismatch")
    if instant(ticket.get("expires_at"), "approval expiry") <= now:
        raise EvidenceError("browser: approval expired")
    if ticket.get("result") == "pending":
        return "pending"
    if (
        ticket.get("result") != "allowed-once"
        or ticket.get("decided_by") != selected.approver_id
        or not selected.authorized_at
        <= instant(ticket.get("decided_at"), "approval decision")
        <= now
    ):
        raise EvidenceError("browser: distinct human approval not verified")
    return "allowed-once"


def workspace_reading(workspace: dict, now: datetime) -> str:
    if (
        workspace.get("status") not in ("Active", "Ready")
        or workspace.get("cluster_health") != "Healthy"
    ):
        return "UNKNOWN"
    observed = workspace.get("last_heartbeat")
    if not isinstance(observed, str):
        return "UNKNOWN"
    try:
        heartbeat = instant(observed, "cluster heartbeat")
    except EvidenceError:
        return "UNKNOWN"
    return (
        "FRESH_WORKSPACE_ONLY"
        if -timedelta(seconds=30) <= now - heartbeat <= timedelta(minutes=5)
        else "UNKNOWN"
    )


def advance_creation(
    selected: DemoInput,
    transport: BrowserTransport,
    *,
    origin: str,
    checkpoint: CreationCheckpoint | None = None,
    persist: Callable[[CreationCheckpoint], None] | None = None,
    effects_authorized: bool = False,
    now: datetime | None = None,
) -> tuple[CreationCheckpoint | None, dict]:
    """Advance at most one phase; never repeat an uncertain POST or decide an approval."""
    now = now or datetime.now(UTC)
    if (
        not selected.authorized_at <= now < selected.deadline
        or checked_origin(origin) != transport.origin
    ):
        raise EvidenceError(
            "browser: authorization window or deployment origin mismatch"
        )
    if not effects_authorized:
        raise EvidenceError(
            "browser: explicit live authorization required before effects"
        )
    identity = _response(transport, "GET", "/api/auth/me")
    if (
        identity.get("user_id") != selected.requester_id
        or identity.get("org_id") != selected.org_id
    ):
        raise EvidenceError("browser: requester or organization differs from selection")
    if (
        not transport.browser_page()
        .get_by_role("heading", name="Workspaces")
        .is_visible()
    ):
        raise EvidenceError("browser: authenticated workspace view unavailable")
    if checkpoint is None:
        capabilities = _response(transport, "GET", PREFIX + "/capabilities")
        if (
            not isinstance(capabilities.get("features"), list)
            or "create-operation-id-v1" not in capabilities["features"]
        ):
            raise EvidenceError("browser: stable creation identity unavailable")
        body = {
            "operation_id": selected.request_id,
            "mode": "managed",
            "isolation_mode": "dedicated",
            "cluster_placement": "dedicated",
            "name": selected.workspace_name,
            "account": selected.account,
            "region": selected.region,
            "budget_max_daily_usd": str(selected.budget_usd),
        }
        plan = _response(transport, "POST", PREFIX + "/workspaces/preview", body)
        if (
            plan.get("request_id") != selected.request_id
            or digest(plan.get("revision"), "plan revision") != selected.plan_revision
            or not isinstance(plan.get("approval_request"), dict)
            or plan["approval_request"].get("idempotency_key") != selected.request_id
            or plan["approval_request"].get("action") != "provision"
            or plan["approval_request"].get("parameters", {}).get("plan_revision")
            != selected.plan_revision
            or plan["approval_request"].get("workspace_id") != plan.get("workspace_id")
            or plan.get("mode") != "managed"
            or not isinstance(plan.get("target"), dict)
            or plan.get("target", {}).get("account") != selected.account
            or plan.get("target", {}).get("region") != selected.region
        ):
            raise EvidenceError("browser: reviewed plan or selected target differs")
        workspace_id = identifier(plan["workspace_id"], "workspace_id")
        approval = _response(
            transport, "POST", PREFIX + "/operation-approvals", plan["approval_request"]
        )
        approval_id = identifier(approval.get("approval_id"), "approval_id")
        checkpoint = CreationCheckpoint(
            selected.request_id,
            workspace_id,
            selected.plan_revision,
            approval_id,
            str(uuid4()),
        )
        _approval(approval, selected, checkpoint, now)
        if persist is None:
            raise EvidenceError(
                "browser: private checkpoint writer required before approval handoff"
            )
        persist(checkpoint)
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "awaiting independent human approval",
        }
    if (
        checkpoint.request_id != selected.request_id
        or digest(checkpoint.plan_revision, "plan revision") != selected.plan_revision
        or identifier(checkpoint.workspace_id, "workspace_id")
        != checkpoint.workspace_id
        or identifier(checkpoint.approval_id, "approval_id") != checkpoint.approval_id
        or identifier(checkpoint.retirement_request_id, "retirement request")
        in (checkpoint.request_id, checkpoint.approval_id)
    ):
        raise EvidenceError("browser: saved creation checkpoint differs")
    approval = _response(
        transport, "GET", PREFIX + f"/operation-approvals/{checkpoint.approval_id}"
    )
    if _approval(approval, selected, checkpoint, now) == "pending":
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "awaiting independent human approval",
        }
    if not checkpoint.submitted:
        if persist is None:
            raise EvidenceError(
                "browser: private checkpoint writer required before creation"
            )
        body = {
            "operation_id": selected.request_id,
            "mode": "managed",
            "isolation_mode": "dedicated",
            "cluster_placement": "dedicated",
            "name": selected.workspace_name,
            "account": selected.account,
            "region": selected.region,
            "budget_max_daily_usd": str(selected.budget_usd),
            "plan_revision": selected.plan_revision,
            "approval_id": checkpoint.approval_id,
        }
        checkpoint = CreationCheckpoint(**{**vars(checkpoint), "submitted": True})
        persist(checkpoint)
        try:
            result = _response(transport, "POST", PREFIX + "/workspaces", body)
        except EvidenceError:
            return checkpoint, {
                "status": "BLOCKED",
                "reason": "creation reply uncertain; recover original request",
            }
        if (
            result.get("id") != checkpoint.workspace_id
            or result.get("org_id") != selected.org_id
        ):
            raise EvidenceError("browser: creation receipt differs from reviewed plan")
    operation = _response(
        transport, "GET", PREFIX + f"/operations/by-idempotency/{selected.request_id}"
    )
    if (
        operation.get("idempotency_key") != selected.request_id
        or operation.get("workspace_id") != checkpoint.workspace_id
    ):
        raise EvidenceError(
            "browser: recovered operation differs from original request"
        )
    workspace = _response(
        transport, "GET", PREFIX + f"/workspaces/{checkpoint.workspace_id}"
    )
    if (
        workspace.get("id") != checkpoint.workspace_id
        or workspace.get("org_id") != selected.org_id
        or workspace.get("name") != selected.workspace_name
    ):
        raise EvidenceError("browser: workspace re-entry differs from original target")
    operation_id = identifier(
        workspace.get("provisioning_operation_id"), "provisioning operation"
    )
    page = transport.browser_page()
    if (
        inspect_reentry(page, selected.workspace_name)["reason"]
        != "read-only re-entry visible; sign-in, authority and Ready not independently proved"
    ):
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "browser re-entry unavailable",
        }
    if (
        inspect_original_details(
            page, checkpoint.workspace_id, selected.request_id, operation_id
        )["reason"]
        != "original identities visible; session and provider authority unverified"
    ):
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "original operation not verified after refresh",
        }
    retirement_id = checkpoint.retirement_request_id
    status, preview = transport.request(
        "POST",
        PREFIX + f"/workspaces/{checkpoint.workspace_id}/retirement/preview",
        {"operation_id": retirement_id},
    )
    try:
        review = retirement_preview(
            selected,
            checkpoint.workspace_id,
            operation_id,
            retirement_id,
            status,
            preview,
        )
    except EvidenceError:
        raise EvidenceError(
            "browser: retirement preview differs from original operation"
        ) from None
    readiness = workspace_reading(workspace, now)
    return checkpoint, {
        "status": "BLOCKED",
        "reason": review["reason"],
        "creation_observed": True,
        "readiness": readiness,
        "retirement": review["status"],
    }
