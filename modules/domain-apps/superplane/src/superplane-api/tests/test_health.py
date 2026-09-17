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
    """Health response has exactly the expected keys."""
    response = await client.get("/health")
    data = response.json()
    assert set(data.keys()) == {"status", "version"}
