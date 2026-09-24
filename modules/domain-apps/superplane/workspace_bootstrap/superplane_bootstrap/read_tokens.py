"""Hashed bootstrap read-token issuance under the original shared execution grant."""

from contextlib import asynccontextmanager
from hashlib import sha256
import json
import secrets


class ReadTokenRefused(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def token_digest(token):
    return sha256(b"superplane-bootstrap-read:v1:" + token.encode()).hexdigest()


async def issue_bootstrap_read_token(*, connect, grant, domain_connect=None):
    """Trusted producer only; the raw token is returned once and never stored.

    A lost response is recoverable by issuing a replacement under the same live
    lease and claim. Upsert revokes the previous hash atomically. Callers must
    deliver this result over the private authenticated worker channel.
    """
    from harness_jobs.execution_rpc import ExecutionGrant
    from harness_jobs.leases import lock_lease
    from superplane_bootstrap.state import claim_fingerprint

    if not isinstance(grant, ExecutionGrant):
        raise ReadTokenRefused(403, "A current shared execution grant is required")
    lease = grant.lease
    async with connect() as connection, connection.transaction():
        if not await lock_lease(connection, lease):
            raise ReadTokenRefused(403, "Bootstrap execution lease is stale")
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
            raise ReadTokenRefused(403, "Bootstrap operation is unavailable")
        current = await connection.fetchrow(
            "SELECT expires_at,runtime_deadline FROM harness_operation_leases WHERE operation_id=$1",
            lease.operation_id,
        )
        expiry = min(current["expires_at"], current["runtime_deadline"])
        async with _domain(connection, domain_connect) as domain:
            reservation = await domain.fetchrow(
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
                raise ReadTokenRefused(
                    409, "No active bootstrap reservation matches the grant"
                )
            identity = json.loads(reservation["identity_json"])
            claim = claim_fingerprint(reservation["attempt_token"])
            if (
                identity.get("workspace_id") != lease.workspace_id
                or claim != reservation["claim"]
            ):
                raise ReadTokenRefused(409, "Bootstrap reservation identity differs")
            token = "sp-bootstrap-read-" + secrets.token_urlsafe(32)
            await domain.execute(
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


@asynccontextmanager
async def _domain(shared, connect):
    if connect is None:
        yield shared
    else:
        async with connect() as domain, domain.transaction():
            yield domain
