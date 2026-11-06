"""Fail-closed SQL holds for Product snapshots and run-specific source packages."""

from datetime import UTC, datetime

from sqlalchemy import select

from l5_services.models.source_retention import SourceRetention


class RetentionError(ValueError):
    pass


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RetentionError("aware retention time required")
    return value.astimezone(UTC)


def _snapshot_expiry(snapshot):
    if snapshot is None:
        return None
    try:
        return _utc(datetime.fromisoformat(snapshot["expires_at"].replace("Z", "+00:00")))
    except (TypeError, ValueError, AttributeError, KeyError) as error:
        raise RetentionError("pinned snapshot expiry required") from error


async def pin_source_hold(session, run, artifact_ref, *, snapshot=None, required_until, policy_expires_at,
                          now, reason="remote_generation"):
    """Reserve a scoped source hold in the run transaction before native Start."""
    required_until = _utc(required_until)
    policy_expires_at = _utc(policy_expires_at)
    now = _utc(now)
    expiry = _snapshot_expiry(snapshot)
    if (run.service_id != "well_architected" or not isinstance(artifact_ref, dict)
            or not artifact_ref.get("artifact_id") or not artifact_ref.get("version")
            or not artifact_ref.get("sha256") or len(artifact_ref["sha256"]) != 64
            or reason != "remote_generation" or required_until <= now
            or policy_expires_at < required_until or expiry is not None and expiry < required_until
            or snapshot is not None and artifact_ref not in (snapshot.get("content"), snapshot.get("file_manifest"))):
        raise RetentionError("mandatory source retention cannot be met before Start")
    existing = await session.get(SourceRetention, (run.tenant_id, artifact_ref["artifact_id"], run.run_id))
    if existing is not None:
        if (existing.artifact_ref != artifact_ref or _utc(existing.required_until) != required_until
                or _utc(existing.policy_expires_at) != policy_expires_at):
            raise RetentionError("pinned source hold conflicts with existing run")
        return existing
    hold = SourceRetention(tenant_id=run.tenant_id, artifact_id=artifact_ref["artifact_id"],
                           run_id=run.run_id, project_id=run.project_id, artifact_ref=artifact_ref,
                           required_until=required_until, policy_expires_at=policy_expires_at,
                           reason=reason)
    session.add(hold)
    await session.flush()
    return hold


def revoke_source_hold(hold, *, now):
    """Deny future reuse without deleting bytes remote work may still need."""
    if hold.released_at is None and hold.revoked_at is None:
        hold.revoked_at = _utc(now)


def source_reuse_allowed(hold, *, now):
    return hold.revoked_at is None and hold.released_at is None and _utc(now) < _utc(hold.policy_expires_at)


def release_source_hold(hold, run, proof, *, now):
    """Record native terminal proof; local cancellation/expiry cannot release a hold."""
    now = _utc(now)
    native = run.native_job_ref or {}
    if (hold.tenant_id != run.tenant_id or hold.run_id != run.run_id
            or hold.project_id != run.project_id or hold.released_at is not None
            or not isinstance(proof, dict)
            or proof.get("tenant_id") != run.tenant_id or proof.get("run_id") != run.run_id
            or not native.get("generation_id") or proof.get("generation_id") != native["generation_id"]
            or proof.get("provider_state") not in {"completed", "error", "cancelled"}
            or not isinstance(proof.get("revision"), int) or proof["revision"] < 1):
        raise RetentionError("matching remote terminal proof required")
    hold.last_evidence = proof
    hold.released_at = now
    return {"artifact": hold.artifact_ref, "run_id": run.run_id, "released_at": now.isoformat(),
            "proof_revision": proof["revision"]}


async def source_cleanup_allowed(session, tenant_id, artifact_ref, *, now):
    """Fail closed if any run still holds this exact immutable artifact version."""
    now = _utc(now)
    if (not isinstance(artifact_ref, dict) or not artifact_ref.get("artifact_id")
            or not artifact_ref.get("version")):
        return False
    result = await session.execute(select(SourceRetention).where(
        SourceRetention.artifact_id == artifact_ref["artifact_id"],
    ).with_for_update())
    holds = result.scalars().all()
    if any(not isinstance(hold.artifact_ref, dict) or not hold.artifact_ref.get("version")
           or hold.artifact_ref.get("artifact_id") != artifact_ref["artifact_id"] for hold in holds):
        return False
    holds = [hold for hold in holds if hold.artifact_ref["version"] == artifact_ref["version"]]
    return any(hold.tenant_id == tenant_id for hold in holds) and all(
        hold.artifact_ref == artifact_ref and hold.released_at is not None
        and _utc(hold.required_until) <= now for hold in holds
    )
