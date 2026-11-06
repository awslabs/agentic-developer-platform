from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.auth import cli_login, client_source


def request(headers=None, peer="192.0.2.10"):
    return Request(
        {"type": "http", "client": (peer, 1234) if peer else None, "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    )


def test_ingress_proof_is_required_and_forwarded_headers_are_ignored(monkeypatch):
    monkeypatch.setattr(client_source, "get_settings", lambda: SimpleNamespace(apigw_provenance_secret="trusted-edge"))
    headers = {"x-adp-client-ip": "198.51.100.1", "x-adp-client-provenance": "forged", "x-forwarded-for": "203.0.113.1"}
    assert client_source.login_source(request(headers)) == "192.0.2.10"
    headers["x-adp-client-provenance"] = "trusted-edge"
    assert client_source.login_source(request(headers)) == "198.51.100.1"
    headers["x-adp-client-ip"] = "not-an-address"
    with pytest.raises(HTTPException) as error:
        client_source.login_source(request(headers))
    assert error.value.status_code == 400


def test_duplicate_client_addresses_cannot_override_ingress_identity(monkeypatch):
    monkeypatch.setattr(client_source, "get_settings", lambda: SimpleNamespace(apigw_provenance_secret="trusted-edge"))
    req = request({"x-adp-client-ip": "198.51.100.1", "x-adp-client-provenance": "trusted-edge"})
    req.scope["headers"].append((b"x-adp-client-ip", b"203.0.113.1"))
    with pytest.raises(HTTPException) as error:
        client_source.login_source(req)
    assert error.value.status_code == 400


def test_missing_transport_does_not_create_a_shared_unknown_bucket(monkeypatch):
    monkeypatch.setattr(client_source, "get_settings", lambda: SimpleNamespace(apigw_provenance_secret=""))
    with pytest.raises(HTTPException) as error:
        client_source.login_source(request(peer=None))
    assert error.value.status_code == 503


def test_device_limit_expiry_does_not_reset_live_quotas(monkeypatch):
    monkeypatch.setattr(cli_login.time, "monotonic", lambda: 100)
    cli_login._start_times.clear()
    try:
        cli_login._start_times["expired"] = [1]
        for _ in range(cli_login._START_RATE_LIMIT):
            cli_login._check_start_rate("active")
        assert "expired" not in cli_login._start_times
        cli_login._check_start_rate("another-source")
        with pytest.raises(HTTPException):
            cli_login._check_start_rate("active")
    finally:
        cli_login._start_times.clear()
