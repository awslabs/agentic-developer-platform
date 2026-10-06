"""A workspace caller cannot replace deployment-owned network or IAM authority."""

import pytest
from pydantic import ValidationError

from app.schemas.workspace import CreateWorkspaceRequest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("supplied_vpc_id", "vpc-0123456789abcdef0"),
        ("supplied_private_subnet_ids", ["subnet-0123456789abcdef0"]),
        ("networking_mode", "supplied"),
        ("role_arn", "arn:aws:iam::111122223333:role/management"),
        ("workspace_role_permissions_boundary_arn", ""),
        (
            "workspace_admin_automation_role_arns",
            ["arn:aws:iam::111122223333:role/management"],
        ),
        ("workspace_variables", {"supplied_vpc_id": "vpc-0123456789abcdef0"}),
    ],
)
def test_public_create_cannot_override_the_installed_provider_recipe(field, value):
    request = {"name": "demo-workspace", "mode": "managed"}
    assert CreateWorkspaceRequest.model_validate(request).mode == "managed"
    with pytest.raises(ValidationError) as refused:
        CreateWorkspaceRequest.model_validate({**request, field: value})
    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == (field,)
        for error in refused.value.errors()
    )
