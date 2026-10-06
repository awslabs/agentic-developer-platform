"""The installed `adp superplane onboarding` surface, exercised as an installed CLI.

WHY THESE TESTS RUN A SUBPROCESS AND NOT A FUNCTION
---------------------------------------------------
Nearly every defect this surface can ship survives an in-process test of its
handlers. A repo checkout has every helper file beside every other, so a helper
that was never added to the installer's manifest, never allowlisted for download,
and never dispatched still imports and runs perfectly from a test — while a real
user's `adp update` does not fetch it and `adp superplane onboarding` reports an
unknown command. That failure is invisible from inside the process.

So the tests here drive `bash adp ...` from the `adp_bin` fixture, which is the
layout `install.sh` actually produces, with a sandboxed HOME and a real HTTP
gateway on a loopback port. Serialization, exit codes, the stdout/stderr split
and the on-disk state file are all observed as a caller observes them. The
packaging tests then assert the manifest entries directly, because those are the
parts a passing functional test cannot vouch for.

The gateway is a recording server, and the assertions are mostly about what it
*received* — or, for the unavailable paths, that it received nothing. "It printed
a sensible message" is not evidence that no request was sent, and for this
surface an unintended request is the actual hazard: a create submitted without a
durable identity spends money that a retry then spends again.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).parents[4]
CLI = REPO / "modules/gateway/cli"
HELPER = CLI / "adp-superplane-onboarding.py"
CONTRACT = REPO / "modules/domain-apps/superplane/ui/contract.ts"
PROXY_ROUTES = REPO / "modules/gateway/src/domain_proxy/superplane_routes.json"

# Explicit older-deployment fixture. Production flags are independently checked
# against the actual gateway allowlist by the contract tests below.
ONBOARDING_ENDPOINTS = (
    "adoptWorkspace",
    "previewWorkspace",
    "getOperation",
    "recoverOperation",
    "requestApproval",
    "getApproval",
    "decideApproval",
    "listLifecycleProposals",
    "previewLifecycleProposal",
    "continueLifecycleProposal",
)
UNAVAILABLE_ROUTE_SETUP = f"for name in {ONBOARDING_ENDPOINTS!r}:\n    helper.ENDPOINTS[name] = dict(helper.ENDPOINTS[name], served=False)\n"


def token_for_org(org="org-a"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "test-principal", "custom:org_id": org}).encode()).decode().rstrip("=")
    return f"test.{claims}.signature"


TOKEN = token_for_org()

# A value that must never appear anywhere: not in stdout, not in stderr, not in
# the state file. Distinctive so a substring search cannot match it by accident.
SECRET = "zzTOPSECRETvalue4242zz"

# ---------------------------------------------------------------------------
# Vault credential rows, in the shape the server really returns
# ---------------------------------------------------------------------------
#
# WHY THESE ARE NOT `{"credential_id": ..., "service": ..., "label": ...}`
# ----------------------------------------------------------------------
# That was the previous fixture shape, and it was the shape this CLI *assumed*
# rather than the shape `GET /vault/credentials` emits. `CredentialResponse`
# (app/schemas/account.py) has no `credential_id` field at all: it carries `id`
# (the domain registry's own row key), `adp_credential_id` (the vault handle),
# `provider` and `name`. The server matches a submitted reference against
# `adp_credential_id`, so a client reading `id` binds a value the registry lookup
# cannot find — and a fixture written in the client's preferred shape agrees with
# that bug and passes.
#
# `ROW_ID` and `HANDLE` are deliberately different. When a fixture makes them
# equal, a client reading the wrong field still passes.
ROW_ID = "11111111-1111-4111-8111-111111111111"
HANDLE = "adp-cred-prod-bedrock"
PROVIDER = "bedrock"
LABEL = "Prod Bedrock"


def vault_row(**overrides: object) -> dict:
    """One row of `GET /vault/credentials`, per `CredentialResponse`."""
    row = {
        "id": ROW_ID,
        "org_id": "22222222-2222-4222-8222-222222222222",
        "name": "Production display name",
        "provider": PROVIDER,
        "credential_type": "iam_role",
        "adp_credential_id": HANDLE,
        "status": "Active",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def expected_bind_body(row: dict | None = None) -> dict:
    """The reference `accept_connection_request` will accept for a row.

    All three of `credential_id`, `service` and `label` are required — the contract
    raises on any missing one — and `_registry_reference` separately requires
    `provider` to equal the credential's own service.
    """
    row = row or vault_row()
    return {
        "provider": row["provider"],
        "credential_id": row["adp_credential_id"],
        "service": row["provider"],
        "label": LABEL,
    }


def vault_reply(gateway: RecordingGateway, rows: list | None = None) -> None:
    """Serve the vault listing. `bind` resolves its reference through this."""
    gateway.reply(
        "GET",
        "/superplane/v1/vault/credentials",
        200,
        {"credentials": rows if rows is not None else [vault_row()], "total": 1},
    )
    gateway.reply(
        "GET",
        "/auth/credentials",
        200,
        [
            {"id": row["adp_credential_id"], "service": row["provider"], "label": LABEL}
            for row in (rows if rows is not None else [vault_row()])
            if isinstance(row, dict) and row.get("adp_credential_id")
        ],
    )
    for row in rows if rows is not None else [vault_row()]:
        if isinstance(row, dict) and row.get("adp_credential_id"):
            gateway.reply("PUT", f"/auth/credentials/{row['adp_credential_id']}/workspaces/ws-1", 200, {"delegated": True})


class RecordingGateway:
    """A real HTTP gateway that writes down every request it receives.

    Records the path, method and body so a test can assert on what was actually
    serialized and sent, rather than on what a handler returned. Replies are
    queued per method+path; an unqueued path answers 404, which is what the real
    domain proxy does for a route outside its allowlist — so a test that invents
    a path fails instead of quietly passing against a server that answers
    anything.
    """

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.replies: dict[tuple[str, str], tuple[int, object]] = {}
        # Paths that accept the request, record it, and then never answer — used to
        # hold a client in the mid-request state a crash or eviction would.
        self.hangs: set[tuple[str, str]] = set()
        self._release = __import__("threading").Event()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                gateway.received.append(
                    {
                        "method": method,
                        "path": self.path,
                        "raw_body": raw,
                        "body": json.loads(raw) if raw else None,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                if (method, self.path) in gateway.hangs:
                    # Request recorded, no reply. Waits on an event the teardown
                    # sets, so the thread cannot outlive the test as a leak.
                    gateway._release.wait(timeout=120)
                    return
                status, body = gateway.replies.get((method, self.path), (404, {"error": "not_found"}))
                if callable(body):
                    body = body(json.loads(raw) if raw else None)
                payload = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
                self._handle("PUT")

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
                self._handle("POST")

            def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
                self._handle("DELETE")

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        import threading

        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def reply(self, method: str, path: str, status: int = 200, body: object = None) -> None:
        self.replies[(method, "/api" + path)] = (status, body)

    def hang(self, method: str, path: str) -> None:
        """Accept and record the request, then never answer it."""
        self.hangs.add((method, "/api" + path))

    def close(self) -> None:
        # Release any hung handler first, or shutdown() blocks on its thread.
        self._release.set()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def gateway():
    server = RecordingGateway()
    yield server
    server.close()


@pytest.fixture
def unavailable_onboarding_routes(adp_bin: Path):
    """Disable routes only in the copied install, retaining front-door coverage."""
    helper = adp_bin / "adp-superplane-onboarding.py"
    source = helper.read_text()
    main_guard = 'if __name__ == "__main__":'
    assert source.count(main_guard) == 1
    setup = UNAVAILABLE_ROUTE_SETUP.replace("helper.ENDPOINTS", "ENDPOINTS")
    helper.write_text(source.replace(main_guard, setup + "\n" + main_guard))
    yield
    helper.write_text(source)


@pytest.fixture
def onboarding(adp_bin: Path, adp_home: Path, gateway: RecordingGateway):
    """Run `adp superplane onboarding ...` through the installed front door.

    Signed in by seeding the token store the way `login` leaves it: how a token
    was obtained is not what these tests are about, and a real Cognito login
    cannot be deterministic.
    """
    store = adp_home / ".bedrock-gateway"
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    (store / "config.json").write_text(json.dumps({"gateway_url": gateway.url + "/api"}))
    tokens = store / "tokens.json"
    tokens.write_text(json.dumps({"id_token": "id", "access_token": TOKEN, "refresh_token": "r", "expires_at": int(time.time()) + 36000}))
    tokens.chmod(0o600)

    def run(args: list[str], stdin: str = "", timeout: int = 60) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(adp_home)
        for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT_NAME", "BG_CONFIG_DIR", "ADP_HOME", "ADP_ORG"):
            env.pop(leaked, None)
        return subprocess.run(
            ["bash", str(adp_bin / "adp"), "superplane", "onboarding", *args],
            capture_output=True,
            text=True,
            input=stdin,
            env=env,
            timeout=timeout,
        )

    run.home = adp_home  # type: ignore[attr-defined]
    run.gateway = gateway  # type: ignore[attr-defined]
    return run


def document(result: subprocess.CompletedProcess) -> dict:
    """The one JSON object on stdout.

    Parses the WHOLE of stdout rather than scanning for a JSON-looking line: the
    `--json` contract is exactly one object and nothing else, so a stray print
    beside it must fail here. That is what makes the output safe to pipe.
    """
    assert result.stdout.strip(), f"nothing on stdout; stderr was: {result.stderr}"
    return json.loads(result.stdout)


def state_path(home: Path) -> Path:
    """Ask the real `adp_common` where this helper's state file lives.

    Deriving it rather than hardcoding `~/.adp/state/...`: a wrong guess would
    make `state_file()` return an empty dict forever, and the assertions that no
    secret reaches the state file would pass vacuously against a file nobody was
    looking at. A layout change must break this loudly instead.
    """
    probe = subprocess.run(
        ["python3", "-c", "import adp_common,sys; print(adp_common.state_path(sys.argv[1]))", "superplane_onboarding"],
        capture_output=True,
        text=True,
        cwd=str(CLI),
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    return Path(probe.stdout.strip())


def state_file(home: Path) -> dict:
    path = state_path(home)
    return json.loads(path.read_text()) if path.is_file() else {}


# ---------------------------------------------------------------------------
# The contract is mirrored, in both directions
# ---------------------------------------------------------------------------


def endpoints_of_helper() -> dict:
    """Read ENDPOINTS out of the helper without importing it.

    Parsed from source with ast so this test does not depend on the helper's
    imports resolving, and so it reads what ships rather than what a test
    environment happens to make importable.
    """
    import ast

    tree = ast.parse(HELPER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "ENDPOINTS" for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("ENDPOINTS not found in the helper")


def endpoints_of_contract() -> dict:
    """Read the TypeScript ENDPOINTS table the browser client uses."""
    source = CONTRACT.read_text()
    block = re.search(r"export const ENDPOINTS[^=]*=\s*\{(.*?)\n\} as const", source, re.S)
    assert block, "ENDPOINTS table not found in contract.ts"
    found = {}
    for entry in re.finditer(
        r"(\w+)\s*:\s*\{[^}]*?method\s*:\s*'(\w+)'[^}]*?path\s*:\s*'([^']+)'[^}]*?served\s*:\s*(true|false)",
        block.group(1),
        re.S,
    ):
        name, method, path, served = entry.groups()
        found[name] = {"method": method, "path": path, "served": served == "true"}
    assert found, "no endpoints parsed out of contract.ts"
    return found


def served_by_proxy() -> set[tuple[str, str]]:
    """The method/path pairs the gateway's domain proxy actually forwards.

    The proxy's own allowlist is the authority on what exists. Deriving the
    expectation from it means a route added or removed there is reflected here
    without anyone remembering to update a hand-written list — the failure mode
    where a CLI keeps calling a route that was withdrawn.
    """
    # Unpacked exactly as `domain_proxy/superplane.py` unpacks it — a flat list of
    # [method, path] pairs. Reading it the way the real consumer does means a
    # change to the file's shape breaks this test rather than silently producing
    # an empty expectation that every assertion then passes against.
    pairs = {(method.upper(), path) for method, path in json.loads(PROXY_ROUTES.read_text())}
    assert pairs, "no routes parsed out of the proxy allowlist"
    return pairs


def normalise(path: str) -> str:
    """Compare route SHAPES, ignoring what each side names its parameters.

    `{workspace_id}` and `{workspaceId}` are the same route. Comparing the raw
    strings would fail on a naming difference that cannot affect a request,
    which trains people to edit the test rather than read it.
    """
    return re.sub(r"\{\w+\}", "{}", path)


class TestTheCliAndTheBrowserAgreeOnTheApi:
    """One API, described once.

    Two clients that drift apart produce the worst kind of report: the browser
    says a capability is unavailable while the CLI 404s against a path the
    browser never had, and neither answer identifies the real cause. These tests
    compare the tables field by field, in both directions, so neither file can be
    edited alone.
    """

    def test_every_contract_endpoint_exists_in_the_cli_with_the_same_method_and_path(self) -> None:
        contract, helper = endpoints_of_contract(), endpoints_of_helper()
        for name, declared in contract.items():
            assert name in helper, f"contract.ts declares {name} but the CLI does not"
            assert helper[name]["method"] == declared["method"], name
            assert normalise(helper[name]["path"]) == normalise(declared["path"]), name

    def test_the_cli_declares_no_endpoint_the_contract_does_not(self) -> None:
        """The direction that catches an invented path.

        Without this, the CLI could grow a plausible-looking route that no
        server serves and every mirroring test would still pass.
        """
        extra = set(endpoints_of_helper()) - set(endpoints_of_contract())
        assert not extra, f"the CLI declares endpoints absent from contract.ts: {sorted(extra)}"

    def test_the_two_clients_agree_on_which_endpoints_are_served(self) -> None:
        contract, helper = endpoints_of_contract(), endpoints_of_helper()
        disagreements = {
            name: (helper[name]["served"], declared["served"]) for name, declared in contract.items() if helper[name]["served"] != declared["served"]
        }
        assert not disagreements, f"CLI vs contract.ts served flags disagree: {disagreements}"

    def test_unmounted_retirement_access_is_not_claimed_by_either_client(self) -> None:
        proxy = served_by_proxy()
        contract, helper = endpoints_of_contract(), endpoints_of_helper()
        for name in ("previewRetirementAccess", "admitRetirementAccess"):
            endpoint = helper[name]
            if (endpoint["method"], normalise(endpoint["path"])) not in proxy:
                assert endpoint["served"] is False
                assert contract[name]["served"] is False

    def test_every_served_endpoint_is_one_the_gateway_proxy_forwards(self) -> None:
        """`served: True` is a claim about the deployed proxy, checked against it.

        A helper marking an endpoint served that the proxy does not forward
        sends a request that 404s — indistinguishable to the user from "that
        workspace does not exist", which sends them looking for a missing
        resource instead of a missing route.
        """
        proxy = {(method, normalise(path)) for method, path in served_by_proxy()}
        for name, declared in endpoints_of_helper().items():
            if declared["served"]:
                pair = (declared["method"], normalise(declared["path"]))
                assert pair in proxy, f"{name} is marked served but the proxy does not forward {pair}"

    def test_no_endpoint_marked_unavailable_is_actually_forwarded(self) -> None:
        """The inverse, so a capability is not withheld after it ships.

        Left unchecked, a route that became available would keep being reported
        as unavailable forever — the CLI would be lying in the safe direction,
        which is still lying, and the user would never discover the feature.
        """
        proxy = {(method, normalise(path)) for method, path in served_by_proxy()}
        for name, declared in endpoints_of_helper().items():
            if not declared["served"]:
                pair = (declared["method"], normalise(declared["path"]))
                assert pair not in proxy, f"{name} is reported unavailable but the proxy forwards {pair}"

    def test_every_declared_unavailable_diagnostic_explains_its_capability_in_users_terms(self) -> None:
        """Diagnostics name the capability, never an internal identifier.

        "Blocked on #1234" tells the person at the terminal nothing they can act
        on. Prose describing the capability lets them ask their platform team a
        concrete question. The stable machine-readable handle is the error code
        and the endpoint name, both asserted elsewhere.
        """
        for name, declared in endpoints_of_helper().items():
            if declared["served"] and "capability" not in declared:
                continue
            capability = declared.get("capability") or ""
            assert capability, f"{name} is unavailable but names no capability"
            assert not re.search(r"#\d+|issue[\s-]*\d+|story", capability, re.I), f"{name} exposes an internal identifier to users: {capability!r}"


class TestTheIdempotencyContractMatchesTheBrowserClient:
    def test_both_clients_use_the_same_feature_name_and_carry_it_in_the_body(self) -> None:
        """The identity must travel in the body, in both clients, under one name.

        The gateway's domain proxy rebuilds the upstream request with only
        `Authorization` and `Content-Type`, so a header-borne idempotency key is
        silently dropped in transit: the request succeeds, deduplicates nothing,
        and reports success. Two clients disagreeing about the field name fails
        the same silent way.
        """
        source = HELPER.read_text()
        contract = CONTRACT.read_text()
        for pattern, label in (
            (r"CREATE_IDEMPOTENCY_FEATURE\s*=\s*['\"]create-operation-id-v1['\"]", "feature name"),
            (r"OPERATION_ID_FIELD\s*=\s*['\"]operation_id['\"]", "body field"),
            (r"IDEMPOTENCY_TRANSPORT\s*=\s*['\"]body['\"]", "transport"),
        ):
            assert re.search(pattern, source), f"the CLI does not pin the {label}"
            assert re.search(pattern, contract), f"contract.ts does not pin the {label}"


# ---------------------------------------------------------------------------
# Packaging: the five places a new helper must be registered
# ---------------------------------------------------------------------------


class TestTheHelperActuallyShipsToAUsersMachine:
    """Each of these is independently fatal and invisible in a checkout.

    A repo clone has the file beside its siblings, so the helper imports and runs
    in every local test even when nothing would deliver it to a user. These
    assertions are the only place that gap is visible.
    """

    NAME = "adp-superplane-onboarding.py"

    def test_the_installer_and_the_updater_both_fetch_it(self) -> None:
        """Two separate manifests, and missing either one breaks a real install.

        `install.sh` is the first install; `adp`'s own list is what `adp update`
        pulls. A helper in only one of them ships to new machines and never
        reaches existing ones, or the reverse.
        """
        for path in (CLI / "install.sh", CLI / "adp"):
            manifest = re.search(r'CLI_FILES="([^"]+)"', path.read_text())
            assert manifest, f"no CLI_FILES manifest in {path.name}"
            assert self.NAME in manifest.group(1).split(), f"{path.name} would not deliver {self.NAME}"

    def test_the_gateway_serves_it_for_download_with_a_python_media_type(self) -> None:
        """Absent from the allowlist, the download 404s and the install is broken.

        The media type matters separately: the fallback is the shell type, and a
        python helper served as a shell script is a file the installer writes but
        nothing can execute correctly.
        """
        source = (REPO / "modules/gateway/src/cli_download/routes.py").read_text()
        allowlist = re.search(r"ALLOWED_SCRIPTS[^=]*=\s*\{(.*?)\n\}", source, re.S)
        media = re.search(r"SCRIPT_MEDIA_TYPES[^=]*=\s*\{(.*?)\n\}", source, re.S)
        assert allowlist and media
        assert f'"{self.NAME}"' in allowlist.group(1), f"{self.NAME} is not downloadable"
        assert re.search(rf'"{re.escape(self.NAME)}":\s*PYTHON_SCRIPT_MEDIA_TYPE', media.group(1)), (
            f"{self.NAME} would be served with the fallback shell media type"
        )

    def test_every_pinned_copy_of_the_cli_file_list_includes_it(self) -> None:
        """The allowlist is asserted as an exact set in three other places.

        `ALLOWED_SCRIPTS` is deliberately pinned rather than derived, so that
        adding a file to `cli/` cannot make it publicly downloadable by accident
        on an unauthenticated route. The cost of that safety property is that a
        helper which genuinely should ship has to be added to each pinned copy,
        and a copy that still holds the old set fails as a *set inequality* whose
        message is two truncated `{'adp', ...}` renderings — which names neither
        the file nor the reason.

        This test exists because I missed one of them: the assertions above check
        the manifests and the route source, all of which passed, while
        tests/test_cli_download.py's own expectation did not and turned up only
        in a CI shard.
        """
        pinned = {
            "tests/test_cli_download.py": REPO / "modules/gateway/tests/test_cli_download.py",
            "tests/cli/conftest.py": REPO / "modules/gateway/tests/cli/conftest.py",
            "tests/cli/test_deploy_filter_covers_cli.py": REPO / "modules/gateway/tests/cli/test_deploy_filter_covers_cli.py",
        }
        for label, path in pinned.items():
            # The name, not a quoted literal: test_deploy_filter_covers_cli.py
            # pins repo-relative paths while the other two pin bare filenames.
            assert self.NAME in path.read_text(), f"{label} pins a CLI file list that omits {self.NAME}"

    def test_a_change_to_the_helper_triggers_the_cli_deploy(self) -> None:
        """Otherwise the file changes and no new artifact is published.

        The user's `adp update` then fetches the previous version, and the fix
        appears merged while being absent from every machine.
        """
        workflow = (REPO / ".github/workflows/gateway-deploy.yml").read_text()
        assert f"modules/gateway/cli/{self.NAME}" in workflow or "modules/gateway/cli/**" in workflow, (
            "a change to the onboarding helper would publish no new CLI artifact"
        )

    def test_the_verb_is_discoverable_from_the_top_level_help(self) -> None:
        """A capability nobody can find is a capability nobody uses."""
        assert "superplane onboarding" in (CLI / "adp").read_text()

    def test_the_parent_helper_advertises_onboarding_only_when_the_file_ships(self) -> None:
        """A listed verb that cannot run is worse than an unlisted one.

        An install missing the helper must not offer `onboarding` in its help and
        then fail on it; the parent checks for the file, as `adp-admin.py` does
        for its own sub-areas.
        """
        source = (CLI / "adp-superplane.py").read_text()
        assert "ONBOARDING_HELPER" in source
        assert re.search(r"if Path\(__file__\)\.with_name\(ONBOARDING_HELPER\)\.is_file\(\)", source), (
            "the parent advertises onboarding unconditionally"
        )


class TestTheInstalledCliCanActuallyRunTheVerb:
    """The end-to-end check that the registration above is sufficient.

    Runs through `bash adp` in the installed layout, so a missing dispatch arm or
    a helper the parent cannot find fails here.
    """

    def test_the_installed_front_door_dispatches_to_the_onboarding_helper(self, onboarding) -> None:
        result = onboarding(["capabilities", "--json"])
        body = document(result)
        assert body["command"] == "superplane onboarding capabilities"
        # Not a usage error, which is what an unregistered verb would produce.
        assert result.returncode == 4, result.stderr

    def test_an_unknown_onboarding_subcommand_is_a_usage_error(self, onboarding) -> None:
        """Exit 1, distinct from the exit 4 a real unavailability produces.

        A script must be able to tell "I typed it wrong" from "this environment
        cannot do that yet"; collapsing them makes a typo look like a missing
        feature and invites someone to go asking for a deployment change.
        """
        result = onboarding(["teleport", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Unavailability is honest, and sends nothing
# ---------------------------------------------------------------------------


UNAVAILABLE_VERBS = [
    (["plan", "--name", "w1"], "previewWorkspace"),
    # `--plan-revision` is required on adopt for the same reason as on create: an
    # adoption is bound to the plan the operator read. Passed here so the case
    # under test is the unserved endpoint and not a usage error.
    (
        ["adopt", "--name", "w1", "--cluster", "arn:cluster", "--plan-revision", "rev-7", "--yes"],
        "adoptWorkspace",
    ),
    (["operation", "show", "--operation-id", "op-1"], "getOperation"),
    (["operation", "recover", "--key", "key-1"], "recoverOperation"),
]


@pytest.mark.usefixtures("unavailable_onboarding_routes")
class TestAnUnavailableCapabilityIsReportedWithoutBeingAttempted:
    @pytest.mark.parametrize("argv,endpoint", UNAVAILABLE_VERBS, ids=[e for _, e in UNAVAILABLE_VERBS])
    def test_it_exits_four_and_names_the_endpoint(self, onboarding, argv, endpoint) -> None:
        """Exit 4, not 5.

        Nothing broke and nothing was attempted — the environment lacks a
        feature, which is a resumable state. A script reading exit 5 would treat
        this as a failure to retry or escalate.
        """
        result = onboarding([*argv, "--json"])
        body = document(result)
        assert result.returncode == 4, result.stdout + result.stderr
        assert body["status"] == "unavailable"
        assert body["detail"]["endpoint"] == endpoint
        assert body["detail"]["reason"] == "not-deployed"

    @pytest.mark.parametrize("argv,endpoint", UNAVAILABLE_VERBS, ids=[e for _, e in UNAVAILABLE_VERBS])
    def test_no_request_reaches_the_gateway_at_all(self, onboarding, argv, endpoint) -> None:
        """The assertion that matters, and the one a message check cannot make.

        Reporting "unavailable" after sending a request that 404d would look
        identical in the output while having contacted the server — and for
        `create`-adjacent verbs, an attempted call is precisely the hazard.
        """
        onboarding([*argv, "--json"])
        assert onboarding.gateway.received == [], f"{endpoint} contacted the gateway: {onboarding.gateway.received}"

    @pytest.mark.parametrize("argv,endpoint", UNAVAILABLE_VERBS, ids=[e for _, e in UNAVAILABLE_VERBS])
    def test_the_explanation_names_a_capability_and_no_internal_identifier(self, onboarding, argv, endpoint) -> None:
        result = onboarding([*argv, "--json"])
        detail = document(result)["detail"]
        assert detail["capability"], f"{endpoint} named no capability"
        assert not re.search(r"#\d+|\bstory\b|\bticket\b", detail["detail"], re.I), detail["detail"]

    def test_an_unavailable_capability_report_says_what_is_unknown(self, onboarding) -> None:
        """An empty report would read as "nothing is supported".

        Those are different facts. Listing the unknowns keeps the reader from
        concluding the environment supports no providers and no modes when in
        truth none of it has been observed.
        """
        detail = document(onboarding(["capabilities", "--json"]))["detail"]
        assert detail["unknown"], "the capability report claims no unknowns"
        assert any("operation identity" in line for line in detail["unknown"])


@pytest.mark.usefixtures("unavailable_onboarding_routes")
class TestACreateIsRefusedRatherThanRiskingADuplicate:
    def test_it_will_not_submit_when_idempotency_cannot_be_confirmed(self, onboarding) -> None:
        """Fail-closed, and nothing sent.

        A create submitted without a confirmed identity returns 201 while the
        server deduplicates nothing — the unknown field is simply ignored. The
        first lost reply then produces a retry that builds a second workspace and
        spends twice, arrived at through a request that looked entirely
        successful. So the submission does not happen.
        """
        result = onboarding(["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        body = document(result)
        assert result.returncode == 4, result.stdout + result.stderr
        assert body["status"] == "unavailable"
        assert body["detail"]["performed"] == "nothing"
        assert onboarding.gateway.received == [], "a create was attempted without confirmed idempotency"

    def test_no_receipt_and_no_identity_are_persisted_for_a_submission_never_made(self, onboarding) -> None:
        """State must not record an operation that does not exist.

        A receipt for an unsent submission would later be "recovered", sending
        someone looking for an operation the server never heard of.
        """
        onboarding(["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert state_file(onboarding.home).get("receipts", {}) == {}

    def test_a_create_is_refused_before_a_plan_can_even_be_reviewed(self, onboarding) -> None:
        """Exact-plan binding is a precondition, not a nicety.

        With no reviewable plan there is nothing for the user to have approved,
        so a submission could not be bound to it. Refusing names that
        consequence instead of submitting something unreviewed.
        """
        detail = document(onboarding(["create", "--name", "w1", "--plan-revision", "r1", "--yes", "--json"]))["detail"]
        assert "plan" in detail["consequence"].lower()
        assert "no submission was made" in detail["consequence"].lower()


# ---------------------------------------------------------------------------
# Served endpoints: real requests, real serialization
# ---------------------------------------------------------------------------


class TestBindingAProviderCredentialSendsExactlyAReference:
    """What these tests used to assert, and why it was wrong.

    Every test in this class previously passed `--provider bedrock --credential-id
    cred-9` with NO vault listing served, and asserted the body was
    `{"provider": "bedrock", "credential_id": "cred-9"}`. It passed. The real server
    would have refused it twice over:

    * `accept_connection_request` requires `credential_id`, `service` AND `label`,
      and raises a contract violation naming the missing ones -> HTTP 400.
    * `_registry_reference` matches `CredentialRegistry.adp_credential_id`, so even
      a complete reference built from a row's `id` finds nothing.

    A hand-typed id hides both faults, because it is no particular field of any
    real row. So the reference is now resolved out of a served vault listing whose
    rows are the real `CredentialResponse` shape, and the expected body comes from
    that row rather than from a literal.
    """

    def test_the_request_body_is_the_whole_reference_and_nothing_else(self, onboarding) -> None:
        """The whole body, asserted exactly.

        An exact comparison rather than a subset check: the risk is BOTH a missing
        required field and an EXTRA one, and a subset assertion is blind to each in
        turn. Onboarding binds a reference the vault already holds, so any
        additional key is either a leak or an unreviewed contract change.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply(
            "POST",
            "/superplane/v1/workspaces/ws-1/provider-connections",
            201,
            {"connection_id": "conn-1", "provider": PROVIDER, "status": "Pending"},
        )
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        sent = [r for r in onboarding.gateway.received if r["method"] == "POST"]
        assert len(sent) == 1, sent
        assert sent[0]["body"] == expected_bind_body()

    def test_it_sends_the_vault_handle_and_not_the_registry_row_key(self, onboarding) -> None:
        """The decisive mapping, pinned on its own.

        Getting it wrong is silent at the client and confusing at the server: the
        bind fails with "credential is not registered" for a credential plainly
        visible in `connection credentials`. The row carries both ids and only one
        of them is the reference.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        sent = [r for r in onboarding.gateway.received if r["method"] == "POST"]
        assert sent[0]["body"]["credential_id"] == HANDLE
        assert ROW_ID not in sent[0]["raw_body"]

    def test_an_id_that_is_not_in_the_vault_is_refused_before_anything_is_sent(self, onboarding) -> None:
        """A local refusal, not a 400 from the far end.

        The server's "not registered" refusal cannot distinguish "you sent the
        wrong one of the two ids you were holding" from "that credential does not
        exist". Resolving the reference against the org's own listing first means
        the message can say which, and no mutation is attempted.
        """
        vault_reply(onboarding.gateway)
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", ROW_ID, "--yes", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        body = document(result)
        assert body["error"]["code"] == "credential_not_registered"
        assert not [r for r in onboarding.gateway.received if r["method"] == "POST"], onboarding.gateway.received

    def test_a_provider_that_contradicts_the_credential_is_refused_locally(self, onboarding) -> None:
        """`--provider` is an assertion, never an override.

        `_registry_reference` raises `CredentialProviderMismatch` when the submitted
        provider differs from the credential's own service, and the refusal names
        neither field. Checking the agreement here lets the error name both, and
        makes it impossible to bind one credential while labelling it as another.
        """
        vault_reply(onboarding.gateway)
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--provider", "anthropic", "--yes", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        body = document(result)
        assert body["error"]["code"] == "credential_provider_mismatch"
        # Both families named, so the user can see which one to correct.
        assert PROVIDER in body["error"]["message"]
        assert "anthropic" in body["error"]["message"]
        assert not [r for r in onboarding.gateway.received if r["method"] == "POST"]

    def test_the_provider_is_derived_when_the_flag_is_omitted(self, onboarding) -> None:
        """`--provider` is optional, because the credential already answers it.

        It was `required=True`, which forced every caller to retype a value the
        server insists must match the credential — an invitation to a mismatch that
        no flag combination can make useful.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        sent = [r for r in onboarding.gateway.received if r["method"] == "POST"]
        assert sent[0]["body"]["provider"] == PROVIDER
        assert sent[0]["body"]["service"] == sent[0]["body"]["provider"]

    def test_it_reaches_the_workspace_scoped_path_the_proxy_forwards(self, onboarding) -> None:
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        sent = [r for r in onboarding.gateway.received if r["method"] == "POST"]
        assert sent[0]["path"] == "/api/superplane/v1/workspaces/ws-1/provider-connections"

    def test_a_workspace_id_containing_a_slash_cannot_reach_another_route(self, onboarding) -> None:
        """Percent-encoded, so a crafted id cannot add a path segment.

        Without encoding, a workspace id of `ws-1/../../admin` would rewrite the
        request onto a different route than the declared one — which would make
        declaring routes pointless.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("PUT", f"/auth/credentials/{HANDLE}/workspaces/ws-1%2F..%2Fevil", 200, {"delegated": True})
        onboarding(
            [
                "connection",
                "bind",
                "--workspace",
                "ws-1/../evil",
                "--credential-id",
                HANDLE,
                "--yes",
                "--json",
            ]
        )
        sent = [r for r in onboarding.gateway.received if r["method"] == "POST"]
        assert sent, f"no bind request was sent: {onboarding.gateway.received}"
        path = sent[0]["path"]
        # Asserted on SEGMENT STRUCTURE, not on absence of the substrings. The
        # characters `..` and `evil` legitimately survive inside the encoded
        # value — what must not survive is their ability to act as path syntax.
        # A substring check would also pass for an implementation that stripped
        # the characters instead of encoding them, which would silently address a
        # different workspace than the one named.
        segments = path.split("/")
        assert ".." not in segments, f"a traversal segment reached the path: {path}"
        assert "evil" not in segments, f"a crafted segment reached the path: {path}"
        assert segments == ["", "api", "superplane", "v1", "workspaces", "ws-1%2F..%2Fevil", "provider-connections"], path

    def test_a_successful_bind_does_not_claim_the_credential_was_validated(self, onboarding) -> None:
        """A 201 means stored, not working.

        Whether a credential can actually reach the provider is a question only
        the provider answers. Treating the bind's success as validation is how a
        workspace comes to look ready while every job against it fails. The server
        agrees structurally: `service.register` creates the row PENDING, never
        ACTIVE, because an unvalidated reference admits no work.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c", "status": "Pending"})
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        body = document(result)
        assert "not a validation" in (body["next_action"] or "")
        assert "validation" not in json.dumps(body["detail"]) or body["detail"].get("validation") is None

    def test_a_row_without_a_vault_handle_is_not_offered_at_all(self, onboarding) -> None:
        """Dropped, not bound with a fallback id.

        The fallback this pins the absence of was the defect: reading `id` when
        `adp_credential_id` was missing produced a reference the registry cannot
        match. A row that cannot supply the reference the server requires is not
        bindable, and saying so is more useful than a 400.
        """
        unbindable = vault_row()
        del unbindable["adp_credential_id"]
        vault_reply(onboarding.gateway, [unbindable])
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", ROW_ID, "--yes", "--json"])
        assert result.returncode == 1
        assert document(result)["error"]["code"] == "credential_not_registered"
        assert not [r for r in onboarding.gateway.received if r["method"] == "POST"]


class TestGatewayAttestedValidation:
    def test_forwards_only_the_gateway_report(self, onboarding):
        ref = expected_bind_body()
        report = {
            "credential_valid": True,
            "permissions_sufficient": True,
            "quota_available": True,
            "observed_capacity": None,
            "checked_at": "2026-09-24T00:00:00Z",
            "detail": "provider observed",
        }
        connection = {"connection_id": "conn-1", "provider": PROVIDER, "credential": ref}
        onboarding.gateway.reply("GET", "/superplane/v1/workspaces/ws-1/provider-connections/conn-1", 200, connection)
        onboarding.gateway.reply(
            "POST",
            f"/auth/credentials/{HANDLE}/workspaces/ws-1/validation",
            200,
            {"credential_id": HANDLE, "workspace_id": "ws-1", "validation": report},
        )
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections/conn-1/validation", 200, connection)
        result = onboarding(["connection", "validate", "--workspace", "ws-1", "--connection-id", "conn-1", "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        posts = [request for request in onboarding.gateway.received if request["method"] == "POST"]
        assert posts[0]["body"] is None
        assert posts[1]["body"] == report

    def test_unavailable_provider_does_not_post_a_domain_report(self, onboarding):
        onboarding.gateway.reply("GET", "/superplane/v1/workspaces/ws-1/provider-connections/conn-1", 200, {"credential": expected_bind_body()})
        onboarding.gateway.reply("POST", f"/auth/credentials/{HANDLE}/workspaces/ws-1/validation", 503, {"detail": "provider validation unavailable"})
        result = onboarding(["connection", "validate", "--workspace", "ws-1", "--connection-id", "conn-1", "--yes", "--json"])
        assert result.returncode != 0
        assert not [request for request in onboarding.gateway.received if request["method"] == "POST" and "/superplane/v1/" in request["path"]]


class TestReadingsFiledByTheServiceAreReportedSeparately:
    """The readings still have to be readable — through the served GET.

    `GET .../provider-connections/{id}` returns whatever readings the attesting
    service has recorded, in `validation_response`'s shape. That is a real reply to
    a real route, so these tests exercise the reading logic without any client ever
    claiming to have performed a check.
    """

    CONNECTION = "/superplane/v1/workspaces/ws-1/provider-connections/conn-1"

    def test_each_reading_is_preserved_rather_than_reduced_to_one_verdict(self, onboarding) -> None:
        """Three readings, three different operator actions.

        An invalid credential is re-entered; insufficient permissions are widened
        at the provider; exhausted quota is raised or waited out. A single
        "provider not ready" would send someone to the wrong place, so the
        readings survive to the JSON.
        """
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {
                "connection_id": "conn-1",
                "provider": PROVIDER,
                "status": "Pending",
                "validation": {
                    "credential_valid": True,
                    "permissions_sufficient": False,
                    "quota_available": True,
                    "observed_capacity": None,
                    "checked_at": "2026-09-23T10:00:00+00:00",
                },
                "admits_new_work": True,
            },
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        readings = document(result)["detail"]["readings"]
        assert readings["credential_valid"] is True
        assert readings["permissions_sufficient"] is False
        assert readings["quota_available"] is True
        # No aggregate: the separation exists in the output, not only upstream.
        assert "ok" not in readings and "valid" not in readings, readings

    def test_an_unmeasured_capacity_stays_null_instead_of_becoming_zero(self, onboarding) -> None:
        """Not measured is not the same as none available.

        Coercing a null capacity to 0 would report "no capacity" for a check that
        simply was not run, which reads as a provider problem to go fix.
        """
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {
                "connection_id": "conn-1",
                "provider": PROVIDER,
                "validation": {"credential_valid": True, "permissions_sufficient": True, "quota_available": True, "observed_capacity": None},
            },
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        assert document(result)["detail"]["readings"]["observed_capacity"] is None

    def test_a_partial_reading_yields_unknown_readiness_not_not_ready(self, onboarding) -> None:
        """An unmeasured reading makes readiness unknown, never False.

        "Not ready" invites a fix; "unknown" invites a check. A missing
        measurement reported as not-ready sends someone hunting a fault that has
        not been shown to exist.
        """
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {
                "connection_id": "conn-1",
                "provider": PROVIDER,
                "validation": {"credential_valid": True, "permissions_sufficient": None, "quota_available": True},
            },
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        readiness = document(result)["detail"]["provider_readiness"]
        assert readiness["ready"] is None, readiness
        assert "unknown" in readiness["reason"].lower()

    def test_a_connection_with_no_reading_is_unassessed_not_failing(self, onboarding) -> None:
        """A PENDING connection nobody has attested is the ordinary case.

        It is what every bind produces, so reading it as a failure would report
        every freshly bound credential as broken.
        """
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {"connection_id": "conn-1", "provider": PROVIDER, "status": "Pending", "admits_new_work": False},
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        detail = document(result)["detail"]
        assert detail["readings"] is None, detail
        assert detail["provider_readiness"]["ready"] is None, detail

    def test_a_valid_credential_that_admits_no_work_is_not_ready(self, onboarding) -> None:
        """Validity and admission are separate facts.

        A revoked or disabled connection can hold a perfectly valid credential.
        Reading readiness from the credential alone would call it ready.
        """
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {
                "connection_id": "conn-1",
                "provider": PROVIDER,
                "validation": {"credential_valid": True, "permissions_sufficient": True, "quota_available": True},
                "admits_new_work": False,
            },
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        readiness = document(result)["detail"]["provider_readiness"]
        assert readiness["ready"] is False
        assert "does not admit new work" in readiness["reason"]

    def test_fresh_valid_readings_without_admission_authority_remain_unknown(self, onboarding) -> None:
        onboarding.gateway.reply(
            "GET",
            self.CONNECTION,
            200,
            {
                "connection_id": "conn-1",
                "provider": PROVIDER,
                "validation": {
                    "credential_valid": True,
                    "permissions_sufficient": True,
                    "quota_available": True,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            },
        )
        result = onboarding(["connection", "show", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        assert document(result)["detail"]["provider_readiness"]["ready"] is None


class TestRevokingAConnectionLeavesTheVaultCredentialAlone:
    def test_a_204_with_no_body_is_still_a_successful_revoke(self, onboarding) -> None:
        """Success is not inferred from response content.

        ADP answers a successful delete with 204 and an empty body. Parsing that
        as JSON raises, and an implementation that treats the raise as a failure
        reports a completed revoke as broken — prompting a retry against a
        connection that is already gone.
        """
        onboarding.gateway.reply("DELETE", "/superplane/v1/workspaces/ws-1/provider-connections/conn-1", 204, None)
        result = onboarding(["connection", "revoke", "--workspace", "ws-1", "--connection-id", "conn-1", "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        body = document(result)
        assert body["detail"]["revoked"] == "conn-1"
        assert body["detail"]["vault_credential"] == "untouched"

    def test_it_does_not_delete_the_vault_credential(self, onboarding) -> None:
        """Revoking a binding must not destroy the secret it referenced.

        The credential may be bound to other workspaces, and a revoke that
        deleted it would break them — a blast radius far wider than the action
        the user asked for.
        """
        onboarding.gateway.reply("DELETE", "/superplane/v1/workspaces/ws-1/provider-connections/conn-1", 204, None)
        onboarding(["connection", "revoke", "--workspace", "ws-1", "--connection-id", "conn-1", "--yes", "--json"])
        assert not any("/vault/credentials" in r["path"] for r in onboarding.gateway.received), onboarding.gateway.received


class TestCredentialListingCarriesReferencesOnly:
    def test_a_secret_the_server_sends_anyway_does_not_reach_the_output(self, onboarding) -> None:
        """Defence against the server, not only against this client.

        Fields are named explicitly rather than spread, so a value the API should
        never have included cannot flow into stdout — where it would land in a
        terminal scrollback, a CI log and a pasted bug report.
        """
        vault_reply(onboarding.gateway, [vault_row(secret_value=SECRET)])
        result = onboarding(["connection", "credentials", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert SECRET not in result.stdout
        assert SECRET not in result.stderr
        body = document(result)
        # Three named fields, mapped off the real row. The registry row key is not
        # among them: it is not the reference the server matches, so carrying it
        # would only offer a caller the wrong id to bind.
        assert body["detail"]["credentials"] == [{"credential_id": HANDLE, "service": PROVIDER, "label": LABEL}]
        assert ROW_ID not in result.stdout

    def test_one_unreadable_row_does_not_hide_the_readable_ones(self, onboarding) -> None:
        """A malformed record must not blank the list.

        Dropping the whole response would tell a user they have no credentials
        while they are looking at them in the console.
        """
        readable = vault_row(name="b", adp_credential_id="adp-cred-b")
        vault_reply(onboarding.gateway, [{"nonsense": True}, readable])
        result = onboarding(["connection", "credentials", "--json"])
        detail = document(result)["detail"]
        assert [c["credential_id"] for c in detail["credentials"]] == ["adp-cred-b"]
        assert detail["count"] == 1


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class TestNoSecretEverPassesThroughThisSurface:
    @pytest.mark.parametrize("flag", ["--api-key", "--token", "--secret", "--password", "--private-key", "--access-key"])
    def test_a_credential_shaped_flag_is_refused(self, onboarding, flag) -> None:
        """Refused, not accepted-with-a-warning.

        By the time a warning could print, the value is in the shell history file
        and was visible in the process list to every other user on the machine.
        There is no way to un-leak it, so the flag is not part of this surface at
        all.
        """
        result = onboarding(["connection", "bind", "--workspace", "ws-1", flag, SECRET, "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert document(result)["error"]["code"] == "secret_in_argv"

    @pytest.mark.parametrize("form", [["--api-key", SECRET], [f"--api-key={SECRET}"]])
    def test_the_value_is_not_reflected_back_in_either_stream(self, onboarding, form) -> None:
        """Both spellings, and the value never echoed.

        `--api-key=V` and `--api-key V` are one leak with two shapes; splitting
        on `=` catches both. Echoing the rejected value into an error message
        would copy it from the history file into the CI log.
        """
        result = onboarding(["connection", "bind", "--workspace", "ws-1", *form, "--json"])
        assert SECRET not in result.stdout
        assert SECRET not in result.stderr

    def test_the_refusal_points_at_the_reference_based_alternative(self, onboarding) -> None:
        """A refusal without a route forward is just an obstacle."""
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--api-key", SECRET, "--json"])
        message = document(result)["error"]["message"]
        assert "--credential-id" in message
        assert "Nothing was sent" in message

    def test_nothing_is_sent_when_a_secret_flag_is_rejected(self, onboarding) -> None:
        onboarding(["connection", "bind", "--workspace", "ws-1", "--api-key", SECRET, "--json"])
        assert onboarding.gateway.received == []

    def test_no_secret_shaped_field_is_written_to_the_cli_state_file(self, onboarding) -> None:
        """State outlives the process, and gets copied into bug reports.

        This is the place a leaked secret persists longest, so the state file is
        checked by field name against the shared list rather than by eyeballing
        one example.
        """
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        onboarding(["connection", "bind", "--workspace", "ws-1", "--provider", "bedrock", "--credential-id", "cred-9", "--yes", "--json"])
        serialized = json.dumps(state_file(onboarding.home))
        for field in ("secret", "password", "token", "api_key", "private_key", "credential_value", "secret_value"):
            assert f'"{field}"' not in serialized, f"{field} reached the state file: {serialized}"

    def test_the_helper_declares_no_flag_that_takes_a_secret_value(self, onboarding) -> None:
        """A structural check, not a behavioural one.

        The runtime rejection is a tripwire; this asserts there is no flag for a
        secret to arrive through in the first place. A surface with no such
        parameter cannot leak one however it is called.
        """
        source = HELPER.read_text()
        declarations = re.findall(r'add_argument\(\s*"(--[a-z-]+)"', source)
        for flag in declarations:
            assert not re.search(r"secret|password|api-key|private-key|^--token$", flag), (
                f"the onboarding parser declares a secret-bearing flag: {flag}"
            )


# ---------------------------------------------------------------------------
# Readiness: three readings, and deliberately no aggregate
# ---------------------------------------------------------------------------


class TestReadinessIsReportedAsSeparateReadings:
    def seed(self, onboarding, *, status="Active", heartbeat=..., health="Healthy") -> None:
        # The sentinel default matters: `heartbeat or <now>` would substitute a
        # fresh timestamp for the empty string a "never reported" test is trying
        # to inject, so that test would assert against a freshly-beating cluster
        # and pass for the wrong reason.
        if heartbeat is ...:
            heartbeat = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        onboarding.gateway.reply(
            "GET",
            "/superplane/v1/workspaces/ws-1",
            200,
            {"id": "ws-1", "status": status, "cluster_health": health, "last_heartbeat": heartbeat},
        )

    def test_there_is_no_single_overall_ready_field(self, onboarding) -> None:
        """The absence of an aggregate is the feature.

        Control-plane health says nothing about whether a given workspace has a
        reconciling controller, a valid credential, or any capacity. One green
        field computed from these would tell someone they can launch work, and
        they would — so no key exists that a caller could read that way.
        """
        self.seed(onboarding)
        detail = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]
        for forbidden in ("ready", "overall", "overall_ready", "execution_ready", "all_ready", "healthy"):
            assert forbidden not in detail, f"an aggregate verdict {forbidden!r} appeared: {detail}"
        assert {"control_plane", "workspace", "provider"} <= set(detail)

    def test_control_plane_health_is_unknown_rather_than_inferred(self, onboarding) -> None:
        """A successful workspace read is not evidence of control-plane health.

        The domain's own health route is not reachable through the governed
        surface, so the honest answer is that it has not been observed. Reporting
        "reachable" because some other request worked is an inference the data
        does not support.
        """
        self.seed(onboarding)
        reading = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]["control_plane"]
        assert reading["ready"] is None
        assert "not been observed" in reading["reason"]

    def test_a_stale_heartbeat_makes_workspace_readiness_unknown_not_ready(self, onboarding) -> None:
        """A cluster that was healthy ten minutes ago is not known healthy now.

        Treating a stale observation as current is how a "ready" verdict outlives
        the thing it describes, so the reading degrades to unknown.
        """
        self.seed(onboarding, heartbeat=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)))
        reading = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]["workspace"]
        assert reading["ready"] is None, reading
        assert reading["freshness"] == "stale"

    def test_a_cluster_that_never_reported_is_distinguished_from_one_that_stopped(self, onboarding) -> None:
        """Never observed and stopped reporting need different responses.

        The first is a wait during provisioning; the second is a fault. One
        message for both sends someone debugging a healthy new workspace.
        """
        self.seed(onboarding, heartbeat="")
        reading = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]["workspace"]
        assert reading["freshness"] == "unknown"
        assert "never reported" in reading["reason"]

    def test_a_provisioning_workspace_is_not_ready_and_says_why(self, onboarding) -> None:
        self.seed(onboarding, status="Provisioning")
        reading = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]["workspace"]
        assert reading["ready"] is False
        assert "Provisioning" in reading["reason"]

    def test_provider_readiness_is_unassessed_rather_than_false_when_not_asked_for(self, onboarding) -> None:
        """No connection named means not assessed, and it says so.

        Defaulting to not-ready would report a provider problem for a question
        nobody asked.
        """
        self.seed(onboarding)
        reading = document(onboarding(["readiness", "--workspace", "ws-1", "--json"]))["detail"]["provider"]
        assert reading["ready"] is None
        assert "--connection-id" in reading["reason"]

    def test_one_unreadable_reading_degrades_only_itself(self, onboarding) -> None:
        """Partial failure is the normal case during onboarding.

        A brand-new workspace has never reported and has no connection at all, so
        a single failed read must not blank the readings that did succeed — that
        would hide the one piece of information the user needs next.
        """
        self.seed(onboarding)
        result = onboarding(["readiness", "--workspace", "ws-1", "--connection-id", "missing-conn", "--json"])
        detail = document(result)["detail"]
        assert result.returncode == 0, result.stdout + result.stderr
        assert detail["workspace"]["ready"] is True, detail["workspace"]
        assert detail["provider"]["ready"] is None
        assert "provider" in detail["unread"]

    def test_reporting_readings_succeeds_even_when_the_workspace_is_not_ready(self, onboarding) -> None:
        """Exit 0 means "the reading was produced", not "everything is ready".

        Those are different facts, and a non-zero exit here would mean the report
        itself failed. A caller branching on readiness reads the readings.
        """
        self.seed(onboarding, status="Provisioning")
        result = onboarding(["readiness", "--workspace", "ws-1", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert document(result)["status"] == "ok"


# ---------------------------------------------------------------------------
# Durable receipts
# ---------------------------------------------------------------------------


class TestOperationReceiptsSurviveAndStayScoped:
    def seed_receipt(self, home: Path, *, key="key-1", state="unknown", org="", operation_id=None) -> None:
        """Write a receipt the way a prior submission would have left one.

        Written through the real `adp_common.write_state`, not by hand. Hand-rolling
        the file produced one with default permissions, which the helper correctly
        REFUSES to read — state files must be 0600 and owned by the caller. So a
        hand-written fixture tests the refusal path while appearing to test
        recovery, and every recovery assertion passes vacuously against an
        unreadable file. Using the production writer makes the fixture exactly what
        a prior run leaves behind, permissions included.
        """
        receipt = {
            "idempotency_key": key,
            "operation_id": operation_id,
            "fingerprint": "abc123",
            "created_at": "2026-09-23T10:00:00Z",
            "state": state,
            "workspace_id": None,
        }
        # The SCOPE comes from the helper's own `receipt_scope`, not from a literal.
        # A hand-written `{"deployment_id": ""}` matched nothing — the real stamp on
        # a legacy machine is `"default"` — so every scoped read found no receipt and
        # the recovery tests passed while asserting against an empty result. Deriving
        # it means the "mine" fixture is genuinely in scope, which is what gives the
        # cross-tenant exclusion test below its meaning: the two differ only in org.
        program = (
            "import adp_common, json, sys\n"
            "helper = adp_common.load_provider('adp-superplane-onboarding.py')\n"
            "receipt = json.loads(sys.argv[2])\n"
            "receipt['scope'] = helper.receipt_scope()\n"
            "if sys.argv[3]: receipt['scope']['org_id'] = sys.argv[3]\n"
            # Scope FIRST: the key includes it, so this is also the assertion that
            # two organizations' receipts for one intent occupy different slots.
            "helper.write_receipt(receipt['scope'], sys.argv[1], receipt)\n"
        )
        written = subprocess.run(
            ["python3", "-c", program, f"create:{key}", json.dumps(receipt), org],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(home)},
            timeout=30,
        )
        assert written.returncode == 0, written.stderr

    def test_an_unknown_state_is_readable_and_is_not_presented_as_a_failure(self, onboarding) -> None:
        """`unknown` must never become `failed`.

        Collapsing the two is the most damaging simplification available here:
        reporting a lost reply as a failure invites a resubmission, and the
        operation it would duplicate may have succeeded.
        """
        self.seed_receipt(onboarding.home, state="unknown")
        result = onboarding(["operation", "list", "--json"])
        receipts = document(result)["detail"]["receipts"]
        assert [r["state"] for r in receipts.values()] == ["unknown"]
        assert "failed" not in result.stdout

    @pytest.mark.usefixtures("unavailable_onboarding_routes")
    def test_a_receipt_is_still_reported_when_the_server_cannot_be_asked(self, onboarding) -> None:
        """The local receipt is the point of persisting it.

        After a lost reply the identity is the only thing standing between a
        retry and a duplicate workspace. Withholding it because the lookup route
        is unavailable would remove the one piece of recoverable information at
        exactly the moment it is needed.
        """
        self.seed_receipt(onboarding.home, key="key-7", state="unknown")
        result = onboarding(["operation", "recover", "--key", "key-7", "--json"])
        detail = document(result)["detail"]
        assert result.returncode == 4
        assert detail["local_receipt"]["idempotency_key"] == "key-7"
        assert detail["state_is"] == "unknown"
        assert "must not be retried blindly" in detail["consequence"]

    def test_receipts_from_another_organization_are_not_listed(self, onboarding) -> None:
        """A receipt from another tenant names an operation not in this namespace.

        Listing it invites acting on it — reading, or worse resuming, an identity
        that belongs to a different tenant.
        """
        self.seed_receipt(onboarding.home, key="mine", org="")
        self.seed_receipt(onboarding.home, key="theirs", org="org-other")
        result = onboarding(["operation", "list", "--json"])
        keys = {r["idempotency_key"] for r in document(result)["detail"]["receipts"].values()}
        assert keys == {"mine"}, keys

    @pytest.mark.usefixtures("unavailable_onboarding_routes")
    def test_another_organizations_receipt_is_not_recoverable_by_key(self, onboarding) -> None:
        """Scope isolation holds on direct lookup, not only on listing.

        A filtered list with an unfiltered lookup is no isolation at all: the key
        is guessable from the other tenant's own logs.
        """
        self.seed_receipt(onboarding.home, key="theirs", org="org-other", state="unknown")
        result = onboarding(["operation", "recover", "--key", "theirs", "--json"])
        assert document(result)["detail"]["local_receipt"] is None

    def test_the_state_file_is_private_to_its_owner(self, onboarding) -> None:
        """Receipts are written 0600, and a wider mode is refused on read.

        The state file persists operation identities across runs, so it is the
        longest-lived artifact this surface produces and the one most likely to be
        copied into a shared location. A group- or world-readable state file on a
        multi-user host exposes it, and the helper refuses to read one rather than
        trusting a file anyone could have edited.
        """
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        onboarding(["connection", "bind", "--workspace", "ws-1", "--provider", "bedrock", "--credential-id", "cred-9", "--yes", "--json"])
        self.seed_receipt(onboarding.home, key="perms")
        path = state_path(onboarding.home)
        assert path.is_file(), "no state file was written"
        assert path.stat().st_mode & 0o077 == 0, f"state file is not private: {oct(path.stat().st_mode)}"

        path.chmod(0o644)
        widened = onboarding(["operation", "list", "--json"])
        assert widened.returncode == 5, widened.stdout + widened.stderr
        assert document(widened)["error"]["code"] == "unsafe_file"

    def test_the_receipt_holds_identifiers_and_no_secret_shaped_field(self, onboarding) -> None:
        self.seed_receipt(onboarding.home, key="key-1")
        result = onboarding(["operation", "list", "--json"])
        receipt = next(iter(document(result)["detail"]["receipts"].values()))
        assert set(receipt) == {
            "idempotency_key",
            "operation_id",
            "state",
            "workspace_id",
            "fingerprint",
            "created_at",
            "observed_at",
            "approval_id",
            "submission_stage",
        }


class TestTheOutputContractHoldsForPiping:
    def test_narration_goes_to_stderr_so_stdout_stays_one_json_object(self, onboarding) -> None:
        """`--json` means exactly one object on stdout and nothing else.

        Progress text interleaved on stdout breaks every caller that pipes this
        into a parser, and it breaks it intermittently — only when there happened
        to be something to narrate.
        """
        vault_reply(onboarding.gateway)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/ws-1/provider-connections", 201, {"connection_id": "c"})
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--yes", "--json"])
        parsed = json.loads(result.stdout)  # raises if anything else was printed
        assert parsed["status"] == "ok"
        assert "Binding" in result.stderr, "progress narration did not go to stderr"
        # The reference resolution narrates too, and that line must not be on stdout
        # either — it is the one added by this repair, so it is the one most likely
        # to have been written to the wrong stream.
        assert "Resolving credential" in result.stderr

    def test_a_usage_error_still_produces_clean_json_on_stdout(self, onboarding) -> None:
        """The JSON contract must survive a parse failure.

        `--json` is read from raw argv before parsing precisely so a bad
        invocation answers with a parseable object instead of argparse prose that
        a caller's parser then chokes on.
        """
        result = onboarding(["readiness", "--json"])  # --workspace is required
        body = document(result)
        assert result.returncode == 1
        assert body["error"]["code"] == "usage_error"


class TestAMutationIsNotPerformedWithoutStatedIntent:
    def test_a_non_interactive_run_without_yes_changes_nothing(self, onboarding) -> None:
        """No terminal and no --yes means no stated intent.

        Choosing an answer on a script's behalf is exactly what a scripted
        onboarding must not do, so it refuses and says which flag states the
        intent.
        """
        result = onboarding(["connection", "revoke", "--workspace", "ws-1", "--connection-id", "conn-1", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert document(result)["error"]["code"] == "confirmation_required"
        assert onboarding.gateway.received == []

    def test_dry_run_reports_the_request_without_sending_it(self, onboarding) -> None:
        """A review of the exact body, with nothing sent.

        The value is that what is displayed is the request that would go out, so
        the review is of the real thing rather than a description of it.
        """
        vault_reply(onboarding.gateway)
        result = onboarding(["connection", "bind", "--workspace", "ws-1", "--credential-id", HANDLE, "--dry-run", "--json"])
        detail = document(result)["detail"]
        assert result.returncode == 0, result.stdout + result.stderr
        assert detail["performed"] == "nothing"
        # The body shown is the one that would go out, built by the same function
        # the real submission uses. Displaying a description instead would let the
        # displayed and submitted bodies diverge, which is the whole hazard a
        # dry-run exists to remove.
        assert detail["would_send"] == expected_bind_body()
        # A read happened — the reference has to be resolved before there is a body
        # to review — but NO mutation. Asserted by method rather than by an empty
        # ledger, because "nothing at all was sent" is no longer the right claim and
        # would have to be weakened to a vacuous one.
        assert [r["method"] for r in onboarding.gateway.received] == ["GET", "GET"], onboarding.gateway.received
        assert not any(r["method"] == "POST" for r in onboarding.gateway.received)


class TestTheseTestsCanActuallyFail:
    """The counterfactual.

    A suite whose recording gateway never registers a request would pass just as
    happily against a CLI that sent nothing anywhere — so the "nothing was sent"
    assertions above would be vacuous. This proves the ledger records real
    traffic, which is what gives those assertions their force.
    """

    def test_the_recording_gateway_registers_a_request_that_is_really_sent(self, onboarding) -> None:
        onboarding.gateway.reply("GET", "/superplane/v1/vault/credentials", 200, {"credentials": []})
        onboarding(["connection", "credentials", "--json"])
        assert onboarding.gateway.received, "the ledger recorded nothing for a call that should have been made"
        assert onboarding.gateway.received[0]["authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# The create path, exercised in the state where it is reachable
# ---------------------------------------------------------------------------


class TestTheCreatePathOnceTheEndpointsAreServed:
    """The safety properties of a create, tested where they can actually run.

    WHY THIS CLASS EXISTS
    ---------------------
    Mutation testing found that five safety properties of the create path were
    unverified by unavailable-route tests, and the cause was not a missing assertion:
    with `previewWorkspace` unserved, `create` refuses before it
    ever reaches the idempotency gate, the receipt write, or the code that records
    a lost reply as `unknown`. Every test that went through `adp superplane
    onboarding create` returned at the first guard, so deleting the gate, opening
    the fail-closed capability check, reordering the receipt write after the
    request, and collapsing `unknown` into `failed` all left the suite green.

    These tests explicitly select each deployment's served routes and drive the
    same `main()` through a real process:
    real argv, real serialization, real stdout, real state file on disk. The only
    thing substituted is the deployment's route availability, which is exactly the
    variable under test.
    """

    # Imports the shipped helper, disables onboarding routes, then enables only
    # the named endpoints before calling the real main(). Parsing, request
    # building, receipt writing and output are unchanged.
    DRIVER = (
        "import sys, adp_common\n"
        "helper = adp_common.load_provider('adp-superplane-onboarding.py')\n" + UNAVAILABLE_ROUTE_SETUP + "for name in sys.argv[1].split(','):\n"
        "    helper.ENDPOINTS[name] = dict(helper.ENDPOINTS[name], served=True)\n"
        "sys.exit(helper.main(sys.argv[2:]))\n"
    )

    def run(self, onboarding, serve: str, args: list[str]) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(onboarding.home)
        env.pop("ADP_ORG", None)
        return subprocess.run(
            ["python3", "-c", self.DRIVER, serve, *args],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env=env,
            timeout=60,
        )

    SERVED = "previewWorkspace,capabilities"
    PLAN = {"revision": "rev-7", "mode": "managed", "target": {}, "approval_required": False}

    def test_a_create_is_refused_when_the_server_does_not_advertise_the_feature(self, onboarding) -> None:
        """The gate the mutation opened, now actually reached.

        The server answers the capability query successfully and simply does not
        list the feature. That must refuse: a create submitted anyway returns 201
        while the server ignores the unknown identity field and deduplicates
        nothing, so the first lost reply produces a retry that builds a second
        workspace.
        """
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["something-else"]})
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        body = json.loads(result.stdout)
        assert result.returncode == 4, result.stdout + result.stderr
        assert body["status"] == "unavailable"
        assert body["detail"]["required_feature"] == "create-operation-id-v1"
        assert not any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received)

    def test_an_unreadable_capability_report_also_refuses_the_create(self, onboarding) -> None:
        """Fail-closed on a malformed report, not fail-open.

        A body this client cannot read advertises nothing. Treating an unparseable
        report as permission would enable the create precisely when the least is
        known about the server.
        """
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, ["not", "an", "object"])
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        assert not any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received)

    def advertise(self, onboarding) -> None:
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["create-operation-id-v1"]})
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)

    def test_the_identity_travels_in_the_body_and_binds_the_reviewed_plan(self, onboarding) -> None:
        """The identity must be in the body, and the plan revision with it.

        A header cannot work: the gateway's domain proxy rebuilds the upstream
        request with only `Authorization` and `Content-Type`, so a header-borne key
        is dropped in transit — the request succeeds, deduplicates nothing, and
        reports success. The plan revision is what ties the submission to the plan
        the user actually reviewed.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 201, {"id": "ws-new", "provisioning_operation_id": "op-1"})
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        submission = next(r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/workspaces"))
        assert submission["body"]["operation_id"], "no operation identity was submitted"
        assert submission["body"]["plan_revision"] == "rev-7"
        # And not in a header, where it would be silently dropped by the proxy.
        assert "Idempotency-Key" not in submission and "idempotency" not in json.dumps(submission["raw_body"]).lower()

    def test_the_receipt_survives_a_process_killed_mid_request(self, onboarding) -> None:
        """Ordering is the recoverability property, tested where ordering is all there is.

        WHY THIS KILLS THE PROCESS INSTEAD OF RETURNING AN ERROR
        -------------------------------------------------------
        An earlier version of this test made the submission return 500 and asserted
        a receipt existed afterwards. That passed even with the pre-request write
        DELETED, because the `except` branch writes a receipt too — so the test
        proved only that *some* write happens on a handled error, which is not the
        property. Mutation testing caught it.

        The case the ordering actually defends is the one where no handler runs at
        all: the machine reboots, the terminal is closed, the container is evicted
        while the request is in flight. Then the only receipt that can exist is one
        written BEFORE the request. So the gateway here accepts the connection and
        never replies, the process is killed with SIGKILL — which Python cannot
        intercept — and the receipt must still be on disk. With the write moved
        after the request, nothing survives and the submitted identity is lost,
        leaving a possibly-created workspace with no way to recognise a retry.
        """
        import signal

        self.advertise(onboarding)
        # No reply queued for the create: the handler blocks reading a reply that
        # never comes, holding the process in exactly the mid-request state.
        onboarding.gateway.hang("POST", "/superplane/v1/workspaces")

        env = os.environ.copy()
        env["HOME"] = str(onboarding.home)
        env.pop("ADP_ORG", None)
        process = subprocess.Popen(
            ["python3", "-c", self.DRIVER, self.SERVED, "create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(CLI),
            env=env,
        )
        try:
            # Wait for the request to actually arrive, so the kill lands mid-flight
            # rather than before the helper got as far as submitting.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the create request never reached the gateway")
            process.send_signal(signal.SIGKILL)
        finally:
            process.wait(timeout=30)

        # SIGKILL cannot be handled, so nothing ran after the request began.
        assert process.returncode in (-9, 137), process.returncode
        receipts = state_file(onboarding.home).get("receipts") or {}
        assert receipts, (
            "no receipt survived a process killed mid-request: the submitted identity is unrecoverable, so a retry would risk a second workspace"
        )
        receipt = next(iter(receipts.values()))
        assert receipt["idempotency_key"], receipt
        # And the identity on disk is the one that was actually sent, or it is useless.
        submitted = next(r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/workspaces"))
        assert submitted["body"]["operation_id"] == receipt["idempotency_key"]

    def test_a_handled_failure_also_leaves_the_receipt_recoverable(self, onboarding) -> None:
        """The ordinary error path, kept as its own case.

        Distinct from the kill test above: this one proves the receipt is readable
        and correctly marked after a handled failure, which is the common case.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 500, {"error": "boom"})
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        receipts = state_file(onboarding.home).get("receipts") or {}
        assert receipts, "the receipt did not survive a failed submission, so a retry cannot reuse the identity"
        assert next(iter(receipts.values()))["idempotency_key"]

    def test_a_lost_reply_is_recorded_as_unknown_and_never_as_failed(self, onboarding) -> None:
        """The most damaging simplification, now actually exercised.

        The submission may have been accepted. Recording `failed` invites a
        resubmission that duplicates an operation which may have succeeded, so the
        state stays `unknown` and the message says so in those words.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 500, {"error": "boom"})
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        body = json.loads(result.stdout)
        assert body["error"]["code"] == "operation_unknown"
        assert "UNKNOWN" in body["error"]["message"] and "not failed" in body["error"]["message"]
        receipt = next(iter((state_file(onboarding.home).get("receipts") or {}).values()))
        assert receipt["state"] == "unknown", receipt

    def test_retrying_the_same_command_reuses_the_identity_rather_than_minting_one(self, onboarding) -> None:
        """The payoff: a retry after a lost reply cannot duplicate.

        This is what the receipt is for. Two runs of the same command must submit
        the same identity, so the server can recognise the second as the first.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 500, {"error": "boom"})
        self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        first = next(iter((state_file(onboarding.home).get("receipts") or {}).values()))["idempotency_key"]

        onboarding.gateway.received.clear()
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 201, {"id": "ws-new", "provisioning_operation_id": "op-1"})
        self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        resubmission = next(r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/workspaces"))
        assert resubmission["body"]["operation_id"] == first, "a retry minted a new identity and could duplicate the workspace"

    def test_changed_inputs_against_a_live_submission_are_reported_not_resolved(self, onboarding) -> None:
        """A changed payload under a live identity is a conflict only the user can settle.

        Reusing the identity would ask the server to treat two different requests
        as one; minting a new one would submit a second workspace while the first
        may already exist. Neither is the CLI's call to make.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 500, {"error": "boom"})
        self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])

        onboarding.gateway.received.clear()
        changed = self.run(
            onboarding,
            self.SERVED,
            ["create", "--name", "w1", "--isolation", "namespace", "--plan-revision", "rev-7", "--yes", "--json"],
        )
        assert changed.returncode == 1, changed.stdout + changed.stderr
        assert json.loads(changed.stdout)["error"]["code"] == "operation_conflict"
        assert not any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received)

    def test_a_secret_bearing_receipt_is_refused_before_it_reaches_the_disk(self, onboarding) -> None:
        """The state-file tripwire, exercised against a real write.

        Raises rather than redacting: a redacted leak is a leak that shipped, and
        the state file is where a secret would persist beyond the process and get
        copied into a bug report. Driven through the real `write_receipt` so the
        check is proven to sit on the path to disk, not merely to exist.
        """
        program = (
            "import adp_common, sys\n"
            "helper = adp_common.load_provider('adp-superplane-onboarding.py')\n"
            "try:\n"
            "    helper.write_receipt({'deployment_id': 'd', 'org_id': 'o'}, 'create:x', {'idempotency_key': 'k', 'api_key': 'LEAKED'})\n"
            "except adp_common.CliError as exc:\n"
            "    print(exc.code); sys.exit(7)\n"
            "sys.exit(0)\n"
        )
        result = subprocess.run(
            ["python3", "-c", program],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(onboarding.home)},
            timeout=30,
        )
        assert result.returncode == 7, f"a secret-bearing receipt was accepted: {result.stdout} {result.stderr}"
        assert "secret_material_refused" in result.stdout
        assert "LEAKED" not in json.dumps(state_file(onboarding.home))


class TestAReadableReplyIsNotAFinishedOperation:
    """A 201 means accepted, and the receipt must not claim more than that.

    WHY THIS IS A DEFECT AND NOT A NICETY
    -------------------------------------
    `POST /workspaces` answers 201 with `status: "Provisioning"` and builds the
    cluster afterwards (`routers/workspaces.py`). Recording that as `succeeded`
    asserts a terminal outcome the server never gave, and terminal is load-bearing
    in two places: `claim_identity` lets a changed payload mint a fresh identity
    once the earlier receipt is terminal, and the operator reads the state as
    "ready". So the wrong terminal state both permits a duplicate submission and
    tells someone their workspace is up while provisioning may still fail.
    """

    DRIVER = TestTheCreatePathOnceTheEndpointsAreServed.DRIVER
    SERVED = "previewWorkspace,capabilities"
    PLAN = {"revision": "rev-7", "mode": "managed", "target": {}, "approval_required": False}

    run = TestTheCreatePathOnceTheEndpointsAreServed.run
    advertise = TestTheCreatePathOnceTheEndpointsAreServed.advertise

    def create(self, onboarding, reply: dict, status_code: int = 201):
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", status_code, reply)
        result = self.run(onboarding, self.SERVED, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout), next(iter((state_file(onboarding.home).get("receipts") or {}).values()))

    def test_a_provisioning_reply_is_recorded_as_running_not_succeeded(self, onboarding) -> None:
        """The exact body the domain API returns today."""
        body, receipt = self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1", "status": "Provisioning"})
        assert receipt["state"] == "running", (
            f"a workspace the server said was still Provisioning was recorded as {receipt['state']!r}; "
            "a terminal receipt lets changed inputs mint a second identity and tells the operator it is ready"
        )
        assert body["detail"]["still_running"] is True
        # And the output names the command that resolves it, rather than leaving
        # the operator to work out that 'ok' did not mean 'finished'.
        assert receipt["idempotency_key"] in body["detail"]["next_check"]

    def test_an_active_reply_is_recorded_as_succeeded(self, onboarding) -> None:
        """The other half: a server that really did finish must read as finished.

        Without this the change would be indistinguishable from never reporting
        success at all, which would be its own defect — an operation that can
        never settle can never be superseded by a new intent.
        """
        body, receipt = self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1", "status": "Active"})
        assert receipt["state"] == "succeeded", receipt
        assert "still_running" not in body["detail"]

    def test_a_failed_reply_is_recorded_as_failed(self, onboarding) -> None:
        """`Failed` is the one status the router writes that is conclusively over."""
        _, receipt = self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1", "status": "Failed"})
        assert receipt["state"] == "failed", receipt

    def test_a_reply_with_no_status_is_not_assumed_finished(self, onboarding) -> None:
        """Absent means unestablished, and unestablished is not terminal.

        A reply this client cannot read a state from establishes nothing about
        completion. Defaulting to non-terminal costs a poll; defaulting to
        terminal costs a duplicate or a false assurance.
        """
        _, receipt = self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1"})
        assert not state_is_terminal_state(receipt["state"]), receipt

    def test_an_unrecognised_status_is_not_assumed_finished(self, onboarding) -> None:
        """A status this version has never heard of is a wait, not a success.

        The domain's vocabulary is not internally consistent — the router writes
        `Provisioning`/`Teardown`/`Failed`, the model defines
        `pending`/`bootstrapping`/`active`/`drift_detected` — and it will grow. A
        client that reads anything unfamiliar as success is wrong by default on
        every future addition.
        """
        _, receipt = self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1", "status": "reconciling"})
        assert not state_is_terminal_state(receipt["state"]), receipt

    def test_a_still_running_receipt_still_refuses_changed_inputs(self, onboarding) -> None:
        """The consequence, demonstrated rather than argued.

        This is why the state matters: with `Provisioning` recorded as terminal, a
        create with edited inputs mints a new identity and builds a SECOND
        workspace while the first is still being built. Non-terminal makes it the
        conflict it is.
        """
        self.create(onboarding, {"id": "ws-1", "provisioning_operation_id": "op-1", "status": "Provisioning"})
        onboarding.gateway.received.clear()
        changed = self.run(
            onboarding,
            self.SERVED,
            ["create", "--name", "w1", "--isolation", "namespace", "--plan-revision", "rev-7", "--yes", "--json"],
        )
        assert changed.returncode == 1, changed.stdout + changed.stderr
        assert json.loads(changed.stdout)["error"]["code"] == "operation_conflict"
        assert not any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received), (
            "a second workspace was submitted while the first was still provisioning"
        )


def state_is_terminal_state(state: str) -> bool:
    """Terminal per the contract, asserted here rather than re-listed loosely."""
    return state in ("succeeded", "failed")


class TestAdoptingAClusterUsesTheSameGuardsAsCreating:
    """Adoption is not a smaller act than creation, and is no longer treated as one.

    WHAT THE REVIEWED HEAD DID
    --------------------------
    `adopt_command` sent `POST /workspaces/adopt` the moment its route was served:
    no capability check, no reviewed plan, no confirmation, no identity, no
    receipt, and `--dry-run` ignored outright. Every one of those guards existed a
    few lines above in `create_command`. The act being guarded is if anything more
    consequential — adoption points the platform at a cluster that already exists
    and already holds someone's work, so a submission nobody confirmed and nobody
    can recover is worse there than for a create of something new.

    These tests drive the real installed helper with the adopt route served, which
    is the state a deployment that enables it will have. Without them the repair
    would be unverifiable at this revision for exactly the reason the create tests
    above had to be written: an unreachable guard is untested by construction.
    """

    DRIVER = TestTheCreatePathOnceTheEndpointsAreServed.DRIVER
    SERVED = "previewWorkspace,capabilities,adoptWorkspace"
    PLAN = {"revision": "rev-7", "mode": "adopt", "target": {}, "approval_required": False}

    run = TestTheCreatePathOnceTheEndpointsAreServed.run

    ARGS = ["adopt", "--name", "w1", "--cluster", "arn:aws:eks:eu-west-2:1:cluster/prod"]

    def advertise(self, onboarding, features=("create-operation-id-v1",)) -> None:
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": list(features)})
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)

    def adoptions(self, onboarding) -> list[dict]:
        return [r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/adopt")]

    def test_a_dry_run_submits_nothing_and_claims_nothing(self, onboarding) -> None:
        """`--dry-run` was ignored entirely, which is the worst kind of ignored.

        An operator inspecting an adoption before committing to it would have
        performed the adoption. And nothing may be claimed either: a receipt for a
        request that was never sent is a claim the next real attempt would resume
        against an operation that does not exist.
        """
        self.advertise(onboarding)
        result = self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--dry-run", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        body = json.loads(result.stdout)
        assert body["detail"]["dry_run"] is True and body["detail"]["performed"] == "nothing"
        assert not self.adoptions(onboarding), "a dry-run adoption reached the server"
        assert not (state_file(onboarding.home).get("receipts") or {}), "a dry run left a claim behind"

    def test_an_unconfirmed_adoption_is_refused_rather_than_guessed(self, onboarding) -> None:
        """No terminal to ask on and no --yes means no stated intent.

        Choosing an answer on a script's behalf is what a scripted onboarding must
        not do, and here the answer would take over a running cluster.
        """
        self.advertise(onboarding)
        result = self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert json.loads(result.stdout)["error"]["code"] == "confirmation_required"
        assert not self.adoptions(onboarding)

    def test_an_adoption_is_refused_when_the_identity_feature_is_not_advertised(self, onboarding) -> None:
        """Fail-closed, exactly as a create is.

        Without the guarantee a lost reply cannot be retried safely, and for an
        adoption the retry would re-point the platform at a cluster it may already
        have adopted.
        """
        self.advertise(onboarding, features=("something-else",))
        result = self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        body = json.loads(result.stdout)
        assert body["status"] == "unavailable"
        assert body["detail"]["required_feature"] == "create-operation-id-v1"
        assert not self.adoptions(onboarding)

    def test_an_adoption_without_a_reviewable_plan_is_refused(self, onboarding) -> None:
        """What the adoption will change must be readable before it is agreed to."""
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["create-operation-id-v1"]})
        result = self.run(
            onboarding,
            "capabilities,adoptWorkspace",
            [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"],
        )
        assert result.returncode == 4, result.stdout + result.stderr
        assert json.loads(result.stdout)["detail"]["endpoint"] == "previewWorkspace"
        assert not self.adoptions(onboarding)

    def test_an_adoption_carries_an_identity_and_the_reviewed_plan(self, onboarding) -> None:
        """The submitted body, asserted at the wire."""
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/adopt", 201, {"id": "ws-1", "status": "Provisioning"})
        result = self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        submission = self.adoptions(onboarding)[0]
        assert submission["body"]["operation_id"], "an adoption was submitted with no operation identity"
        assert submission["body"]["plan_revision"] == "rev-7"
        assert submission["body"]["cluster_reference"] == self.ARGS[-1]
        receipt = next(iter((state_file(onboarding.home).get("receipts") or {}).values()))
        assert receipt["idempotency_key"] == submission["body"]["operation_id"]
        # Accepted, not finished: the reply said Provisioning.
        assert receipt["state"] == "running", receipt

    def test_retrying_an_adoption_reuses_the_identity(self, onboarding) -> None:
        """The payoff. Two adoptions of one cluster must be one operation."""
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/adopt", 500, {"error": "boom"})
        first = self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"])
        assert first.returncode == 4, first.stdout + first.stderr
        assert json.loads(first.stdout)["error"]["code"] == "operation_unknown"
        claimed = next(iter((state_file(onboarding.home).get("receipts") or {}).values()))
        assert claimed["state"] == "unknown", claimed

        onboarding.gateway.received.clear()
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/adopt", 201, {"id": "ws-1", "status": "Active"})
        self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"])
        assert self.adoptions(onboarding)[0]["body"]["operation_id"] == claimed["idempotency_key"], (
            "a retried adoption minted a new identity and could adopt the cluster twice"
        )

    def test_adopting_a_different_cluster_under_one_name_is_a_separate_intent(self, onboarding) -> None:
        """Keyed on the cluster, because the cluster is what is being taken over.

        Keyed on the workspace name alone, adopting cluster B under a name already
        used for cluster A would resume A's identity — so the server would be asked
        to treat two different clusters as one request. The receipt key carries the
        cluster reference so these stay two operations.
        """
        self.advertise(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/adopt", 201, {"id": "ws-1", "status": "Active"})
        self.run(onboarding, self.SERVED, [*self.ARGS, "--plan-revision", "rev-7", "--yes", "--json"])
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/adopt", 201, {"id": "ws-2", "status": "Active"})
        other = [*self.ARGS[:-1], "arn:aws:eks:eu-west-2:1:cluster/staging"]
        self.run(onboarding, self.SERVED, [*other, "--plan-revision", "rev-7", "--yes", "--json"])

        keys = {r["body"]["operation_id"] for r in self.adoptions(onboarding)}
        assert len(keys) == 2, f"two different clusters were adopted under one operation identity: {keys}"
        assert len(state_file(onboarding.home).get("receipts") or {}) == 2

    def test_an_adoption_with_no_cluster_is_refused_before_anything_is_claimed(self, onboarding) -> None:
        """A request the server cannot interpret must not leave a receipt behind."""
        self.advertise(onboarding)
        result = self.run(onboarding, self.SERVED, ["adopt", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert not self.adoptions(onboarding)
        assert not (state_file(onboarding.home).get("receipts") or {})

    def test_a_plan_revision_is_required_on_the_command_line(self, onboarding) -> None:
        """Refused by the parser, so there is no path that submits an unbound adoption."""
        self.advertise(onboarding)
        result = self.run(onboarding, self.SERVED, [*self.ARGS, "--yes", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert json.loads(result.stdout)["error"]["code"] == "usage_error"
        assert not self.adoptions(onboarding)


class TestConcurrentCommandsCannotMintTwoIdentities:
    """Two `adp` invocations at once — real processes, one state file.

    WHY REAL PROCESSES AND NOT THREADS
    ----------------------------------
    The property under test is that a lock held in one OS process excludes a
    different OS process. Threads share an interpreter, so a thread-based test
    can pass against a lock that is only an in-process mutex — which is exactly
    the defect, not the fix. Two `subprocess.Popen` calls against one sandboxed
    HOME are what the operator actually does with two terminals, and they are the
    only shape that can distinguish the two implementations.

    WHAT GOES WRONG WITHOUT IT
    --------------------------
    Claiming is a read-modify-write over the shared state file. Unlocked, both
    processes read no receipt, both mint, both write — and the second write
    discards the first. Two submissions then carry two identities, so a server
    deduplicating faithfully still builds two workspaces and bills for both,
    because nobody told it the two requests were one.
    """

    DRIVER = (
        "import sys, adp_common\n"
        "helper = adp_common.load_provider('adp-superplane-onboarding.py')\n" + UNAVAILABLE_ROUTE_SETUP + "for name in sys.argv[1].split(','):\n"
        "    helper.ENDPOINTS[name] = dict(helper.ENDPOINTS[name], served=True)\n"
        "sys.exit(helper.main(sys.argv[2:]))\n"
    )
    SERVED = "previewWorkspace,capabilities"
    PLAN = {"revision": "rev-7", "mode": "managed", "target": {}, "approval_required": False}

    def advertise(self, onboarding) -> None:
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["create-operation-id-v1"]})
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces", 201, {"id": "ws-1", "provisioning_operation_id": "op-1"})

    def submissions(self, onboarding) -> list[dict]:
        """Every create actually sent. The wire is the record that matters."""
        return [r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/workspaces")]

    def spawn(self, onboarding, args: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        env["HOME"] = str(onboarding.home)
        env.pop("ADP_ORG", None)
        return subprocess.Popen(
            ["python3", "-c", self.DRIVER, self.SERVED, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(CLI),
            env=env,
        )

    def test_two_simultaneous_creates_submit_exactly_one_identity(self, onboarding) -> None:
        """The headline property, observed at the wire rather than in the state file.

        Both processes are started before either can finish, so their claims
        genuinely overlap. Whatever reaches the gateway is what the server will act
        on, so the assertion is on the submitted bodies: every `operation_id` that
        was actually sent must be the same string. A state file holding one receipt
        while two different identities went out would be a passing test over a
        broken product.
        """
        self.advertise(onboarding)
        args = ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"]
        first, second = self.spawn(onboarding, args), self.spawn(onboarding, args)
        for process in (first, second):
            process.wait(timeout=90)

        identities = {r["body"].get("operation_id") for r in self.submissions(onboarding)}
        assert len(identities) == 1, (
            f"two concurrent creates submitted {len(identities)} distinct operation identities "
            f"({identities}); the server cannot deduplicate them and would build two workspaces"
        )
        # And one receipt on disk, not two: a second slot for the same intent would
        # mean the next retry could pick either.
        receipts = state_file(onboarding.home).get("receipts") or {}
        assert len(receipts) == 1, receipts
        assert next(iter(receipts.values()))["idempotency_key"] == next(iter(identities))

    def test_a_concurrent_command_waits_rather_than_reading_stale_state(self, onboarding) -> None:
        """The loser waits and resumes; it does not mint alongside.

        Distinguished from the test above because that one could in principle pass
        by luck of scheduling if the two processes happened not to overlap. Here the
        second process is started while the first is provably still inside its
        request — the gateway holds the create open — so the overlap is not optional.
        """
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["create-operation-id-v1"]})
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, self.PLAN)
        onboarding.gateway.hang("POST", "/superplane/v1/workspaces")

        args = ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"]
        first = self.spawn(onboarding, args)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the first create never reached the gateway")

            # The first process is now blocked mid-request, holding a persisted
            # claim and NOT holding the lock — the lock covers the claim, not the
            # network call. Holding it across the request would mean one hung
            # submission wedged every other terminal, including the one trying to
            # inspect it.
            second = self.spawn(onboarding, args)
            # Waited at the WIRE, not on exit: the gateway hangs this POST too, so
            # the second process is still blocked when the assertion matters. What
            # it sent is already decided and is the only thing the server can act
            # on, so that is what gets asserted.
            deadline = time.monotonic() + 60
            try:
                while time.monotonic() < deadline:
                    if len(self.submissions(onboarding)) >= 2:
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("the second create never reached the gateway; it neither resumed the identity nor reported a conflict")
            finally:
                second.kill()
                second.wait(timeout=30)
        finally:
            first.kill()
            first.wait(timeout=30)

        identities = {r["body"].get("operation_id") for r in self.submissions(onboarding)}
        assert len(identities) == 1, (
            f"a command run alongside an in-flight create minted a second identity ({identities}), so the two submissions cannot be recognised as one"
        )

    def test_a_create_waits_for_a_held_claim_lock_before_submitting(self, onboarding) -> None:
        """The deterministic test of mutual exclusion, driven through the real command.

        WHY THIS EXISTS ALONGSIDE THE TWO-PROCESS TEST ABOVE
        ---------------------------------------------------
        That test races two real creates, which is the operator's actual scenario —
        but a race is not a discriminator. Mutation testing proved it: removing the
        lock entirely left it passing, because the claim is a couple of filesystem
        calls and two freshly-started interpreters rarely land inside that window.
        A test that cannot fail when the mechanism is deleted is not evidence for
        the mechanism.

        So the window is made arbitrarily wide instead of hoped for: the lock is
        taken and held by the test, and a real `create --yes` is run against it. If
        the claim is locked, the create cannot reach the gateway while the lock is
        held and proceeds once it is released. If it is not locked, the create
        submits immediately — which is precisely the duplicate-producing behaviour.
        """
        self.advertise(onboarding)
        lock = subprocess.run(
            [
                "python3",
                "-c",
                "import adp_common; print(adp_common.state_path('superplane_onboarding').with_suffix('.lock'))",
            ],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(onboarding.home)},
            timeout=30,
        )
        assert lock.returncode == 0, lock.stderr
        lock_dir = Path(lock.stdout.strip())
        lock_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_dir.mkdir(mode=0o700)

        create = self.spawn(onboarding, ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"])
        try:
            # Waited on an observable checkpoint rather than a bare sleep: the
            # capability check is the last thing a create does before it claims,
            # so its arrival proves the process is at the claim and not merely
            # slow to start. A sleep alone would make a passing result mean
            # "python is slow" just as readily as "the claim is exclusive".
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(r["path"].endswith("/capabilities") for r in onboarding.gateway.received):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the create never got as far as checking capabilities")
            time.sleep(1.5)

            assert not self.submissions(onboarding), (
                "a create submitted while another command held the claim lock; the claim is not "
                "mutually exclusive, so two commands can mint two identities for one intent"
            )

            lock_dir.rmdir()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.submissions(onboarding):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the create never submitted after the lock was released; waiting must be a delay, not a refusal")
        finally:
            create.kill()
            create.wait(timeout=30)

    def test_a_dry_run_leaves_no_claim_for_a_request_it_never_sent(self, onboarding) -> None:
        """A rehearsal must not occupy the slot.

        The claim persists, which is what makes a crash recoverable — and is also
        why a dry run must not make one. A claim for a request nobody sent turns the
        operator's next real attempt with different inputs into a conflict against a
        phantom, which they can only clear by editing state by hand.
        """
        self.advertise(onboarding)
        result = subprocess.run(
            ["python3", "-c", self.DRIVER, self.SERVED, "create", "--name", "w1", "--plan-revision", "rev-7", "--dry-run", "--json"],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(onboarding.home), "ADP_ORG": ""},
            timeout=60,
        )
        body = json.loads(result.stdout)
        assert body["detail"]["dry_run"] is True, body
        assert body["detail"]["performed"] == "nothing"
        # No identity invented for a submission that did not happen: printing one
        # invites the operator to quote it at support for a nonexistent operation.
        assert body["detail"]["identity"] is None, body["detail"]
        assert not (state_file(onboarding.home).get("receipts") or {}), "a dry run left a claim on disk"
        assert not any(r["method"] == "POST" and r["path"].endswith("/workspaces") for r in onboarding.gateway.received)

    def test_two_organizations_claim_the_same_intent_without_colliding(self, onboarding) -> None:
        """One workspace name, two tenants, two independent operations.

        The receipt key carries the scope, so these occupy different slots. Keyed by
        intent alone they would share one, and the second tenant's claim would
        overwrite the first's — destroying an identity that may be the only record
        of a paid operation, and reporting it afterwards as merely absent.
        """
        self.advertise(onboarding)
        args = ["create", "--name", "w1", "--plan-revision", "rev-7", "--yes", "--json"]
        for org in ("org-a", "org-b"):
            env = os.environ.copy()
            env["HOME"] = str(onboarding.home)
            env["ADP_ORG"] = org
            token_path = onboarding.home / ".bedrock-gateway" / "tokens.json"
            token_data = json.loads(token_path.read_text())
            token_data["access_token"] = token_for_org(org)
            token_path.write_text(json.dumps(token_data))
            done = subprocess.run(
                ["python3", "-c", self.DRIVER, self.SERVED, *args],
                capture_output=True,
                text=True,
                cwd=str(CLI),
                env=env,
                timeout=60,
            )
            assert done.returncode == 0, done.stdout + done.stderr

        receipts = state_file(onboarding.home).get("receipts") or {}
        assert len(receipts) == 2, f"one organization's claim overwrote the other's: {receipts}"
        orgs = {(r.get("scope") or {}).get("org_id") for r in receipts.values()}
        assert orgs == {"org-a", "org-b"}, orgs
        keys = {r["idempotency_key"] for r in receipts.values()}
        assert len(keys) == 2, "two tenants' operations share one identity"

    def test_a_held_lock_reports_unknown_rather_than_failure(self, onboarding) -> None:
        """A wait that times out is exit 4, not exit 5.

        Whether the holder's submission succeeded cannot be known from here.
        Reporting a failure would invite a retry of something that may already have
        spent money, so the contract's pending/unavailable code is the honest one.
        """
        program = (
            "import adp_common, sys\n"
            "try:\n"
            "    with adp_common.state_lock('superplane_onboarding', 'busy.', timeout=0):\n"
            "        pass\n"
            "except adp_common.CliError as exc:\n"
            "    print(exc.code, exc.exit_code, exc)\n"
            "    sys.exit(exc.exit_code)\n"
        )
        # Take the lock in this process first, so the child provably contends.
        held = subprocess.run(
            [
                "python3",
                "-c",
                "import adp_common; p = adp_common.state_path('superplane_onboarding').with_suffix('.lock'); p.mkdir(mode=0o700)",
            ],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(onboarding.home)},
            timeout=30,
        )
        assert held.returncode == 0, held.stderr

        result = subprocess.run(
            ["python3", "-c", program],
            capture_output=True,
            text=True,
            cwd=str(CLI),
            env={**os.environ, "HOME": str(onboarding.home)},
            timeout=30,
        )
        assert result.returncode == 4, result.stdout + result.stderr
        assert "lock_busy" in result.stdout
        # The message must name the path, or a stale lock is unclearable without
        # the operator reverse-engineering the layout.
        assert ".lock" in result.stdout or ".lock" in result.stderr


class TestOnboardingApprovalJourney:
    DRIVER = TestTheCreatePathOnceTheEndpointsAreServed.DRIVER
    run = TestTheCreatePathOnceTheEndpointsAreServed.run
    SERVED = "previewWorkspace,capabilities,requestApproval,getApproval,decideApproval"

    def test_one_request_identity_survives_plan_approval_and_create(self, onboarding):
        plan = {"revision": "rev-7", "mode": "managed", "target": {}, "approval_required": True}
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, plan)
        planned = self.run(onboarding, self.SERVED, ["plan", "--name", "approved-workspace", "--json"])
        assert planned.returncode == 0, planned.stdout + planned.stderr
        request_id = document(planned)["detail"]["request_id"]
        approval_body = {
            "workspace_id": "ws-approved",
            "action": "provision",
            "idempotency_key": request_id,
            "parameters": {"plan_revision": "rev-7", "region": "us-east-1"},
        }
        plan["approval_request"] = approval_body
        onboarding.gateway.reply("POST", "/superplane/v1/workspaces/preview", 200, plan)
        approval = {"approval_id": "approval-1", "result": "pending", "revoked": False, "can_decide": False, "expires_at": "2999-01-01T00:00:00Z"}
        onboarding.gateway.reply("POST", "/superplane/v1/operation-approvals", 200, approval)
        requested = self.run(
            onboarding, self.SERVED, ["approval", "request", "--name", "approved-workspace", "--plan-revision", "rev-7", "--yes", "--json"]
        )
        assert requested.returncode == 0, requested.stdout + requested.stderr
        approval_posts = [r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/operation-approvals")]
        assert approval_posts[0]["body"] == approval_body
        receipt = next(iter(state_file(onboarding.home)["receipts"].values()))
        assert receipt["idempotency_key"] == request_id
        assert receipt["approval_id"] == "approval-1"
        assert receipt["submission_stage"] == "approval"
        onboarding.gateway.reply("GET", "/superplane/v1/operation-approvals/approval-1", 200, dict(approval, result="allowed-once"))
        onboarding.gateway.reply("GET", "/superplane/v1/capabilities", 200, {"features": ["create-operation-id-v1"]})
        onboarding.gateway.reply(
            "POST",
            "/superplane/v1/workspaces",
            201,
            {"id": "ws-approved", "status": "Provisioning", "provisioning_operation_id": "harness-operation-9"},
        )
        submitted = self.run(onboarding, self.SERVED, ["create", "--name", "approved-workspace", "--plan-revision", "rev-7", "--yes", "--json"])
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        creates = [r for r in onboarding.gateway.received if r["method"] == "POST" and r["path"].endswith("/workspaces")]
        assert creates[0]["body"]["operation_id"] == request_id
        assert creates[0]["body"]["approval_id"] == "approval-1"
        assert document(submitted)["detail"]["receipt"]["operation_id"] == "harness-operation-9"

    def test_a_requester_cannot_decide_their_own_approval(self, onboarding):
        onboarding.gateway.reply("GET", "/superplane/v1/operation-approvals/approval-1", 200, {"approval_id": "approval-1", "can_decide": False})
        result = self.run(
            onboarding, self.SERVED, ["approval", "decide", "--approval-id", "approval-1", "--result", "allowed-once", "--yes", "--json"]
        )
        assert result.returncode == 3
        assert not [r for r in onboarding.gateway.received if r["method"] == "POST"]


class TestSavedLifecycleJourney:
    DRIVER = TestTheCreatePathOnceTheEndpointsAreServed.DRIVER
    run = TestTheCreatePathOnceTheEndpointsAreServed.run
    SERVED = "listLifecycleProposals,previewLifecycleProposal,continueLifecycleProposal,requestApproval,getApproval,recoverOperation"
    BASE = "/superplane/v1/workspaces/ws-lifecycle/lifecycle-proposals/artifact-plan"
    REVISION = "a" * 64

    def test_completed_workspace_has_no_next_approval_plan(self, onboarding):
        path = "/superplane/v1/workspaces/ws-lifecycle/lifecycle-proposals"
        onboarding.gateway.reply("GET", path, 200, {"workspace_id": "ws-lifecycle", "proposals": []})
        result = self.run(onboarding, self.SERVED, ["lifecycle", "list", "--workspace", "ws-lifecycle", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert document(result)["detail"]["proposals"] == []
        assert not [row for row in onboarding.gateway.received if row["method"] == "POST"]

    def proposal(self, body):
        request_id = body["operation_id"]
        return {
            "status": "awaiting_plan_approval",
            "artifact_id": "artifact-plan",
            "workspace_id": "ws-lifecycle",
            "source_operation_id": "completed-prepare",
            "request_revision": "b" * 64,
            "phase": "apply-infrastructure",
            "account_id": "123456789012",
            "target": {"region": "us-east-1"},
            "plan_file_sha256": "c" * 64,
            "plan_json_sha256": "d" * 64,
            "inventory": [{"address": "aws_eks_cluster.main", "actions": ["create"]}],
            "estimate": {"max_cost_micros": 1000000},
            "request_id": request_id,
            "revision": self.REVISION,
            "approval_request": {
                "workspace_id": "ws-lifecycle",
                "action": "provision",
                "idempotency_key": request_id,
                "parameters": {"lifecycle_artifact_id": "artifact-plan", "plan_file_sha256": "c" * 64, "max_cost_micros": "1000000"},
            },
        }

    def approval(self, **overrides):
        return dict(
            {
                "approval_id": "approval-phase",
                "workspace_id": "ws-lifecycle",
                "plan_digest": self.REVISION,
                "result": "allowed-once",
                "revoked": False,
                "expires_at": "2999-01-01T00:00:00Z",
                "can_decide": False,
            },
            **overrides,
        )

    def args(self, verb, *extra):
        result = ["lifecycle", verb, "--workspace", "ws-lifecycle", "--artifact-id", "artifact-plan", "--json"]
        if verb in ("request-approval", "continue"):
            result += ["--plan-revision", self.REVISION, "--yes"]
        return result + list(extra)

    def prepare(self, onboarding):
        onboarding.gateway.reply("POST", self.BASE + "/preview", 200, self.proposal)
        planned = self.run(onboarding, self.SERVED, self.args("plan"))
        assert planned.returncode == 0, planned.stdout + planned.stderr
        request_id = document(planned)["detail"]["request_id"]
        onboarding.gateway.reply("POST", "/superplane/v1/operation-approvals", 200, self.approval(result="pending"))
        approved = self.run(onboarding, self.SERVED, self.args("request-approval"))
        assert approved.returncode == 0, approved.stdout + approved.stderr
        onboarding.gateway.reply("GET", "/superplane/v1/operation-approvals/approval-phase", 200, self.approval())
        return request_id

    def test_exact_plan_approval_continuation_and_recovery_keep_one_identity(self, onboarding):
        request_id = self.prepare(onboarding)
        approvals = [r for r in onboarding.gateway.received if r["path"].endswith("/operation-approvals")]
        assert approvals[0]["body"] == self.proposal({"operation_id": request_id})["approval_request"]
        operation = {"request_id": request_id, "workspace_id": "ws-lifecycle", "provisioning_operation_id": "server-phase-2", "state": "succeeded"}
        onboarding.gateway.reply("POST", self.BASE + "/continue", 200, operation)
        submitted = self.run(onboarding, self.SERVED, self.args("continue"))
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        assert document(submitted)["detail"]["receipt"]["operation_id"] == "server-phase-2"
        assert "readiness is checked separately" in document(submitted)["detail"]["readiness"]
        posts = [r for r in onboarding.gateway.received if r["path"].endswith("/continue")]
        assert posts[0]["body"] == {"operation_id": request_id, "approval_id": "approval-phase"}
        # Once the API advances, a resumed command must not require its old proposal.
        onboarding.gateway.reply("POST", self.BASE + "/preview", 409, {"error": "source_advanced"})
        onboarding.gateway.reply("GET", "/superplane/v1/operations/by-idempotency/" + request_id, 200, operation)
        count = len(onboarding.gateway.received)
        recovered = self.run(onboarding, self.SERVED, self.args("continue"))
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert not [r for r in onboarding.gateway.received[count:] if r["method"] == "POST"]
        assert document(recovered)["detail"]["receipt"]["idempotency_key"] == request_id

    @pytest.mark.parametrize(
        "overrides",
        [
            {"result": "pending"},
            {"result": "rejected"},
            {"expires_at": "2001-01-01T00:00:00Z"},
            {"revoked": True},
            {"plan_digest": "e" * 64},
            {"workspace_id": "foreign"},
        ],
    )
    def test_noncurrent_or_different_approval_cannot_continue(self, onboarding, overrides):
        self.prepare(onboarding)
        onboarding.gateway.reply("GET", "/superplane/v1/operation-approvals/approval-phase", 200, self.approval(**overrides))
        result = self.run(onboarding, self.SERVED, self.args("continue"))
        assert result.returncode == 4
        assert not [r for r in onboarding.gateway.received if r["path"].endswith("/continue")]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("workspace_id", "foreign"),
            ("artifact_id", "different"),
            ("request_id", "different"),
            ("plan_file_sha256", "missing"),
        ],
    )
    def test_preview_refuses_identity_mismatch_or_missing_apply_hash(self, onboarding, field, value):
        onboarding.gateway.reply("POST", self.BASE + "/preview", 200, lambda body: dict(self.proposal(body), **{field: value}))
        result = self.run(onboarding, self.SERVED, self.args("plan"))
        assert result.returncode == 4
        assert not [r for r in onboarding.gateway.received if r["path"].endswith(("/continue", "/operation-approvals"))]

    def test_changed_hash_at_final_preview_prevents_continuation(self, onboarding):
        self.prepare(onboarding)
        count = 0

        def changed(body):
            nonlocal count
            count += 1
            return dict(self.proposal(body), plan_file_sha256=("c" if count == 1 else "e") * 64)

        onboarding.gateway.reply("POST", self.BASE + "/preview", 200, changed)
        result = self.run(onboarding, self.SERVED, self.args("continue"))
        assert result.returncode == 4
        assert count == 2
        assert not [r for r in onboarding.gateway.received if r["path"].endswith("/continue")]

    def test_lost_continuation_reply_keeps_identity_and_recovers_without_new_submission(self, onboarding):
        request_id = self.prepare(onboarding)
        onboarding.gateway.reply("POST", self.BASE + "/continue", 503, {"error": "response_lost"})
        result = self.run(onboarding, self.SERVED, self.args("continue"))
        assert result.returncode == 4
        receipt = next(iter(state_file(onboarding.home)["receipts"].values()))
        assert receipt["state"] == "unknown"
        assert receipt["submission_stage"] == "submitted"
        assert receipt["idempotency_key"] == request_id
        operation = {"request_id": request_id, "workspace_id": "ws-lifecycle", "provisioning_operation_id": "accepted-phase", "state": "running"}
        onboarding.gateway.reply("GET", "/superplane/v1/operations/by-idempotency/" + request_id, 200, operation)
        result = self.run(onboarding, self.SERVED, self.args("continue"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert len([r for r in onboarding.gateway.received if r["path"].endswith("/continue")]) == 1
        assert document(result)["detail"]["receipt"]["operation_id"] == "accepted-phase"

    @pytest.mark.parametrize("verb", ["request-approval", "continue"])
    def test_dry_run_never_writes_receipts_or_sends_request(self, onboarding, verb):
        result = self.run(onboarding, self.SERVED, self.args(verb, "--dry-run"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert state_file(onboarding.home) == {}
        assert not [r for r in onboarding.gateway.received if r["path"].startswith("/api/superplane/")]

    @pytest.mark.usefixtures("unavailable_onboarding_routes")
    def test_unserved_lifecycle_fails_before_any_request(self, onboarding):
        result = onboarding(self.args("plan"))
        assert result.returncode == 4
        assert state_file(onboarding.home) == {}
        assert not [r for r in onboarding.gateway.received if r["path"].startswith("/api/superplane/")]

    @pytest.mark.parametrize("field,code", [("debug_output", 0), ("secret_value", 1)])
    def test_lifecycle_extra_response_fields_never_expose_secret_values(self, onboarding, field, code):
        onboarding.gateway.reply("POST", self.BASE + "/preview", 200, lambda body: dict(self.proposal(body), **{field: SECRET}))
        result = self.run(onboarding, self.SERVED, self.args("plan"))
        assert result.returncode == code, result.stdout + result.stderr
        assert SECRET not in result.stdout + result.stderr + json.dumps(state_file(onboarding.home))
        if code == 0:
            assert field not in document(result)["detail"]["plan"]

    def test_approval_and_operation_extra_fields_never_escape_to_stdout(self, onboarding):
        request_id = self.prepare(onboarding)
        onboarding.gateway.reply("POST", "/superplane/v1/operation-approvals", 200, self.approval(debug_output=SECRET))
        approval = self.run(onboarding, self.SERVED, self.args("request-approval"))
        assert approval.returncode == 0, approval.stdout + approval.stderr
        assert SECRET not in approval.stdout + approval.stderr
        onboarding.gateway.reply(
            "POST",
            self.BASE + "/continue",
            200,
            {
                "request_id": request_id,
                "workspace_id": "ws-lifecycle",
                "provisioning_operation_id": "server-phase-2",
                "state": "running",
                "debug_output": SECRET,
            },
        )
        submitted = self.run(onboarding, self.SERVED, self.args("continue"))
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        assert SECRET not in submitted.stdout + submitted.stderr + json.dumps(state_file(onboarding.home))
