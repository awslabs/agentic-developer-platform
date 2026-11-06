"""Genuine local-human approval endpoints; no fixture or external issuer proof."""

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OperatorLocalApproval, OperatorLocalApprovalRequest
from src.agentauth.native_test_routes import NO_STORE, CapabilityRevocation, body, enrollment, refused
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.auth.middleware import get_cognito_user_context
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/operator-tools", tags=["operator-tool-admission"])


def reference(approval):
    return {
        "producer": "operator_local",
        "run_id": approval.run_id,
        "tenant_id": approval.principal.tenant_id,
        "subject_id": approval.principal.subject_id,
        "approval_revision": approval.revision,
        "deployment_id": approval.deployment_id,
        "source_revision": approval.source_revision,
        "contract_sha256": approval.contract_sha256,
        "gateway_origin": approval.gateway_origin,
    }


@router.post("/approvals")
async def approve(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, OperatorLocalApprovalRequest)
    try:
        approval = await enrollment(request).approve_operator(context, request=parsed)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse(
        {
            "reference": reference(approval),
            "scope": "operator_local",
            "cap_usd": "20",
            "expires_at": approval.dispatch_expires_at,
            "run_started_at": approval.approved_at,
        },
        headers=NO_STORE,
    )


@router.post("/capabilities/exchange")
async def exchange(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, CapabilityRevocation)
    try:
        issuer = enrollment(request)
        token = await issuer.exchange_operator(context, run_id=parsed.run_id)
        approval = await run_in_threadpool(issuer.store.approval, context.org_id, parsed.run_id)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse(
        {"capability": token, "scope": "operator_local", "all_in_admission": "required", "reference": reference(approval)}, headers=NO_STORE
    )


@router.post("/capabilities/revoke")
async def revoke(request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    parsed = await body(request, CapabilityRevocation)
    try:
        issuer = enrollment(request)
        approval = await run_in_threadpool(issuer.store.approval, context.org_id, parsed.run_id)
        if type(approval) is not OperatorLocalApproval:
            raise AdmissionRefusedError("native_test_producer_refused")
        await issuer.revoke(context, run_id=parsed.run_id)
    except (AdmissionRefusedError, BootstrapRefusedError) as exc:
        return refused(exc)
    return JSONResponse({"run_id": parsed.run_id, "revoked": True}, headers=NO_STORE)


@router.get("/runs/{run_id}/status")
async def run_status(run_id: str, request: Request, context: TokenContext = Depends(get_cognito_user_context)):
    from src.agentauth.native_test_lifecycle import NativeTestLifecycle

    try:
        issuer = enrollment(request)
        lifecycle = request.app.state.native_test_runtime.tool_lifecycle
        values = request.headers.getlist(CAPABILITY_HEADER)
        if type(lifecycle) is not NativeTestLifecycle or lifecycle.enrollment is not issuer or len(values) != 1:
            raise AdmissionRefusedError("native_test_lifecycle_unavailable")
        approval = await run_in_threadpool(issuer.store.approval, context.org_id, run_id)
        if type(approval) is not OperatorLocalApproval:
            raise AdmissionRefusedError("native_test_producer_refused")
        value = await lifecycle.status(context=context, credential=values[0], run_id=run_id)
    except AdmissionRefusedError as exc:
        return refused(exc)
    return JSONResponse(value, headers=NO_STORE)
