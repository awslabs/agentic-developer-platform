"""Three deployments, three real recording gateways, one installed CLI (Issue #5413).

This is the deterministic stand-in for the live EC2 acceptance: the whole chain
runs for real — the installed `adp` front door, the resolver, the python helper,
`adp_common`, the bash auth helper and its token store — and the only fixtures are
the gateways themselves, which are real HTTP servers that write down every request
they receive together with the credential it carried.

That matters because the bug this story exists to prevent cannot be observed from
the outside. A command that prints "Signed in as alice" while sending alice's
integration token to the development gateway looks identical to a correct one. So
nothing here asserts on printed text. Every test asks the gateways what they
received:

* the intended gateway must have the request, bearing THAT deployment's token;
* both unintended gateways must have received nothing at all.

The second half is the part a weaker suite omits, and it is the half that catches
a silent leak — "it worked" is not evidence that it only worked once, in one
place.

The counterfactual is checked too (`TestTheLedgerCanActuallyCatchALeak`): a suite
whose recording gateways never register a wrong-target request would pass just as
happily against a CLI that sent nothing anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

NAMES = ("dev", "integration", "preprod")


class RecordingGateway:
    """A real gateway that writes down what it was asked and who asked.

    The ledger is the evidence, so it records the credential's IDENTITY (which
    deployment's token arrived) rather than the token value — the tests need to
    distinguish tokens, not to hold them, and a fixture that logged real-looking
    credentials to disk would be a bad habit to leave in the repo.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.token = f"token-for-{name}-do-not-reuse"
        self.received: list[dict] = []
        self._delay = 0.0
        ledger = self.received
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def _record(self) -> None:
                if gateway._delay:
                    time.sleep(gateway._delay)
                presented = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                ledger.append({"path": self.path, "token_owner": gateway.owner_of(presented)})
                body = b"[]"
                if self.path == "/api/me/persona-models":
                    body = json.dumps({"tenant_id": f"tenant-{gateway.name}", "entries": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # BaseHTTPRequestHandler dispatches on these exact names.
            do_GET = _record  # noqa: N815
            do_POST = _record  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    #: Filled in by the fixture once all three exist, so each gateway can name
    #: the owner of any token it is shown — including one that is not its own.
    peers: dict[str, str] = {}

    def owner_of(self, presented: str) -> str:
        return self.peers.get(presented, "unknown" if presented else "none")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def delay_responses(self, seconds: float) -> None:
        self._delay = seconds

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def gateways():
    running = {name: RecordingGateway(name) for name in NAMES}
    RecordingGateway.peers = {gateway.token: name for name, gateway in running.items()}
    yield running
    RecordingGateway.peers = {}
    for gateway in running.values():
        gateway.close()


@pytest.fixture
def machine(adp_bin: Path, adp_home: Path, gateways):
    """Three deployments registered and signed in, `dev` as the saved default.

    Signed in by seeding each store the way `login` leaves it, because the point
    under test is where a token GOES, not how it was obtained — and a real Cognito
    login cannot be part of a deterministic suite.
    """

    def run(args: list[str], extra_env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(adp_home)
        for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT_NAME", "ADP_DEPLOYMENT_SOURCE", "BG_CONFIG_DIR", "ADP_HOME"):
            env.pop(leaked, None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", str(adp_bin / "adp"), *args], capture_output=True, text=True, env=env, timeout=timeout)

    for name, gateway in gateways.items():
        added = run(["deployment", "add", name, "--url", gateway.url])
        assert added.returncode == 0, added.stderr
        store = _store_of(adp_bin, adp_home, name)
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        (store / "config.json").write_text(json.dumps({"gateway_url": gateway.url + "/api"}))
        tokens = store / "tokens.json"
        tokens.write_text(
            json.dumps({"id_token": "id", "access_token": gateway.token, "refresh_token": "refresh", "expires_at": int(time.time()) + 36000})
        )
        tokens.chmod(0o600)
    assert run(["deployment", "use", "dev"]).returncode == 0
    return run


def _store_of(adp_bin: Path, adp_home: Path, name: str) -> Path:
    """Ask the real resolver where a deployment's files live."""
    probe = subprocess.run(
        ["python3", str(adp_bin / "adp_deployments.py"), "resolve", "--deployment", name, "--format", "json"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(adp_home), "ADP_HOME": str(adp_home / ".adp")},
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    return Path(json.loads(probe.stdout)["config_dir"])


def only(gateways, expected: str) -> list[dict]:
    """Assert the intended gateway was used and NEITHER other one was."""
    for name, gateway in gateways.items():
        if name != expected:
            assert gateway.received == [], f"a request for {expected} also reached {name}: {gateway.received}"
    hits = gateways[expected].received
    assert hits, f"nothing reached {expected}"
    return hits


def reset(gateways) -> None:
    for gateway in gateways.values():
        gateway.received.clear()


# ---------------------------------------------------------------------------
# One request, one gateway, one token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", NAMES)
class TestEachSelectionReachesOnlyItsOwnGateway:
    def test_models_command_from_main_uses_the_selected_session(self, machine, gateways, target) -> None:
        result = machine(["--deployment", target, "models", "mappings", "list", "--json"])

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["detail"]["tenant_id"] == f"tenant-{target}"
        assert only(gateways, target) == [{"path": "/api/me/persona-models", "token_owner": target}]

    def test_the_explicit_flag_sends_the_right_token_to_the_right_gateway(self, machine, gateways, target) -> None:
        result = machine(["--deployment", target, "aws", "list"])

        assert result.returncode == 0, result.stderr
        assert [hit["token_owner"] for hit in only(gateways, target)] == [target]

    def test_the_environment_variable_sends_the_right_token_to_the_right_gateway(self, machine, gateways, target) -> None:
        result = machine(["aws", "list"], {"ADP_DEPLOYMENT": target})

        assert result.returncode == 0, result.stderr
        assert [hit["token_owner"] for hit in only(gateways, target)] == [target]

    def test_the_saved_default_sends_the_right_token_to_the_right_gateway(self, machine, gateways, target) -> None:
        assert machine(["deployment", "use", target]).returncode == 0
        reset(gateways)

        result = machine(["aws", "list"])

        assert result.returncode == 0, result.stderr
        assert [hit["token_owner"] for hit in only(gateways, target)] == [target]


class TestTheLedgerCanActuallyCatchALeak:
    """Without this, a CLI that sent nothing anywhere would satisfy the tests above.

    Both halves of the evidence are proven live: a gateway does register a request
    it should not have received, and it does report a foreign token as foreign.
    """

    def test_a_deliberately_crossed_request_is_visible_in_the_ledger(self, machine, gateways) -> None:
        crossed = subprocess.run(
            [
                "curl",
                "-fsS",
                "-H",
                f"Authorization: Bearer {gateways['integration'].token}",
                f"{gateways['dev'].url}/api/auth/credentials?scope=user",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert crossed.returncode == 0, crossed.stderr
        assert gateways["dev"].received == [{"path": "/api/auth/credentials?scope=user", "token_owner": "integration"}]
        with pytest.raises(AssertionError, match="also reached dev"):
            only(gateways, "integration")


# ---------------------------------------------------------------------------
# Concurrency: three at once, and the default moving underneath them
# ---------------------------------------------------------------------------


class TestThreeConcurrentCommandsStayIndependent:
    def test_all_three_run_at_once_without_crossing(self, machine, adp_bin, adp_home, gateways) -> None:
        """The product requirement in one test: three terminals, one install."""
        processes = {}
        for name in NAMES:
            env = os.environ.copy()
            env["HOME"] = str(adp_home)
            env.pop("ADP_HOME", None)
            env["ADP_DEPLOYMENT"] = name
            processes[name] = subprocess.Popen(
                ["bash", str(adp_bin / "adp"), "aws", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

        for name, process in processes.items():
            assert process.wait(timeout=90) == 0, f"{name}: {process.stderr.read()}"

        for name, gateway in gateways.items():
            assert [hit["token_owner"] for hit in gateway.received] == [name], f"{name} saw {gateway.received}"

    def test_changing_the_saved_default_mid_request_does_not_redirect_it(self, machine, adp_bin, adp_home, gateways) -> None:
        """AC-02 as a race, not as a claim: the request is made slow at the gateway,
        the default is moved while it is in flight, and it must still land where it
        was aimed, with the token it started with."""
        gateways["dev"].delay_responses(2.0)
        env = os.environ.copy()
        env["HOME"] = str(adp_home)
        env.pop("ADP_HOME", None)

        in_flight = subprocess.Popen(
            ["bash", str(adp_bin / "adp"), "aws", "list"],  # no selection: the saved default, which is `dev`
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        time.sleep(0.75)  # inside the delayed response
        switched = machine(["deployment", "use", "preprod"])
        assert switched.returncode == 0, switched.stderr

        assert in_flight.wait(timeout=90) == 0, in_flight.stderr.read()
        assert [hit["token_owner"] for hit in only(gateways, "dev")] == ["dev"]

    def test_the_switch_did_take_effect_for_the_next_command(self, machine, gateways) -> None:
        """The pin must not be mistaken for a switch that silently failed."""
        assert machine(["deployment", "use", "preprod"]).returncode == 0
        reset(gateways)

        assert machine(["aws", "list"]).returncode == 0

        assert [hit["token_owner"] for hit in only(gateways, "preprod")] == ["preprod"]


# ---------------------------------------------------------------------------
# Logging out, and selections that cannot be honoured
# ---------------------------------------------------------------------------


class TestLoggingOutOfOneLeavesTheOthersWorking:
    def test_the_other_two_still_reach_their_own_gateways(self, machine, gateways) -> None:
        assert machine(["--deployment", "dev", "logout"]).returncode == 0
        reset(gateways)

        for name in ("integration", "preprod"):
            assert machine(["--deployment", name, "aws", "list"]).returncode == 0

        assert [hit["token_owner"] for hit in gateways["integration"].received] == ["integration"]
        assert [hit["token_owner"] for hit in gateways["preprod"].received] == ["preprod"]
        assert gateways["dev"].received == []

    def test_the_logged_out_one_fails_and_borrows_nobody_elses_token(self, machine, gateways) -> None:
        """The dangerous shape of this failure is a fallback, so the assertion is
        that NO gateway was contacted — not merely that the command failed."""
        assert machine(["--deployment", "dev", "logout"]).returncode == 0
        reset(gateways)

        result = machine(["--deployment", "dev", "aws", "list"])

        assert result.returncode != 0
        for name, gateway in gateways.items():
            assert gateway.received == [], f"a signed-out command still reached {name}: {gateway.received}"


class TestAnUnhonourableSelectionContactsNobody:
    def test_an_unknown_deployment_reaches_no_gateway(self, machine, gateways) -> None:
        result = machine(["--deployment", "staging", "aws", "list"])

        assert result.returncode != 0
        assert "staging" in result.stderr
        for name, gateway in gateways.items():
            assert gateway.received == [], f"an unknown selection still reached {name}"

    def test_an_unknown_environment_selection_reaches_no_gateway(self, machine, gateways) -> None:
        result = machine(["aws", "list"], {"ADP_DEPLOYMENT": "staging"})

        assert result.returncode != 0
        for name, gateway in gateways.items():
            assert gateway.received == [], f"an unknown selection still reached {name}"
