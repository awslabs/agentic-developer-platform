"""Tests for the GET /health endpoint."""

import pytest


@pytest.mark.asyncio
async def test_health_returns_200(client):
    """Health endpoint returns 200 with status and version."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_response_body(client):
    """Health response contains expected status and version fields."""
    response = await client.get("/health")
    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_response_schema(client):
    """Health response has exactly the expected keys.

    Issue #5055 (U14) added the two identity-posture flags so the environment's
    state can be asserted rather than inferred from a code default (R5 acc. 5).
    This assertion is deliberately exact rather than a subset check: /health is
    consumed by probes and deployment checks, so a field appearing or vanishing
    unnoticed is a contract change, and this test is where that gets decided.
    """
    response = await client.get("/health")
    data = response.json()
    assert set(data.keys()) == {
        "status",
        "version",
        "cognito_enabled",
        "domain_auth_enforced",
    }
