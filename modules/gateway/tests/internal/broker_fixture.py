"""Explicit downstream route fixture, never a substitute for real broker tests."""

from types import SimpleNamespace

from fastapi import HTTPException, Request

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding import BindingResult


def install_broker_fixture(app, *, user, run, tenant, token_context=None, expected_key=None):
    """Fix the authenticated run out of band; request selectors cannot change it."""

    async def verified(request: Request):
        if expected_key is not None and request.headers.get("X-Internal-Api-Key") != expected_key:
            raise HTTPException(403)
        request.state.token_context = token_context or SimpleNamespace(
            user_id="fixture-worker",
            org_id=tenant,
            scope="internal",
            credential_scopes=["credential:raw-read", "credential:materialize"],
        )
        request.state.agent_credential_binding = BindingResult(user, True, False, user, run, tenant)

    app.dependency_overrides[verify_internal_or_irsa] = verified
