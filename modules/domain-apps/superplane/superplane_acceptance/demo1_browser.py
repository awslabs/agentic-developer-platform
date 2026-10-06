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
from .demo1_lineage import lineage_report

PREFIX = "/api/superplane/v1"


class BrowserTransport(Protocol):
    origin: str

    def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, object]: ...

    def create_workspace(
        self, body: dict, before_send: Callable[[], None]
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

    def __init__(
        self,
        page,
        origin: str,
        *,
        release_id: str | None = None,
        remaining_ms: Callable[[], int] = lambda: 30_000,
    ):
        self.page = page
        self.origin = checked_origin(origin)
        self.release_id = (
            digest(release_id, "browser release") if release_id is not None else None
        )
        self.remaining_ms = remaining_ms

    def browser_page(self):
        return self.page

    def create_workspace(
        self, body: dict, before_send: Callable[[], None]
    ) -> tuple[int, object]:
        return self.request(
            "POST", PREFIX + "/workspaces", body, before_send=before_send
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[int, object]:
        from playwright.sync_api import Error as PlaywrightError

        if (
            method not in ("GET", "POST")
            or not (path == "/api/auth/me" or path.startswith(PREFIX + "/"))
            or any(character in path for character in ("?", "#", "%", "\\"))
            or any(part in ("", ".", "..") for part in path.split("/")[1:])
        ):
            raise EvidenceError("browser: unapproved request path")
        if (
            urlsplit(self.page.url).scheme + "://" + urlsplit(self.page.url).netloc
            != self.origin
        ):
            raise EvidenceError("browser: session origin changed")
        if method == "POST" and self.release_id is not None:
            status, _ = self.request("GET", PREFIX + "/capabilities")
            if status != 200:
                raise EvidenceError(
                    "browser: public release probe unavailable before submission"
                )
        timeout = min(30_000, self.remaining_ms())
        if timeout <= 0:
            raise EvidenceError("browser: authorized runtime exhausted")
        if before_send is not None:
            before_send()
        try:
            result = self.page.evaluate(
                """async ({method, path, body, timeout}) => {
                  const token = localStorage.getItem('cognito_access_token');
                  if (!token) return [401, null, null];
                  const response = await fetch(path, {
                    method, credentials: 'same-origin', redirect: 'error',
                    signal: AbortSignal.timeout(timeout),
                    headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'},
                    ...(body === null ? {} : {body: JSON.stringify(body)})
                  });
                  let data = null;
                  try { data = await response.json(); } catch { data = null; }
                  return [response.status, data, response.headers.get('X-Superplane-Release')];
                }""",
                {"method": method, "path": path, "body": body, "timeout": timeout},
            )
        except (PlaywrightError, OSError, RuntimeError, ValueError):
            raise EvidenceError(
                "browser: response unavailable; retain original request"
            ) from None
        if (
            not isinstance(result, list)
            or len(result) != 3
            or type(result[0]) is not int
        ):
            raise EvidenceError("browser: invalid response; retain original request")
        if (
            self.release_id is not None
            and path.startswith(PREFIX + "/")
            and result[2] != self.release_id
        ):
            raise EvidenceError(
                "browser: public route release differs; retain original request"
            )
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
    transport: BrowserTransport,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    before_send: Callable[[], None] | None = None,
) -> dict:
    try:
        if before_send is None:
            status, value = transport.request(method, path, body)
        else:
            status, value = transport.create_workspace(body, before_send)
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


def _plan_parameters_match(parameters: object, plan_revision: str) -> bool:
    return (
        isinstance(parameters, dict)
        and parameters.get("plan_revision") == plan_revision
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parameters.items()
        )
    )


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
        or not _plan_parameters_match(
            ticket["request"].get("parameters"), selected.plan_revision
        )
        or ticket.get("revoked") is not False
        or not isinstance(ticket.get("approvers"), list)
        or not all(isinstance(approver, str) for approver in ticket["approvers"])
        or selected.approver_id not in ticket["approvers"]
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
    verify_lineage: Callable[[CreationCheckpoint, str, str], dict] | None = None,
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
            or not _plan_parameters_match(
                plan["approval_request"].get("parameters"), selected.plan_revision
            )
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
    if not checkpoint.submitted:
        approval = _response(
            transport, "GET", PREFIX + f"/operation-approvals/{checkpoint.approval_id}"
        )
        if _approval(approval, selected, checkpoint, now) == "pending":
            return checkpoint, {
                "status": "BLOCKED",
                "reason": "awaiting independent human approval",
            }
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
        preflight_completed = False

        def mark_submitted():
            nonlocal checkpoint, preflight_completed
            preflight_completed = True
            submitted = CreationCheckpoint(**{**vars(checkpoint), "submitted": True})
            persist(submitted)
            checkpoint = submitted

        try:
            result = _response(
                transport,
                "POST",
                PREFIX + "/workspaces",
                body,
                before_send=mark_submitted,
            )
        except EvidenceError:
            if not preflight_completed:
                return checkpoint, {
                    "status": "BLOCKED",
                    "reason": "creation not sent; retry original request after pre-send checks succeed",
                }
            if not checkpoint.submitted:
                raise EvidenceError(
                    "browser: checkpoint persistence failed; retain original request for reconciliation"
                ) from None
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
    if operation.get("request_id") != selected.request_id:
        raise EvidenceError(
            "browser: recovered operation differs from original request"
        )
    operation_id = identifier(
        operation.get("provisioning_operation_id"), "recovered provisioning operation"
    )
    if (
        "workspace_id" in operation
        and operation["workspace_id"] is None
        and operation.get("phase") == "workspace_registration"
    ):
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "original operation found; workspace registration incomplete",
        }
    if operation.get("workspace_id") != checkpoint.workspace_id:
        raise EvidenceError(
            "browser: recovered operation differs from original workspace"
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
    current_id = identifier(
        workspace.get("provisioning_operation_id"), "current operation"
    )
    current_request = selected.request_id
    lineage = None
    if current_id != operation_id:
        if verify_lineage is None:
            raise EvidenceError("browser: immutable continuation lineage required")
        lineage = verify_lineage(checkpoint, operation_id, current_id)
        current_request = lineage["current_request_id"]
        current = _response(transport, "GET", PREFIX + f"/operations/{current_id}")
        if (
            current.get("request_id") != current_request
            or current.get("provisioning_operation_id") != current_id
            or current.get("workspace_id") != checkpoint.workspace_id
        ):
            raise EvidenceError(
                "browser: current operation differs from verified continuation"
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
            page, checkpoint.workspace_id, current_request, current_id
        )["reason"]
        != "original identities visible; session and provider authority unverified"
    ):
        return checkpoint, {
            "status": "BLOCKED",
            "reason": "workspace operation not verified after refresh",
        }
    if lineage is not None:
        refreshed = _response(
            transport, "GET", PREFIX + f"/workspaces/{checkpoint.workspace_id}"
        )
        if any(
            refreshed.get(key) != workspace.get(key)
            for key in ("id", "org_id", "name", "provisioning_operation_id")
        ):
            raise EvidenceError(
                "browser: workspace changed during continuation re-entry"
            )
        workspace = refreshed
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
            current_id,
            retirement_id,
            status,
            preview,
        )
    except EvidenceError:
        raise EvidenceError(
            "browser: retirement preview differs from verified workspace operation"
        ) from None
    readiness = workspace_reading(workspace, now)
    return checkpoint, {
        "status": "BLOCKED",
        "reason": review["reason"],
        "creation_observed": True,
        "readiness": readiness,
        "retirement": review["status"],
        **({"lineage": lineage_report(lineage)} if lineage is not None else {}),
    }
