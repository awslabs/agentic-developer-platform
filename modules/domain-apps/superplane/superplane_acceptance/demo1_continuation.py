"""One separately approved lifecycle continuation, never a replay of uncertain work."""

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID, uuid5

from .demo1_browser import PREFIX, _approval, _response
from .demo1_evidence import EvidenceError, digest, identifier
from .demo1_live import PrivateCheckpoint
from .demo1_report import reference

PHASES = ("apply-infrastructure", "bootstrap-workspace")


def require(condition):
    if not condition:
        raise EvidenceError(
            "continuation: selected phase, target or exact approval differs"
        )


@dataclass(frozen=True)
class ContinuationCheckpoint:
    request_id: str
    workspace_id: str
    plan_revision: str
    approval_id: str
    artifact_id: str
    source_operation_id: str
    phase: str
    revision: str
    submitted: bool = False


class PrivateContinuation(PrivateCheckpoint):
    state_type = ContinuationCheckpoint

    def __init__(self, path, selected, envelope, original, phase):
        require(
            original is not None
            and original.submitted
            and phase in PHASES
            and original.request_id == selected.request_id
            and original.plan_revision == selected.plan_revision
        )
        super().__init__(path, selected, envelope.origin, envelope=envelope)
        self.version = "demo1-continuation-v1"
        self.original, self.phase = original, phase
        self.scope = sha256(
            json.dumps(
                {
                    "execution": self.scope,
                    "original": asdict(original),
                    "phase": phase,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _validate(self, state):
        require(type(state) is ContinuationCheckpoint and type(state.submitted) is bool)
        for key in ("request_id", "workspace_id", "approval_id", "source_operation_id"):
            identifier(getattr(state, key), "continuation identity")
        for key in ("plan_revision", "artifact_id", "revision"):
            digest(getattr(state, key), "continuation digest")
        require(
            state.workspace_id == self.original.workspace_id
            and state.plan_revision == self.selected.plan_revision
            and state.phase == self.phase
            and state.request_id
            == request_identity(self.original, state.artifact_id, state.phase)
            and state.request_id
            not in (
                self.original.request_id,
                state.source_operation_id,
                state.approval_id,
            )
        )


def request_identity(original, artifact_id, phase):
    return str(
        uuid5(UUID(original.request_id), f"demo1-continuation:{phase}:{artifact_id}")
    )


def validate_review(
    selected, envelope, original, phase, source, artifact, request, review, now
):
    require(isinstance(review, dict))
    require(
        all(
            review.get(key) == value
            for key, value in {
                "status": "awaiting_plan_approval",
                "workspace_id": original.workspace_id,
                "source_operation_id": source,
                "artifact_id": artifact,
                "request_id": request,
                "request_revision": selected.plan_revision,
                "phase": phase,
                "account_id": selected.account,
            }.items()
        )
    )
    target = review.get("target")
    require(
        isinstance(target, dict)
        and all(
            target.get(key) == value
            for key, value in {
                "account_id": selected.account,
                "aws_region": selected.region,
                "org_id": selected.org_id,
                "workspace_id": original.workspace_id,
            }.items()
        )
    )
    approval = review.get("approval_request")
    require(
        isinstance(approval, dict)
        and set(approval) == {"workspace_id", "action", "idempotency_key", "parameters"}
    )
    require(
        approval["workspace_id"] == original.workspace_id
        and approval["action"] == "provision"
        and approval["idempotency_key"] == request
    )
    parameters = approval["parameters"]
    require(
        isinstance(parameters, dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parameters.items()
        )
    )
    require(
        all(
            parameters.get(key) == value
            for key, value in {
                "plan_revision": selected.plan_revision,
                "lifecycle_phase": phase,
                "lifecycle_source_operation_id": source,
                "lifecycle_artifact_id": artifact,
                "provider": "aws",
                "provider_account_id": selected.account,
                "aws_account_id": selected.account,
            }.items()
        )
    )
    try:
        lifecycle = json.loads(parameters["lifecycle_request"])
        inputs = json.loads(parameters["lifecycle_inputs"])
        limits = {
            key: int(parameters[key])
            for key in ("max_cost_micros", "max_runtime_seconds", "max_resource_units")
        }
    except (ValueError, KeyError, TypeError):
        raise EvidenceError(
            "continuation: malformed lifecycle target or limits"
        ) from None
    require(
        isinstance(lifecycle, dict)
        and all(
            lifecycle.get(key) == value
            for key, value in {
                "mode": "existing-account-managed",
                "target_account_id": selected.account,
                "region": selected.region,
                "workspace_id": original.workspace_id,
            }.items()
        )
    )
    require(
        isinstance(inputs, dict)
        and inputs.get("cluster_placement", "dedicated") == "dedicated"
    )
    require(
        0 <= limits["max_cost_micros"] <= selected.budget_usd * 1_000_000
        and 0
        < limits["max_runtime_seconds"]
        <= min(envelope.max_runtime_seconds, (selected.deadline - now).total_seconds())
        and 0 <= limits["max_resource_units"]
    )
    digest(review.get("revision"), "continuation revision")
    return approval


def advance_continuation(
    selected, envelope, transport, store, *, clock, verified_source
):
    now = clock()
    require(selected.authorized_at <= now < selected.deadline)
    original, phase = store.original, store.phase
    saved = store.load()
    prefix = PREFIX + f"/workspaces/{original.workspace_id}"
    if saved is not None and saved.submitted:
        result = _response(
            transport, "GET", PREFIX + f"/operations/by-idempotency/{saved.request_id}"
        )
        require(
            result.get("request_id") == saved.request_id
            and result.get("workspace_id") == original.workspace_id
        )
        operation = identifier(
            result.get("provisioning_operation_id"), "continuation operation"
        )
        require(operation != saved.source_operation_id)
        return {
            "status": "BLOCKED",
            "reason": "continuation recovered; readiness and cleanup remain separate",
            "submission_observed": True,
            "request_ref": reference(saved.request_id),
            "operation_ref": reference(operation),
            "phase": phase,
        }
    workspace = _response(transport, "GET", prefix)
    require(
        workspace.get("id") == original.workspace_id
        and workspace.get("org_id") == selected.org_id
    )
    source = identifier(
        workspace.get("provisioning_operation_id"), "continuation source"
    )
    require(reference(source) == verified_source)
    current = _response(transport, "GET", PREFIX + f"/operations/{source}")
    require(
        current.get("provisioning_operation_id") == source
        and current.get("workspace_id") == original.workspace_id
    )
    if current.get("state") != "succeeded":
        return {
            "status": "BLOCKED",
            "reason": "current lifecycle phase has not succeeded",
        }
    proposals = _response(transport, "GET", prefix + "/lifecycle-proposals")
    require(
        proposals.get("workspace_id") == original.workspace_id
        and isinstance(proposals.get("proposals"), list)
    )
    candidates = [
        item
        for item in proposals["proposals"]
        if isinstance(item, dict)
        and item.get("phase") == phase
        and item.get("source_operation_id") == source
    ]
    if not candidates:
        return {
            "status": "BLOCKED",
            "reason": "selected continuation proposal unavailable",
        }
    require(len(candidates) == 1)
    proposal = candidates[0]
    artifact = digest(proposal.get("artifact_id"), "continuation artifact")
    request = request_identity(original, artifact, phase)
    route = prefix + f"/lifecycle-proposals/{artifact}"
    if saved is not None:
        require(saved.request_id == request and saved.source_operation_id == source)
    review = _response(transport, "POST", route + "/preview", {"operation_id": request})
    approval_request = validate_review(
        selected, envelope, original, phase, source, artifact, request, review, clock()
    )
    require(all(review.get(key) == proposal.get(key) for key in proposal))
    if saved is None:
        ticket = _response(
            transport, "POST", PREFIX + "/operation-approvals", approval_request
        )
        saved = ContinuationCheckpoint(
            request,
            original.workspace_id,
            selected.plan_revision,
            identifier(ticket.get("approval_id"), "continuation approval"),
            artifact,
            source,
            phase,
            review["revision"],
        )
    else:
        require(saved.revision == review["revision"])
        ticket = _response(
            transport, "GET", PREFIX + f"/operation-approvals/{saved.approval_id}"
        )
    require(
        ticket.get("plan_digest") == saved.revision
        and isinstance(ticket.get("request"), dict)
        and ticket["request"].get("contract_version") == "v1"
        and ticket["request"].get("parameters") == approval_request["parameters"]
        and ticket.get("envelope")
        == {
            key: int(approval_request["parameters"][key])
            for key in ("max_resource_units", "max_cost_micros", "max_runtime_seconds")
        }
    )
    decision = _approval(ticket, replace(selected, request_id=request), saved, clock())
    store.save(saved)
    if decision == "pending":
        return {
            "status": "BLOCKED",
            "reason": "awaiting independent continuation approval",
            "phase": phase,
            "request_ref": reference(request),
            "approval_ref": reference(saved.approval_id),
        }
    attempted = False

    def mark_submitted():
        nonlocal saved, attempted
        attempted = True
        state = replace(saved, submitted=True)
        store.save(state)
        saved = state

    try:
        status, response = transport.request(
            "POST",
            route + "/continue",
            {"operation_id": request, "approval_id": saved.approval_id},
            before_send=mark_submitted,
        )
        require(status in (200, 201) and isinstance(response, dict))
        require(
            response.get("request_id") == request
            and response.get("workspace_id") == original.workspace_id
            and response.get("phase") == phase
        )
        require(
            identifier(
                response.get("provisioning_operation_id"), "continuation operation"
            )
            != source
        )
    except (EvidenceError, OSError, RuntimeError, ValueError):
        if attempted and not saved.submitted:
            raise EvidenceError(
                "continuation: checkpoint write uncertain; retain phase checkpoint for reconciliation"
            ) from None
        return {
            "status": "BLOCKED",
            "reason": "continuation reply uncertain; recover same request"
            if saved.submitted
            else "continuation not sent; retry after pre-send checks succeed",
        }
    return {
        "status": "BLOCKED",
        "reason": "continuation submitted; execution and readiness unverified",
        "phase": phase,
        "request_ref": reference(request),
        "submission_observed": True,
    }
