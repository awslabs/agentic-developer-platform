"""Separately approved cleanup preparation with durable, original-request recovery."""

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from workspace_provisioning.retirement_access_plan import PHASE, access_identity
from workspace_provisioning.runtime_config import LifecycleRefused

from .demo1_browser import PREFIX, RequestNotSent, _approval, _response
from .demo1_evidence import EvidenceError, digest, identifier, text
from .demo1_live import PrivateCheckpoint
from .demo1_report import reference
from .demo1_retirement import (
    read_access_review,
    validate_access_review,
    workspace_source,
)


def require(condition):
    if not condition:
        raise EvidenceError(
            "cleanup preparation: saved identity, approval or admission differs"
        )


@dataclass(frozen=True)
class CleanupCheckpoint:
    request_id: str
    workspace_id: str
    retirement_request_id: str
    source_operation_id: str
    original_allocation_id: str
    plan_revision: str
    revision: str
    approval_id: str
    submitted: bool = False


class PrivateCleanup(PrivateCheckpoint):
    state_type = CleanupCheckpoint

    def __init__(self, path, selected, envelope, original):
        require(
            original is not None
            and original.submitted
            and original.request_id == selected.request_id
            and original.plan_revision == selected.plan_revision
        )
        super().__init__(path, selected, envelope.origin, envelope=envelope)
        self.version = "demo1-cleanup-preparation-v1"
        self.original = original
        self.scope = sha256(
            json.dumps(
                {"execution": self.scope, "original": asdict(original)}, sort_keys=True
            ).encode()
        ).hexdigest()

    def _validate(self, state):
        require(type(state) is CleanupCheckpoint and type(state.submitted) is bool)
        for key in (
            "request_id",
            "workspace_id",
            "retirement_request_id",
            "source_operation_id",
            "approval_id",
        ):
            identifier(getattr(state, key), "cleanup identity")
        for key in ("plan_revision", "revision"):
            digest(getattr(state, key), "cleanup revision")
        text(state.original_allocation_id, "cleanup allocation")
        try:
            request, _ = access_identity(
                self.selected.org_id,
                self.original.workspace_id,
                state.original_allocation_id,
                self.original.retirement_request_id,
            )
        except LifecycleRefused:
            raise EvidenceError(
                "cleanup preparation: invalid saved allocation"
            ) from None
        require(
            state.request_id == request
            and state.workspace_id == self.original.workspace_id
            and state.retirement_request_id == self.original.retirement_request_id
            and len(
                {
                    state.request_id,
                    state.retirement_request_id,
                    state.approval_id,
                    state.source_operation_id,
                    self.original.request_id,
                    self.original.approval_id,
                }
            )
            == 6
        )


def admission_body(saved):
    return {
        "operation_id": saved.retirement_request_id,
        "plan_revision": saved.revision,
        "approval_id": saved.approval_id,
    }


def receipt(response, saved, operation=None):
    require(
        isinstance(response, dict)
        and response.get("retirement_request_id") == saved.retirement_request_id
        and response.get("request_id") == saved.request_id
        and response.get("workspace_id") == saved.workspace_id
        and response.get("phase") == PHASE
        and response.get("retirement_complete") is False
        and response.get("retryable") is False
        and response.get("state")
        in ("pending", "running", "succeeded", "failed", "cancelled", "unknown")
    )
    observed = identifier(response.get("control_operation_id"), "cleanup operation")
    require(
        observed
        not in (
            saved.source_operation_id,
            saved.request_id,
            saved.retirement_request_id,
        )
        and (operation is None or observed == operation)
    )
    return {
        "status": "BLOCKED",
        "phase": PHASE,
        "submission_observed": True,
        "request_ref": reference(saved.request_id),
        "operation_ref": reference(observed),
        "state": response["state"],
        "retirement_complete": False,
        "reason": "preparation admission observed; immutable grants, fencing, complete inventory and deletion remain unverified",
    }


def advance_cleanup(selected, envelope, transport, store, browser, *, clock):
    original = store.original
    saved = store.load()
    prefix = PREFIX + f"/workspaces/{original.workspace_id}/retirement/access"
    if saved is not None and saved.submitted:
        _, source = workspace_source(
            selected, envelope, transport, original, browser, now=clock()
        )
        require(source == saved.source_operation_id)
        admitted = _response(
            transport, "GET", PREFIX + f"/operations/by-idempotency/{saved.request_id}"
        )
        require(
            admitted.get("request_id") == saved.request_id
            and admitted.get("workspace_id") == saved.workspace_id
            and admitted.get("phase") == "execution"
        )
        operation = identifier(
            admitted.get("provisioning_operation_id"), "cleanup operation"
        )
        require(
            operation
            not in (
                saved.source_operation_id,
                saved.request_id,
                saved.retirement_request_id,
            )
        )
        require(store.load() == saved)
        result = receipt(
            _response(transport, "POST", prefix, admission_body(saved)),
            saved,
            operation,
        )
        result["reason"] = (
            "original preparation admission and registration recovered; no new approval; fencing and deletion remain unverified"
        )
        return result
    review, _ = read_access_review(
        selected, envelope, transport, original, browser, now=clock()
    )
    request = review["approval_request"]
    if saved is not None:
        require(
            saved.revision == review["revision"]
            and saved.request_id == review["request_id"]
            and saved.source_operation_id == review["source_operation_id"]
        )
        ticket = _response(
            transport, "GET", PREFIX + f"/operation-approvals/{saved.approval_id}"
        )
    else:
        ticket = _response(transport, "POST", PREFIX + "/operation-approvals", request)
        saved = CleanupCheckpoint(
            review["request_id"],
            original.workspace_id,
            original.retirement_request_id,
            review["source_operation_id"],
            review["original_allocation_id"],
            request["parameters"]["plan_revision"],
            review["revision"],
            identifier(ticket.get("approval_id"), "cleanup approval"),
        )
    store._validate(saved)
    require(
        ticket.get("plan_digest") == saved.revision
        and isinstance(ticket.get("request"), dict)
        and ticket["request"].get("contract_version") == "v1"
        and ticket["request"].get("parameters") == request["parameters"]
        and ticket.get("envelope")
        == {
            key: int(request["parameters"][key])
            for key in ("max_resource_units", "max_cost_micros", "max_runtime_seconds")
        }
    )
    selection = replace(
        selected, request_id=saved.request_id, plan_revision=saved.plan_revision
    )
    decision = _approval(ticket, selection, saved, clock())
    store.save(saved)
    if decision == "pending":
        return {
            "status": "BLOCKED",
            "phase": PHASE,
            "request_ref": reference(saved.request_id),
            "approval_ref": reference(saved.approval_id),
            "reason": "awaiting independent cleanup-preparation approval; no preparation submitted",
        }
    attempted = False

    def mark_submitted():
        nonlocal saved, attempted
        _, source = workspace_source(
            selected, envelope, transport, original, browser, now=clock()
        )
        require(source == saved.source_operation_id)
        now = clock()
        require(selected.authorized_at <= now < selected.deadline)
        require(_approval(ticket, selection, saved, now) == "allowed-once")
        validate_access_review(
            review, selected, envelope, original, saved.source_operation_id, now
        )
        attempted = True
        state = replace(saved, submitted=True)
        store.save(state)
        saved = state

    try:
        status, response = transport.request(
            "POST", prefix, admission_body(saved), before_send=mark_submitted
        )
        require(status in (200, 201))
        return receipt(response, saved)
    except RequestNotSent:
        store.restore_unsent(saved)
        return {
            "status": "BLOCKED",
            "phase": PHASE,
            "reason": "preparation not sent; retry original request after pre-send checks succeed",
        }
    except (EvidenceError, OSError, RuntimeError, ValueError):
        if attempted and not saved.submitted:
            raise EvidenceError(
                "cleanup preparation: checkpoint write uncertain; preserve original files for reconciliation"
            ) from None
        return {
            "status": "BLOCKED",
            "phase": PHASE,
            "reason": "preparation reply uncertain; recover original request"
            if saved.submitted
            else "preparation not sent; retry original request after pre-send checks succeed",
        }
