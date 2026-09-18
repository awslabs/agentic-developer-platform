"""The `adp` front door's deployment selection (Issue #5413).

`test_adp_deployments.py` covers the resolver in isolation. This suite covers the
part that can only be observed by running the REAL installed `adp` script: that it
resolves once at entry and pins the result, that the pin actually reaches the auth
helper's token store, and — the behaviour an existing user cares about most — that
a machine which never registered a named deployment behaves exactly as it did
before this change.

The assertions are chosen around the failure that motivated the issue: a command
using one deployment's endpoint with another deployment's session. That cannot be
caught by checking a message, so these tests check WHICH STORE the helper reads
and writes, and that three stores stay independent.

Each test gets a sandboxed HOME, so nothing here can touch a developer's own
session or AWS credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .conftest import write_adp_session

DEV_URL = "https://dev.gw.example.test"
INT_URL = "https://integration.gw.example.test"
PREPROD_URL = "https://preprod.gw.example.test"


@pytest.fixture
def adp(adp_bin: Path, adp_home: Path):
    """Run the installed `adp` with a sandboxed HOME and no inherited selection."""

    def _run(args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(adp_home)
        for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "BG_CONFIG_DIR", "ADP_HOME", "BG_AWS_PROFILE"):
            env.pop(leaked, None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(adp_bin / "adp"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    return _run


@pytest.fixture
def resolved(adp_bin: Path, adp_home: Path):
    """Ask the real resolver where a deployment's files live.

    Tests assert on the STORE, not on printed text: "did this command touch the
    right token file" is the question that distinguishes working isolation from a
    reassuring message.
    """

    def _resolved(name: str | None = None, fmt: str = "json"):
        args = ["resolve"] + (["--deployment", name] if name else []) + ["--format", fmt]
        probe = subprocess.run(
            ["python3", str(adp_bin / "adp_deployments.py"), *args],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(adp_home), "ADP_HOME": str(adp_home / ".adp")},
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        return json.loads(probe.stdout) if fmt == "json" else probe.stdout

    return _resolved


@pytest.fixture
def seed_session():
    """Give one deployment a valid-looking session in its own store."""

    def _seed(config_dir: Path, url: str = DEV_URL) -> Path:
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({"gateway_url": url + "/api"}))
        (config_dir / "tokens.json").write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 9999999999}))
        return config_dir

    return _seed


@pytest.fixture
def three_deployments(adp):
    """dev, integration and preprod registered, with dev as the saved default."""
    for name, url in (("dev", DEV_URL), ("integration", INT_URL), ("preprod", PREPROD_URL)):
        result = adp(["deployment", "add", name, "--url", url])
        assert result.returncode == 0, result.stderr
    assert adp(["deployment", "use", "dev"]).returncode == 0
    return adp


class TestDeploymentVerb:
    def test_list_on_a_fresh_machine_explains_how_to_start(self, adp) -> None:
        result = adp(["deployment", "list"])

        assert result.returncode == 0, result.stderr
        assert "deployment add" in result.stdout

    def test_add_then_list_shows_the_deployment(self, adp) -> None:
        assert adp(["deployment", "add", "dev", "--url", DEV_URL]).returncode == 0

        listed = adp(["deployment", "list"])

        assert "dev" in listed.stdout
        assert DEV_URL + "/api" in listed.stdout

    def test_add_requires_a_url(self, adp) -> None:
        result = adp(["deployment", "add", "dev"])

        assert result.returncode != 0
        assert result.stdout.strip() == "", "a failure must not print a success line"

    def test_an_unknown_deployment_subcommand_is_refused(self, adp) -> None:
        result = adp(["deployment", "frobnicate"])

        assert result.returncode == 1
        assert "frobnicate" in result.stderr

    def test_deployment_verb_works_when_the_selection_is_broken(self, adp) -> None:
        """The command you reach for to FIX a bad selection must not need a good one."""
        assert adp(["deployment", "add", "dev", "--url", DEV_URL]).returncode == 0

        result = adp(["deployment", "list"], {"ADP_DEPLOYMENT": "does-not-exist"})

        assert result.returncode == 0, result.stderr
        assert "dev" in result.stdout

    def test_deployment_verb_rejects_a_selection_flag(self, adp) -> None:
        """`adp --deployment x deployment add y` is a confused instruction."""
        result = adp(["--deployment", "dev", "deployment", "list"])

        assert result.returncode == 1
        assert "no --deployment" in result.stderr.lower() or "takes no" in result.stderr.lower()


class TestSelectionFlag:
    def test_the_flag_needs_a_name(self, adp) -> None:
        result = adp(["--deployment"])

        assert result.returncode == 1
        assert "--deployment" in result.stderr

    def test_an_empty_flag_value_is_refused(self, adp) -> None:
        result = adp(["--deployment=", "status"])

        assert result.returncode == 1

    def test_an_unknown_name_fails_and_does_not_fall_back(self, three_deployments) -> None:
        result = three_deployments(["--deployment", "staging", "status"])

        assert result.returncode == 1
        assert "staging" in result.stderr
        assert "Signed in" not in result.stdout, "it must not report on some other deployment"

    def test_status_names_the_selected_deployment_and_why(self, three_deployments) -> None:
        result = three_deployments(["--deployment", "preprod", "status"])

        assert "preprod" in result.stdout
        assert "flag" in result.stdout

    def test_the_flag_beats_the_environment_variable(self, three_deployments) -> None:
        result = three_deployments(["--deployment", "preprod", "status"], {"ADP_DEPLOYMENT": "integration"})

        assert "preprod" in result.stdout
        assert "integration" not in result.stdout

    def test_the_environment_variable_beats_the_saved_default(self, three_deployments) -> None:
        result = three_deployments(["status"], {"ADP_DEPLOYMENT": "integration"})

        assert "integration" in result.stdout

    def test_the_saved_default_is_used_when_nothing_else_selects(self, three_deployments) -> None:
        result = three_deployments(["status"])

        assert "dev" in result.stdout

    def test_the_flag_must_precede_the_verb(self, three_deployments) -> None:
        """After the verb it belongs to the tool — `adp codex --deployment` is Codex's."""
        result = three_deployments(["status", "--deployment", "preprod"])

        assert "preprod" not in result.stdout, "a trailing flag must not select a deployment"


class TestStoreIsolation:
    """The assertions that actually prevent a token reaching the wrong gateway."""

    def test_each_deployment_gets_its_own_store(self, three_deployments, resolved) -> None:
        stores = {name: resolved(name)["config_dir"] for name in ("dev", "integration", "preprod")}

        assert len(set(stores.values())) == 3, f"three deployments must not share a store: {stores}"

    def test_signing_in_to_one_leaves_the_others_signed_out(self, three_deployments, resolved, seed_session) -> None:
        """A session is per-deployment: seeding dev must not sign integration in."""
        seed_session(Path(resolved("dev")["config_dir"]))

        assert three_deployments(["--deployment", "dev", "status"]).returncode == 0
        assert three_deployments(["--deployment", "integration", "status"]).returncode == 1, "integration has no session of its own and must say so"

    def test_logout_signs_out_only_the_selected_deployment(self, three_deployments, resolved, seed_session) -> None:
        """`adp logout` is a pass-through to the core helper; it must hit ONE store."""
        dev = seed_session(Path(resolved("dev")["config_dir"]))
        integration = seed_session(Path(resolved("integration")["config_dir"]), INT_URL)

        three_deployments(["--deployment", "dev", "logout"])

        assert not (dev / "tokens.json").exists(), "logout must clear the SELECTED deployment"
        assert (integration / "tokens.json").exists(), "logout must not sign other deployments out"

    def test_each_deployment_writes_a_distinct_aws_profile(self, three_deployments, resolved) -> None:
        """A fixed profile name would make three logins overwrite one another."""
        profiles = set()
        for name in ("dev", "integration", "preprod"):
            line = next(item for item in resolved(name, "env").splitlines() if "BG_AWS_PROFILE" in item)
            profiles.add(line.split("=", 1)[1])

        assert len(profiles) == 3, f"AWS profiles must not collide: {profiles}"


class TestLegacyMachineUnchanged:
    """A user who never registers a deployment must not notice this change."""

    @pytest.fixture
    def legacy(self, adp, adp_home):
        # Created the way install.sh does — plain mkdir, so 0755 under a normal
        # umask. Adoption must tolerate that rather than demand 0700.
        (adp_home / ".bedrock-gateway").mkdir(parents=True, exist_ok=True)
        write_adp_session(adp_home, username="github_alice")
        return adp

    def test_status_works_and_never_mentions_deployments(self, legacy) -> None:
        result = legacy(["status"])

        assert result.returncode == 0, result.stderr
        assert "github_alice" in result.stdout
        assert "Deployment:" not in result.stdout, "naming a concept the user never met is a regression"

    def test_the_existing_store_is_used_in_place(self, legacy, adp_home) -> None:
        assert legacy(["status"]).returncode == 0

        copies = [path for path in adp_home.rglob("tokens.json")]
        assert copies == [adp_home / ".bedrock-gateway" / "tokens.json"], f"the session must stay in exactly one file, found {copies}"

    def test_listing_shows_it_without_registering_anything(self, legacy, adp_home) -> None:
        result = legacy(["deployment", "list"])

        assert result.returncode == 0, result.stderr
        assert "default" in result.stdout
        assert not (adp_home / ".adp" / "deployments.json").exists(), "a read-only command must not write"

    def test_a_0755_legacy_store_is_not_rejected(self, legacy, adp_home) -> None:
        """install.sh has always created it 0755; failing on that breaks everyone."""
        (adp_home / ".bedrock-gateway").chmod(0o755)

        result = legacy(["status"])

        assert result.returncode == 0, f"a pre-existing 0755 store must still work: {result.stderr}"

    def test_the_legacy_aws_profile_name_is_preserved(self, legacy, resolved) -> None:
        """Existing AWS_PROFILE=bedrock-gateway setups must keep working."""
        assert "export BG_AWS_PROFILE=bedrock-gateway\n" in resolved(None, "env")


class TestHelpSurface:
    def test_help_documents_the_deployment_commands(self, adp) -> None:
        result = adp(["help"])

        assert "deployment add" in result.stdout
        assert "--deployment" in result.stdout

    def test_help_states_that_an_unknown_name_does_not_fall_back(self, adp) -> None:
        """The safety property is the one users need told, not just implemented."""
        result = adp(["help"])

        assert "never quietly falls back" in result.stdout

    def test_help_needs_no_deployment(self, adp) -> None:
        result = adp(["help"], {"ADP_DEPLOYMENT": "does-not-exist"})

        assert result.returncode == 0, "help must work when the selection is broken"
