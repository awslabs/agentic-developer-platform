"""EC2 custody uses the private authenticated gateway and a configured CA."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.crew import handoff
from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget


@pytest.fixture
def host():
    endpoint = "https://10.4.0.3:8443"
    return ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id="user-a",
            target_kind="ec2-instance",
            target_ref="box-a",
            generation=2,
            expires_at=4_000_000_000,
            endpoint=endpoint,
        ),
        "ready",
    )


async def test_ec2_custody_requires_configured_trust(monkeypatch, tmp_path, host):
    from types import SimpleNamespace

    from src.crew import host_client_identity

    monkeypatch.setattr(host_client_identity, "get_settings", lambda: SimpleNamespace(token_secret_key=""))
    monkeypatch.delenv("ADP_CREW_HOST_CA_FILE", raising=False)
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_CERT_FILE", raising=False)
    monkeypatch.delenv("ADP_CREW_HOST_CLIENT_KEY_FILE", raising=False)
    with pytest.raises(HTTPException) as missing:
        await handoff.crew_host_client(host)
    assert missing.value.status_code == 503
    monkeypatch.setenv("ADP_CREW_HOST_CA_FILE", str(tmp_path / "untrusted.pem"))
    with pytest.raises(HTTPException) as missing_client:
        await handoff.crew_host_client(host)
    assert missing_client.value.status_code == 503
    monkeypatch.setenv("ADP_CREW_HOST_CLIENT_CERT_FILE", str(tmp_path / "client.pem"))
    monkeypatch.setenv("ADP_CREW_HOST_CLIENT_KEY_FILE", str(tmp_path / "client-key.pem"))
    with pytest.raises(HTTPException) as absent:
        await handoff.crew_host_client(host)
    assert absent.value.status_code == 503


async def test_ec2_custody_mints_through_private_https_only(host, monkeypatch):
    captured = []
    run_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(handoff, "active_crew_run", lambda *_args, **_kwargs: _run(run_id))
    monkeypatch.setattr(handoff, "sign_envelope", lambda **_kwargs: "signed-by-gateway")
    delivered = []

    async def deliver(owner, target, *, now):
        delivered.append((owner, target, now))

    monkeypatch.setattr(handoff, "deliver_model_token", deliver)

    def minted(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "link_token": "minted_link_token_for_owner",
                "lease_generation": 2,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
            },
        )

    async def mock_host_client(_target):
        return httpx.AsyncClient(transport=httpx.MockTransport(minted), trust_env=False)

    monkeypatch.setattr(handoff, "crew_host_client", mock_host_client)
    assert (
        await handoff.mint_crew_link(CrewOwner("tenant-a", "user-a"), host, now=int(datetime.now(UTC).timestamp())) == "minted_link_token_for_owner"
    )
    (request,) = captured
    assert str(request.url) == "https://10.4.0.3:8443/mint"
    assert request.headers["x-adp-crew-signature"] == "signed-by-gateway"
    assert json.loads(request.content)["run_id"] == run_id
    assert len(delivered) == 1 and delivered[0][1] is host


@pytest.mark.parametrize("endpoint", ["http://10.4.0.3:8443", "https://10.4.0.3:5477", "https://203.0.113.1:8443", "https://127.0.0.1:8443"])
async def test_ec2_custody_refuses_untrusted_network_endpoints(endpoint, host, monkeypatch):
    monkeypatch.setattr(handoff, "active_crew_run", lambda *_args, **_kwargs: _run("run-a"))
    monkeypatch.setattr(handoff, "sign_envelope", lambda **_kwargs: "signed-by-gateway")
    monkeypatch.setattr(handoff, "crew_host_client", lambda _target: pytest.fail("never dial untrusted endpoint"))
    target = ResolvedCrewTarget(endpoint, host.lease.model_copy(update={"endpoint": endpoint}), "ready")
    with pytest.raises(HTTPException) as denied:
        await handoff.mint_crew_link(CrewOwner("tenant-a", "user-a"), target, now=int(datetime.now(UTC).timestamp()))
    assert denied.value.status_code == 503


async def _run(run_id):
    return run_id


@pytest.mark.parametrize(
    "tamper",
    [
        None,
        "missing_binding",
        "other_run",
        "wrong_instance",
        "wrong_owner",
        "wrong_scope",
        "wrong_generation",
        "expired",
        "wrong_session",
        "inactive_marker",
    ],
)
async def test_host_mint_binding_uses_attested_session_not_pod_owner_marker(host, monkeypatch, tamper):
    now = int(datetime.now(UTC).timestamp())
    run_id = "00000000-0000-4000-8000-000000000001"
    target = replace(host, host_run_id=run_id, host_instance_ref="i-current")
    marker = {"run_id": run_id, "status": "active"}
    row = {
        "run_id": run_id,
        "status": "active",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "root_human_id": "user-a",
        "kind": "crew",
        "executor_scope": "session",
        "lease_generation": 2,
        "target_kind": "ec2-instance",
        "target_ref": "i-current",
        "session_id": "box-a",
        "lease_expires_at": now + 300,
    }
    if tamper == "missing_binding":
        target = replace(target, host_run_id=None)
    elif tamper == "other_run":
        marker["run_id"] = "00000000-0000-4000-8000-000000000002"
    elif tamper == "wrong_instance":
        row["target_ref"] = "i-retired"
    elif tamper == "wrong_owner":
        row["user_id"] = "user-b"
    elif tamper == "wrong_scope":
        row["executor_scope"] = "user"
    elif tamper == "wrong_generation":
        row["lease_generation"] = 1
    elif tamper == "expired":
        row["lease_expires_at"] = now - 1
    elif tamper == "wrong_session":
        row["session_id"] = "box-b"
    elif tamper == "inactive_marker":
        marker["status"] = "fenced"
    keys = []

    def get_item(**kwargs):
        keys.append(kwargs)
        return {"Item": marker}

    monkeypatch.setattr(
        handoff,
        "current_crew_store",
        lambda: SimpleNamespace(
            table=SimpleNamespace(get_item=get_item),
            read_run=lambda _: row,
        ),
    )
    if tamper is None:
        assert await handoff.active_crew_run(CrewOwner("tenant-a", "user-a"), target, now=now) == run_id
    else:
        with pytest.raises(HTTPException) as denied:
            await handoff.active_crew_run(CrewOwner("tenant-a", "user-a"), target, now=now)
        assert denied.value.status_code == 409
    if tamper == "missing_binding":
        assert keys == []
    else:
        assert keys == [{"Key": {"event_id": "crew-lease#tenant-a#box-a#2", "arrived_at": "crew"}, "ConsistentRead": True}]


async def test_current_host_certificate_is_pinned_before_any_http_request(host, tmp_path, monkeypatch):
    import asyncio
    import hashlib
    import ssl
    import subprocess

    certificate, key = tmp_path / "box.pem", tmp_path / "box.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=crew",
            "-addext",
            "subjectAltName=IP:10.4.0.3",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(certificate), str(key))

    async def close_connection(_reader, writer):
        writer.close()

    server = await asyncio.start_server(close_connection, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def local_connect(_host, _port, **kwargs):
        return await original_connect("127.0.0.1", port, **kwargs)

    monkeypatch.setattr(handoff.asyncio, "open_connection", local_connect)
    monkeypatch.setenv("ADP_CREW_HOST_CLIENT_CERT_FILE", str(certificate))
    monkeypatch.setenv("ADP_CREW_HOST_CLIENT_KEY_FILE", str(key))
    try:
        current = replace(host, host_fingerprint=fingerprint)
        context = await handoff.crew_host_context(current)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        reader, writer = await original_connect("127.0.0.1", port, ssl=context, server_hostname="10.4.0.3")
        assert reader is not None
        writer.close()
        await writer.wait_closed()
        with pytest.raises(HTTPException) as denied:
            await handoff.crew_host_context(replace(current, host_fingerprint="f" * 64))
        assert denied.value.status_code == 503
    finally:
        server.close()
        await server.wait_closed()
