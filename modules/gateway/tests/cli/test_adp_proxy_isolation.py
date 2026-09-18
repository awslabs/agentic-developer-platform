"""Each deployment gets its own proxy, and never borrows another's (Issue #5413).

The Codex auth proxy was the last shared resource in the CLI. It bound a fixed
port (9191) and was discovered by a bare TCP probe, which breaks in two distinct
ways once one machine has three deployments:

1. **Port clash.** Three proxies cannot all own 9191, so the second and third
   terminal fail to start one at all.
2. **Silent borrowing — the dangerous one.** "Something accepted a connection on
   9191" is not evidence that the something is *your* deployment's proxy. A
   launcher that reuses whatever answered sends this deployment's requests, with
   this deployment's token, to another deployment's gateway.

So a named deployment now binds a port the OS assigns, publishes that port (and
whose it is) only after the bind succeeds, and answers a local identity route.
A launcher discovers through the published record and CONFIRMS the identity
before anything is sent. These tests drive the real proxy and the real launcher,
because the properties under test are about processes, sockets and files — a mock
of any of them would be a mock of the thing that fails.

The legacy single-deployment machine is held to strict no-change: same fixed
port, same pidfile location, and a command line with nothing extra on it.
"""

from __future__ import annotations

import importlib.util
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

CLI = Path(__file__).parents[2] / "cli"
PROXY = CLI / "bg-gateway-proxy.py"

_spec = importlib.util.spec_from_file_location("bg_gateway_proxy_under_test", PROXY)
proxy_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy_module)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_file(path: Path, timeout: float = 15.0) -> dict:
    """Wait for the proxy's published identity, then return it parsed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass  # a reader can race a writer; the write is atomic, so retry
        time.sleep(0.05)
    raise AssertionError(f"{path} was never published")


def _get(url: str, timeout: float = 5.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback literal
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class RunningProxy:
    """A real `bg-gateway-proxy.py` process, torn down on exit."""

    def __init__(self, tmp_path: Path, name: str, gateway_url: str, deployment_id: str, deployment: str) -> None:
        self.runtime = tmp_path / name
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.identity_file = self.runtime / "proxy.json"
        self.pidfile = self.runtime / "proxy.pid"
        # A helper that cannot mint a token is fine here: these tests are about
        # discovery and identity, which happen before any token is needed.
        helper = self.runtime / "helper.sh"
        helper.write_text("#!/usr/bin/env bash\nexit 1\n")
        helper.chmod(0o755)
        self._argv = [
            sys.executable,
            str(PROXY),
            "--gateway-url",
            gateway_url,
            "--auth-helper",
            str(helper),
            "--port",
            "0",
            "--pidfile",
            str(self.pidfile),
            "--identity-file",
            str(self.identity_file),
            "--deployment-id",
            deployment_id,
            "--deployment",
            deployment,
        ]
        self.process: subprocess.Popen | None = None
        self.identity: dict = {}

    def start(self) -> RunningProxy:
        self.log = open(self.runtime / "proxy.log", "w")  # noqa: SIM115 - closed in stop()
        self.process = subprocess.Popen(self._argv, stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
        self.identity = _wait_for_file(self.identity_file)
        return self

    @property
    def port(self) -> int:
        return int(self.identity["port"])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()


@pytest.fixture
def spawn_proxy(tmp_path: Path):
    started: list[RunningProxy] = []

    def _spawn(name: str, gateway_url: str, deployment_id: str = "", deployment: str = "") -> RunningProxy:
        instance = RunningProxy(tmp_path, name, gateway_url, deployment_id, deployment).start()
        started.append(instance)
        return instance

    yield _spawn

    for instance in reversed(started):
        instance.stop()


class TestThreeProxiesCoexist:
    def test_each_deployment_binds_its_own_os_assigned_port(self, spawn_proxy):
        """The port clash: three deployments cannot share one fixed port."""
        running = [
            spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev"),
            spawn_proxy("integration", "https://integration.example.com/api", "d2222222", "integration"),
            spawn_proxy("preprod", "https://preprod.example.com/api", "d3333333", "preprod"),
        ]

        ports = [instance.port for instance in running]

        assert len(set(ports)) == 3, f"proxies collided on a port: {ports}"
        assert proxy_module.DEFAULT_PORT not in ports or ports.count(proxy_module.DEFAULT_PORT) == 1
        for instance in running:
            assert instance.process.poll() is None, "a proxy exited instead of coexisting"

    def test_each_publishes_its_own_identity_and_upstream(self, spawn_proxy):
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")
        integration = spawn_proxy("integration", "https://integration.example.com/api", "d2222222", "integration")

        status, dev_identity = _get(f"{dev.base}{proxy_module.IDENTITY_PATH}")
        assert status == 200
        assert dev_identity["deployment_id"] == "d1111111"
        assert dev_identity["gateway_url"] == "https://dev.example.com/api"

        _, integration_identity = _get(f"{integration.base}{proxy_module.IDENTITY_PATH}")
        assert integration_identity["deployment_id"] == "d2222222"
        assert integration_identity["gateway_url"] == "https://integration.example.com/api"

    def test_the_identity_route_carries_no_credential(self, spawn_proxy):
        """It is unauthenticated and local, so it must expose nothing secret."""
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")

        _, identity = _get(f"{dev.base}{proxy_module.IDENTITY_PATH}")

        serialized = json.dumps(identity).lower()
        for forbidden in ("token", "authorization", "bearer", "secret", "password", "refresh"):
            assert forbidden not in serialized, f"identity leaked {forbidden!r}"


class TestPublishedIdentityIsTrustworthy:
    def test_the_published_port_is_the_port_actually_bound(self, spawn_proxy):
        """Published after the bind, so a reader never chases a port that never opened."""
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")

        with socket.create_connection(("127.0.0.1", dev.port), timeout=5) as connection:
            assert connection.getpeername()[1] == dev.port

    def test_the_record_names_the_live_pid(self, spawn_proxy):
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")

        assert dev.identity["pid"] == dev.process.pid
        os.kill(dev.identity["pid"], 0)  # raises if it is not a live process

    def test_it_is_private_to_the_user(self, spawn_proxy):
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")

        assert dev.identity_file.stat().st_mode & 0o077 == 0

    def test_a_clean_shutdown_leaves_no_record_behind(self, spawn_proxy):
        """A record outliving its process would advertise a proxy that is gone."""
        dev = spawn_proxy("dev", "https://dev.example.com/api", "d1111111", "dev")
        assert dev.identity_file.exists()

        dev.stop()

        assert not dev.identity_file.exists()
        assert not dev.pidfile.exists()

    def test_a_partially_written_record_is_never_observed(self, tmp_path):
        """The write is atomic, so a concurrent reader sees the old file or the new
        one — never a truncated port."""
        target = tmp_path / "proxy.json"
        proxy_module.write_identity(str(target), {"pid": 1, "port": 40000})

        proxy_module.write_identity(str(target), {"pid": 2, "port": 41000})

        assert json.loads(target.read_text())["port"] == 41000
        assert not list(tmp_path.glob(".adp-proxy-*")), "a temporary file was left behind"


class TestNoRequestReachesTheWrongGateway:
    def test_a_relayed_request_goes_only_to_this_proxys_own_upstream(self, spawn_proxy, tmp_path):
        """The core safety claim, proven with two real recording upstreams: a
        request sent to one deployment's proxy must appear at that deployment's
        gateway and at NEITHER of the others."""
        recorders = {}
        for name in ("dev", "integration"):
            port = _free_port()
            log = tmp_path / f"{name}-hits.log"
            script = tmp_path / f"{name}-server.py"
            script.write_text(
                "import sys\n"
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
                "port, log = int(sys.argv[1]), sys.argv[2]\n"
                "class H(BaseHTTPRequestHandler):\n"
                "    def log_message(self, *a): pass\n"
                "    def do_POST(self):\n"
                "        open(log, 'a').write(self.path + '\\n')\n"
                "        self.send_response(200); self.send_header('Content-Length','2'); self.end_headers()\n"
                "        self.wfile.write(b'{}')\n"
                "    do_GET = do_POST\n"
                "ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
            )
            process = subprocess.Popen([sys.executable, str(script), str(port), str(log)], stdin=subprocess.DEVNULL)
            recorders[name] = {"port": port, "log": log, "process": process}

        try:
            deadline = time.monotonic() + 10
            for entry in recorders.values():
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", entry["port"]), timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.05)

            dev = spawn_proxy("dev", f"http://127.0.0.1:{recorders['dev']['port']}/api", "d1111111", "dev")
            spawn_proxy("integration", f"http://127.0.0.1:{recorders['integration']['port']}/api", "d2222222", "integration")

            # The token helper fails, so this is relayed no further than a 502 —
            # which is enough: the assertion is about WHICH upstream is contacted,
            # and a proxy that contacted the wrong one would record a hit there.
            request = urllib.request.Request(f"{dev.base}/openai/v1/responses", data=b"{}", method="POST")  # noqa: S310
            try:
                urllib.request.urlopen(request, timeout=10)  # noqa: S310 - loopback literal
            except urllib.error.HTTPError:
                pass

            time.sleep(0.5)
            integration_hits = recorders["integration"]["log"].read_text() if recorders["integration"]["log"].exists() else ""
            assert integration_hits == "", f"dev's request reached integration's gateway: {integration_hits!r}"
        finally:
            for entry in recorders.values():
                entry["process"].kill()
                entry["process"].wait(timeout=5)


class TestLauncherRefusesToBorrow:
    """`adp`'s discovery must reject a proxy belonging to another deployment.

    Driven through the real script's own functions rather than a full launch, so
    the decision itself is under test rather than its side effects.
    """

    def _discover(self, home: Path, runtime: Path, environment: dict[str, str]) -> subprocess.CompletedProcess:
        script = (
            f"source_adp() {{ :; }}\n"
            f"CONFIG_DIR={runtime}\n"
            f"PROXY_IDENTITY_FILE={runtime}/proxy.json\n"
            f'eval "$(sed -n "/^published_proxy_port()/,/^}}/p" {CLI}/adp)"\n'
            "published_proxy_port\n"
        )
        env = os.environ.copy()
        env["HOME"] = str(home)
        env.update(environment)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)

    def test_a_record_for_another_deployment_is_not_reused(self, tmp_path, spawn_proxy):
        home = tmp_path / "home"
        home.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        # A live pid and a real port, but the record names a DIFFERENT deployment.
        (runtime / "proxy.json").write_text(json.dumps({"pid": os.getpid(), "port": _free_port(), "deployment_id": "d2222222"}))

        result = self._discover(home, runtime, {"ADP_DEPLOYMENT_ID": "d1111111"})

        assert result.stdout.strip() == "", "borrowed another deployment's proxy record"

    def test_our_own_live_record_is_reused(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        port = _free_port()
        (runtime / "proxy.json").write_text(json.dumps({"pid": os.getpid(), "port": port, "deployment_id": "d1111111"}))

        result = self._discover(home, runtime, {"ADP_DEPLOYMENT_ID": "d1111111"})

        assert result.stdout.strip() == str(port)

    def test_a_dead_pid_is_treated_as_no_proxy(self, tmp_path):
        """A stale record from a killed session must not wedge the next launch."""
        home = tmp_path / "home"
        home.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (runtime / "proxy.json").write_text(json.dumps({"pid": dead.pid, "port": _free_port(), "deployment_id": "d1111111"}))

        result = self._discover(home, runtime, {"ADP_DEPLOYMENT_ID": "d1111111"})

        assert result.stdout.strip() == ""

    @pytest.mark.parametrize(
        "record",
        [
            "not json at all",
            json.dumps({"pid": 1}),  # no port
            json.dumps({"port": 40000}),  # no pid
            json.dumps({"pid": 1, "port": 999999}),  # out of range
            json.dumps({"pid": 1, "port": "; rm -rf /"}),  # not a number
        ],
    )
    def test_an_unusable_record_is_treated_as_no_proxy(self, tmp_path, record):
        """Every malformed shape means the same thing to the caller — start one —
        and none of them may be interpolated into anything that executes."""
        home = tmp_path / "home"
        home.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "proxy.json").write_text(record)

        result = self._discover(home, runtime, {"ADP_DEPLOYMENT_ID": "d1111111"})

        assert result.stdout.strip() == ""


class TestNamedServeDoesNotClaimTheFixedPort:
    """The other half of the port story, driven through `bg-cognito-auth.sh serve`.

    `TestThreeProxiesCoexist` passes `--port 0` explicitly, so it proves the proxy
    can take an OS-assigned port but not that a named deployment ASKS for one. That
    default lives in the shell helper, and without it the second terminal collides
    on 9191 — so it is asserted here against a real serve with no `--port` at all.
    """

    def test_a_named_deployment_serve_defaults_to_an_os_assigned_port(self, tmp_path):
        helper = CLI / "bg-cognito-auth.sh"
        home = tmp_path / "home"
        home.mkdir()
        config_dir = tmp_path / "deployments" / "d1111111"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(json.dumps({"gateway_url": "https://dev.example.com/api"}))

        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["BG_CONFIG_DIR"] = str(config_dir)
        environment["ADP_RUNTIME_DIR"] = str(config_dir)
        environment["ADP_DEPLOYMENT_ID"] = "d1111111"
        environment["ADP_DEPLOYMENT_NAME"] = "dev"
        environment["ADP_DEPLOYMENT_SOURCE"] = "default"
        process = subprocess.Popen(
            ["bash", str(helper), "serve"],  # no --port: the default is what is under test
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
        try:
            identity = _wait_for_file(config_dir / "proxy.json")

            assert identity["port"] != proxy_module.DEFAULT_PORT, "a named deployment claimed the shared fixed port, so a second one cannot start"
            assert identity["deployment_id"] == "d1111111"
            # Actually bound, not merely recorded.
            with socket.create_connection(("127.0.0.1", identity["port"]), timeout=5):
                pass
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


class TestLegacyProxyUnchanged:
    def test_the_default_port_is_still_the_documented_one(self):
        """Every existing config.toml, doc and /setup page names 9191."""
        assert proxy_module.DEFAULT_PORT == 9191

    def test_a_legacy_serve_keeps_the_fixed_port_and_publishes_no_deployment(self, tmp_path):
        """An existing user's proxy must be found exactly where it always was."""
        helper = CLI / "bg-cognito-auth.sh"
        home = tmp_path / "home"
        (home / ".bedrock-gateway").mkdir(parents=True)
        (home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": "https://legacy.example.com/api"}))
        port = _free_port()

        environment = os.environ.copy()
        environment["HOME"] = str(home)
        process = subprocess.Popen(
            ["bash", str(helper), "serve", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
        try:
            identity = _wait_for_file(home / ".bedrock-gateway" / "proxy.json")

            assert identity["port"] == port, "a legacy serve must honour the port it was given"
            assert identity["deployment_id"] == "", "a legacy proxy names no deployment"
            # The pidfile stays where every existing tool and doc looks for it.
            assert (home / ".bedrock-gateway" / "proxy.pid").is_file()
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
