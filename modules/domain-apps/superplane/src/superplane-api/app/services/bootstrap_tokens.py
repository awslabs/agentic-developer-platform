"""Issue a read capability from an actual held shared grant and bootstrap claim."""

from datetime import UTC, datetime
from hashlib import sha256
import json
import secrets

from fastapi import HTTPException
from sqlalchemy import select

from app.models.bootstrap import WorkspaceBootstrapReadToken
from app.services.bootstrap_observation import operation_connect
from app.services.leases import _as_utc


def token_digest(token):
    return sha256(b"superplane-bootstrap-read:v1:" + token.encode()).hexdigest()


async def issue_bootstrap_read_token(*, connect, grant):
    """Trusted producer only; the raw token is returned once and never stored.

    A lost response is recoverable by issuing a replacement under the same live
    lease and claim. Upsert revokes the previous hash atomically. Callers must
    deliver this result over the private authenticated worker channel.
    """
    from harness_jobs.execution_rpc import ExecutionGrant
    from harness_jobs.leases import lock_lease
    from superplane_bootstrap.state import claim_fingerprint

    if not isinstance(grant, ExecutionGrant):
        raise HTTPException(403, "A current shared execution grant is required")
    lease = grant.lease
    async with connect() as connection, connection.transaction():
        if not await lock_lease(connection, lease):
            raise HTTPException(403, "Bootstrap execution lease is stale")
        operation = await connection.fetchrow(
            "SELECT action, state, cancel_requested_at FROM harness_operations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
        if (
            operation is None
            or operation["action"] != "provision"
            or operation["state"] not in {"pending", "running", "retrying"}
            or operation["cancel_requested_at"] is not None
        ):
            raise HTTPException(403, "Bootstrap operation is unavailable")
        reservation = await connection.fetchrow(
            "SELECT r.identity_json, r.attempt_token, a.claim FROM workspace_bootstrap_reservations r "
            "JOIN workspace_bootstrap_authority a ON a.workspace_id=r.workspace_id "
            "JOIN organizations o ON (a.org_id=o.id::text OR a.org_id=o.adp_org_id) "
            "WHERE r.workspace_id=$1 AND r.state='reserved' AND a.operation_id=$2 "
            "AND o.id::text=$3 AND a.claim=encode(sha256($4::bytea || convert_to(r.attempt_token,'UTF8')),'hex') "
            # Readiness is checked again after installer revocation, before
            # canonical publication. Read-only observation still belongs to the
            # same retained claim and current shared lease at that point.
            "AND (a.progress_json::jsonb->>'phase'='active' OR "
            "(a.revoked=true AND a.progress_json::jsonb->>'phase'='revoked' "
            "AND a.progress_json::jsonb->'retain_workspace'='true'::jsonb)) FOR UPDATE OF r,a",
            lease.workspace_id,
            lease.operation_id,
            lease.org_id,
            b"superplane-workspace-bootstrap-claim:v1:",
        )
        if reservation is None:
            raise HTTPException(
                409, "No active bootstrap reservation matches the grant"
            )
        identity = json.loads(reservation["identity_json"])
        claim = claim_fingerprint(reservation["attempt_token"])
        if (
            identity.get("workspace_id") != lease.workspace_id
            or claim != reservation["claim"]
        ):
            raise HTTPException(409, "Bootstrap reservation identity differs")
        token = "sp-bootstrap-read-" + secrets.token_urlsafe(32)
        current = await connection.fetchrow(
            "SELECT expires_at,runtime_deadline FROM harness_operation_leases WHERE operation_id=$1",
            lease.operation_id,
        )
        expiry = min(current["expires_at"], current["runtime_deadline"])
        await connection.execute(
            "INSERT INTO workspace_bootstrap_read_tokens "
            "(workspace_id,org_id,operation_id,registration_claim,token_hash,lease_holder,lease_attempt_id,lease_fence_token,expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(workspace_id) DO UPDATE SET "
            "org_id=EXCLUDED.org_id,operation_id=EXCLUDED.operation_id,registration_claim=EXCLUDED.registration_claim,"
            "token_hash=EXCLUDED.token_hash,lease_holder=EXCLUDED.lease_holder,lease_attempt_id=EXCLUDED.lease_attempt_id,"
            "lease_fence_token=EXCLUDED.lease_fence_token,expires_at=EXCLUDED.expires_at",
            lease.workspace_id,
            lease.org_id,
            lease.operation_id,
            claim,
            token_digest(token),
            lease.holder,
            lease.attempt_id,
            lease.fence_token,
            expiry,
        )
    return token


async def authenticate_bootstrap_token(request, db):
    raw = request.headers.get("authorization", "")
    if not raw.startswith("sp-bootstrap-read-") or len(raw) > 256:
        raise HTTPException(401, "Bootstrap read credential required")
    token = await db.scalar(
        select(WorkspaceBootstrapReadToken)
        .where(WorkspaceBootstrapReadToken.token_hash == token_digest(raw))
        .execution_options(populate_existing=True)
    )
    if token is None or _as_utc(token.expires_at) <= datetime.now(UTC):
        raise HTTPException(401, "Bootstrap read credential is unavailable")
    async with operation_connect(request)() as connection:
        current = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM harness_operation_leases l JOIN harness_operations o USING(operation_id) "
            "WHERE l.operation_id=$1 AND l.org_id=$2 AND l.workspace_id=$3 "
            "AND l.holder=$4 AND l.attempt_id=$5 AND l.fence_token=$6 AND l.closed_at IS NULL "
            "AND l.expires_at>clock_timestamp() AND l.runtime_deadline>clock_timestamp() "
            "AND o.cancel_requested_at IS NULL AND o.action='provision' AND o.state IN ('pending','running','retrying'))",
            token.operation_id,
            token.org_id,
            token.workspace_id,
            token.lease_holder,
            token.lease_attempt_id,
            token.lease_fence_token,
        )
    if not current:
        raise HTTPException(
            401, "Bootstrap read credential has a stale execution lease"
        )
    return token
