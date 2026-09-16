"""Issue #4842 (D4=Option A): the canonical org-create route's error mapping.

``POST /api/admin/identity/organizations`` used to wrap its service call in
``except Exception`` and answer **409** for every failure. A 409 tells the caller
"your input collides with existing state — change it and retry", so a DB outage, a
bug in the service, or a failed Cognito call all arrived looking like the
operator's fault. They would pick a new id, get the same 409, and have nothing
pointing at the real cause — while the 5xx that should have paged someone never
appeared in the error-rate metrics at all.

The rule these tests pin: an integrity violation is the one genuine conflict;
everything else is ours and must surface as a 5xx.

Written against the real router with an overridden ``get_db`` rather than by
calling the handler function directly — the handler is where the mapping lives,
but the *status code the client sees* is the actual contract, and only the ASGI
round-trip proves it (a raised ``BedrockGatewayError`` reaching the client as its
own status depends on an app-level handler the handler body knows nothing about).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.router import router
from src.admin.installations.guards import InstallationClaimError
from src.admin.installations.resolver import OwnerState
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.organization import Organization
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio

CREATE_PATH = "/api/admin/identity/organizations"
BODY = {
    "id": "acme-test",
    "name": "Acme Test",
    "plan": "free",
    "channels": {"github": [], "slack": [], "whatsapp": []},
}


def _client(*, user: TokenContext, db: AsyncSession) -> TestClient:
    application = FastAPI()
    application.include_router(router)

    @application.exception_handler(BedrockGatewayError)
    async def _gateway_error(_request, exc: BedrockGatewayError):
        # Mirrors app.py's registered handler.
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def _user():
        return user

    async def _db():
        yield db

    application.dependency_overrides[get_current_user] = _user
    application.dependency_overrides[require_admin] = _user
    application.dependency_overrides[get_db] = _db
    # raise_server_exceptions=False so an unhandled error becomes a 500 response
    # instead of propagating — otherwise "is it a 500?" cannot be asserted.
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def no_aws_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the service's POST-COMMIT side-effects (DDB + Cognito).

    Only needed by the tests that let the real service run. The route constructs
    the service itself, so there is no injection point, and both side-effects are
    unconditional AWS calls. Patched at the method rather than the constructor so
    the service's own transaction — the part under test — is untouched.
    """
    from src.admin.identity.cognito_sync import CognitoSyncService
    from src.admin.identity.identity_index_writer import IdentityIndexWriter

    monkeypatch.setattr(IdentityIndexWriter, "sync_org_channels", AsyncMock())
    monkeypatch.setattr(CognitoSyncService, "ensure_org_group", AsyncMock(return_value=True))


@pytest.fixture
def stub_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace ``OrganizationsService.create_organization`` with a controllable stub.

    The failures under test (DB outage, service bug) cannot be provoked reliably
    against SQLite, and the mapping is a property of the ROUTE, not of any
    particular way the service broke. Patching the seam lets each test name the
    exact exception class and assert only the status the client receives.
    """
    import src.admin.identity.router as router_module

    stub = AsyncMock()
    monkeypatch.setattr(router_module.OrganizationsService, "create_organization", stub)
    return stub


class TestCreateOrganizationErrorMapping:
    async def test_a_duplicate_is_still_a_409(self, db_session: AsyncSession, platform_admin_context: TokenContext, stub_service: AsyncMock):
        """The one genuine conflict keeps its status — this is not a regression.

        Narrowing the handler must not cost the behaviour operators rely on: a
        second org with a taken id/name is the caller's problem to fix, and 409 is
        the answer that says so.
        """
        stub_service.side_effect = IntegrityError("INSERT ...", {}, Exception("UNIQUE constraint failed: organizations.id"))

        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 409
        assert "acme-test" in response.json()["detail"]

    async def test_a_real_duplicate_id_is_a_409_end_to_end(
        self, db_session: AsyncSession, platform_admin_context: TokenContext, no_aws_side_effects: None
    ):
        """The same 409, provoked by a REAL uniqueness violation, not a stub.

        Guards the stub above: if ``IntegrityError`` were not what the database
        actually raises on a duplicate here, every stubbed assertion would be
        testing a fiction. The org is seeded first so the second insert genuinely
        collides.
        """
        db_session.add(Organization(id="acme-test", name="Acme Test", aws_accounts=[], role_mappings={}, settings={}))
        await db_session.commit()

        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 409

    async def test_an_unexpected_failure_is_a_500_not_a_409(
        self, db_session: AsyncSession, platform_admin_context: TokenContext, stub_service: AsyncMock
    ):
        """THE regression this issue closes: a server fault must not read as a conflict.

        ``RuntimeError`` stands in for the whole class — connection reset, a bug in
        the service, a failed Cognito call. None of them are the caller's input,
        so none of them may be reported as a conflict the caller could resolve by
        retrying with a different id.
        """
        stub_service.side_effect = RuntimeError("connection reset by peer")

        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 500
        assert response.status_code != 409

    async def test_a_value_error_from_the_service_is_a_500_not_a_409(
        self, db_session: AsyncSession, platform_admin_context: TokenContext, stub_service: AsyncMock
    ):
        """A ``ValueError`` is a bug or a validation gap, not a state collision.

        Included separately because it is the exception class most likely to be
        raised by future service-side validation, and mapping it to 409 would
        quietly recreate the masking behaviour for exactly the errors most likely
        to appear next.
        """
        stub_service.side_effect = ValueError("channels.github[0].installation_id is not numeric")

        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 500

    async def test_an_installation_claim_refusal_keeps_its_own_status(
        self, db_session: AsyncSession, platform_admin_context: TokenContext, stub_service: AsyncMock
    ):
        """A fail-closed refusal must not be flattened into this route's generic 409.

        ``InstallationClaimError`` is a ``BedrockGatewayError`` carrying its own
        status and message. Here it is a 403 (unverifiable ownership) precisely
        because the old blanket handler would have turned it into a 409 — the
        operator would be told to change the id when the real answer is that
        ownership could not be attested. Re-raising untouched is what preserves
        that distinction; the 500 branch must not swallow it either.
        """
        stub_service.side_effect = InstallationClaimError(
            "Ownership of installation 5559991 could not be verified.",
            status_code=403,
            state=OwnerState.UNATTESTABLE,
            installation_id=5559991,
            org_id="acme-test",
        )

        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 403
        assert response.json()["error"] == "installation_claim_denied"

    async def test_an_empty_name_is_a_422_not_a_409(self, db_session: AsyncSession, platform_admin_context: TokenContext, stub_service: AsyncMock):
        """Body validation still answers 422, and never reaches the service.

        Inherited from the deprecated route's coverage (#4842 moved it here with
        the canonical designation). Worth keeping distinct from the 409/500 split:
        a malformed body is the caller's fault but is not a *collision*, so 409
        would be as misleading here as it was for a server fault.
        """
        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json={**BODY, "name": ""})

        assert response.status_code == 422
        stub_service.assert_not_called()

    async def test_a_successful_create_is_still_a_201(
        self, db_session: AsyncSession, platform_admin_context: TokenContext, no_aws_side_effects: None
    ):
        """The happy path is untouched by the narrowing."""
        response = _client(user=platform_admin_context, db=db_session).post(CREATE_PATH, json=BODY)

        assert response.status_code == 201
        assert response.json()["id"] == "acme-test"
