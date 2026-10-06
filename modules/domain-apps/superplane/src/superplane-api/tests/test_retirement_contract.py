"""Review the prospective receipt without claiming retirement is available."""

import uuid

import pytest
from app.routers.retirement import (
    RetirementAdmission,
    RetirementAdmissionResponse,
    RetirementPreview,
    RetirementReviewResponse,
    router,
)
from pydantic import ValidationError


def test_retirement_http_contract_keeps_approval_and_idempotency_explicit():
    paths = {route.path: route for route in router.routes}
    preview = paths["/workspaces/{workspace_id}/retirement/preview"]
    admit = paths["/workspaces/{workspace_id}/retirement"]
    assert preview.body_field.field_info.annotation is RetirementPreview
    assert preview.response_model is RetirementReviewResponse
    assert admit.body_field.field_info.annotation is RetirementAdmission
    assert admit.response_model is RetirementAdmissionResponse
    identifier = str(uuid.uuid4())
    assert RetirementPreview.model_validate({"operation_id": identifier}).operation_id
    with pytest.raises(ValidationError):
        RetirementAdmission.model_validate({"operation_id": identifier})
    with pytest.raises(ValidationError):
        RetirementAdmission.model_validate(
            {
                "operation_id": identifier,
                "plan_revision": "a" * 64,
                "approval_id": "explicit-approval",
                "allocation_id": "caller-selected",
            }
        )


def test_admission_receipt_cannot_claim_terminal_cleanup():
    receipt = {
        "request_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "phase": "retire-workspace",
        "state": "admitted",
        "retryable": False,
        "retirement_complete": False,
        "original_allocation_id": "original",
        "control_allocation_id": "control",
    }
    assert (
        RetirementAdmissionResponse.model_validate(receipt).retirement_complete is False
    )
    with pytest.raises(ValidationError):
        RetirementAdmissionResponse.model_validate(
            {**receipt, "retirement_complete": True}
        )
