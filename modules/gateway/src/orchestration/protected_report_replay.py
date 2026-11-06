"""Restore an existing reporting capability only in a replay's transport envelope."""

from datetime import UTC

from src.shared.models.base import utcnow

from .run_reports import OrchestrationRunReport, prepare_run_report


async def restore_report_envelope(session, envelope):
    """Recover the original credential without persisting it or creating authority.

    Public dispatch decisions intentionally omit reporting credentials. Protected
    bootstrap digests cover the complete transport envelope, so replay must restore
    the same capability before comparing that digest or provisioning an unstarted
    dispatch. Existing assignment, signing-key and protected-start fences remain
    authoritative; this helper never creates a missing report assignment.
    """
    row = await session.get(OrchestrationRunReport, envelope["message_id"])
    if row is None:
        return envelope
    if row.worker_receipt is not None or row.terminal_receipt is not None:
        return None
    graph, source = envelope["orchestration"], envelope["source_ref"]
    expiry = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
    if (
        expiry <= utcnow()
        or row.org_id != envelope["tenant_id"]
        or row.flow_id != graph["flow_id"]
        or row.node_id != graph["node_id"]
        or row.attempt != graph["attempt"]
        or row.persona != envelope["persona"]
        or row.repo != source["repo"]
        or row.installation_id != source["installation_id"]
        or row.provider_repository_id != source.get("provider_repository_id")
        or {key: value for key, value in row.dispatch_metadata.items() if key != "report_nonce"} != envelope
    ):
        raise ValueError("protected_dispatch_report_changed")
    restored = dict(envelope)
    # The existing-row path reconstructs and verifies the original credential's
    # hash. It neither inserts a row nor stores a raw credential in SQL.
    await prepare_run_report(session, restored)
    return restored
