# tests/cli/test_adp_update.py
"""Tests for `adp update` against a gateway that really serves the files.

Self-update is the reason this CLI can ship at all: it replaces "re-download the
scripts by hand", so the docs can say `adp update` instead of restating the curl
line. It is also the one verb that fetches and then overwrites its own
executables, so the failure modes are worth pinning:

- it must pull from the gateway that installed it, resolved from the stored
  config — not from a hardcoded or guessed origin;
- a fetch failure must leave the working CLI in place rather than a half-written
  one, because the recovery tool IS the thing being replaced;
- it must leave *.prev behind so `--rollback` works after a bad update.

The other `adp update` cases (rollback, no-gateway-known) need no server and live
in test_adp_setup.py. This module exists for the ones that do.
"""

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Read the set install.sh actually downloads rather than restating it. A second
# hardcoded copy silently goes stale: when adp-superplane.py joined the set
# (#5039) a duplicated list made the mock gateway 404 on it, so these tests
# failed for a reason that had nothing to do with `adp update`.
_INSTALL_SH = Path(__file__).parents[2] / "cli/install.sh"
CLI_FILES = ["install.sh"] + re.search(r'^CLI_FILES="([^"]+)"', _INSTALL_SH.read_text(), re.MULTILINE).group(1).split()


class _CliServer:
    """A stand-in for GET /api/cli/{script_name}.

    Serves real file bytes from a directory, so `adp update` exercises the same
    download route contract the gateway implements (src/cli_download/routes.py).
    Individual paths can be made to fail, to test the abort path.
    """

    def __init__(self, source_dir: Path):
        self.source_dir = source_dir
        self.fail_paths: set[str] = set()
        self.requested: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "_CliServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requested.append(self.path)
                name = self.path.rsplit("/", 1)[-1]
                source = outer.source_dir / name
                if self.path in outer.fail_paths or not source.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = source.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/x-shellscript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"


@pytest.fixture
def upstream(tmp_path: Path, cli_dir: Path):
    """A gateway serving a *newer* copy of the CLI than the one installed.

    `adp` is marked so a test can tell the fetched copy from the installed one;
    the rest are the real files, so what lands is genuinely runnable.
    """
    source = tmp_path / "upstream"
    source.mkdir()
    for name in CLI_FILES:
        source.joinpath(name).write_bytes((cli_dir / name).read_bytes())

    marked = source / "adp"
    marked.write_text(marked.read_text().replace('ADP_VERSION="1.0.0"', 'ADP_VERSION="9.9.9-from-gateway"'))

    with _CliServer(source) as server:
        yield server


@pytest.fixture
def installed(tmp_path: Path, cli_dir: Path, upstream) -> tuple[Path, Path]:
    """An install whose config points at the mock gateway. Returns (bin, home)."""
    bin_dir = tmp_path / "installed-bin"
    bin_dir.mkdir()
    # Derived from install.sh's CLI_FILES, like `upstream` above, rather than
    # hardcoded: a second copy of the file list goes stale the moment a CLI file
    # is added, and then this fixture fails for a reason unrelated to `update`.
    for name in (item for item in CLI_FILES if item != "install.sh"):
        target = bin_dir / name
        target.write_bytes((cli_dir / name).read_bytes())
        target.chmod(0o755)

    home = tmp_path / "update-home"
    (home / ".bedrock-gateway").mkdir(parents=True)
    # 0700, as install.sh and the auth core both create it. The deployment
    # registry refuses to adopt a legacy store with looser permissions -- a
    # world-readable directory holds this user's refresh token -- so a fixture
    # left at the default mode would fail #5413's tests on the fixture's own
    # mistake rather than on anything `adp update` did.
    (home / ".bedrock-gateway").chmod(0o700)
    (home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": upstream.url, "client_id": "abc123"}))

    return bin_dir, home


def _run_adp(bin_dir: Path, home: Path, args: list[str], *, deployment: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["SHELL"] = "/bin/sh"
    # `ADP_DEPLOYMENT` pins one terminal's selection (#5413). Passed here rather
    # than via `--deployment` for the verbs that refuse a selection flag.
    if deployment is not None:
        env["ADP_DEPLOYMENT"] = deployment
    return subprocess.run(["bash", str(bin_dir / "adp"), *args], capture_output=True, text=True, env=env, timeout=60)


def _listed_deployments(bin_dir: Path, home: Path) -> list[dict]:
    """The registry as the real CLI reports it, for fixture self-checks."""
    result = _run_adp(bin_dir, home, ["deployment", "list", "--json"])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["deployments"]


class TestUpdate:
    def test_pulls_the_new_cli_from_the_stored_gateway(self, installed) -> None:
        bin_dir, home = installed

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode == 0, result.stderr
        assert "9.9.9-from-gateway" in (bin_dir / "adp").read_text()

    def test_fetches_the_installer_and_all_three_files(self, installed, upstream) -> None:
        """install.sh does the work; `adp update` just re-runs it against the
        same prefix, so the whole set refreshes together."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        for name in CLI_FILES:
            assert f"/api/cli/{name}" in upstream.requested, f"{name} was not fetched"

    def test_updates_in_place_without_moving_the_prefix(self, installed) -> None:
        """The updated CLI must stay where PATH already points."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        for name in CLI_FILES:
            if name == "install.sh":
                continue  # fetched to a temp dir to run, not installed into the prefix
            assert (bin_dir / name).is_file()
        assert not (home / ".adp").exists(), "must not silently install to the default prefix"

    def test_the_updated_cli_runs(self, installed) -> None:
        bin_dir, home = installed
        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        result = _run_adp(bin_dir, home, ["version"])

        assert result.returncode == 0, result.stderr
        assert "9.9.9-from-gateway" in result.stdout

    def test_leaves_a_rollback_target(self, installed) -> None:
        """Without this, a bad update is unrecoverable without the curl line."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        assert (bin_dir / "adp.prev").is_file()
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp.prev").read_text()

    def test_update_then_rollback_restores_the_old_version(self, installed) -> None:
        bin_dir, home = installed
        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        result = _run_adp(bin_dir, home, ["update", "--rollback"])

        assert result.returncode == 0, result.stderr
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        assert _run_adp(bin_dir, home, ["version"]).returncode == 0, "rolled-back CLI must run"

    def test_keeps_the_session_config(self, installed, upstream) -> None:
        """Updating must not log the user out."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        config = json.loads((home / ".bedrock-gateway" / "config.json").read_text())
        assert config["client_id"] == "abc123"
        assert config["gateway_url"] == upstream.url


class TestUpdateFailure:
    def test_a_missing_installer_leaves_the_working_cli_intact(self, installed, upstream) -> None:
        """The recovery tool is the thing being replaced, so a failed fetch must
        abort before touching anything."""
        bin_dir, home = installed
        upstream.fail_paths.add("/api/cli/install.sh")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        assert _run_adp(bin_dir, home, ["version"]).returncode == 0

    def test_a_missing_payload_file_is_reported_not_silently_skipped(self, installed, upstream) -> None:
        """A partial update that reports success is worse than a failed one: the
        user would not know to roll back."""
        bin_dir, home = installed
        upstream.fail_paths.add("/api/cli/bg-cognito-auth.sh")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert "bg-cognito-auth.sh" in result.stderr

    def test_an_unreachable_gateway_fails_cleanly(self, installed, tmp_path: Path) -> None:
        bin_dir, home = installed
        # Port 1 on loopback: nothing listens, connection refused immediately.
        (home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": "http://127.0.0.1:1/api"}))

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()

    def test_a_pinned_update_lands_exactly_that_version(self, installed, upstream) -> None:
        """The matching case must still work.

        A pin that rejected everything would pass every failure test below and be
        worthless, so the happy path is pinned too: served 2.1.0, asked for 2.1.0,
        got 2.1.0.
        """
        bin_dir, home = installed
        served = upstream.source_dir / "adp"
        served.write_text(served.read_text().replace('ADP_VERSION="9.9.9-from-gateway"', 'ADP_VERSION="2.1.0"'))

        result = _run_adp(bin_dir, home, ["update", "--to", "2.1.0"])

        assert result.returncode == 0, result.stderr
        assert "2.1.0" in _run_adp(bin_dir, home, ["version"]).stdout

    def test_a_non_semver_served_version_cannot_be_pinned(self, installed) -> None:
        """`--to` accepts only exact semver, so an oddly-versioned build is
        refused before anything is downloaded rather than installed anyway."""
        bin_dir, home = installed

        result = _run_adp(bin_dir, home, ["update", "--to", "9.9.9-from-gateway"])

        assert result.returncode == 1
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()

    def test_a_pinned_update_that_disagrees_with_the_gateway_installs_nothing(self, installed) -> None:
        """The end-to-end version of the pin: a real gateway serving a version
        the caller did not ask for must leave the working CLI untouched."""
        bin_dir, home = installed

        result = _run_adp(bin_dir, home, ["update", "--to", "1.2.3"])

        assert result.returncode != 0
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        assert _run_adp(bin_dir, home, ["version"]).returncode == 0, "the CLI must still run"


class TestUpdateWithThreeDeployments:
    """AC-08 (#5413): a CLI serving three deployments must survive its own update.

    The registry and the three per-deployment stores live under `~/.adp`, and
    `install.sh` writes into the PREFIX, so in principle they never meet. In
    principle is not the standard here: this is the verb that overwrites its own
    executables, and a user with development, integration and pre-production
    terminals open loses three sessions at once if it touches the registry. The
    single-deployment `test_keeps_the_session_config` above could not catch that,
    because a legacy install keeps its session somewhere else entirely.

    One of the three is the real mock gateway and is made the default, which is
    what a genuine multi-deployment install looks like: the CLI is installed once,
    from one of the deployments it goes on to serve.
    """

    OTHERS = ("integration", "preprod")

    @staticmethod
    def _register_three(bin_dir: Path, home: Path, upstream) -> None:
        """development = the serving gateway; the other two are registered only.

        Registered-not-signed-in is the ordinary state, not an edge case: `adp
        deployment add` writes a record and makes no request, so the two remote
        environments have a URL and no config.json until someone logs in to them.

        The legacy store is rebound to its own url first. The `installed` fixture
        points it at the mock gateway, and a second name for an already-registered
        url is an ALIAS — one stable id, one store, one session. Left that way,
        `development` was simply another name for the adopted legacy record, so a
        class named for three deployments exercised two, and any assertion that
        `development` routes to the mock gateway was true by fixture construction
        rather than by anything the resolver did.
        """
        legacy_config = home / ".bedrock-gateway" / "config.json"
        legacy_config.write_text(json.dumps({"gateway_url": "https://legacy.example.invalid/api", "client_id": "abc123"}))
        for name, url in (
            ("development", upstream.url),
            ("integration", "https://integration.example.invalid/api"),
            ("preprod", "https://preprod.example.invalid/api"),
        ):
            result = _run_adp(bin_dir, home, ["deployment", "add", name, "--url", url])
            assert result.returncode == 0, f"registering {name} failed: {result.stderr}"
        assert _run_adp(bin_dir, home, ["deployment", "use", "development"]).returncode == 0
        # Four genuinely distinct deployments (the three above plus adopted legacy),
        # so "three deployments" is a fact about the registry, not a hope.
        identifiers = {entry["name"]: entry["deployment_id"] for entry in _listed_deployments(bin_dir, home)}
        for name in ("development", "integration", "preprod"):
            assert identifiers[name] != identifiers.get("default"), f"{name} must not be an alias of the legacy store"
        assert len({identifiers[name] for name in ("development", "integration", "preprod")}) == 3, (
            f"the three deployments share a store: {identifiers}"
        )

    @staticmethod
    def _listed(bin_dir: Path, home: Path) -> dict:
        result = _run_adp(bin_dir, home, ["deployment", "list", "--json"])
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_update_keeps_every_record_and_the_saved_default(self, installed, upstream) -> None:
        bin_dir, home = installed
        self._register_three(bin_dir, home, upstream)
        before = self._listed(bin_dir, home)

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        after = self._listed(bin_dir, home)
        assert [entry["name"] for entry in after["deployments"]] == [entry["name"] for entry in before["deployments"]]
        # The stable ids matter more than the names: an id change would orphan the
        # per-deployment store holding that deployment's session.
        assert [entry["deployment_id"] for entry in after["deployments"]] == [entry["deployment_id"] for entry in before["deployments"]]
        assert after["default"] == "development"
        assert "9.9.9-from-gateway" in (bin_dir / "adp").read_text()

    def test_the_updated_cli_still_routes_each_deployment_to_its_own_url(self, installed, upstream) -> None:
        """Records surviving is not the same as selection still working.

        Selected through the environment rather than `--deployment`, because
        `adp deployment` is one of the verbs exempt from resolution — it MANAGES
        deployments, so a selection flag would be ambiguous about which of the two
        names it meant, and it refuses one by design. The environment variable is
        the surface a second terminal actually uses.
        """
        bin_dir, home = installed
        self._register_three(bin_dir, home, upstream)

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        expected = {
            "development": upstream.url,
            "integration": "https://integration.example.invalid/api",
            "preprod": "https://preprod.example.invalid/api",
        }
        for name, url in expected.items():
            result = _run_adp(bin_dir, home, ["deployment", "list", "--json"], deployment=name)
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["effective"] == name
            selected = next(entry for entry in payload["deployments"] if entry["name"] == name)
            assert selected["gateway_url"] == url

    def test_updating_one_deployment_never_rewrites_another_deployments_store(self, installed, upstream) -> None:
        """`adp --deployment X update` must not touch the legacy store's binding.

        install.sh persists the gateway URL it installed from, and it resolved that
        store as a hardcoded `~/.bedrock-gateway` while `adp update` ran it as a
        child of a command pinned to a DIFFERENT deployment. The result was the
        legacy store's `gateway_url` rewritten to the selected deployment's URL
        with the legacy refresh token still sitting beside it — so the next refresh
        sent one deployment's credential to another deployment's gateway.
        """
        bin_dir, home = installed
        legacy_config = home / ".bedrock-gateway" / "config.json"
        # `_register_three` binds the legacy store to its own url and asserts that
        # none of the three is an alias of it, which is what makes the crossing
        # under test possible at all.
        self._register_three(bin_dir, home, upstream)
        before = json.loads(legacy_config.read_text())
        assert before["gateway_url"] == "https://legacy.example.invalid/api"

        # `development` is the deployment that actually serves the CLI, so this is
        # an update that genuinely succeeds while pinned away from legacy.
        result = _run_adp(bin_dir, home, ["update"], deployment="development")

        assert result.returncode == 0, result.stderr
        assert json.loads(legacy_config.read_text())["gateway_url"] == before["gateway_url"], (
            "an update pinned to another deployment must not rebind the legacy store"
        )

    def test_rollback_restores_the_executables_and_not_the_registry(self, installed, upstream) -> None:
        """A deployment added after an update must not vanish when it is undone.

        The executables and the registry have different lifetimes: one is the
        software, the other is the user's own state. Rolling the first back to
        recover from a bad release must not roll the second back to a moment
        before a deployment the user registered — they would have to notice the
        absence and re-add it, on a CLI they are already rolling back.
        """
        bin_dir, home = installed
        self._register_three(bin_dir, home, upstream)
        assert _run_adp(bin_dir, home, ["update"]).returncode == 0
        added = _run_adp(bin_dir, home, ["deployment", "add", "sandbox", "--url", "https://sandbox.example.invalid/api"])
        assert added.returncode == 0, added.stderr

        result = _run_adp(bin_dir, home, ["update", "--rollback"])

        assert result.returncode == 0, result.stderr
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        names = [entry["name"] for entry in self._listed(bin_dir, home)["deployments"]]
        # `default` is the adopted legacy record: this fixture has a
        # `~/.bedrock-gateway` store, so the first mutating command registers it
        # alongside the named ones (AC-07). It belongs in the assertion rather
        # than being filtered out — a rollback that dropped the legacy record
        # would log out the pre-#5413 session, which is the worst version of this
        # bug and the one a set of only named deployments could not see.
        assert set(names) == {"default", "development", "integration", "preprod", "sandbox"}

    def test_a_failed_update_leaves_every_record_untouched(self, installed, upstream) -> None:
        """The abort path must be as safe for the registry as for the executables."""
        bin_dir, home = installed
        self._register_three(bin_dir, home, upstream)
        before = self._listed(bin_dir, home)
        upstream.fail_paths.add("/api/cli/adp_deployments.py")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert "adp_deployments.py" in result.stderr
        assert self._listed(bin_dir, home) == before

    def test_the_deployment_resolver_is_part_of_the_installed_set(self, installed, upstream) -> None:
        """Sibling resolution, not PATH: a missing resolver is a broken install.

        `adp` runs `adp_deployments.py` from its own directory, so an update that
        refreshed every other file would leave the new front door calling an old
        resolver — or none at all. Asserting it is in the fetched set keeps
        `install.sh`'s CLI_FILES and the gateway's download route in step with what
        the front door expects to find beside it.
        """
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        assert "/api/cli/adp_deployments.py" in upstream.requested
        assert (bin_dir / "adp_deployments.py").is_file()

    def test_update_works_when_the_default_deployment_was_never_signed_in_to(self, installed, upstream) -> None:
        """A registered-but-unauthenticated default must not block a CLI update.

        The defect this closes: `adp update` read the gateway URL only from the
        SELECTED deployment's `config.json`, which sign-in writes — so a user who
        ran `adp deployment add preprod` and `adp deployment use preprod` before
        logging in got "No gateway URL ... cannot tell where to update from",
        naming a path inside `~/.adp/deployments/<id>/` they had never seen, while
        the registry had held that deployment's URL since the moment they added it.

        Updating the software is not a per-deployment operation — the CLI is
        installed once — so the registered URL is a legitimate answer to "where do
        I update from", and the registry always has one.
        """
        bin_dir, home = installed
        # The fixture's legacy store points at the SAME url the mock gateway serves.
        # Leaving it there made `development` an ALIAS of the adopted legacy record —
        # one id, one store, `~/.bedrock-gateway` — which already holds a config.json
        # from the fixture. The "never signed in to" premise was then false, and the
        # store path asserted below (`~/.adp/deployments/default/`) was one the CLI
        # never writes, so the assertion held under every behaviour. Bind legacy to
        # its own url so `development` is a genuinely separate, session-less
        # deployment and the registry fallback is actually what makes update work.
        legacy_config = home / ".bedrock-gateway" / "config.json"
        legacy_config.write_text(json.dumps({"gateway_url": "https://legacy.example.invalid/api", "client_id": "abc123"}))
        added = _run_adp(bin_dir, home, ["deployment", "add", "development", "--url", upstream.url])
        assert added.returncode == 0, added.stderr
        assert _run_adp(bin_dir, home, ["deployment", "use", "development"]).returncode == 0
        listed = self._listed(bin_dir, home)
        identifier = next(entry["deployment_id"] for entry in listed["deployments"] if entry["name"] == "development")
        assert identifier != "default", "the fixture must not make 'development' an alias of the legacy store"
        store = home / ".adp" / "deployments" / identifier
        assert store.is_dir(), "add must create the deployment's own store directory"
        assert not (store / "config.json").exists(), "add must write no session"
        assert not next(entry["signed_in"] for entry in listed["deployments"] if entry["name"] == "development")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode == 0, result.stderr
        assert "9.9.9-from-gateway" in (bin_dir / "adp").read_text()
