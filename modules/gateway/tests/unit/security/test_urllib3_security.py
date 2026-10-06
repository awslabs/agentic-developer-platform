"""Reject oversized HTTP chunk-size fields without buffering the whole field."""

from http.client import HTTPResponse as WireResponse
from io import BytesIO

import pytest
from urllib3.exceptions import ProtocolError
from urllib3.response import HTTPResponse


class CountingBody(BytesIO):
    bytes_read = 0

    def readline(self, size=-1):
        line = super().readline(size)
        self.bytes_read += len(line)
        return line


class SocketFixture:
    def __init__(self, payload):
        self.body = CountingBody(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + payload)

    def makefile(self, *args, **kwargs):
        return self.body


def response(payload):
    sock = SocketFixture(payload)
    wire = WireResponse(sock)
    wire.begin()
    sock.body.bytes_read = 0
    return HTTPResponse(body=wire, headers=dict(wire.getheaders()), preload_content=False), sock.body


@pytest.mark.parametrize("method", ["stream", "read_chunked"])
def test_streaming_rejects_oversized_chunk_size_before_buffering_it(method):
    # The extension is legal syntax; only its excessive size makes it unsafe.
    reply, body = response(b"1;" + b"x" * 200_000 + b"\r\nz\r\n0\r\n\r\n")
    with pytest.raises(ProtocolError):
        list(getattr(reply, method)())
    assert body.bytes_read <= 65_537


@pytest.mark.parametrize("method", ["stream", "read_chunked"])
def test_normal_chunk_extensions_remain_supported(method):
    reply, _ = response(b"2;fixture=value\r\nok\r\n0\r\n\r\n")
    try:
        assert b"".join(getattr(reply, method)()) == b"ok"
    finally:
        reply.close()
