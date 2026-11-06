"""HTTP/1.1 transport with a guarded, synchronous request-write boundary."""

from __future__ import annotations

import asyncio
import select
import ssl
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from src.budget.metering_admission import MeteringAdmissionError, MeteringDispatchPermit
from src.orchestration.provider_quotes import adapter_for
from src.proxy.mantle_service import MANTLE_RESPONSES_PATH


class MeteredProvenUnsentError(MeteringAdmissionError):
    def __init__(self, permit: MeteringDispatchPermit, message: str = "metered request was not submitted"):
        super().__init__(message)
        self.permit = permit


class MeteredUnsentCancelled(asyncio.CancelledError):
    def __init__(self, permit: MeteringDispatchPermit):
        super().__init__("metered request cancelled before submission")
        self.permit = permit


class MeteredAmbiguousSend(httpx.TransportError):
    pass


@dataclass
class _Dispatch:
    request: httpx.Request
    permit: MeteringDispatchPermit
    model: str
    submitted: bool = False
    claimed: bool = False

    def before_write(self) -> None:
        if self.submitted:
            return
        adapter = adapter_for(MANTLE_RESPONSES_PATH)
        if adapter is None:
            raise MeteringAdmissionError("published pricing revision unavailable")
        self.permit.consume(
            submitted=self.request.content,
            model=self.model,
            destination=str(self.request.url),
            pricing_revision=adapter.current_pricing_revision(),
        )
        self.permit.mark_physical_submission()
        self.submitted = True


_CURRENT_DISPATCH: ContextVar[_Dispatch | None] = ContextVar("metered_responses_dispatch", default=None)


class _WriterStream(AsyncNetworkStream):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(self._reader.read(max_bytes), timeout)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout() from exc
        except OSError as exc:
            raise httpcore.ReadError() from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        dispatch = _CURRENT_DISPATCH.get()
        if dispatch is None:
            raise MeteringAdmissionError("metered transport has no dispatch permit")
        dispatch.before_write()
        try:
            self._writer.write(buffer)
            await asyncio.wait_for(self._writer.drain(), timeout)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout() from exc
        except OSError as exc:
            raise httpcore.WriteError() from exc

    async def aclose(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        except (OSError, TimeoutError):
            pass

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None) -> _WriterStream:
        try:
            await asyncio.wait_for(self._writer.start_tls(ssl_context, server_hostname=server_hostname, ssl_handshake_timeout=timeout), timeout)
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            await self.aclose()
            raise httpcore.ConnectError() from exc
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "is_readable":
            sock = self._writer.get_extra_info("socket")
            try:
                return not sock or bool(select.select([sock], [], [], 0)[0])
            except (OSError, ValueError):
                return True
        return self._writer.get_extra_info({"client_addr": "sockname", "server_addr": "peername", "socket": "socket"}.get(info, info))


class _WriterBackend(AsyncNetworkBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> AsyncNetworkStream:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, local_addr=(local_address, 0) if local_address else None), timeout
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout() from exc
        except OSError as exc:
            raise httpcore.ConnectError() from exc
        if socket_options:
            sock = writer.get_extra_info("socket")
            try:
                for option in socket_options:
                    sock.setsockopt(*option)
            except OSError as exc:
                writer.close()
                await writer.wait_closed()
                raise httpcore.ConnectError() from exc
        return _WriterStream(reader, writer)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> AsyncNetworkStream:
        raise httpcore.ConnectError("metered transport does not support unix sockets")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class MeteredHTTPTransport(httpx.AsyncHTTPTransport):
    """Use pinned HTTPX/httpcore's pool and CA configuration with guarded socket writes."""

    def __init__(self, *, verify: ssl.SSLContext | str | bool = True, limits: httpx.Limits | None = None):
        limits = limits or httpx.Limits(max_connections=20, max_keepalive_connections=10)
        super().__init__(verify=verify, http2=False, limits=limits, retries=0)
        ssl_context = self._pool._ssl_context
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http2=False,
            retries=0,
            network_backend=_WriterBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        dispatch = _CURRENT_DISPATCH.get()
        if dispatch is None:
            raise MeteringAdmissionError("metered transport has no dispatch permit")
        if request is not dispatch.request or dispatch.claimed:
            raise MeteringAdmissionError("metered transport requires exactly one bound request")
        dispatch.claimed = True
        return await super().handle_async_request(request)

    async def dispatch_once(
        self, client: httpx.AsyncClient, request: httpx.Request, permit: MeteringDispatchPermit, *, model: str, stream: bool
    ) -> httpx.Response:
        if client._transport_for_url(request.url) is not self:
            raise MeteredProvenUnsentError(permit, "metered client uses a different transport")
        if client.follow_redirects or any(client.event_hooks.values()) or client.auth is not None:
            raise MeteredProvenUnsentError(permit, "metered client must not rewrite or replay the bound request")
        if not isinstance(request.stream, httpx.ByteStream):
            raise MeteredProvenUnsentError(permit, "metered request body must be immutable bytes")
        try:
            request.content
        except httpx.RequestNotRead as exc:
            raise MeteredProvenUnsentError(permit, "metered request body must be immutable bytes") from exc
        state = _Dispatch(request, permit, model)
        token = _CURRENT_DISPATCH.set(state)
        try:
            response = await client.send(request, stream=stream)
            if not state.submitted:
                await response.aclose()
                raise MeteringAdmissionError("metered transport returned without a physical submission")
            return response
        except asyncio.CancelledError as exc:
            if state.submitted:
                raise
            raise MeteredUnsentCancelled(permit) from exc
        except Exception as exc:
            if state.submitted:
                raise MeteredAmbiguousSend("metered submission outcome is ambiguous") from exc
            raise MeteredProvenUnsentError(permit) from exc
        finally:
            _CURRENT_DISPATCH.reset(token)
