"""Loopback checks for metered physical submission without a billed provider."""

import asyncio
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src.budget.metering_admission import MeteringAdmissionError, MeteringDispatchPermit
from src.orchestration.provider_quotes import ProviderQuote, request_digest
from src.proxy.metered_transport import MeteredAmbiguousSend, MeteredHTTPTransport, MeteredProvenUnsentError, MeteredUnsentCancelled


class Permit:
    def __init__(self):
        self.calls = []
        self.reject = False
        self.revision = "published-revision"
        self.physical_submitted = False

    def mark_physical_submission(self):
        self.physical_submitted = True

    def consume(self, **binding):
        self.calls.append(binding)
        if self.reject or binding["pricing_revision"] != self.revision or len(self.calls) > 1:
            raise MeteringAdmissionError("dispatch permit expired or already consumed")


@asynccontextmanager
async def loopback(tls: ssl.SSLContext | None = None):
    received = []
    arrived = asyncio.Event()
    release = asyncio.Event()
    release.set()

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1].strip()) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            received.append(body)
            arrived.set()
            await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    try:
        async with server:
            scheme, hostname = ("https", "localhost") if tls else ("http", "127.0.0.1")
            yield f"{scheme}://{hostname}:{server.sockets[0].getsockname()[1]}/openai/v1/responses", received, arrived, release
    finally:
        release.set()


@pytest.fixture(autouse=True)
def published_pricing(monkeypatch):
    monkeypatch.setattr(
        "src.proxy.metered_transport.adapter_for",
        lambda path: SimpleNamespace(current_pricing_revision=lambda: "published-revision"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_bound_request_reaches_physical_provider_once(stream):
    async with loopback() as (url, received, arrived, _release):
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request = client.build_request("POST", url, content=b'{"model":"test"}')
            permit = Permit()
            response = await transport.dispatch_once(client, request, permit, model="test", stream=stream)
            assert response.status_code == 200
            assert await response.aread() == b"OK"
            await response.aclose()
            await asyncio.wait_for(arrived.wait(), 1)
            assert received == [request.content]
            assert permit.calls == [{"submitted": request.content, "model": "test", "destination": url, "pricing_revision": "published-revision"}]


@pytest.mark.asyncio
async def test_real_one_use_permit_binds_physical_request_and_rejects_replay():
    async with loopback() as (url, received, _arrived, _release):
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request = client.build_request("POST", url, content=b"bound-bytes")
            model = "openai.gpt-6-astra"
            quote = ProviderQuote(
                provider="bedrock",
                endpoint="/openai/v1/responses",
                capability="responses",
                billing_model_id=model,
                request_sha256=request_digest(request.content),
                pricing_revision="published-revision",
                currency="USD",
                issued_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(seconds=10),
                max_input_tokens=10,
                max_output_tokens=10,
                max_tool_tokens=0,
                max_cache_write_tokens=0,
                total_usd=Decimal("0.001"),
                evidence_source="local-loopback",
            )
            permit = MeteringDispatchPermit(
                tenant_id="local-test",
                request_id="local-permit",
                revision=1,
                producer_epoch=1,
                quote=quote,
                submitted_sha256=request_digest(request.content),
                destination_sha256=request_digest(url.encode()),
                expires_at=datetime.now(UTC) + timedelta(seconds=10),
                monotonic_deadline=monotonic() + 10,
            )
            response = await transport.dispatch_once(client, request, permit, model=model, stream=False)
            assert response.status_code == 200
            assert received == [b"bound-bytes"]
            with pytest.raises(MeteredProvenUnsentError):
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"bound-bytes"), permit, model=model, stream=False)
            assert received == [b"bound-bytes"]


@pytest.mark.asyncio
async def test_tls_ca_checked_before_permit_consumption():
    with TemporaryDirectory() as directory:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(str(cert_path), str(key_path))
        async with loopback(server_tls) as (url, received, _arrived, _release):
            trusted = MeteredHTTPTransport(verify=ssl.create_default_context(cafile=str(cert_path)))
            async with httpx.AsyncClient(transport=trusted, trust_env=False) as client:
                permit = Permit()
                response = await trusted.dispatch_once(
                    client, client.build_request("POST", url, content=b"trusted"), permit, model="test", stream=False
                )
                assert response.status_code == 200
                assert received == [b"trusted"]
                assert len(permit.calls) == 1
            untrusted = MeteredHTTPTransport()
            async with httpx.AsyncClient(transport=untrusted, trust_env=False) as client:
                permit = Permit()
                with pytest.raises(MeteredProvenUnsentError):
                    await untrusted.dispatch_once(client, client.build_request("POST", url, content=b"untrusted"), permit, model="test", stream=False)
                assert permit.calls == []
                assert received == [b"trusted"]


@pytest.mark.asyncio
async def test_expiry_after_pool_wait_rejects_without_provider_request():
    async with loopback() as (url, received, arrived, release):
        release.clear()
        transport = MeteredHTTPTransport(limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request = client.build_request("POST", url, content=b"first")
            first = asyncio.create_task(transport.dispatch_once(client, request, Permit(), model="test", stream=False))
            await asyncio.wait_for(arrived.wait(), 1)
            second_permit = Permit()
            second = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"second"), second_permit, model="test", stream=False)
            )
            await asyncio.sleep(0)
            second_permit.reject = True
            release.set()
            assert (await asyncio.wait_for(first, 1)).status_code == 200
            with pytest.raises(MeteringAdmissionError):
                await asyncio.wait_for(second, 1)
            assert len(second_permit.calls) == 1
            assert received == [b"first"]


@pytest.mark.asyncio
async def test_pricing_publication_before_handoff_refuses_without_sending(monkeypatch):
    revision = ["published-revision"]
    monkeypatch.setattr(
        "src.proxy.metered_transport.adapter_for",
        lambda path: SimpleNamespace(current_pricing_revision=lambda: revision[0]),
    )
    async with loopback() as (url, received, arrived, release):
        release.clear()
        transport = MeteredHTTPTransport(limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            first = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"first"), Permit(), model="test", stream=False)
            )
            await asyncio.wait_for(arrived.wait(), 1)
            permit = Permit()
            second = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"second"), permit, model="test", stream=False)
            )
            await asyncio.sleep(0)
            revision[0] = "new-generation"
            release.set()
            assert (await asyncio.wait_for(first, 1)).status_code == 200
            with pytest.raises(MeteringAdmissionError):
                await asyncio.wait_for(second, 1)
            assert permit.calls[0]["pricing_revision"] == "new-generation"
            assert received == [b"first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_delay", [0, 1.1], ids=["immediate-handoff", "delayed-handoff"])
async def test_pricing_publication_after_handoff_does_not_reprice(monkeypatch, handoff_delay):
    revision = ["published-revision"]
    monkeypatch.setattr(
        "src.proxy.metered_transport.adapter_for",
        lambda path: SimpleNamespace(current_pricing_revision=lambda: revision[0]),
    )
    async with loopback() as (url, received, arrived, release):
        release.clear()
        transport = MeteredHTTPTransport()
        connect_tcp = transport._pool._network_backend.connect_tcp

        async def delayed_connect(*args, **kwargs):
            await asyncio.sleep(handoff_delay)
            return await connect_tcp(*args, **kwargs)

        monkeypatch.setattr(transport._pool._network_backend, "connect_tcp", delayed_connect)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            permit = Permit()
            sent = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"historical"), permit, model="test", stream=False)
            )
            await asyncio.wait_for(arrived.wait(), 10)
            revision[0] = "new-generation"
            release.set()
            assert (await asyncio.wait_for(sent, 10)).status_code == 200
            assert permit.calls[0]["pricing_revision"] == "published-revision"
            assert received == [b"historical"]


@pytest.mark.asyncio
async def test_cancelled_pool_wait_has_no_dispatch():
    async with loopback() as (url, received, arrived, release):
        release.clear()
        transport = MeteredHTTPTransport(limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            first = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"first"), Permit(), model="test", stream=False)
            )
            await asyncio.wait_for(arrived.wait(), 1)
            permit = Permit()
            cancelled = asyncio.create_task(
                transport.dispatch_once(client, client.build_request("POST", url, content=b"cancelled"), permit, model="test", stream=False)
            )
            await asyncio.sleep(0)
            cancelled.cancel()
            with pytest.raises(MeteredUnsentCancelled) as outcome:
                await cancelled
            assert outcome.value.permit is permit
            release.set()
            assert (await asyncio.wait_for(first, 1)).status_code == 200
            assert permit.calls == []
            assert received == [b"first"]


@pytest.mark.asyncio
async def test_lost_send_acknowledgment_never_replays(monkeypatch):
    received = []

    async def close_without_headers(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = await reader.readexactly(len(b"sent-once"))
        received.append(body)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(close_without_headers, "127.0.0.1", 0)
    async with server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/openai/v1/responses"
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            permit = Permit()
            with pytest.raises(MeteredAmbiguousSend) as outcome:
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"sent-once"), permit, model="test", stream=False)
            assert isinstance(outcome.value.__cause__, httpx.RemoteProtocolError)
            assert permit.physical_submitted
            assert received == [b"sent-once"]
            assert len(permit.calls) == 1
            with pytest.raises(MeteringAdmissionError):
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"sent-once"), permit, model="test", stream=False)
            assert received == [b"sent-once"]


@pytest.mark.asyncio
async def test_wrong_client_or_missing_permit_never_sends():
    async with loopback() as (url, received, _arrived, _release):
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            async with httpx.AsyncClient(trust_env=False) as other:
                with pytest.raises(MeteringAdmissionError, match="different transport"):
                    await transport.dispatch_once(other, other.build_request("POST", url, content=b"wrong"), Permit(), model="test", stream=True)
            with pytest.raises(MeteringAdmissionError, match="no dispatch permit"):
                await client.post(url, content=b"unmetered")
        bypass = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport, mounts={"all://": bypass}, trust_env=False) as client:
            with pytest.raises(MeteringAdmissionError, match="different transport"):
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"bypass"), Permit(), model="test", stream=False)
        assert received == []


@pytest.mark.asyncio
async def test_response_hook_refused_before_any_submission():
    async with loopback() as (url, received, _arrived, _release):
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:

            async def hook(response):
                await client.post(url, content=b"unbound")

            client.event_hooks["response"].append(hook)
            permit = Permit()
            with pytest.raises(MeteredProvenUnsentError) as outcome:
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"bound"), permit, model="test", stream=False)
            assert outcome.value.permit is permit
            assert permit.calls == []
            assert received == []


@pytest.mark.asyncio
async def test_late_response_hook_cannot_send_second_request():
    async with loopback() as (url, received, _arrived, _release):
        transport = MeteredHTTPTransport()
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:

            async def hook(response):
                await client.post(url, content=b"unbound")

            original_send = client.send

            async def send_with_late_hook(request, **kwargs):
                client.event_hooks["response"].append(hook)
                try:
                    return await original_send(request, **kwargs)
                finally:
                    client.event_hooks["response"].remove(hook)

            client.send = send_with_late_hook
            permit = Permit()
            with pytest.raises(MeteredAmbiguousSend):
                await transport.dispatch_once(client, client.build_request("POST", url, content=b"bound"), permit, model="test", stream=False)
            assert received == [b"bound"]
            assert len(permit.calls) == 1
            assert permit.physical_submitted
