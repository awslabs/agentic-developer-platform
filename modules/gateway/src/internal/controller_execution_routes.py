"""Read one owned execution grant using the existing ADP run/workload verifier.

This endpoint neither admits operations nor acquires leases. A trusted Superplane
executor uses the answer to compose ExecutionRPCServer; the Go worker receives
only that server's scoped socket token. Every read verifies current IAM registry
scope, the MAC-bound run, TokenReview-bound pod and durable paid execution lease.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.operation_contract import identity_contract
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.domain_operation_runtime import authenticated
from src.internal.domain_operation_store import operation_session
from src.internal.vault_evidence_routes import _granted_permissions, _verified_executor


async def get_operation_db(request: Request, _: None = Depends(verify_internal_or_irsa)):
    binding, *_ = await authenticated(request, mode="execution")
    async with operation_session(binding) as session:
        yield session


router = APIRouter(prefix="/internal/v1/controller-execution", tags=["controller-execution"])


class ExecutionAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=255)


@router.post("/authority")
async def execution_authority(
    body: ExecutionAuthorityRequest,
    request: Request,
    db: AsyncSession = Depends(get_operation_db),
    _: None = Depends(verify_internal_or_irsa),
):
    if "workspace:provision" not in _granted_permissions(request):
        raise HTTPException(403, "execution authority refused")
    holder, org_id = await _verified_executor(request)
    try:
        result = await db.execute(
            text("""
            SELECT o.operation_id, o.org_id, o.workspace_id, o.job_id,
                   o.request_payload, o.plan_digest, l.holder, l.attempt_id,
                   l.fence_token, l.expires_at, l.acquired_at, l.runtime_deadline,
                   l.attempts, l.max_attempts, a.reservation_state,
                   a.max_resource_units, a.max_runtime_seconds, a.max_cost_micros
              FROM harness_operations o
              JOIN harness_operation_leases l ON l.operation_id=o.operation_id
              JOIN harness_approval_consumption a ON a.operation_id=o.operation_id
             WHERE o.operation_id=:operation_id AND o.org_id=:org_id
               AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id
               AND l.holder=:holder AND l.closed_at IS NULL
               AND l.expires_at > clock_timestamp()
               AND l.runtime_deadline > clock_timestamp()
               AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id
               AND a.plan_digest=o.plan_digest
               AND a.reservation_state IN ('confirmed', 'retained')
            """),
            {"operation_id": body.operation_id, "org_id": org_id, "holder": holder},
        )
        row = result.mappings().one_or_none()
    except SQLAlchemyError:
        raise HTTPException(503, "execution authority unavailable") from None
    if row is None:
        raise HTTPException(403, "execution authority refused")
    try:
        contract = identity_contract()
        admitted = contract.decode_payload(row["request_payload"])
        if contract.payload_digest(admitted) != row["plan_digest"]:
            raise ValueError("digest mismatch")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(403, "execution authority refused") from None
    except RuntimeError:
        raise HTTPException(503, "execution contract unavailable") from None
    # The database read may have waited. Revoke a response if its real run/pod
    # changed during that wait, before returning any admitted plan metadata.
    if await _verified_executor(request) != (holder, org_id) or "workspace:provision" not in _granted_permissions(request):
        raise HTTPException(403, "execution authority refused")
    if min(row["expires_at"], row["runtime_deadline"]) <= datetime.now(UTC):
        raise HTTPException(403, "execution authority refused")
    return {"version": 1, **dict(row)}


from src.internal.domain_operation_binding_proof_routes import router as binding_router  # noqa: E402
from src.internal.domain_operation_routes import router as domain_router  # noqa: E402

for domain_route in (*domain_router.routes, *binding_router.routes):
    router.routes.append(domain_route)
