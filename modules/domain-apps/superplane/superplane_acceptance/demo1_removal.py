"""Separately approved browser removal and original-request recovery.

Historical preparation permits reviewing a request, never deletion by itself.
The server retains all current grant, ownership, fence and admission checks.
"""

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from .demo1_browser import PREFIX, _approval, _response
from .demo1_evidence import EvidenceError, digest, identifier, instant
from .demo1_live import PrivateCheckpoint
from .demo1_report import reference
from .demo1_teardown import validate_teardown_review


def require(condition):
    if not condition:
        raise EvidenceError(
            "removal: original request, approval or provider baseline differs"
        )


@dataclass(frozen=True)
class RemovalCheckpoint:
    request_id: str
    workspace_id: str
    source_operation_id: str
    revision: str
    plan_revision: str
    approval_id: str | None
    provider_baseline: dict
    ready_observed_at: str
    submitted: bool = False
    operation_id: str | None = None


class PrivateRemoval(PrivateCheckpoint):
    state_type = RemovalCheckpoint

    def __init__(self, path, selected, envelope, original, preparation):
        require(
            original is not None
            and original.submitted
            and preparation is not None
            and preparation.submitted
        )
        require(
            preparation.workspace_id == original.workspace_id
            and preparation.retirement_request_id == original.retirement_request_id
        )
        super().__init__(path, selected, envelope.origin, envelope=envelope)
        self.version = "demo1-removal-v1"
        self.original, self.preparation = original, preparation
        self.envelope = envelope
        self.scope = sha256(
            json.dumps(
                {
                    "execution": self.scope,
                    "original": asdict(original),
                    "preparation": asdict(preparation),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _validate(self, state):
        require(type(state) is RemovalCheckpoint and type(state.submitted) is bool)
        for key in ("request_id", "workspace_id", "source_operation_id"):
            identifier(getattr(state, key), "removal identity")
        for key in ("revision", "plan_revision"):
            digest(getattr(state, key), "removal revision")
        require(
            state.request_id == self.original.retirement_request_id
            and state.workspace_id == self.original.workspace_id
            and state.source_operation_id == self.preparation.source_operation_id
        )
        if state.approval_id is not None:
            identifier(state.approval_id, "removal approval")
        require(not state.submitted or state.approval_id is not None)
        require(
            state.approval_id
            not in {
                self.original.approval_id,
                self.preparation.approval_id,
                self.original.request_id,
                self.preparation.request_id,
                state.request_id,
            }
        )
        require(isinstance(state.provider_baseline, dict) and state.provider_baseline)
        from .demo1_removal_provider import validate_provider_baseline

        validate_provider_baseline(
            self.selected, self.envelope, self.original, state.provider_baseline
        )
        require(
            state.provider_baseline["source_operation_id"] == state.source_operation_id
        )
        require(
            self.selected.authorized_at
            <= instant(state.ready_observed_at, "workspace ready observation")
            <= instant(state.provider_baseline["started_at"], "baseline start")
            < self.selected.deadline
        )
        if state.operation_id is not None:
            identifier(state.operation_id, "removal operation")
            require(
                state.submitted
                and state.operation_id
                not in {state.request_id, state.source_operation_id}
            )

    def save(self, state):
        self._pending_submission = None
        self._assert_held()
        self._validate(state)
        previous = self.load()
        if previous is not None:
            allowed = asdict(previous)
            # Observed approval/operation identities can be filled once; they can
            # never be replaced and all plan/baseline/Ready evidence stays fixed.
            if previous.approval_id is None and not previous.submitted:
                allowed["approval_id"] = state.approval_id
            if previous.operation_id is None and state.submitted:
                allowed["operation_id"] = state.operation_id
            allowed["submitted"] = state.submitted
            require(
                allowed == asdict(state) and (not previous.submitted or state.submitted)
            )
        self._write(state)
        if previous is not None and not previous.submitted and state.submitted:
            self._pending_submission = (previous, state)


def _recover(selected, transport, store, *, clock):
    saved = store.load()
    require(
        saved is not None
        and saved.submitted
        and selected.authorized_at <= clock() < selected.deadline
    )
    from .demo1_controls import recover_removal

    response = recover_removal(transport, saved)
    require(
        response.get("request_id") == saved.request_id
        and response.get("workspace_id") == saved.workspace_id
        and response.get("phase") == "execution"
    )
    operation = identifier(
        response.get("provisioning_operation_id"), "removal operation"
    )
    require(
        operation not in {saved.request_id, saved.source_operation_id}
        and saved.operation_id in (None, operation)
    )
    require(
        response.get("state")
        in {"pending", "running", "succeeded", "failed", "cancelled", "unknown"}
    )
    require(store.load() == saved)
    store.save(replace(saved, operation_id=operation))
    return {
        "status": "BLOCKED",
        "submission_observed": True,
        "request_ref": reference(saved.request_id),
        "operation_ref": reference(operation),
        "approval_ref": reference(saved.approval_id),
        "state": response["state"],
        "observed_at": clock().isoformat(),
        "reason": "original removal operation observed; provider absence and survivors require independent verification",
    }


def advance_removal(
    selected,
    envelope,
    transport,
    store,
    *,
    clock,
    review=None,
    artifact=None,
    provider_baseline=None,
    ready_observed_at=None,
):
    saved = store.load()
    if saved is not None and saved.submitted:
        return _recover(selected, transport, store, clock=clock)
    require(review is not None and artifact is not None)
    validate_teardown_review(
        review, selected, envelope, store.original, artifact, clock()
    )
    require(review["source_operation_id"] == store.preparation.source_operation_id)
    request = review["approval_request"]
    if saved is None:
        require(isinstance(provider_baseline, dict) and provider_baseline)
        saved = RemovalCheckpoint(
            store.original.retirement_request_id,
            store.original.workspace_id,
            review["source_operation_id"],
            review["revision"],
            request["parameters"]["plan_revision"],
            None,
            provider_baseline,
            ready_observed_at,
        )
        store.save(saved)
    require(
        saved.revision == review["revision"]
        and saved.plan_revision == request["parameters"]["plan_revision"]
    )
    if saved.approval_id is None:
        ticket = _response(transport, "POST", PREFIX + "/operation-approvals", request)
        saved = replace(
            saved, approval_id=identifier(ticket.get("approval_id"), "removal approval")
        )
    else:
        ticket = _response(
            transport, "GET", PREFIX + f"/operation-approvals/{saved.approval_id}"
        )
    store._validate(saved)
    require(
        ticket.get("plan_digest") == saved.revision
        and isinstance(ticket.get("request"), dict)
        and ticket["request"].get("contract_version") == "v1"
        and ticket["request"].get("parameters") == request["parameters"]
    )
    selection = replace(
        selected, request_id=saved.request_id, plan_revision=saved.plan_revision
    )
    decision = _approval(ticket, selection, saved, clock(), action="teardown")
    store.save(saved)
    if decision == "pending":
        return {
            "status": "BLOCKED",
            "request_ref": reference(saved.request_id),
            "approval_ref": reference(saved.approval_id),
            "reason": "awaiting independent removal approval; no removal submitted",
        }

    def before_send():
        nonlocal saved
        require(
            selected.authorized_at <= clock() < selected.deadline
            and store.load() == saved
        )
        store._validate(saved)
        # A fresh exact approval is checked at transmission; the UI and service
        # additionally recheck their current plan and authorization.
        fresh = _response(
            transport, "GET", PREFIX + f"/operation-approvals/{saved.approval_id}"
        )
        require(
            _approval(fresh, selection, saved, clock(), action="teardown")
            == "allowed-once"
            and fresh.get("plan_digest") == saved.revision
            and fresh.get("request") == ticket.get("request")
        )
        saved = replace(saved, submitted=True)
        store.save(saved)

    from .demo1_controls import remove_workspace

    try:
        status, response = remove_workspace(transport, saved, before_send)
        require(
            status in (200, 201)
            and response.get("request_id") == saved.request_id
            and response.get("workspace_id") == saved.workspace_id
            and response.get("phase") == "retire-workspace"
            and response.get("retirement_complete") is False
            and response.get("retryable") is False
        )
        operation = identifier(response.get("operation_id"), "removal operation")
        require(saved.submitted and store.load() == saved)
        store.save(replace(saved, operation_id=operation))
    except (EvidenceError, OSError, RuntimeError, ValueError):
        return {
            "status": "BLOCKED",
            "request_ref": reference(saved.request_id),
            "reason": "removal reply uncertain; recover original request"
            if saved.submitted
            else "removal not sent; restore original reviewed request",
        }
    return _recover(selected, transport, store, clock=clock)


def run_removal(
    selected, envelope, session, creation_store, cleanup_store, removal_store, *, clock
):
    """Complete one bounded requester invocation; approval may require re-entry."""
    import time

    from .demo1_browser import advance_creation, workspace_reading
    from .demo1_cleanup import advance_cleanup
    from .demo1_cleanup_evidence import observe_cleanup
    from .demo1_ownership import observe_ownership
    from .demo1_removal_provider import (
        capture_provider_baseline,
        verify_provider_removal,
    )
    from .demo1_runtime import RuntimeReader
    from .demo1_session import observe_in_browser

    expires = time.monotonic() + envelope.max_runtime_seconds

    def remaining():
        seconds = min(
            expires - time.monotonic(), (selected.deadline - clock()).total_seconds()
        )
        require(selected.authorized_at <= clock() < selected.deadline and seconds >= 1)
        return int(seconds)

    original = creation_store.load()
    require(
        original == removal_store.original
        and cleanup_store.load() == removal_store.preparation
    )
    reader = RuntimeReader(selected, envelope.runtime_target, clock=clock)
    runtime = reader.observe(remaining())

    def run(transport):
        saved = removal_store.load()
        if saved is not None and saved.submitted:
            result = advance_removal(
                selected, envelope, transport, removal_store, clock=clock
            )
        else:
            _, browser = advance_creation(
                selected,
                transport,
                origin=envelope.origin,
                checkpoint=original,
                persist=creation_store.save,
                restore_unsent=creation_store.restore_unsent,
                preview_retirement=False,
                effects_authorized=True,
                now=clock(),
            )
            require(
                browser.get("creation_observed") is True
                and browser.get("bootstrap_complete") is True
            )
            workspace = _response(
                transport, "GET", PREFIX + f"/workspaces/{original.workspace_id}"
            )
            require(
                workspace.get("provisioning_operation_id")
                == removal_store.preparation.source_operation_id
                and workspace_reading(workspace, clock()) == "FRESH_WORKSPACE_ONLY"
            )
            readiness = transport.page.get_by_role(
                "region", name="Readiness", exact=True
            )
            readiness.get_by_role("listitem").nth(1).get_by_text(
                "Ready", exact=True
            ).wait_for(state="visible", timeout=min(30_000, transport.remaining_ms()))
            ready_observed_at = clock().isoformat()
            preparation = advance_cleanup(
                selected, envelope, transport, cleanup_store, browser, clock=clock
            )
            require(preparation.get("state") == "succeeded")
            review = _response(
                transport,
                "POST",
                PREFIX + f"/workspaces/{original.workspace_id}/retirement/preview",
                {"operation_id": original.retirement_request_id},
            )
            artifact = observe_cleanup(
                reader,
                cleanup_store,
                preparation,
                remaining(),
                now=clock(),
                review=review,
            )
            _, ownership = observe_ownership(
                reader, original, remaining(), transport=transport, now=clock()
            )
            baseline = (
                saved.provider_baseline
                if saved is not None
                else capture_provider_baseline(
                    selected, envelope, original, ownership, remaining(), clock=clock
                )
            )
            result = advance_removal(
                selected,
                envelope,
                transport,
                removal_store,
                clock=clock,
                review=review,
                artifact=artifact,
                provider_baseline=baseline,
                ready_observed_at=ready_observed_at,
            )
        saved = removal_store.load()
        if saved is not None:
            result["ready_before_removal"] = {
                "status": "OBSERVED",
                "scope": "saved pre-removal workspace readiness row and completed bootstrap",
                "observed_at": saved.ready_observed_at,
            }
        if result.get("state") == "succeeded":
            workspace = _response(
                transport, "GET", PREFIX + f"/workspaces/{original.workspace_id}"
            )
            require(
                workspace.get("id") == original.workspace_id
                and workspace.get("org_id") == selected.org_id
                and workspace.get("status") == "retired"
                and workspace.get("provisioning_operation_id")
                == removal_store.preparation.source_operation_id
            )
            final = verify_provider_removal(
                selected,
                envelope,
                original,
                removal_store.load().provider_baseline,
                remaining(),
                clock=clock,
            )
            result["provider"] = final
            complete = (
                final.get("status") == "OBSERVED"
                and final.get("inventory_complete") is True
                and all(
                    final.get("checks", {}).get(check, {}).get("status") == "PASS"
                    for check in ("owned_absence", "survivors")
                )
            )
            result["status"] = "PASS" if complete else "BLOCKED"
            result["reason"] = (
                "Demo 1 completed: workspace Ready observed, original approved removal recovered, scoped provider absence and survivors verified; cost remains unknown"
                if complete
                else "governed removal completed; independent scoped provider verification remains incomplete; cost remains unknown"
            )
            result["scope"] = (
                "Demo 1 dedicated workspace only; broader acceptance scenarios remain unverified"
            )
        return result

    result = observe_in_browser(
        selected, envelope, session, run, max_runtime_seconds=remaining()
    )
    return {
        "status": result["status"],
        "scope": "dedicated Demo 1 workspace create, Ready, approved removal and scoped provider verification",
        "runtime": runtime,
        "demo1": {
            "status": result["status"],
            "scope": "dedicated workspace create, Ready, approved removal and scoped provider verification",
            "cost_usd": None,
        },
        "browser": {"removal": result},
        "reason": result["reason"],
    }
