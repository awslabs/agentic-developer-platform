"""Error transport for the native Bedrock stream (#5025)."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing

from fastapi import HTTPException
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Send

from src.proxy.eventstream_codec import encode_event_message
from src.proxy.exceptions import BedrockInvocationError


class BedrockStreamingResponse(StreamingResponse):
    """Defer HTTP success until the first wire frame, preserving disconnect handling.

    A keepalive also commits the status: long initial waits stay alive, and any
    subsequent failure is an explicit terminal error in the negotiated protocol.
    Error frames live outside the usage/logging iterators so they cannot turn a
    failed invocation into successfully completed model usage.
    """

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        error_handler: Callable[[Exception], HTTPException],
        media_type: str,
        headers: dict[str, str],
    ):
        super().__init__(content, media_type=media_type, headers=headers)
        self.error_handler = error_handler

    def _error_frame(self, error: HTTPException) -> bytes:
        detail = error.detail
        if self.media_type == "text/event-stream":
            payload = {"type": "error", "error": {"type": detail["error"], **{k: v for k, v in detail.items() if k != "error"}}}
            return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()
        # These are the SDK-modeled fields of ModelStreamErrorException. Keep
        # structured ADP details in the modeled originalMessage field.
        return encode_event_message(
            json.dumps({"message": detail["message"], "originalStatusCode": error.status_code, "originalMessage": json.dumps(detail)}).encode(),
            {":message-type": "exception", ":exception-type": "modelStreamErrorException", ":content-type": "application/json"},
        )

    async def _send_error(self, error: Exception, send: Send, *, started: bool) -> None:
        mapped = self.error_handler(error)
        if started:
            await send({"type": "http.response.body", "body": self._error_frame(mapped), "more_body": False})
        else:
            response = JSONResponse({"detail": mapped.detail}, status_code=mapped.status_code, headers=mapped.headers)
            await send({"type": "http.response.start", "status": response.status_code, "headers": response.raw_headers})
            await send({"type": "http.response.body", "body": response.body, "more_body": False})

    async def stream_response(self, send: Send) -> None:
        started = False
        async with aclosing(self.body_iterator):
            while True:
                # Catch only upstream reads. A failed client send is a disconnect,
                # not a provider error, and must still close the entire iterator chain.
                try:
                    chunk = await anext(self.body_iterator)
                except StopAsyncIteration:
                    break
                except Exception as error:
                    await self._send_error(error, send, started=started)
                    return
                if not chunk:
                    continue
                if not started:
                    await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
                    started = True
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if not started:
                await self._send_error(BedrockInvocationError("Bedrock returned an empty stream"), send, started=False)
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
