"""Gateway replicas share one client certificate without exporting their key."""

import asyncio
import hashlib
import ssl
import subprocess
from types import SimpleNamespace

import pytest

from src.crew import host_client_identity


def test_domain_separated_client_certificate_is_stable_and_private(monkeypatch):
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_CERT_FILE", raising=False)
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_KEY_FILE", raising=False)
    monkeypatch.setattr(host_client_identity, "get_settings", lambda: SimpleNamespace(token_secret_key="a" * 64))
    first = host_client_identity.gateway_client_identity()
    host_client_identity._derived_identity.cache_clear()
    second = host_client_identity.gateway_client_identity()
    assert first.certificate == second.certificate
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint == hashlib.sha256(ssl.PEM_cert_to_DER_cert(first.certificate)).hexdigest()
    assert second.private_key_file != first.private_key_file
    assert "PRIVATE KEY" not in first.certificate
    assert host_client_identity.Path(first.private_key_file).stat().st_mode & 0o077 == 0
    monkeypatch.setattr(host_client_identity, "get_settings", lambda: SimpleNamespace(token_secret_key="b" * 64))
    assert host_client_identity.gateway_client_identity().fingerprint != first.fingerprint


@pytest.mark.asyncio
async def test_gateway_certificate_completes_mutually_authenticated_tls(tmp_path, monkeypatch):
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_CERT_FILE", raising=False)
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_KEY_FILE", raising=False)
    monkeypatch.setattr(host_client_identity, "get_settings", lambda: SimpleNamespace(token_secret_key="a" * 64))
    client = host_client_identity.gateway_client_identity()
    server_certificate, server_key = tmp_path / "server.pem", tmp_path / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=crew-host",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(server_key),
            "-out",
            str(server_certificate),
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(server_certificate), str(server_key))
    server_context.verify_mode = ssl.CERT_REQUIRED
    server_context.load_verify_locations(cadata=client.certificate)
    peers = []

    async def accept(_reader, writer):
        peers.append(writer.get_extra_info("ssl_object").getpeercert(binary_form=True))
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=server_context)
    try:
        caller = ssl.create_default_context(cafile=str(server_certificate))
        caller.load_cert_chain(client.certificate_file, client.private_key_file)
        _reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1], ssl=caller, server_hostname="127.0.0.1")
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        assert peers and hashlib.sha256(peers[0]).hexdigest() == client.fingerprint
    finally:
        server.close()
        await server.wait_closed()
