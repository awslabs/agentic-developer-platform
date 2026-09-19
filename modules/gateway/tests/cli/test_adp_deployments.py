"""The deployment registry, storage layout and selection rule (Issue #5413).

The CLI used to hold exactly one gateway URL and one session at fixed paths, so a
developer working against development, integration and pre-production could not use
three terminals safely: changing which environment was "current" in one could leave
another combining an old endpoint with a different environment's token.

`adp_deployments` is the single implementation of that decision — the bash front
door shells out to it, the python helpers import it — and these tests pin the
behaviours where being wrong means a CREDENTIAL REACHES THE WRONG GATEWAY, not
merely that a message is misleading:

* precedence is flag > inherited pin > environment > saved default > legacy, and an
  unknown explicit selection FAILS rather than falling back;
* two names for one canonical URL are aliases sharing one session, never a second
  copy of a rotating refresh token;
* the same name with a different URL is refused, so a name is never silently
  rebound under a process that already resolved it;
* the legacy single-deployment store is adopted IN PLACE, so an existing user is
  not asked to log in again and their refresh token stays in one file;
* the filesystem authority is a random id, so a removed-then-re-added name gets a
  different directory and an old process cannot attach to a new target;
* an inherited context whose id and storage path disagree is rejected outright.

Every test redirects ADP_HOME and BG_CONFIG_DIR into tmp_path, so nothing here can
read or write the developer's own session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
MODULE_PATH = CLI_DIR / "adp_deployments.py"

_spec = importlib.util.spec_from_file_location("adp_deployments_under_test", MODULE_PATH)
deployments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deployments)

DeploymentError = deployments.DeploymentError

DEV_URL = "https://dev.gw.example.test"
INT_URL = "https://integration.gw.example.test"
PREPROD_URL = "https://preprod.gw.example.test"


@pytest.mark.parametrize(
    "arguments",
    [
        ["-c", "model_reasoning_effort='low'"],
        ["exec", "--config", 'model_providers."adp-gateway".http_headers={"X-Request-ID"="fixture"}'],
        ["-c", "'model_providers'.'other'.base_url='https://other.example.test'"],
        ["--", "-c", "model_provider='literal prompt'"],
    ],
)
def test_codex_non_transport_options_are_preserved(arguments):
    before = list(arguments)
    deployments.check_codex_args(arguments)
    assert arguments == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["-c"],
        ["-c", "model_provider"],
        ["-c", r'"model\u00zz"=1'],
        ["-c", r'"model\U00110000"=1'],
        ["-c", r'"model\q"=1'],
    ],
)
def test_codex_invalid_key_syntax_fails_closed(arguments):
    with pytest.raises(DeploymentError) as error:
        deployments.check_codex_args(arguments)
    assert error.value.code == "invalid_arguments"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect every path this module derives, including the legacy store."""
    adp_home = tmp_path / "adp-home"
    legacy = tmp_path / "legacy-bedrock-gateway"
    monkeypatch.setenv("ADP_HOME", str(adp_home))
    monkeypatch.setenv("BG_CONFIG_DIR", str(legacy))
    for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT_NAME", "ADP_DEPLOYMENT_SOURCE"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "unused-home"))
    return adp_home


@pytest.fixture
def legacy_store(tmp_path):
    """A pre-#5413 store: config + tokens at the old fixed paths."""

    def _make(url: str = "https://legacy.gw.example.test/api", *, tokens: bool = True):
        directory = Path(os.environ["BG_CONFIG_DIR"])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "config.json").write_text(json.dumps({"gateway_url": url, "client_id": "legacy-client"}))
        if tokens:
            (directory / "tokens.json").write_text(json.dumps({"refresh_token": "legacy-refresh-token-fixture"}))
        return directory

    return _make


def run_module(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Drive the module as the bash front door does — a real subprocess."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


class TestNameAndUrlValidation:
    """Invalid input must be refused locally, before anything is registered."""

    @pytest.mark.parametrize(
        "name",
        ["", "Dev", "1dev", "dev/prod", "../escape", "dev prod", "dev\n", "dev.prod", "-dev", "a" * 64],
    )
    def test_rejects_unsafe_names(self, name: str) -> None:
        with pytest.raises(DeploymentError) as excinfo:
            deployments.validate_name(name)
        assert excinfo.value.exit_code == 1, "an invalid name is a usage error, not an operation failure"

    @pytest.mark.parametrize("name", ["dev", "d", "integration", "pre-prod", "pre_prod", "a" * 63])
    def test_accepts_ordinary_names(self, name: str) -> None:
        assert deployments.validate_name(name) == name

    @pytest.mark.parametrize(
        "url",
        [
            "http://dev.gw.example.test",  # plaintext to a remote host
            "https://user:pass@dev.gw.example.test",  # embedded credentials
            "https://dev.gw.example.test?token=x",  # query string
            "https://dev.gw.example.test#frag",
            "ftp://dev.gw.example.test",
            "file:///etc/passwd",
            "not-a-url",
            "",
        ],
    )
    def test_rejects_unsafe_urls(self, url: str) -> None:
        with pytest.raises(DeploymentError) as excinfo:
            deployments.canonical_url(url)
        assert excinfo.value.exit_code == 1

    @pytest.mark.parametrize(
        "given",
        ["https://gw.example.test", "https://gw.example.test/", "https://gw.example.test/api", "https://gw.example.test/api/"],
    )
    def test_four_spellings_are_one_deployment(self, given: str) -> None:
        """Alias detection depends on this: four spellings must canonicalize to one."""
        assert deployments.canonical_url(given) == "https://gw.example.test/api"

    def test_preserves_a_legitimate_base_path(self) -> None:
        assert deployments.canonical_url("https://gw.example.test/adp") == "https://gw.example.test/adp/api"

    def test_loopback_http_is_allowed_for_recording_gateways(self) -> None:
        """The deterministic tests point the real CLI at a real local server."""
        assert deployments.canonical_url("http://127.0.0.1:8181") == "http://127.0.0.1:8181/api"


class TestAdd:
    def test_first_deployment_becomes_the_default(self) -> None:
        detail = deployments.add("dev", DEV_URL)

        assert detail["deployment"] == "dev"
        assert detail["default"] == "dev", "a single-deployment user must never have to run `deployment use`"
        assert detail["gateway_url"] == DEV_URL + "/api"

    def test_second_deployment_does_not_steal_the_default(self) -> None:
        deployments.add("dev", DEV_URL)
        detail = deployments.add("integration", INT_URL)

        assert detail["default"] == "dev"

    def test_same_name_same_url_is_idempotent(self) -> None:
        deployments.add("dev", DEV_URL)
        first = deployments.listing()

        detail = deployments.add("dev", DEV_URL + "/api/")  # a different spelling of the same URL

        assert detail["status"] == "unchanged"
        assert deployments.listing() == first, "re-running add must change nothing"

    def test_same_name_different_url_is_refused(self) -> None:
        deployments.add("dev", DEV_URL)

        with pytest.raises(DeploymentError) as excinfo:
            deployments.add("dev", INT_URL)

        assert excinfo.value.code == "deployment_conflict"
        assert deployments.listing()["deployments"][0]["gateway_url"] == DEV_URL + "/api", "the binding must be intact"

    def test_a_second_name_for_one_url_shares_the_session(self) -> None:
        """An alias is one store. Copying a rotating refresh token kills one copy."""
        first = deployments.add("dev", DEV_URL)
        second = deployments.add("development", DEV_URL)

        assert second["deployment_id"] == first["deployment_id"]
        assert second["alias_of"] == "dev"
        assert deployments.resolve("dev").config_dir == deployments.resolve("development").config_dir

    def test_distinct_urls_get_distinct_stores(self) -> None:
        dev = deployments.add("dev", DEV_URL)
        integration = deployments.add("integration", INT_URL)

        assert dev["deployment_id"] != integration["deployment_id"]
        assert deployments.resolve("dev").config_dir != deployments.resolve("integration").config_dir

    def test_add_writes_no_token_and_makes_no_request(self, isolated_home: Path) -> None:
        deployments.add("dev", DEV_URL)

        tokens = list(isolated_home.rglob("tokens.json"))
        assert tokens == [], "registration is local metadata only"

    def test_private_directories_are_created_0700(self, isolated_home: Path) -> None:
        deployments.add("dev", DEV_URL)
        resolved = deployments.resolve("dev")

        for directory in (resolved.config_dir, resolved.state_dir, resolved.runtime_dir, resolved.log_dir):
            assert directory.is_dir()
            assert directory.stat().st_mode & 0o077 == 0, f"{directory} must not be group/world accessible"


class TestSelectionPrecedence:
    @pytest.fixture(autouse=True)
    def three(self):
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        deployments.add("preprod", PREPROD_URL)
        deployments.use("dev")

    def test_saved_default_is_used_when_nothing_else_selects(self) -> None:
        resolved = deployments.resolve()

        assert (resolved.name, resolved.selection_source) == ("dev", "default")

    def test_environment_variable_beats_the_saved_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ADP_DEPLOYMENT", "integration")

        resolved = deployments.resolve()

        assert (resolved.name, resolved.selection_source) == ("integration", "environment")

    def test_explicit_flag_beats_the_environment_variable(self, monkeypatch) -> None:
        """`adp --deployment preprod ...` must win in a terminal that exported dev."""
        monkeypatch.setenv("ADP_DEPLOYMENT", "integration")

        resolved = deployments.resolve("preprod")

        assert (resolved.name, resolved.selection_source) == ("preprod", "flag")

    def test_empty_environment_variable_is_treated_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("ADP_DEPLOYMENT", "   ")

        assert deployments.resolve().name == "dev"

    def test_unknown_explicit_selection_fails_and_never_falls_back(self) -> None:
        with pytest.raises(DeploymentError) as excinfo:
            deployments.resolve("staging")

        assert excinfo.value.code == "deployment_not_found"
        assert excinfo.value.exit_code == 1
        assert "staging" in str(excinfo.value)

    def test_unknown_environment_selection_fails_and_never_falls_back(self, monkeypatch) -> None:
        """Silently using `dev` here would send integration's work to development."""
        monkeypatch.setenv("ADP_DEPLOYMENT", "staging")

        with pytest.raises(DeploymentError) as excinfo:
            deployments.resolve()

        assert excinfo.value.code == "deployment_not_found"

    def test_changing_the_default_does_not_move_a_pinned_context(self, monkeypatch) -> None:
        """The whole point: another terminal's `use` cannot redirect this command."""
        pinned = deployments.resolve("preprod")
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", pinned.id)

        deployments.use("integration")

        assert deployments.resolve().name == "preprod"
        assert deployments.resolve().config_dir == pinned.config_dir

    def test_inherited_pin_beats_a_child_environment_variable(self, monkeypatch) -> None:
        """A child process must not be able to re-select mid-command."""
        pinned = deployments.resolve("preprod")
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", pinned.id)
        monkeypatch.setenv("ADP_DEPLOYMENT", "dev")

        assert deployments.resolve().name == "preprod"

    def test_inherited_id_with_a_foreign_storage_path_is_rejected(self, monkeypatch) -> None:
        """An inherited BG_CONFIG_DIR must never aim the helper at another store."""
        preprod = deployments.resolve("preprod")
        integration = deployments.resolve("integration")
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", preprod.id)
        monkeypatch.setenv("BG_CONFIG_DIR", str(integration.config_dir))

        with pytest.raises(DeploymentError) as excinfo:
            deployments.resolve()

        assert excinfo.value.code == "deployment_mismatch"

    def test_inherited_id_for_a_removed_deployment_fails(self, monkeypatch) -> None:
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", "dnot-registered")

        with pytest.raises(DeploymentError) as excinfo:
            deployments.resolve()

        assert excinfo.value.code == "deployment_not_found"

    def test_no_deployment_at_all_gives_an_actionable_instruction(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("ADP_HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path / "no-legacy"))

        with pytest.raises(DeploymentError) as excinfo:
            deployments.resolve()

        assert excinfo.value.code == "deployment_not_found"
        assert "deployment add" in str(excinfo.value)


class TestUse:
    def test_use_changes_only_the_saved_default(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        detail = deployments.use("integration")

        assert detail["default"] == "integration"
        assert deployments.listing()["deployments"][0]["gateway_url"] == DEV_URL + "/api", "bindings are untouched"

    def test_use_reports_when_the_terminal_still_overrides_it(self, monkeypatch) -> None:
        """ "default is now X" while this terminal uses Y is true but misleading."""
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        monkeypatch.setenv("ADP_DEPLOYMENT", "dev")

        detail = deployments.use("integration")

        assert detail["default"] == "integration"
        assert detail["effective_override"] == "dev"

    def test_use_of_an_unknown_name_fails(self) -> None:
        deployments.add("dev", DEV_URL)

        with pytest.raises(DeploymentError) as excinfo:
            deployments.use("staging")

        assert excinfo.value.exit_code == 1

    def test_use_edits_no_shell_rc(self, tmp_path, isolated_home: Path) -> None:
        rc = tmp_path / "unused-home" / ".bashrc"
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text("# untouched\n")
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        deployments.use("integration")

        assert rc.read_text() == "# untouched\n"


class TestRemove:
    def test_aliases_share_one_profile_and_removal_retires_old_credentials(self):
        import configparser

        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        deployments.add("int", INT_URL)
        selected = deployments.resolve("integration")
        assert deployments.resolve("int").aws_profile == selected.aws_profile
        for profile in (selected.aws_profile, "bedrock-gateway-integration", "bedrock-gateway-int", "unrelated"):
            deployments.update_aws_profile("write", profile, "us-east-1", ("fixture-access", "fixture-secret", "fixture-session"))
        deployments.remove("int")
        assert deployments.resolve("integration").aws_profile == selected.aws_profile
        deployments.remove("integration")
        for filename, prefix in (("credentials", ""), ("config", "profile ")):
            parsed = configparser.RawConfigParser()
            parsed.read(Path.home() / ".aws" / filename)
            assert parsed.sections() == [prefix + "unrelated"]

    def test_registry_publication_failure_preserves_the_entire_store(self, monkeypatch) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        store = deployments.resolve("integration").root
        (store / "tokens.json").write_text('"session-fixture"')
        (store / "state" / "handoff.json").write_text('"pending-fixture"')
        before = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}

        def fail(_registry):
            raise OSError("simulated registry publication failure")

        monkeypatch.setattr(deployments, "save_registry", fail)
        with pytest.raises(OSError, match="publication failure"):
            deployments.remove("integration")

        assert "integration" in deployments.load_registry()["deployments"]
        assert {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()} == before

    def test_cleanup_is_after_publication_and_outside_registry_lock(self, monkeypatch) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        store = deployments.resolve("integration").root

        def fail_cleanup(path):
            assert path == store
            assert "integration" not in deployments.load_registry()["deployments"]
            assert not (deployments.adp_home() / "registry.lock").exists()
            raise OSError("simulated cleanup failure")

        monkeypatch.setattr(deployments.shutil, "rmtree", fail_cleanup)
        with pytest.raises(DeploymentError, match="registration was removed.*cleanup"):
            deployments.remove("integration")
        assert store.exists()

    def test_remove_forgets_one_deployment_only(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        deployments.remove("integration")

        names = [entry["name"] for entry in deployments.listing()["deployments"]]
        assert names == ["dev"]

    def test_remove_refuses_the_saved_default(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        with pytest.raises(DeploymentError) as excinfo:
            deployments.remove("dev")

        assert excinfo.value.code == "deployment_busy"
        assert "deployment use integration" in str(excinfo.value), "must name the fix"

    def test_remove_refuses_a_deployment_whose_proxy_is_running(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        selected = deployments.resolve("integration").ensure_directories()
        runtime = selected.runtime_dir
        (runtime / "proxy.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": 9999,
                    "process_start": deployments._process_start(os.getpid()),
                    "proxy": "adp-gateway-proxy",
                    "deployment_id": selected.id,
                    "gateway_url": selected.gateway_url,
                }
            )
        )

        with pytest.raises(DeploymentError) as excinfo:
            deployments.remove("integration")

        assert excinfo.value.code == "deployment_busy"

    @pytest.mark.parametrize("identity", [False, True])
    def test_unrelated_live_pid_does_not_block_removal(self, identity):
        import subprocess

        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        selected = deployments.resolve("integration").ensure_directories()
        process = subprocess.Popen(["sleep", "60"])
        try:
            (selected.runtime_dir / "proxy.pid").write_text(str(process.pid))
            if identity:
                (selected.runtime_dir / "proxy.json").write_text(
                    json.dumps(
                        {
                            "pid": process.pid,
                            "port": 9999,
                            "process_start": "old-process-start",
                            "proxy": "adp-gateway-proxy",
                            "deployment_id": selected.id,
                            "gateway_url": selected.gateway_url,
                        }
                    )
                )
            deployments.remove("integration")
            assert process.poll() is None
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_a_stale_pidfile_does_not_block_removal(self) -> None:
        """A killed session leaves a pidfile behind; that is not active use."""
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        runtime = deployments.resolve("integration").ensure_directories().runtime_dir
        (runtime / "proxy.json").write_text(json.dumps({"pid": 2**30, "port": 9999}))

        assert deployments.remove("integration")["deployment"] == "integration"

    def test_removal_is_reported_as_removal_not_as_registration(self) -> None:
        """The human line is keyed off the returned status, whose default is
        "configured" — so omitting it made a successful removal print
        "Deployment 'integration' is registered for ." and then invite the user to
        sign in to the deployment they had just removed."""
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        result = deployments.remove("integration")

        assert result["status"] == "removed"
        assert result["gateway_url"] == INT_URL + "/api", "the report must name what was removed"

    def test_removing_an_alias_keeps_the_shared_store(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("development", DEV_URL)
        deployments.add("integration", INT_URL)
        deployments.use("integration")

        result = deployments.remove("development")

        assert result["aliases_remaining"] is True
        assert result["removed_store"] is False
        assert deployments.resolve("dev").config_dir.is_dir(), "the shared session must survive"

    def test_remove_signs_no_one_else_out(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        dev_tokens = deployments.resolve("dev").ensure_directories().config_dir / "tokens.json"
        dev_tokens.write_text(json.dumps({"refresh_token": "dev-session-fixture"}))

        deployments.remove("integration")

        assert dev_tokens.is_file()

    def test_readding_a_removed_name_for_a_new_url_gets_a_new_store(self) -> None:
        """So a process still holding the old path cannot write into the new target."""
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)
        old_id = deployments.resolve("integration").id
        deployments.remove("integration")

        deployments.add("integration", PREPROD_URL)

        assert deployments.resolve("integration").id != old_id


class TestLegacyAdoption:
    def test_an_existing_single_deployment_user_keeps_working(self, legacy_store) -> None:
        legacy_store("https://legacy.gw.example.test")

        resolved = deployments.resolve()

        assert resolved.name == "default"
        assert resolved.selection_source == "legacy"
        assert resolved.gateway_url == "https://legacy.gw.example.test/api"

    def test_adoption_points_at_the_existing_paths_and_copies_no_token(self, legacy_store, isolated_home: Path) -> None:
        directory = legacy_store()

        resolved = deployments.resolve()

        assert resolved.config_dir == directory, "the store is adopted in place, not migrated"
        copies = [path for path in isolated_home.rglob("tokens.json")]
        assert copies == [], "a rotating refresh token must live in exactly one file"

    def test_listing_a_legacy_store_writes_nothing(self, legacy_store, isolated_home: Path) -> None:
        """The command a confused user runs first must be safe."""
        legacy_store()

        data = deployments.listing()

        assert [entry["name"] for entry in data["deployments"]] == ["default"]
        assert not deployments.registry_path().exists(), "a read-only command must not register anything"

    def test_the_first_mutating_command_registers_the_legacy_record(self, legacy_store) -> None:
        legacy_store()

        deployments.add("integration", INT_URL)

        registry = json.loads(deployments.registry_path().read_text())
        assert set(registry["deployments"]) == {"default", "integration"}
        assert registry["default"] == "default", "adding a deployment must not steal an existing user's default"

    def test_adoption_is_idempotent(self, legacy_store) -> None:
        legacy_store()
        deployments.add("integration", INT_URL)
        first = deployments.registry_path().read_text()

        deployments.add("preprod", PREPROD_URL)
        deployments.remove("preprod")

        assert json.loads(deployments.registry_path().read_text())["deployments"]["default"] == json.loads(first)["deployments"]["default"]

    def test_the_legacy_store_is_never_deleted(self, legacy_store) -> None:
        directory = legacy_store()
        deployments.add("integration", INT_URL)
        deployments.use("integration")

        result = deployments.remove("default")

        assert result["removed_store"] is False
        assert (directory / "tokens.json").is_file(), "removing the name must not destroy the session"

    def test_named_deployments_do_not_use_the_legacy_store(self, legacy_store) -> None:
        legacy_store()
        deployments.add("integration", INT_URL)

        assert deployments.resolve("integration").config_dir != deployments.resolve("default").config_dir

    def test_no_legacy_store_means_no_implicit_deployment(self) -> None:
        with pytest.raises(DeploymentError):
            deployments.resolve()


class TestCorruptState:
    def test_a_corrupt_registry_fails_visibly_and_is_not_overwritten(self, isolated_home: Path) -> None:
        deployments.add("dev", DEV_URL)
        original = "{ not json"
        deployments.registry_path().write_text(original)

        with pytest.raises(DeploymentError) as excinfo:
            deployments.load_registry()

        assert excinfo.value.code == "deployment_state_unreadable"
        assert deployments.registry_path().read_text() == original

    def test_a_future_schema_version_is_refused_rather_than_guessed(self) -> None:
        deployments.add("dev", DEV_URL)
        registry = json.loads(deployments.registry_path().read_text())
        registry["schema_version"] = 99
        deployments.registry_path().write_text(json.dumps(registry))

        with pytest.raises(DeploymentError) as excinfo:
            deployments.load_registry()

        assert excinfo.value.code == "deployment_schema_unsupported"
        assert "adp update" in str(excinfo.value)

    def test_an_incomplete_record_is_refused(self) -> None:
        deployments.add("dev", DEV_URL)
        registry = json.loads(deployments.registry_path().read_text())
        del registry["deployments"]["dev"]["gateway_url"]
        deployments.registry_path().write_text(json.dumps(registry))

        with pytest.raises(DeploymentError) as excinfo:
            deployments.load_registry()

        assert excinfo.value.code == "deployment_state_unreadable"

    def test_a_corrupt_legacy_config_does_not_crash_listing(self, legacy_store) -> None:
        directory = legacy_store()
        (directory / "config.json").write_text("{ truncated")

        data = deployments.listing()

        assert [entry["name"] for entry in data["deployments"]] == ["default"]
        assert data["deployments"][0]["gateway_url"] == "", "an unreadable URL is reported as unknown, not guessed"


class TestPinnedEnvironment:
    def test_the_pin_carries_id_name_source_and_every_derived_path(self) -> None:
        deployments.add("dev", DEV_URL)
        resolved = deployments.resolve("dev")

        environment = resolved.environment()

        assert environment["ADP_DEPLOYMENT_ID"] == resolved.id
        assert environment["ADP_DEPLOYMENT_NAME"] == "dev"
        assert environment["ADP_DEPLOYMENT_SOURCE"] == "flag"
        assert environment["BG_CONFIG_DIR"] == str(resolved.config_dir)
        assert environment["ADP_STATE_DIR"] == str(resolved.state_dir)
        assert environment["ADP_RUNTIME_DIR"] == str(resolved.runtime_dir)

    def test_describe_carries_no_secret(self) -> None:
        deployments.add("dev", DEV_URL)
        legacy_tokens = deployments.resolve("dev").ensure_directories().config_dir / "tokens.json"
        legacy_tokens.write_text(json.dumps({"refresh_token": "secret-material-fixture"}))

        rendered = json.dumps(deployments.resolve("dev").describe(include_paths=True))

        assert "secret-material-fixture" not in rendered


class TestCommandLineSurface:
    """The bash front door and the tests both drive this as a real subprocess."""

    def test_resolve_env_emits_shell_exports(self) -> None:
        deployments.add("dev", DEV_URL)

        result = run_module(["resolve", "--deployment", "dev", "--format", "env"])

        assert result.returncode == 0, result.stderr
        assert "export ADP_DEPLOYMENT_NAME=dev" in result.stdout
        assert "export BG_CONFIG_DIR=" in result.stdout

    def test_resolve_of_an_unknown_name_exits_1_without_the_network(self) -> None:
        deployments.add("dev", DEV_URL)

        result = run_module(["resolve", "--deployment", "staging"])

        assert result.returncode == 1
        assert "staging" in result.stderr

    def test_json_output_is_json_only_on_stdout(self) -> None:
        result = run_module(["--json", "add", "dev", "--url", DEV_URL])

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "configured"
        assert payload["detail"]["deployment"] == "dev"

    def test_json_error_output_carries_a_stable_code(self) -> None:
        deployments.add("dev", DEV_URL)

        result = run_module(["--json", "add", "dev", "--url", INT_URL])

        assert result.returncode == 1, "a refused rebinding is a usage error the caller can correct"
        assert json.loads(result.stdout)["error"]["code"] == "deployment_conflict"

    def test_list_marks_the_default_and_the_effective_selection(self) -> None:
        deployments.add("dev", DEV_URL)
        deployments.add("integration", INT_URL)

        result = run_module(["list"], env={"ADP_DEPLOYMENT": "integration"})

        assert result.returncode == 0, result.stderr
        assert "Selected: integration (from environment)" in result.stdout

    def test_list_on_a_fresh_machine_says_how_to_add_one(self) -> None:
        result = run_module(["list"])

        assert result.returncode == 0
        assert "deployment add" in result.stdout

    def test_list_json_exposes_no_token_material(self) -> None:
        deployments.add("dev", DEV_URL)
        store = deployments.resolve("dev").ensure_directories().config_dir
        (store / "tokens.json").write_text(json.dumps({"refresh_token": "listing-secret-fixture"}))

        result = run_module(["--json", "list"])

        assert "listing-secret-fixture" not in result.stdout
        assert json.loads(result.stdout)["deployments"][0]["signed_in"] is True


class TestConcurrentRegistryWrites:
    def test_parallel_adds_all_land(self) -> None:
        """Registry writes serialize briefly; none may be lost to a lost update."""
        names = ["dev", "integration", "preprod", "sandbox", "scratch"]
        processes = [
            subprocess.Popen(
                [sys.executable, str(MODULE_PATH), "--json", "add", name, "--url", f"https://{name}.gw.example.test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )
            for name in names
        ]
        for process in processes:
            assert process.wait(timeout=60) == 0, process.stderr.read()

        registered = {entry["name"] for entry in deployments.listing()["deployments"]}
        assert registered == set(names)
