"""Deployment policy is explicit, defaults to streamlined, and is not an API input."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.routers.operation_approvals import ApprovalDecision, ApprovalRequest


def test_streamlined_default_and_strict_opt_in(monkeypatch):
    monkeypatch.delenv("SUPERPLANE_REQUIRE_DISTINCT_APPROVER", raising=False)
    assert Settings(_env_file=None).superplane_require_distinct_approver is False
    monkeypatch.setenv("SUPERPLANE_REQUIRE_DISTINCT_APPROVER", "true")
    assert Settings(_env_file=None).superplane_require_distinct_approver is True
    monkeypatch.setenv("SUPERPLANE_REQUIRE_DISTINCT_APPROVER", "false")
    assert Settings(_env_file=None).superplane_require_distinct_approver is False


@pytest.mark.parametrize("value", ["", "off", "yes", "0", "False"])
def test_invalid_policy_configuration_refuses_startup(monkeypatch, value):
    monkeypatch.setenv("SUPERPLANE_REQUIRE_DISTINCT_APPROVER", value)
    with pytest.raises(ValidationError, match="must be true or false"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "model,payload",
    [
        (
            ApprovalRequest,
            {
                "workspace_id": "workspace",
                "action": "provision",
                "idempotency_key": "original",
                "parameters": {},
            },
        ),
        (ApprovalDecision, {"result": "allowed-once"}),
    ],
)
def test_caller_cannot_override_recorded_policy(model, payload):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model(**payload, requires_distinct_approver=False)
