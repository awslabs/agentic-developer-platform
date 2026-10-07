"""Bounded, lease-bound model frames with a final durable accounting receipt."""

import asyncio
import hashlib
import json

import anyio
import rfc8785
from starlette.responses import StreamingResponse

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_model_json import canonical_model_json

MEDIA_TYPE = "application/x-ndjson"
MAX_FRAME_BYTES = 65_536
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 16_384


class ModelStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def model_stream_response(service, *, launch, operation_id, model_id, request, authorize, on_event=None):
    try:
        encoded = canonical_model_json(request)
    except rfc8785.CanonicalizationError as error:
        raise ChatAuthorizationRefusedError("chat model request is not canonical JSON") from error
    if len(encoded) > MAX_FRAME_BYTES:
        raise ChatAuthorizationRefusedError("chat model request exceeds frame bound")
    binding = {
        "run_id": launch.run_id,
        "session_id": launch.session_id,
        "lease_generation": launch.lease_generation,
        "operation_id": operation_id,
        "request_digest": hashlib.sha256(encoded).hexdigest(),
    }

    async def frames():
        queue = asyncio.Queue(maxsize=8)
        text_bytes = 0
        sequence = 0
        stream_bytes = 0

        async def emit(event):
            nonlocal text_bytes
            if (
                set(event) != {"type", "index", "text"}
                or event["type"] != "text_delta"
                or type(event["index"]) is not int
                or not 0 <= event["index"] < 64
                or not isinstance(event["text"], str)
                or not event["text"]
            ):
                raise ChatAuthorizationUnavailableError("invalid model event")
            text_bytes += len(event["text"].encode("utf-8"))
            if text_bytes > MAX_FRAME_BYTES:
                raise ChatAuthorizationUnavailableError("model text exceeds bound")
            if on_event is not None:
                await on_event(event)
            for offset in range(0, len(event["text"]), 1024):
                await queue.put({**event, "text": event["text"][offset : offset + 1024]})

        async def produce():
            try:
                receipt = await service.execute(
                    launch=launch, operation_id=operation_id, model_id=model_id, request=request, authorize=authorize, on_event=emit
                )
                await queue.put({"type": "receipt", "receipt": receipt})
            except Exception as error:
                await queue.put({"type": "error", "code": "denied" if isinstance(error, ChatAuthorizationRefusedError) else "incomplete"})

        def encode(event):
            return (
                json.dumps({**binding, "sequence": sequence, **event}, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()

        producer = asyncio.create_task(produce())

        async def close_producer():
            if not producer.done():
                producer.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await producer
                except asyncio.CancelledError:
                    pass

        try:
            while True:
                event = await queue.get()
                if event["type"] != "error":
                    try:
                        await authorize()
                    except Exception as error:
                        event = {"type": "error", "code": "denied" if isinstance(error, ChatAuthorizationRefusedError) else "incomplete"}
                try:
                    frame = encode(event)
                    if len(frame) > MAX_FRAME_BYTES or stream_bytes + len(frame) > MAX_STREAM_BYTES - MAX_FRAME_BYTES or sequence >= MAX_FRAMES - 1:
                        raise ValueError
                except (ValueError, TypeError, UnicodeError):
                    event = {"type": "error", "code": "incomplete"}
                    frame = encode(event)
                stream_bytes += len(frame)
                sequence += 1
                if event["type"] == "error":
                    await close_producer()
                yield frame
                if event["type"] != "text_delta":
                    return
        finally:
            await close_producer()

    return ModelStreamingResponse(frames(), media_type=MEDIA_TYPE, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
