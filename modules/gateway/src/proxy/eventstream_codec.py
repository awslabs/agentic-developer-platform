"""AWS eventstream encoder for the Bedrock-native streaming route.

Why this exists: clients that speak Bedrock's wire protocol (the Claude Code
SDK with ``CLAUDE_CODE_USE_BEDROCK=1`` is the one that matters — every agent
worker) call ``/model/{id}/invoke-with-response-stream`` and decode the
response as ``application/vnd.amazon.eventstream`` — the binary framing real
Bedrock always returns. The gateway historically re-emitted the stream as
Anthropic-style text SSE instead. The SDK's eventstream decoder reads the
ASCII ``event:`` bytes as a bogus giant frame length, buffers the ENTIRE
stream waiting for that frame, fails at EOF, and silently retries the whole
request non-streaming — so every agent turn was generated (and billed) twice.
Verified against claude-cli 2.1.220 (the agent-runtime version): text SSE →
full stream consumed + immediate ``/invoke`` fallback; binary eventstream →
single request, stream used.

This module re-encodes the gateway's already-built SSE chunks back into the
binary framing at the route edge. Converting at the edge (after the logging /
usage wrappers) keeps every existing SSE-parsing hook — usage extraction,
chat logging — untouched.

Wire format (https://docs.aws.amazon.com/AmazonS3/latest/API/RESTSelectObjectAppendix.html —
the same framing all AWS eventstream services share):

    [4B total length][4B headers length][4B prelude CRC32]
    [headers][payload][4B message CRC32]

Each Bedrock chunk's payload is ``{"bytes": "<base64 of the Anthropic event
JSON>"}`` with headers ``:event-type=chunk``, ``:content-type=application/json``,
``:message-type=event`` — byte-identical in shape to what Bedrock itself sends.
"""

import base64
import json
import struct
import zlib
from collections.abc import AsyncIterator

# Header value type 7 = string (the only type Bedrock chunk headers use).
_HEADER_TYPE_STRING = 7

#: Headers carried on every Bedrock content chunk, matching real Bedrock output.
_CHUNK_HEADERS: dict[str, str] = {
    ":event-type": "chunk",
    ":content-type": "application/json",
    ":message-type": "event",
}

#: Content type the route must declare when returning this framing.
EVENTSTREAM_CONTENT_TYPE = "application/vnd.amazon.eventstream"


def _encode_header(name: str, value: str) -> bytes:
    """Encode one eventstream string header: [1B name len][name][1B type][2B value len][value]."""
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return bytes([len(name_bytes)]) + name_bytes + bytes([_HEADER_TYPE_STRING]) + struct.pack(">H", len(value_bytes)) + value_bytes


def encode_event_message(payload: bytes, headers: dict[str, str]) -> bytes:
    """Frame one eventstream message: prelude + prelude CRC + headers + payload + message CRC."""
    header_block = b"".join(_encode_header(k, v) for k, v in headers.items())
    headers_len = len(header_block)
    # 12 = prelude (8) + prelude CRC (4); trailing 4 = message CRC.
    total_len = 12 + headers_len + len(payload) + 4
    prelude = struct.pack(">II", total_len, headers_len)
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + prelude_crc + header_block + payload
    message_crc = struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)
    return message + message_crc


def encode_bedrock_chunk(event_json: bytes) -> bytes:
    """Wrap one Anthropic event JSON as a Bedrock ``chunk`` eventstream message.

    Bedrock's chunk payload is a JSON envelope whose ``bytes`` field carries the
    base64-encoded event — decoders (the Anthropic Bedrock SDK included)
    base64-decode ``bytes`` to recover the event.
    """
    payload = json.dumps({"bytes": base64.b64encode(event_json).decode("ascii")}).encode("utf-8")
    return encode_event_message(payload, _CHUNK_HEADERS)


#: Keep-alive frame for the binary eventstream path. A pre-encoded Bedrock chunk
#: carrying an Anthropic ``ping`` event — the protocol's own idle heartbeat, which
#: the SDK decodes and ignores. An SSE-comment keep-alive cannot be used here:
#: ``sse_to_eventstream`` drops non-``data:`` lines, so a comment injected upstream
#: would vanish, and raw SSE bytes injected downstream would corrupt the framing.
#: Injected only during silence to hold CloudFront's ~60s origin idle timeout open.
EVENTSTREAM_KEEPALIVE = encode_bedrock_chunk(b'{"type": "ping"}')


async def sse_to_eventstream(sse_stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-encode a text-SSE byte stream as AWS binary eventstream frames.

    Buffers across chunk boundaries: upstream normally yields one complete
    ``event: X\\ndata: {json}\\n\\n`` block per chunk (see
    ``StreamHandler._format_bedrock_sse``), but nothing downstream may rely on
    that, so partial blocks are held until their terminating blank line
    arrives. Non-``data:`` lines (event names, comments, keep-alives) carry no
    payload on this path and are dropped — the event type is inside the JSON.
    """
    buffer = b""
    async for chunk in sse_stream:
        buffer += chunk
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            for frame in _frames_from_sse_block(block):
                yield frame
    # A final block without its trailing blank line (upstream ended abruptly)
    # still gets forwarded rather than silently dropped.
    if buffer.strip():
        for frame in _frames_from_sse_block(buffer):
            yield frame


def _frames_from_sse_block(block: bytes) -> list[bytes]:
    """Extract ``data:`` payloads from one SSE block and frame each as a chunk."""
    frames: list[bytes] = []
    for line in block.split(b"\n"):
        if line.startswith(b"data: "):
            payload = line[len(b"data: ") :].strip()
            # Only JSON events are Bedrock chunks; OpenAI-style "[DONE]"
            # markers (never produced on the bedrock path) have no framing
            # equivalent and are dropped.
            if payload.startswith(b"{"):
                frames.append(encode_bedrock_chunk(payload))
    return frames
