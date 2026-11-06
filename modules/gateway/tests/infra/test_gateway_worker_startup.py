"""Exercise the shipped serving commands with slow spawned-interpreter hooks."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

GATEWAY = Path(__file__).resolve().parents[2]


def serving_command(source):
    if source == "image":
        line = next(line for line in (GATEWAY / "Dockerfile").read_text().splitlines() if line.startswith("CMD "))
        return json.loads(line.removeprefix("CMD "))
    deployment = yaml.safe_load((GATEWAY / "k8s/deployment.yaml").read_text())
    container = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "bedrockgateway")
    if "command" not in container and "args" not in container:
        return serving_command("image")
    return container["command"] + container["args"]


@pytest.mark.parametrize("source", ["image", "deployment"])
def test_serving_process_starts_without_respawning_slow_instrumented_children(tmp_path, source):
    command = serving_command(source)
    assert command == serving_command("image") == serving_command("deployment")
    assert command[command.index("--timeout-keep-alive") + 1] == "910"
    # Keep the real executable, factory and serving options. Substitute only a
    # dependency-free ASGI factory, so this checks process startup without AWS.
    package = tmp_path / "src"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "app.py").write_text("""import json, os
def create_app():
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": json.dumps({"status": "healthy", "pid": os.getpid()}).encode()})
    return app
""")
    # Injected telemetry initializes before a spawned worker reaches Uvicorn's
    # ping thread. Eight seconds exceeds the pinned supervisor's five seconds.
    # The serving process must retain its own hooks and avoid this child path.
    marker = tmp_path / "bootstrap.log"
    (tmp_path / "sitecustomize.py").write_text("""import os, sys, time
from pathlib import Path
child = "--multiprocessing-fork" in sys.argv
with Path(os.environ["ADP_TEST_BOOTSTRAP_LOG"]).open("a") as stream:
    stream.write(str(os.getpid()) + (":child\\n" if child else ":serving\\n"))
if child:
    time.sleep(8)
""")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command[command.index("--host") + 1] = "127.0.0.1"
    command[command.index("--port") + 1] = str(port)
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", ADP_TEST_BOOTSTRAP_LOG=str(marker))
    log = tmp_path / "server.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", *command], cwd=tmp_path, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            deadline = time.monotonic() + 15
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while True:
                try:
                    with client.open(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                        payload = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    assert process.poll() is None, log.read_text()
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.05)
            assert payload == {"status": "healthy", "pid": process.pid}
            assert marker.read_text().splitlines() == [f"{process.pid}:serving"]
            assert "Child process" not in log.read_text()
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
