"""Tenant selection never grants authority, including to registered worker roles."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.internal.service_authorization import require_service_operation, service_tenant


def request(org="tenant-a", scopes=("internal:audit:read",)):
    req = Request({"type": "http", "headers": []})
    req.state.token_context = SimpleNamespace(
        user_id="iam-agent:auditor", auth_source="iam", scope="internal", org_id=org, credential_scopes=list(scopes)
    )
    return req


def test_tenant_is_derived_and_cannot_be_overridden():
    req = request()
    req.state.token_context.attributed_org_id = "tenant-b"
    assert service_tenant(req, None, "internal:audit:read") == "tenant-a"
    assert service_tenant(req, "tenant-a", "internal:audit:read") == "tenant-a"
    with pytest.raises(HTTPException) as exc:
        service_tenant(req, "tenant-b", "internal:audit:read")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("org", ["", "__platform__"])
def test_platform_org_is_not_implicit_all_tenant_permission(org):
    with pytest.raises(HTTPException):
        service_tenant(request(org), "tenant-b", "internal:audit:read")


def test_cross_tenant_grant_does_not_grant_other_operations():
    req = request("__platform__", ("internal:identity:resolve", "internal:cross-tenant"))
    assert service_tenant(req, "tenant-b", "internal:identity:resolve") == "tenant-b"
    with pytest.raises(HTTPException):
        service_tenant(req, "tenant-b", "internal:audit:read")


@pytest.mark.parametrize("mutation", ["missing", "worker", "jwt", "empty_subject"])
def test_missing_or_wrong_principal_fails_closed(mutation):
    req = request()
    if mutation == "missing":
        del req.state.token_context
    elif mutation == "worker":
        req.state.token_context.credential_scopes = ["credential:raw-read"]
    elif mutation == "jwt":
        req.state.token_context.auth_source = "jwt"
    else:
        req.state.token_context.user_id = ""
    with pytest.raises(HTTPException):
        require_service_operation(req, "internal:audit:read")
