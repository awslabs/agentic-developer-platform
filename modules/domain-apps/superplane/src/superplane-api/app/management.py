"""The explicitly selected management service, before workspace execution is enabled.

This mode serves durable organization administration and registration reads. It
does not run legacy bootstrap/credential loops or advertise execution capability.
The allowlist also gates internal routes, so a machine credential cannot enable a
legacy mutation indirectly.
"""

import os

from fastapi import HTTPException, Request


def management_only() -> bool:
    value = os.environ.get("SUPERPLANE_MANAGEMENT_ONLY", "false")
    if value not in {"true", "false"}:
        raise ValueError("SUPERPLANE_MANAGEMENT_ONLY must be true or false")
    return value == "true"


MANAGEMENT_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/readyz"),
        ("GET", "/orgs/current"),
        ("PATCH", "/orgs/current"),
        ("GET", "/workspaces"),
        ("POST", "/workspaces"),
        ("POST", "/workspaces/preview"),
        ("POST", "/workspaces/adopt"),
        ("POST", "/workspaces/{workspace_id}/retirement/preview"),
        ("POST", "/workspaces/{workspace_id}/retirement"),
        ("GET", "/workspaces/{workspace_id}/lifecycle-proposals"),
        (
            "POST",
            "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview",
        ),
        (
            "POST",
            "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue",
        ),
        ("GET", "/workspaces/{workspace_id}/deployments"),
        ("POST", "/workspaces/{workspace_id}/deployments/preview"),
        ("POST", "/workspaces/{workspace_id}/deployments"),
        ("POST", "/workspaces/{workspace_id}/deployments/{dep_id}/teardown-preview"),
        ("DELETE", "/workspaces/{workspace_id}/deployments/{dep_id}"),
        ("GET", "/capabilities"),
        ("GET", "/operations/{operation_id}"),
        ("GET", "/operations/by-idempotency/{idempotency_key}"),
        ("POST", "/operation-approvals"),
        ("GET", "/operation-approvals/{approval_id}"),
        ("POST", "/operation-approvals/{approval_id}/decision"),
        ("GET", "/workspaces/{workspace_id}"),
        ("GET", "/workspaces/{workspace_id}/provider-connections"),
        ("POST", "/workspaces/{workspace_id}/provider-connections"),
        ("POST", "/workspaces/{workspace_id}/provider-connections/{connection_id}"),
        (
            "POST",
            "/workspaces/{workspace_id}/provider-connections/{connection_id}/validation",
        ),
        (
            "POST",
            "/workspaces/{workspace_id}/provider-connections/{connection_id}/rotation",
        ),
        ("DELETE", "/workspaces/{workspace_id}/provider-connections/{connection_id}"),
        ("GET", "/users"),
        ("GET", "/events"),
        ("GET", "/events/{event_id}"),
        ("GET", "/internal/installation"),
        ("GET", "/internal/installation/workspaces/{workspace_id}/credential-evidence/{connection_id}"),
        ("GET", "/internal/observations/clusters"),
        ("POST", "/internal/observations/leases"),
        ("POST", "/internal/observations/leases/release"),
        ("POST", "/internal/controller/reconcile"),
    }
)


async def enforce_management_surface(request: Request) -> None:
    if management_only():
        route = request.scope.get("route")
        if (request.method, getattr(route, "path", None)) not in MANAGEMENT_ROUTES:
            raise HTTPException(503, "Workspace execution is not activated")
