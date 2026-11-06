"""Native model-test approval/exchange/revocation using actual human JWT auth.

No route designates a fixture, creates consent or installs a personal credential
in CI. The exchange returns its opaque capability once with no-store headers.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.native_test_enrollment import AllInApprovalRequest, ApprovalRequest, Contract, Name, Uuid
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.auth.middleware import get_cognito_user_context
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/native-tests", tags=["native-test-admission"])
MAX_REQUEST = 65536
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class OwnerApproval(Contract):
    subject_id: Name
    approval: ApprovalRequest | AllInApprovalRequest


class CapabilityExchange(Contract):
    run_id: Uuid
    github_oidc_token: str = Field(min_length=1, max_length=16384, repr=False)
    producer_proof: str = Field(min_length=1, max_length=12000, repr=False)


class CapabilityRevocation(Contract):
    run_id: Uuid


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def body(request, model):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST:
            raise HTTPException(413, "native test request too large", headers=NO_STORE)
    try:
        return model.model_validate(json.loads(raw, object_pairs_hook=unique_object))
    except (ValueError, TypeError, ValidationError):
        # Do not expose validation inputs: exchange fields contain credentials.
        raise HTTPException(400, "invalid native test request", headers=NO_STORE) from None


def enrollment(request):
    from src.agentauth.native_test_runtime import NativeTestRuntime

    runtime = getattr(request.app.state, "native_test_runtime", None)
    if type(runtime) is not NativeTestRuntime:
        raise AdmissionRefusedError("native_test_runtime_unavailable")
    return runtime.require_enrollment()


def refused(exc):
    reason = str(exc) if isinstance(exc, AdmissionRefusedError) else "native_test_authority_refused"
    return JSONResponse({"error": reason, "retryable": False}, status_code=403, headers=NO_STORE)


@router.post("/approvals")
async def approve(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, OwnerApproval)
    try:
        approval = await enrollment(request).approve(context, subject_id=parsed.subject_id, request=parsed.approval)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse(
        {
            "run_id": approval.run_id,
            "approval_revision": approval.revision,
            "scope": approval.admission_scope,
            "cap_usd": "20",
            "expires_at": approval.dispatch_expires_at,
            **({"run_started_at": approval.approved_at} if approval.admission_scope == "all_in" else {}),
        },
        headers=NO_STORE,
    )


@router.post("/capabilities/exchange")
async def exchange(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, CapabilityExchange)
    try:
        issuer = enrollment(request)
        token = await issuer.exchange(context, **parsed.model_dump())
        # Derive scope from the same immutable approval the issuer consumed.
        from starlette.concurrency import run_in_threadpool

        approval = await run_in_threadpool(issuer.store.approval, context.org_id, parsed.run_id)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse(
        {
            "capability": token,
            "scope": approval.admission_scope,
            "all_in_admission": "required" if approval.admission_scope == "all_in" else "unavailable",
        },
        headers=NO_STORE,
    )


@router.post("/capabilities/revoke")
async def revoke(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, CapabilityRevocation)
    try:
        await enrollment(request).revoke(context, run_id=parsed.run_id)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse({"run_id": parsed.run_id, "revoked": True}, headers=NO_STORE)


@router.get("/runs/{run_id}/status")
async def run_status(run_id: str, request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    from src.agentauth.native_test_enrollment import CAPABILITY_HEADER
    from src.agentauth.native_test_lifecycle import NativeTestLifecycle
    from src.agentauth.native_test_runtime import NativeTestRuntime

    try:
        runtime = getattr(request.app.state, "native_test_runtime", None)
        values = request.headers.getlist(CAPABILITY_HEADER)
        if (
            type(runtime) is not NativeTestRuntime
            or type(runtime.tool_lifecycle) is not NativeTestLifecycle
            or runtime.tool_lifecycle.enrollment is not runtime.enrollment
            or len(values) != 1
        ):
            raise AdmissionRefusedError("native_test_lifecycle_unavailable")
        value = await runtime.tool_lifecycle.status(context=context, credential=values[0], run_id=run_id)
    except AdmissionRefusedError as exc:
        return refused(exc)
    return JSONResponse(value, headers=NO_STORE)
