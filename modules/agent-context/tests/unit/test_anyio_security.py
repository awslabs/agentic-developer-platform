"""Exercise the TLS identity and process-pool regressions fixed in AnyIO 4.14.2."""

import asyncio
import os
import shutil
import signal
import ssl
import subprocess
import sys

import anyio
import pytest
from anyio.streams.tls import TLSStream


def _write_worker_stderr():
    # This exceeds a pipe's capacity. Worker stderr must not block its result.
    written = sys.stderr.write("x" * (1024 * 1024))
    sys.stderr.flush()
    return written


@pytest.mark.skipif(os.name != "posix", reason="Worker cleanup uses POSIX process groups")
def test_process_worker_stderr_does_not_deadlock():
    # Bound the whole subprocess: the vulnerable version also hangs during
    # cancellation cleanup when its worker stderr pipe fills up.
    script = """
import anyio
from anyio import to_process
from tests.unit.test_anyio_security import _write_worker_stderr
async def check():
    written = await to_process.run_sync(_write_worker_stderr)
    assert written == 1024 * 1024
anyio.run(check)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="TLS fixture needs openssl")
async def test_tls_rejects_idna2003_certificate_alias(tmp_path):
    # IDNA2003 maps faß.example to fass.example. IDNA2008 keeps them distinct.
    # A certificate for the ASCII alias must not authenticate the Unicode name.
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    with anyio.fail_after(15):
        await anyio.run_process(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=fass.example",
                "-addext",
                "subjectAltName=DNS:fass.example",
            ],
            check=True,
        )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(cert))

    async def connected(reader, writer):
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, ssl.SSLError):
            pass

    server = await asyncio.start_server(connected, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with anyio.fail_after(5):
            async with await anyio.connect_tcp("127.0.0.1", port) as stream:
                with pytest.raises(ssl.SSLCertVerificationError):
                    await TLSStream.wrap(
                        stream,
                        hostname="faß.example",
                        ssl_context=client_context,
                    )
