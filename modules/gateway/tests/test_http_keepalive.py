"""Exercise the image's keepalive setting on real HTTP connections and SIGTERM."""

import http.client
import json
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_image_keepalive_reuses_connections_after_five_seconds_and_drains_on_shutdown(tmp_path):
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    command = json.loads(next(line.removeprefix("CMD ") for line in dockerfile.splitlines() if line.startswith("CMD [")))
    timeout = command[command.index("--timeout-keep-alive") + 1]
    assert int(timeout) > 900  # Existing ingress target idle timeout.
    (tmp_path / "probe.py").write_text("""
import asyncio
from pathlib import Path

async def app(scope, receive, send):
    if scope['path'] == '/slow':
        Path('started').touch()
        await asyncio.sleep(0.3)
    body = str(scope['client'][1]).encode()
    await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})
""")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "probe:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--lifespan",
            "off",
            "--timeout-keep-alive",
            timeout,
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    idle = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    active = http.client.HTTPConnection("127.0.0.1", port, timeout=3)

    def get(connection, path):
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 200
        return response.read()

    try:
        for _ in range(100):
            try:
                first = get(idle, "/")
                break
            except ConnectionRefusedError:
                idle.close()
                assert process.poll() is None
                time.sleep(0.05)
        else:
            raise AssertionError("Uvicorn did not start")
        original_socket = idle.sock
        time.sleep(5.2)
        assert get(idle, "/") == first
        assert idle.sock is original_socket
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(get, active, "/slow")
            for _ in range(100):
                if (tmp_path / "started").exists():
                    break
                time.sleep(0.01)
            assert (tmp_path / "started").exists()
            process.terminate()
            assert pending.result(timeout=3)
            process.wait(timeout=3)  # Idle keepalive must not delay graceful shutdown.
        # Recent Uvicorn versions re-raise the original signal after draining.
        assert process.returncode in (0, -signal.SIGTERM)
        assert b"Finished server process" in process.stderr.read()
    finally:
        idle.close()
        active.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
