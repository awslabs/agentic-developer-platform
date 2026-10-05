"""Source-bound C1 browser reads and review-only retirement diagnostics."""

from __future__ import annotations

from contextlib import suppress
from typing import Protocol

from .demo1_evidence import DemoInput, EvidenceError, digest, identifier, text

C1_CHECKPOINT = "2026-10-05.1"


class BrowserPage(Protocol):
    """Playwright Page read/navigation subset; no submit or deletion method."""

    def get_by_role(self, role: str, *, name: str, exact: bool = False): ...

    def get_by_label(self, label: str, *, exact: bool = False): ...

    def reload(self): ...


def inspect_review(page: BrowserPage) -> dict:
    """Probe C1's plan and approval landmarks without pressing submit."""
    plan = approval = False
    with suppress(Exception):
        plan = page.get_by_role("group", name="Review this plan").is_visible()
        approval = page.get_by_role(
            "region", name="Review an operation approval"
        ).is_visible()
    if not (plan and approval):
        return {"status": "BLOCKED", "reason": "plan or approval review missing"}
    return {
        "status": "BLOCKED",
        "reason": "review visible; approval authority not verified",
    }


def inspect_reentry(page: BrowserPage, workspace_display_name: str) -> dict:
    """Use C1's accessible controls in an already authorized browser session.

    This is a read-only UI probe, not a login, creation or removal driver. UI
    visibility cannot authenticate the underlying operation or provider inventory.
    """
    observed = False
    with suppress(Exception):
        if not _visible_after_wait(page.get_by_role("heading", name="Workspaces")):
            return {
                "status": "BLOCKED",
                "reason": "workspace list or login unavailable",
            }
        selection = page.get_by_role("button", name=workspace_display_name, exact=False)
        if not _visible_after_wait(selection):
            return {"status": "BLOCKED", "reason": "original workspace not visible"}
        selection.click()
        if not _visible_after_wait(page.get_by_role("region", name="Readiness")):
            return {"status": "BLOCKED", "reason": "workspace readiness not visible"}
        page.reload()
        if not _visible_after_wait(page.get_by_role("heading", name="Workspaces")):
            return {
                "status": "BLOCKED",
                "reason": "workspace list unavailable after refresh",
            }
        selection = page.get_by_role("button", name=workspace_display_name, exact=False)
        if not _visible_after_wait(selection):
            return {
                "status": "BLOCKED",
                "reason": "workspace not visible after refresh",
            }
        selection.click()
        if not _visible_after_wait(page.get_by_role("region", name="Readiness")):
            return {
                "status": "BLOCKED",
                "reason": "readiness not visible after refresh",
            }
        observed = True
    if not observed:
        return {"status": "BLOCKED", "reason": "browser observation unavailable"}
    return {
        "status": "BLOCKED",
        "reason": "read-only re-entry visible; sign-in, authority and Ready not independently proved",
    }


def _visible_after_wait(locator) -> bool:
    with suppress(Exception):
        locator.wait_for(state="visible", timeout=5000)
        return locator.is_visible()
    return False


def inspect_original_details(
    page: BrowserPage, workspace_id: str, request_id: str, operation_id: str
) -> dict:
    """Read C1's original workspace/request identities, including after refresh."""
    workspace_id = identifier(workspace_id, "workspace_id")
    request_id = identifier(request_id, "request_id")
    operation_id = identifier(operation_id, "operation_id")
    with suppress(Exception):
        details = page.get_by_role("region", name="Workspace details")
        if not _visible_after_wait(details):
            return {"status": "BLOCKED", "reason": "workspace details unavailable"}
        if not _visible_after_wait(
            page.get_by_role("button", name="Refresh workspace details", exact=True)
        ):
            return {"status": "BLOCKED", "reason": "details refresh unavailable"}
        for refresh in (False, True):
            if refresh:
                page.get_by_role(
                    "button", name="Refresh workspace details", exact=True
                ).click()
                details = page.get_by_role("region", name="Workspace details")
            if not _visible_after_wait(details):
                return {"status": "BLOCKED", "reason": "workspace details unavailable"}
            if any(
                not _visible_after_wait(details.get_by_text(identity, exact=True))
                for identity in (workspace_id, operation_id, request_id)
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "original workspace or operation identity not verified",
                }
        return {
            "status": "BLOCKED",
            "reason": "original identities visible; session and provider authority unverified",
        }
    return {"status": "BLOCKED", "reason": "workspace details unreadable"}


def inspect_retirement_review(
    page: BrowserPage,
    selected: DemoInput,
    workspace_id: str,
    source_operation_id: str,
    retirement_operation_id: str,
    status_code: int,
    body: object,
) -> dict:
    """Inspect an already-rendered C1 preview; never request or submit removal."""
    review = retirement_preview(
        selected,
        workspace_id,
        source_operation_id,
        retirement_operation_id,
        status_code,
        body,
    )
    with suppress(Exception):
        retirement = page.get_by_role("region", name="Workspace retirement")
        if (
            not retirement.is_visible()
            or not page.get_by_role(
                "button", name="Review removal", exact=True
            ).is_visible()
        ):
            return {**review, "reason": "removal review unavailable in browser"}
        if status_code != 200:
            return review
        rendered = page.get_by_label("Retirement review", exact=True)
        if not rendered.is_visible():
            return {**review, "reason": "retirement preview not rendered"}
        remove = rendered.get_by_role("button", name="Remove workspace", exact=True)
        if not remove.is_visible():
            return {**review, "reason": "removal control not visible"}
        if remove.is_enabled():
            return {
                **review,
                "status": "FAIL",
                "reason": "removal enabled without approved admission",
            }
        if any(
            not rendered.get_by_text(value, exact=True).is_visible()
            for value in (workspace_id, source_operation_id, retirement_operation_id)
        ):
            return {
                **review,
                "status": "FAIL",
                "reason": "retirement review identity differs from original lineage",
            }
        if not rendered.get_by_text(
            "Unknown; this preview has no cost estimate. "
            "Preserved resources may continue to incur charges.",
            exact=True,
        ).is_visible():
            return {**review, "reason": "preview cost not observable"}
        return {
            **review,
            "reason": "preview visible; admission and cleanup unavailable",
        }
    return {**review, "reason": "retirement review unreadable"}


def retirement_preview(
    selected: DemoInput,
    workspace_id: str,
    source_operation_id: str,
    retirement_operation_id: str,
    status_code: int,
    body: object,
) -> dict:
    """Parse C1's existing review-only response, never an admission receipt."""
    workspace_id = identifier(workspace_id, "workspace_id")
    source_operation_id = identifier(source_operation_id, "source_operation_id")
    retirement_operation_id = identifier(
        retirement_operation_id, "retirement_operation_id"
    )
    if retirement_operation_id in (selected.request_id, source_operation_id):
        raise EvidenceError(
            "retirement: request or operation identity reused from creation"
        )
    if status_code in (403, 503):
        return {
            "status": "BLOCKED",
            "reason": "retirement preview denied or unavailable",
            "admission_available": False,
            "cost": "UNKNOWN",
        }
    if status_code != 200:
        raise EvidenceError("retirement: unexpected preview response")
    expected = {
        "request_id",
        "workspace_id",
        "source_operation_id",
        "source_payload_digest",
        "lifecycle_artifact_id",
        "account_id",
        "region",
        "inventory_sha256",
        "lifecycle_policy_sha256",
        "runtime_config_sha256",
        "steps",
        "preserved",
        "revision",
        "admission_available",
        "blocked_reason",
        "approval_request",
    }
    if not isinstance(body, dict) or set(body) != expected:
        raise EvidenceError("retirement: missing or unexpected review fields")
    if (
        body["request_id"] != retirement_operation_id
        or body["workspace_id"] != workspace_id
        or body["source_operation_id"] != source_operation_id
        or body["account_id"] != selected.account
        or body["region"] != selected.region
    ):
        raise EvidenceError("retirement: preview target or source mismatch")
    if (
        body["admission_available"] is not False
        or body["blocked_reason"] != "staged_cleanup_access_required"
        or body["approval_request"] is not None
    ):
        raise EvidenceError("retirement: unreviewed admission or approval contract")
    for field in (
        "revision",
        "source_payload_digest",
        "inventory_sha256",
        "lifecycle_policy_sha256",
        "runtime_config_sha256",
    ):
        digest(body[field], field)
    text(body["lifecycle_artifact_id"], "lifecycle_artifact_id")
    steps = body["steps"]
    preserved = body["preserved"]
    if not isinstance(steps, list) or not isinstance(preserved, list):
        raise EvidenceError("retirement: invalid ownership inventory")
    for step in steps:
        if not isinstance(step, dict) or set(step) != {
            "step_id",
            "provider",
            "operation_kind",
            "target",
        }:
            raise EvidenceError("retirement: invalid cleanup step")
        for field in step:
            text(step[field], "cleanup step")
    for resource in preserved:
        text(resource, "preserved resource")
    if not steps:
        raise EvidenceError("retirement: no deletion inventory to verify")
    return {
        "status": "BLOCKED",
        "reason": "review-only; cleanup access and admission unavailable",
        "admission_available": False,
        "cost": "UNKNOWN",
        "step_count": len(steps),
        "preserved_count": len(preserved),
    }
