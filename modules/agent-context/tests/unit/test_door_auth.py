"""Request-bound identity on REST and mounted MCP; shared keys confer no access."""

import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from door import auth

pytestmark = pytest.mark.door_auth


@pytest.fixture
def identity(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    monkeypatch.setattr(auth.config, "door_verification_keys", json.dumps({"test": public}))
    return key


def token(key, path="/call", body=b"{}", **changes):
    now = int(time.time())
    claims = dict(
        iss="adp-gateway",
        aud="adp-knowledge-door",
        kid="test",
        sub="run-a#1",
        tenant_id="tenant-a",
        github_login="alice",
        owner_sub="",
        method="POST",
        path=path,
        body_sha256=hashlib.sha256(body).hexdigest(),
        iat=now,
        exp=now + 30,
    )
    claims.update(changes)

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    message = b"adpd1." + encode(json.dumps(claims).encode())
    return (message + b"." + encode(key.sign(message))).decode()


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, next):
        denial = await auth.check_request_auth(request)
        return denial if denial is not None else await next(request)

    async def echo(request: Request):
        return {
            "tenant": request.headers.get("x-tenant-id"),
            "login": request.headers.get("x-github-login"),
            "teams": request.headers.get("x-github-teams"),
            "owner": request.headers.get("x-owner-sub"),
            "run_bound": request.headers.get("x-adp-run-service"),
            "body": (await request.body()).decode(),
        }

    app.add_api_route("/call", echo, methods=["POST"])
    app.add_api_route("/health", lambda: {"ok": True})
    mounted = FastAPI()
    mounted.add_api_route("/", echo, methods=["POST"])
    app.mount("/mcp", mounted)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", ["/call", "/mcp/"])
def test_verified_claims_replace_all_asserted_identity(client, identity, path):
    response = client.post(
        path,
        content=b"{}",
        headers={
            "x-adp-door-identity": token(identity, path),
            "x-internal-api-key": "old-shared-key",
            "x-tenant-id": "victim",
            "x-github-login": "victim",
            "x-github-teams": "victim/admins",
            "x-owner-sub": "victim",
            "x-adp-run-service": "false",
        },
    )
    assert response.status_code == 200
    assert response.json() == dict(
        tenant="tenant-a", login="alice", teams=None, owner="", run_bound="true", body="{}"
    )


@pytest.mark.parametrize("path", ["/call", "/mcp/"])
def test_shared_key_and_disable_flag_cannot_bypass(client, identity, monkeypatch, path):
    monkeypatch.setenv("DOOR_AUTH_ENABLED", "false")
    response = client.post(
        path, content=b"{}", headers={"x-internal-api-key": "old-key", "x-tenant-id": "victim"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "adp-agent-control-listener"},
        {"iss": "attacker"},
        {"kid": "unknown"},
        {"exp": 1},
        {"iat": 1},
        {"iat": True},
        {"exp": 2**62},
        {"method": "GET"},
        {"path": "/mcp/"},
        {"tenant_id": ""},
        {"sub": ""},
        {"github_login": "", "owner_sub": ""},
        {"kid": []},
    ],
)
def test_invalid_claims_denied(client, identity, changes):
    response = client.post(
        "/call", content=b"{}", headers={"x-adp-door-identity": token(identity, **changes)}
    )
    assert response.status_code == 401


def test_changed_body_and_forged_signature_denied(client, identity):
    valid = token(identity)
    assert (
        client.post(
            "/call", content=b'{"tenant":"victim"}', headers={"x-adp-door-identity": valid}
        ).status_code
        == 401
    )
    forged = token(Ed25519PrivateKey.generate())
    assert (
        client.post("/call", content=b"{}", headers={"x-adp-door-identity": forged}).status_code
        == 401
    )


def test_key_rotation_missing_key_and_public_probe(client, identity, monkeypatch):
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(auth.config, "door_verification_keys", "{}")
    assert (
        client.post(
            "/call", content=b"{}", headers={"x-adp-door-identity": token(identity)}
        ).status_code
        == 503
    )


def test_bounded_body(client, identity):
    assert (
        client.post(
            "/call",
            content=b"a" * (auth.MAX_BODY + 1),
            headers={"x-adp-door-identity": token(identity)},
        ).status_code
        == 413
    )


def test_overlap_rotation_and_removal(client, identity, monkeypatch):
    successor = Ed25519PrivateKey.generate()

    def public(key):
        return (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    monkeypatch.setattr(
        auth.config,
        "door_verification_keys",
        json.dumps({"test": public(identity), "next": public(successor)}),
    )
    for signed in (token(identity), token(successor, kid="next")):
        assert (
            client.post("/call", content=b"{}", headers={"x-adp-door-identity": signed}).status_code
            == 200
        )
    monkeypatch.setattr(
        auth.config, "door_verification_keys", json.dumps({"next": public(successor)})
    )
    assert (
        client.post(
            "/call", content=b"{}", headers={"x-adp-door-identity": token(identity)}
        ).status_code
        == 401
    )


def test_unsigned_query_is_rejected(client, identity):
    assert (
        client.post(
            "/call?tenant=victim", content=b"{}", headers={"x-adp-door-identity": token(identity)}
        ).status_code
        == 401
    )
