"""Tests for the AWS eventstream encoder on the Bedrock-native streaming route.

The bug this guards: /model/{id}/invoke-with-response-stream returned text SSE,
which Bedrock-flavored clients (the Claude Code SDK on every agent worker)
cannot decode — the SDK consumed the whole stream, failed at EOF, and silently
retried the request non-streaming, so every agent turn was generated and billed
twice. The route must return binary eventstream by default; the framing is
verified here against botocore's decoder — an independent implementation of the
same wire format the client SDKs use.
"""

import base64
import json

import pytest
from botocore.eventstream import EventStreamBuffer

from src.proxy.eventstream_codec import (
    EVENTSTREAM_CONTENT_TYPE,
    encode_bedrock_chunk,
    encode_event_message,
    sse_to_eventstream,
)


def decode_all(data: bytes):
    """Decode framed messages with botocore's (independent) eventstream parser."""
    buf = EventStreamBuffer()
    buf.add_data(data)
    return list(buf)


def decoded_events(data: bytes) -> list[dict]:
    """Recover the Anthropic event JSONs from framed Bedrock chunks."""
    events = []
    for msg in decode_all(data):
        envelope = json.loads(msg.payload)
        events.append(json.loads(base64.b64decode(envelope["bytes"])))
    return events


async def astream(chunks: list[bytes]):
    for c in chunks:
        yield c


class TestFraming:
    """The binary framing must be decodable by an independent implementation."""

    def test_round_trips_through_botocore_decoder(self):
        event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}
        frame = encode_bedrock_chunk(json.dumps(event).encode())

        messages = decode_all(frame)

        assert len(messages) == 1
        assert messages[0].headers[":event-type"] == "chunk"
        assert messages[0].headers[":content-type"] == "application/json"
        assert messages[0].headers[":message-type"] == "event"
        envelope = json.loads(messages[0].payload)
        assert json.loads(base64.b64decode(envelope["bytes"])) == event

    def test_botocore_validates_crcs(self):
        # botocore raises on checksum mismatch, so a clean decode of a corrupted
        # frame would mean our CRCs are not being checked at all.
        from botocore.exceptions import EventStreamError  # noqa: F401  (import guard)

        frame = bytearray(encode_bedrock_chunk(b'{"type": "message_stop"}'))
        frame[-1] ^= 0xFF  # corrupt the message CRC

        with pytest.raises(Exception):
            decode_all(bytes(frame))

    def test_multiple_frames_concatenate(self):
        frames = b"".join(encode_bedrock_chunk(json.dumps({"type": t}).encode()) for t in ("message_start", "content_block_stop", "message_stop"))
        assert [e["type"] for e in decoded_events(frames)] == [
            "message_start",
            "content_block_stop",
            "message_stop",
        ]

    def test_empty_headers_message(self):
        msg = decode_all(encode_event_message(b"{}", {}))
        assert len(msg) == 1
        assert msg[0].payload == b"{}"


class TestSseToEventstream:
    """The SSE→eventstream converter must survive arbitrary chunk boundaries."""

    @pytest.mark.asyncio
    async def test_converts_one_block_per_chunk(self):
        # The shape StreamHandler._format_bedrock_sse actually yields.
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
            {"type": "message_stop"},
        ]
        chunks = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]

        out = b"".join([frame async for frame in sse_to_eventstream(astream(chunks))])

        assert decoded_events(out) == events

    @pytest.mark.asyncio
    async def test_survives_blocks_split_across_chunks(self):
        event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "split"}}
        raw = f"event: content_block_delta\ndata: {json.dumps(event)}\n\n".encode()
        # Feed one byte at a time — the cruellest possible chunking.
        chunks = [raw[i : i + 1] for i in range(len(raw))]

        out = b"".join([frame async for frame in sse_to_eventstream(astream(chunks))])

        assert decoded_events(out) == [event]

    @pytest.mark.asyncio
    async def test_multiple_blocks_in_one_chunk(self):
        events = [{"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
        combined = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)

        out = b"".join([frame async for frame in sse_to_eventstream(astream([combined]))])

        assert decoded_events(out) == events

    @pytest.mark.asyncio
    async def test_trailing_block_without_blank_line_still_forwarded(self):
        # Upstream ending abruptly must not swallow the final event.
        event = {"type": "message_stop"}
        raw = f"event: message_stop\ndata: {json.dumps(event)}".encode()  # no \n\n

        out = b"".join([frame async for frame in sse_to_eventstream(astream([raw]))])

        assert decoded_events(out) == [event]

    @pytest.mark.asyncio
    async def test_non_json_data_dropped(self):
        # "[DONE]" is OpenAI-format-only and has no eventstream equivalent.
        out = b"".join([frame async for frame in sse_to_eventstream(astream([b"data: [DONE]\n\n"]))])
        assert out == b""

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        out = b"".join([frame async for frame in sse_to_eventstream(astream([]))])
        assert out == b""


class TestRouteContentNegotiation:
    """The Bedrock-native route defaults to binary; explicit SSE opt-out remains."""

    def test_content_type_constant(self):
        assert EVENTSTREAM_CONTENT_TYPE == "application/vnd.amazon.eventstream"

    def test_route_source_negotiates_on_accept(self):
        # Structural check: the streaming-by-path route must consult Accept and
        # return the eventstream converter by default. (A full request-level
        # test needs the app auth stack; the live smoke covers that.)
        import inspect

        from src.proxy import routes

        src = inspect.getsource(routes.invoke_model_stream_by_path)
        assert "sse_to_eventstream" in src
        assert "text/event-stream" in src
        assert "EVENTSTREAM_CONTENT_TYPE" in src
