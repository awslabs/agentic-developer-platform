"""Resolve the immutable accepted document for a commissioned amendment author."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .compile import plan_hash
from .models import AmendmentRequestState, OrchestrationAmendmentRequest
from .pending_amendments import AmendmentRequest, in_force_plan
from .proposal import LoopProposal

MAX_BASE_DOCUMENT_BYTES = 128 * 1024


class AuthoringInputError(ValueError):
    """Stable refusal code; never includes plan content or credentials."""


async def resolve_authoring_input(session: AsyncSession, *, org_id: str, request: AmendmentRequest, author_run_id: str) -> dict:
    row = (
        await session.execute(
            select(OrchestrationAmendmentRequest).where(
                OrchestrationAmendmentRequest.org_id == org_id,
                OrchestrationAmendmentRequest.id == request.id,
            )
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.state != AmendmentRequestState.QUEUED.value
        or row.flow_id != request.flow_id
        or row.replan_decision_id != request.replan_decision_id
        or row.base_plan_version != request.base_plan_version
        or row.base_plan_hash != request.base_plan_hash
        or row.author_run_id != request.author_run_id
        or row.requested_by != request.requested_by
        or row.request_text != request.request_text
        or (row.author_run_id is not None and row.author_run_id != author_run_id)
    ):
        raise AuthoringInputError("authoring_input_binding_mismatch")

    accepted = await in_force_plan(session, org_id=org_id, flow_id=row.flow_id)
    if accepted is None or row.base_plan_version is None or not row.base_plan_hash:
        raise AuthoringInputError("authoring_input_base_missing")
    if accepted.version != row.base_plan_version or accepted.plan_hash != row.base_plan_hash:
        raise AuthoringInputError("authoring_input_base_stale")
    try:
        proposal = LoopProposal.model_validate(accepted.plan_document)
        if plan_hash(proposal) != accepted.plan_hash:
            raise AuthoringInputError("authoring_input_base_hash_mismatch")
        content = json.dumps(accepted.plan_document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        if isinstance(error, AuthoringInputError):
            raise
        raise AuthoringInputError("authoring_input_base_invalid") from None
    if len(content) > MAX_BASE_DOCUMENT_BYTES:
        raise AuthoringInputError("authoring_input_base_too_large")
    return {
        "version": 1,
        "org_id": org_id,
        "flow_id": row.flow_id,
        "request_id": row.id,
        "author_run_id": author_run_id,
        "base_plan_version": accepted.version,
        "base_plan_hash": accepted.plan_hash,
        "document_sha256": hashlib.sha256(content).hexdigest(),
        # A detached copy: post-commit publication cannot observe later ORM edits.
        "document": json.loads(content),
    }
