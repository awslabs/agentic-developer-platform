"""Mounted receiver callback requires real signed proof and fresh authority."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from adp_platform_common.l5_contributions.assertions import sign
from adp_platform_common.l5_contributions.contract import DESTINATIONS, storage_key
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.internal import l5_consumer_routes as routes
from src.internal import route_authority_manifest as manifest

PATH = "/internal/v1/l5/contribution-authority"


@pytest.fixture
def callback(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(routes, "verification_keys", lambda: {"fixture": public})
    reader = AsyncMock(return_value=True)
    sessions = MagicMock()
    factory = MagicMock(return_value=sessions)
    monkeypatch.setattr(routes, "authorize_assertion", reader)
    monkeypatch.setattr(routes, "get_session_factory", factory)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client, key, reader, factory, sessions


def assertion(key, destination="product-context", **options):
    claims = {"scope": {"tenant_id": "fixture-tenant"}, "result_ref": {"result_id": "fixture-result"}, "destination": destination}
    claims["storage_key"] = storage_key(claims["scope"], claims["result_ref"], destination, "1.0")
    claims.update(options.pop("claims", {}))
    return sign(claims, key=key, key_id="fixture", method="GET", path="/fixture", body=b"", destination=destination, **options)


@pytest.mark.parametrize("proof", ["absent", "malformed", "forged", "expired", "future", "audience", "semantic_key", "extra_field", "not_object"])
def test_unverified_callback_never_reads_current_authority(callback, proof):
    client, key, reader, factory, _ = callback
    token = assertion(key)
    if proof == "forged":
        token = assertion(Ed25519PrivateKey.generate())
    elif proof == "expired":
        token = assertion(key, now=int(time.time()) - 60)
    elif proof == "future":
        token = assertion(key, now=int(time.time()) + 60)
    elif proof == "audience":
        token = assertion(key, destination="unregistered-consumer")
    elif proof == "semantic_key":
        token = assertion(key, claims={"storage_key": "substituted"})
    elif proof == "malformed":
        token = "not-a-signed-assertion"
    body = {"assertion": token}
    if proof == "absent":
        body = {}
    elif proof == "extra_field":
        body["authorized"] = True
    elif proof == "not_object":
        body = [token]
    response = client.post(PATH, json=body)
    assert response.status_code == 200
    assert response.json() == {"authorized": False}
    assert response.headers["cache-control"] == "no-store"
    reader.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.parametrize("destination", DESTINATIONS.values())
@pytest.mark.parametrize("authorized", [False, True])
def test_signed_callback_returns_only_fresh_authority_decision(callback, destination, authorized):
    client, key, reader, factory, sessions = callback
    reader.return_value = authorized
    response = client.post(PATH, json={"assertion": assertion(key, destination)})
    assert response.status_code == 200
    assert response.json() == {"authorized": authorized}
    assert response.headers["cache-control"] == "no-store"
    factory.assert_called_once_with()
    reader.assert_awaited_once()
    claims, actual_sessions = reader.await_args.args
    assert actual_sessions is sessions
    assert claims["destination"] == destination
    assert claims["scope"] == {"tenant_id": "fixture-tenant"}


def test_callback_inventory_requires_crypto_and_current_authority(monkeypatch):
    route = next(route for route in routes.router.routes if route.path == PATH)
    assert manifest.classify_internal_route(route).verifier == "verify"
    for calls, message in [({"verify"}, "current contribution authority missing"), ({"authorize_assertion"}, "internal verifier missing")]:
        monkeypatch.setattr(manifest, "_called_verifiers", lambda endpoint: calls)
        with pytest.raises(AssertionError, match=message):
            manifest.classify_internal_route(route)
