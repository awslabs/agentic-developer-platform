"""Operation and tenant entitlements for trusted internal service principals."""

from fastapi import HTTPException, Request


def require_service_operation(request: Request, capability: str):
    principal = getattr(request.state, "token_context", None)
    if (
        principal is None
        or principal.auth_source != "iam"
        or not principal.user_id
        or principal.scope not in {"internal", "platform"}
        or capability not in principal.credential_scopes
    ):
        raise HTTPException(403, "internal service capability required")
    return principal


def service_tenant(request: Request, requested: str | None, capability: str) -> str | None:
    """Return an entitled tenant, never use a requested tenant as authority.

    Cross-tenant identity routing is granted only to the trusted ingress role by
    Terraform, never to worker roles or through the registry's public admin API.
    """
    principal = require_service_operation(request, capability)
    if "internal:cross-tenant" in principal.credential_scopes:
        return requested
    tenant = principal.org_id
    if not tenant or tenant.startswith("__") or (requested is not None and requested != tenant):
        raise HTTPException(403, "tenant not entitled")
    return tenant
