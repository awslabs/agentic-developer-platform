"""The python CLI half honours the selected deployment (Issue #5413).

`adp_common` is where every python helper gets the gateway URL, the token and its
private state directory. Before this change all three were hardcoded to
`~/.bedrock-gateway` and `~/.adp/state`, so three terminals on one machine shared
one session no matter which deployment they named.

The tests here are about ISOLATION and PINNING, which are the two ways this can
fail dangerously:

* isolation — helper state, config and tokens for deployment A must be
  unreachable from a process pinned to deployment B. A leak here is one
  deployment reading another's session.
* pinning — a process resolves ONCE. `gateway_url()` and `access_token()` are
  called at different moments of the same command, and if the second re-read the
  saved default, a concurrent `adp deployment use` could combine one
  deployment's endpoint with another's credential. That is the bug the whole
  story exists to prevent, so it is asserted directly rather than assumed.

The legacy machine is tested just as hard: an existing user with no registry must
keep reading and writing exactly the paths they already have.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location("adp_deployments_under_test", CLI / "adp_deployments.py")
deployments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deployments)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A sandboxed machine with nothing selected and no leaked selection.

    `adp_common` caches its resolution in a module global, which is the point of
    the design — so it MUST be reset between tests or the first test's answer
    silently becomes every later test's answer and the suite stops testing
    anything.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("ADP_HOME", str(home / ".adp"))
    for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT_NAME", "ADP_DEPLOYMENT_SOURCE", "BG_CONFIG_DIR"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
    return home


@pytest.fixture
def registered(isolated):
    """Register two deployments and return a selector for either of them."""
    deployments.add("dev", "https://dev.example.com")
    deployments.add("integration", "https://integration.example.com")

    def select(name, monkeypatch):
        monkeypatch.setenv("ADP_DEPLOYMENT", name)
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
        return deployments.resolve(name)

    return select


class TestSelectedDeploymentDrivesEveryPath:
    def test_config_and_state_follow_the_selection(self, registered, monkeypatch):
        dev = registered("dev", monkeypatch)

        assert common.config_path() == dev.config_dir / "config.json"
        assert common.state_dir() == dev.state_dir

    def test_a_second_deployment_gets_entirely_different_paths(self, registered, monkeypatch):
        registered("dev", monkeypatch)
        dev_config, dev_state = common.config_path(), common.state_dir()

        registered("integration", monkeypatch)
        integration_config, integration_state = common.config_path(), common.state_dir()

        assert dev_config != integration_config
        assert dev_state != integration_state
        # Not merely different strings: neither may be inside the other, or a
        # traversal or a shared parent would reunite them.
        assert dev_state not in integration_state.parents
        assert integration_state not in dev_state.parents

    def test_state_written_under_one_deployment_is_invisible_to_the_other(self, registered, monkeypatch):
        registered("dev", monkeypatch)
        common.write_state("github", {"installation": "dev-only"})

        registered("integration", monkeypatch)

        assert common.read_state("github") == {}, "integration read dev's helper state"

    def test_the_gateway_url_comes_from_the_selected_binding(self, registered, monkeypatch):
        registered("dev", monkeypatch)
        assert common.gateway_url() == "https://dev.example.com/api"

        registered("integration", monkeypatch)
        assert common.gateway_url() == "https://integration.example.com/api"

    def test_a_session_saved_for_one_deployment_lands_only_in_its_own_store(self, registered, monkeypatch):
        dev = registered("dev", monkeypatch)
        integration = deployments.resolve("integration")
        common.write_json(common.config_path(), {"gateway_url": "https://dev.example.com/api"})

        common.save_session(
            {
                "access_token": "access-value",
                "id_token": "id-value",
                "refresh_token": "refresh-value",
                "expires_in": 3600,
                "client_id": "client",
                "user_pool_id": "pool",
                "region": "us-east-1",
            }
        )

        assert (dev.config_dir / "tokens.json").is_file()
        assert not (integration.config_dir / "tokens.json").exists(), "a login leaked into the other deployment's store"


class TestPinnedForTheWholeCommand:
    def test_resolution_happens_once_even_across_many_calls(self, registered, monkeypatch):
        registered("dev", monkeypatch)
        calls = []
        real_resolve = deployments.resolve

        def counting_resolve(*args, **kwargs):
            calls.append(args)
            return real_resolve(*args, **kwargs)

        monkeypatch.setattr(deployments, "resolve", counting_resolve)
        monkeypatch.setattr(common, "load_provider", lambda name: deployments)
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)

        common.config_path()
        common.state_dir()
        common.gateway_url()

        assert len(calls) == 1, f"re-resolved mid-command ({len(calls)} times) — the drift window this design closes"

    def test_changing_the_saved_default_mid_command_does_not_move_the_target(self, registered, monkeypatch):
        """The concurrency case: another terminal runs `deployment use` while this
        command is between reading the endpoint and fetching the token."""
        registered("dev", monkeypatch)
        before = common.gateway_url()

        deployments.use("integration")  # as another terminal would

        assert common.config_path() == deployments.resolve("dev").config_dir / "config.json"
        assert common.gateway_url() == before

    def test_the_token_helper_is_handed_this_processs_deployment(self, registered, monkeypatch):
        dev = registered("dev", monkeypatch)
        captured = {}

        class Result:
            stdout = "token-value\n"

        def fake_run(argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            return Result()

        monkeypatch.setattr(common.subprocess, "run", fake_run)

        assert common.access_token() == "token-value"
        # Passed explicitly, so the child never resolves again — see the docstring.
        assert captured["ADP_DEPLOYMENT_ID"] == dev.id
        assert captured["BG_CONFIG_DIR"] == str(dev.config_dir)
        assert captured["BG_AWS_PROFILE"] == dev.aws_profile
        # The rest of the environment must survive; dropping PATH would leave the
        # helper unable to find `aws`.
        assert captured.get("PATH") == os.environ.get("PATH")


class TestUnhonourableSelectionsFail:
    def test_an_unknown_named_selection_is_an_error_not_a_fallback(self, registered, monkeypatch):
        monkeypatch.setenv("ADP_DEPLOYMENT", "typo")
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)

        with pytest.raises(common.CliError) as failure:
            common.config_path()

        assert failure.value.code == "deployment_not_found"

    def test_an_unregistered_inherited_pin_is_an_error(self, registered, monkeypatch):
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", "dnot-a-real-id")
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)

        with pytest.raises(common.CliError):
            common.config_path()

    def test_a_missing_helper_file_degrades_to_the_legacy_paths(self, isolated, monkeypatch):
        """A partial install must stay repairable: `adp login` and `adp update` are
        how the user fixes it, and both go through these paths."""
        monkeypatch.setattr(common, "load_provider", lambda name: None)
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)

        assert common.config_path() == isolated / ".bedrock-gateway" / "config.json"


class TestLegacyMachineUnchanged:
    def test_paths_are_exactly_what_they_were_before_this_change(self, isolated):
        (isolated / ".bedrock-gateway").mkdir()
        (isolated / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": "https://legacy.example.com/api"}))

        assert common.config_path() == isolated / ".bedrock-gateway" / "config.json"
        assert common.state_dir() == isolated / ".adp" / "state"
        assert common.gateway_url() == "https://legacy.example.com/api"

    def test_a_machine_with_no_store_at_all_still_reports_a_clear_error(self, isolated):
        with pytest.raises(common.CliError) as failure:
            common.gateway_url()

        assert failure.value.code == "gateway_not_configured"

    def test_reading_paths_registers_nothing_and_creates_no_new_directories(self, isolated):
        (isolated / ".bedrock-gateway").mkdir()
        (isolated / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": "https://legacy.example.com/api"}))

        common.config_path()
        common.gateway_url()

        assert not (isolated / ".adp" / "deployments.json").exists(), "a read adopted the legacy store"
        assert not (isolated / ".adp" / "deployments").exists()
