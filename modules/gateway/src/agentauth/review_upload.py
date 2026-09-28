"""Turning a reviewer's uploaded review result into recorded evidence (#5146).

The join between two things that already existed and had nothing between them:
the authenticated own-run artifact transport (#5513), which stores bytes a worker
uploads under a server-derived key, and :mod:`src.orchestration.review_ingest`,
which resolves protected state and records validated evidence in the ledger.

## Why this is a hook on the artifact upload rather than a new endpoint

A review result *is* an own-run artifact. It is produced by one run, it is
immutable once written, and the key it lands under is exactly the
server-derived prefix that makes an artifact reference verifiable — which is
precisely what :func:`review_ingest.verified_artifact_refs` checks. Giving it a
second endpoint would mean a second authorization model for the same bytes, and
the review path would then be the one surface where "the document the server
stored" and "the document the server validated" could differ.

So the document is stored first and observed second, in one request, under one
credential. If the observation refuses, the stored document survives: an attempted
review that was refused is itself a fact an operator needs, and discarding it
would leave a reviewer with a refusal it cannot evidence.

## Why this is a separate module from `artifact_service`

The same reason ``pr_binding_routes`` is separate from ``registration_routes``:
``artifact_service`` is a storage route that holds no database session and reaches
no orchestration state, and every other kind it handles keeps that property. This
is the one kind that opens a transaction, reads protected engine state and writes
to the ledger. Keeping it here means that authorization is reviewed on its own
terms rather than inherited by every future artifact kind.

## What is read from where

Nothing about the review's subject comes from the request. The upload carries the
document; every value it is *checked against* comes from the reviewer's
server-written execution record:

* ``org_id`` — the credential's tenant.
* ``node_id`` / ``attempt`` — ``orchestration_node_id`` and
  ``orchestration_node_attempt``, written by :mod:`src.agentauth.dispatch` from the
  server-composed ``GraphAssignment``, never by the worker. This is the only
  protected link from a reviewer's invocation to the story it was dispatched for:
  a reviewer's run id is minted by delegated dispatch and is deliberately not
  derivable from the node, so ``resolve_registration_target`` — the developer's
  resolver — raises ``UNKNOWN_RUN`` for it.
* ``reviewer_run_id`` — the authenticated invocation id.
* ``installation_id`` — the dispatched installation, so the provider reads happen
  under the tenant's own grant.
* ``own_artifact_prefix`` — :func:`artifact_keys.artifact_prefix` of the same
  record, a digest of tenant and invocation plus the attempt. A worker cannot
  choose it, which is what makes "this reference is under my own prefix" a fact
  rather than a claim.

## Refusals

A caller that is not a dispatched reviewer gets the storage route's ordinary 404,
indistinguishable from every other authorization failure: a caller able to tell
"you are not a reviewer" from "that run does not exist" learns about runs it does
not own. A *validation* refusal is different — it is reachable only after the
caller authenticated as itself, and the arm is the entire diagnostic value, so it
is returned with the receipt as a stable ``ReviewEvidenceRefusal`` code for the
reviewer to report. That mirrors ``pr_binding_routes``' split between 404 for
authorization and a coded response for a genuine domain refusal.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agentauth.artifact_keys import artifact_prefix

logger = logging.getLogger(__name__)

#: The artifact kind carrying a review result. Named for the contract it must
#: satisfy rather than for the producer, because the gateway validates it against
#: ``contracts/orchestration-review/v1`` and a kind named after a persona would
#: invite a second, differently-shaped "reviewer" upload later.
REVIEW_RESULT_KIND = "review-result"

__all__ = ["REVIEW_RESULT_KIND", "ReviewUploadRefusedError", "observe_review_upload"]


class ReviewUploadRefusedError(Exception):
    """A review upload that authenticated but could not be recorded.

    Carries the typed arm and its operator-facing prose. Deliberately distinct from
    the authorization failures around it: those must all look identical to the
    caller, and this one must not, because the reviewer has to know whether to fix
    its document, retry its publication, or stop.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _reviewer_assignment(record: Any, execution: dict) -> tuple[str, int, int]:
    """The story, attempt and installation this reviewer was dispatched for.

    Read from the DynamoDB execution item the gateway wrote at reservation time.
    Raises :class:`KeyError`/:class:`ValueError` on anything missing or unusable,
    which the caller turns into the same 404 as every other authorization failure —
    an execution without an engine assignment is not a dispatched reviewer, and
    saying so would leak which runs exist.
    """
    node_id = execution["orchestration_node_id"]["S"]
    attempt = int(execution["orchestration_node_attempt"]["N"])
    installation_id = int(execution["installation_id"]["N"])
    persona = execution["persona"]["S"]
    if persona not in {"reviewer", "agent-codex-reviewer"} or not node_id or attempt < 1 or installation_id < 1:
        # Only a dispatched reviewer may file review evidence. A developer run
        # uploading this kind would be recording evidence about its own work, which
        # `review_ingest` would refuse as SELF_REVIEW anyway — refused here as well
        # so the narrower fact is enforced by the transport rather than relying on a
        # downstream check to hold.
        raise ValueError("not a dispatched reviewer")
    return node_id, attempt, installation_id


async def observe_review_upload(
    record: Any, execution: dict, *, document: dict[str, Any], reverify: Any, stored_artifact_ref: str, resolve_artifact_ref=None
) -> dict[str, Any]:
    """Record the uploaded review result as evidence, or refuse with its arm.

    ``record`` is the authenticated :class:`ExecutionRecord` and ``execution`` the
    protected DynamoDB item for the same invocation. ``document`` is the parsed
    upload — untrusted, and the only argument that came from the caller.

    ``reverify`` is an awaitable the caller supplies to re-check its credential. It
    is awaited **inside this session, immediately before the commit**, which is the
    ordering ``registration_routes`` established: the authority was verified before
    a slow dependency (here a provider read and several queries), so it must be
    verified again before those effects become durable. A credential revoked or
    superseded mid-request therefore leaves nothing written. Taking it as a
    parameter rather than letting the caller re-check after this returns is
    deliberate — by then the commit has already happened, and "we checked
    afterwards" is not the same guarantee.

    Returns:
        A small receipt: whether evidence was recorded, and the artifact reference
        it was recorded under. No document content, no findings prose.

    Raises:
        ReviewUploadRefusedError: on a typed validation refusal.
        ValueError/KeyError: when the caller is not a dispatched reviewer, which the
            transport converts to its ordinary 404.
        Whatever ``reverify`` raises, uncommitted.
    """
    from src.orchestration.review_ingest import ingest_review_result
    from src.shared.database import get_session_factory

    node_id, attempt, installation_id = _reviewer_assignment(record, execution)

    async with get_session_factory()() as session:
        outcome = await ingest_review_result(
            session,
            document=document,
            org_id=record.tenant_id,
            node_id=node_id,
            attempt=attempt,
            # The authenticated invocation, never a field in the document. A
            # substituted reviewer id is refused by the validator against this.
            reviewer_run_id=record.invocation_id,
            installation_id=installation_id,
            # The same prefix this upload was stored under, so a review citing its
            # own uploaded evidence verifies and one citing another run's does not.
            own_artifact_prefix=artifact_prefix(record),
            stored_artifact_ref=stored_artifact_ref,
            resolve_artifact_ref=resolve_artifact_ref,
        )
        if outcome.refusal is not None:
            # Nothing to commit: `ingest_review_result` writes only on success. The
            # session is closed without committing rather than rolled back
            # explicitly, which is the same thing here and keeps the refusal path
            # free of a write it never made.
            logger.info(
                "review upload refused invocation=%s node=%s attempt=%s reason=%s",
                record.invocation_id,
                node_id,
                attempt,
                outcome.refusal.value,
            )
            raise ReviewUploadRefusedError(outcome.refusal.value, outcome.detail or "")
        if not outcome.recorded:
            # Validated, but the ledger declined the write — a claim generation that
            # advanced mid-request, or a conflicting settled action. Not the
            # reviewer's defect and not a success either, so it must not be reported
            # as recorded: a caller told "recorded" would stop retrying and the
            # evidence would exist nowhere.
            logger.warning(
                "review evidence not persisted invocation=%s node=%s attempt=%s ledger=%s",
                record.invocation_id,
                node_id,
                attempt,
                getattr(outcome.ledger, "reason", None),
            )
            raise ReviewUploadRefusedError("not_recorded", "The review evidence was valid but could not be persisted; retry.")
        # Last thing before durability. The authority was checked before a provider
        # read and several queries; a grant withdrawn in that window must not leave a
        # committed ledger row behind.
        await reverify()
        await session.commit()

    return {"recorded": True, "evidence_ref": outcome.evidence.artifact_ref}
