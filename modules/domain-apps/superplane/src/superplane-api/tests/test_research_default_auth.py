"""Research isolation must hold in the shipped legacy mode as well as strict mode."""

import re
import uuid

import pytest

from app.config import settings
from app.endpoint_inventory import mounted_operations
from app.main import app
from app.middleware.auth import create_access_token
from tests.test_auth import (
    TestResearchTenantIsolation as _TenantIsolation,
    _seed_two_tenant_research,
)


@pytest.fixture
def enforcing(monkeypatch):
    """Run the existing isolation matrix through real legacy token verification."""
    monkeypatch.setattr(settings, "domain_auth_enforced", False)
    monkeypatch.setattr(app.state, "domain_policy", None)
    return None


class TestDefaultResearchTenantIsolation(_TenantIsolation):
    @staticmethod
    def _headers(enforcing, org_id):
        token, _ = create_access_token(org_id)
        return {"Authorization": f"Bearer {token}"}


RESEARCH_ROUTES = sorted(
    (method, path)
    for method, path in mounted_operations(app)
    if path.startswith("/api/v1/research/")
)
assert len(RESEARCH_ROUTES) == 13


@pytest.mark.parametrize("method,template", RESEARCH_ROUTES)
@pytest.mark.parametrize("authorization", [None, "Bearer invalid.token.value"])
async def test_every_default_research_route_refuses_unverified_callers(
    client, enforcing, method, template, authorization
):
    path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), template)
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.request(method, path, headers=headers, json={})
    assert response.status_code == 401, (method, path, response.text)


@pytest.mark.parametrize("identified_user", [False, True])
async def test_default_approval_records_signed_identity_despite_body_spoof(
    client, enforcing, identified_user
):
    seeded = await _seed_two_tenant_research()
    user_id = uuid.uuid4() if identified_user else None
    token, _ = create_access_token(seeded["org_a"], user_id=user_id)
    response = await client.patch(
        f"/api/v1/research/proposals/{seeded['proposal_a']}/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved_by": "body-supplied-impersonation"},
    )
    assert response.status_code == 200, response.text
    expected = str(user_id) if user_id else f"org:{seeded['org_a']}"
    assert response.json()["approved_by"] == expected
