"""Operator admin traffic must fail closed without credential-bearing diagnostics."""

import logging
import ssl

import httpx
import pytest

from src.gitlab.admin_client import GitlabAdmin, ProvisioningError

CANARY = "glpat-synthetic-private-canary"
URL = "https://gitlab.example.test/gitlab"


@pytest.mark.parametrize(
    "status,body",
    [
        (302, CANARY),
        (500, CANARY),
        (200, f"{{invalid-{CANARY}"),
    ],
)
async def test_admin_transport_never_emits_provider_payload(status, body, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text=body, headers={"Location": f"https://elsewhere.example.test/{CANARY}"})
    )
    client = GitlabAdmin(URL, CANARY, transport=transport)
    with pytest.raises(ProvisioningError) as error:
        async with client as admin:
            await admin.request("GET", "runners/all")
    assert CANARY not in str(error.value)
    assert CANARY not in capsys.readouterr().out + capsys.readouterr().err + caplog.text
    assert client.http.follow_redirects is False
    assert client.http.trust_env is False


async def test_connection_failure_hides_exception_text_and_keeps_strict_defaults(capsys):
    def failure(request):
        raise httpx.ConnectError(f"transport {CANARY}", request=request)

    client = GitlabAdmin(URL, CANARY, transport=httpx.MockTransport(failure))
    with pytest.raises(ProvisioningError, match="gitlab_transport_failure") as error:
        async with client:
            pass
    assert CANARY not in str(error.value) + capsys.readouterr().err
    assert client.http.is_closed


def test_private_ca_uses_explicit_verified_context(monkeypatch):
    recorded = {}
    original = ssl.create_default_context

    def create_context(*, cafile):
        recorded["cafile"] = cafile
        return original()

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    client = GitlabAdmin(URL, CANARY, ca_file="/private/operator-root.pem")
    assert recorded["cafile"] == "/private/operator-root.pem"
    assert client.http._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client.http._transport._pool._ssl_context.check_hostname
    import asyncio

    asyncio.run(client.http.aclose())


def test_invalid_ca_refuses_to_construct_client():
    with pytest.raises(ProvisioningError, match="gitlab_ca_not_verified"):
        GitlabAdmin(URL, CANARY, ca_file="/nonexistent/operator-root.pem")


@pytest.mark.parametrize("path", ["https://elsewhere.example.test/user", "//elsewhere.example.test/user", "/user", "../user"])
async def test_admin_refuses_absolute_or_escaping_paths(path):
    seen = []

    def response(request):
        seen.append(request.url)
        return httpx.Response(200, json={"is_admin": True, "id": 7})

    async with GitlabAdmin(URL, CANARY, transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ProvisioningError, match="gitlab_path_not_verified"):
            await client.request("GET", path)
    assert len(seen) == 1
    assert seen[0].host == "gitlab.example.test"
