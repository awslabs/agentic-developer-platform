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
import shutil
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
    @pytest.mark.parametrize("mode", [0o755, 0o777])
    def test_default_install_root_allows_reads_but_not_foreign_writes(self, adp, adp_home, adp_bin, mode) -> None:
        # The shipped installer creates ~/.adp/bin with mkdir -p under the
        # user's umask. That normally leaves the non-secret parent at 0755.
        root = adp_home / ".adp"
        shutil.copytree(adp_bin, root / "bin")
        root.chmod(mode)

        result = adp(["deployment", "add", "dev", "--url", DEV_URL])

        if mode == 0o777:
            assert result.returncode != 0
            assert not (root / "deployments.json").exists()
            return
        assert result.returncode == 0, result.stderr
        registry = root / "deployments.json"
        record = json.loads(registry.read_text())["deployments"]["dev"]
        assert registry.stat().st_mode & 0o777 == 0o600
        assert (root / "deployments" / record["id"]).stat().st_mode & 0o777 == 0o700

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

    @pytest.mark.parametrize("args", [["deployment", "list", "--json"], ["deployment", "--json", "list"]])
    def test_json_is_accepted_on_either_side_of_the_verb(self, three_deployments, adp, args) -> None:
        """Every other `adp` command takes its flags after the subcommand.

        Found by driving the real CLI the way the multi-deployment EC2 journey
        does: `adp deployment list --json` — the position a user and a script both
        reach for first — exited 2 with an argparse usage dump, because `--json`
        was only declared before the verb. A machine-readable surface that fails
        on the natural word order is unusable for exactly the automation it exists
        to serve, so both positions must work and must agree.
        """
        result = adp(args)

        assert result.returncode == 0, f"`adp {' '.join(args)}` failed: {result.stderr}"
        payload = json.loads(result.stdout)
        assert [entry["name"] for entry in payload["deployments"]] == ["dev", "integration", "preprod"]

    def test_json_output_carries_no_prose(self, three_deployments, adp) -> None:
        """A caller parsing this must not have to strip a human-readable banner."""
        result = adp(["deployment", "list", "--json"])

        assert result.stdout.count("\n") == 1, f"expected exactly one JSON line, got: {result.stdout!r}"

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

    @pytest.mark.parametrize("session", ["absent", "valid", "expired"])
    def test_status_json_reports_only_the_selected_session(self, three_deployments, resolved, seed_session, session) -> None:
        store = Path(resolved("preprod")["config_dir"])
        if session != "absent":
            seed_session(store, PREPROD_URL)
            tokens = {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_at": 9999999999 if session == "valid" else 1}
            (store / "tokens.json").write_text(json.dumps(tokens))
        before = {path.name: path.read_bytes() for path in store.glob("*.json")}

        result = three_deployments(["--deployment", "preprod", "status", "--json"], {"ADP_DEPLOYMENT": "integration"})

        assert result.returncode == (1 if session == "absent" else 0), result.stderr
        document = json.loads(result.stdout)
        assert document["command"] == "status"
        assert document["status"] == ("unavailable" if session == "absent" else "configured")
        assert document["detail"]["deployment"] == "preprod"
        assert document["detail"]["gateway_url"] == PREPROD_URL + "/api"
        assert document["detail"]["selection_source"] == "flag"
        assert document["detail"]["signed_in"] == (session != "absent")
        assert document["detail"]["access_token_state"] == session
        assert "access-secret" not in result.stdout and "refresh-secret" not in result.stdout
        assert {path.name: path.read_bytes() for path in store.glob("*.json")} == before

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

    @pytest.mark.parametrize("mode", [0o755, 0o777])
    def test_alias_adopts_readable_legacy_store_but_refuses_foreign_writes(self, legacy, adp_home, mode) -> None:
        store = adp_home / ".bedrock-gateway"
        store.chmod(mode)
        original = (store / "tokens.json").read_bytes()
        result = legacy(["deployment", "add", "dev", "--url", "https://gw.example.com"])

        if mode == 0o777:
            assert result.returncode != 0
            assert (store / "tokens.json").read_bytes() == original
            return
        assert result.returncode == 0, result.stderr
        assert legacy(["--deployment", "dev", "status"]).returncode == 0
        assert (store / "tokens.json").read_bytes() == original
        assert list(adp_home.rglob("tokens.json")) == [store / "tokens.json"]
        assert store.stat().st_mode & 0o777 == 0o755

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


class TestAMisplacedSelectionFlagIsRefused:
    """`--deployment` after the verb must fail, not silently use another target.

    The flag is global and parsed before the verb, so a later one was accepted and
    dropped: `adp token --deployment prod` printed the DEFAULT deployment's access
    token and exited 0. Nothing in that output tells the user their credential came
    from a deployment they did not name, which makes it the same silent
    substitution an unknown name is already refused for.
    """

    @pytest.fixture
    def legacy_plus_prod(self, adp, adp_home):
        write_adp_session(adp_home, username="github_alice")
        assert adp(["deployment", "add", "prod", "--url", PREPROD_URL]).returncode == 0
        return adp

    @pytest.mark.parametrize("verb", ["token", "status", "logout", "refresh"])
    def test_a_post_verb_selection_fails_instead_of_using_another_deployment(self, legacy_plus_prod, verb) -> None:
        result = legacy_plus_prod([verb, "--deployment", "prod"])

        assert result.returncode != 0, f"'adp {verb} --deployment prod' must not silently use the default"
        assert "BEFORE the verb" in result.stderr

    def test_the_refusal_never_emits_a_token(self, legacy_plus_prod) -> None:
        """The concrete harm: a credential on stdout that a script would consume."""
        result = legacy_plus_prod(["token", "--deployment", "prod"])

        assert result.stdout.strip() == "", "a refused command must print no credential"

    def test_the_equals_form_is_refused_too(self, legacy_plus_prod) -> None:
        result = legacy_plus_prod(["token", "--deployment=prod"])

        assert result.returncode != 0
        assert result.stdout.strip() == ""

    def test_the_correct_pre_verb_form_still_selects_that_deployment(self, legacy_plus_prod) -> None:
        result = legacy_plus_prod(["--deployment", "prod", "status"])

        assert "prod" in result.stdout, "the documented form must keep working"

    def test_a_tool_launcher_still_passes_its_flags_through(self, legacy_plus_prod) -> None:
        """codex/claude own every token after the verb; stealing one breaks them."""
        result = legacy_plus_prod(["claude", "--deployment", "prod"])

        assert "BEFORE the verb" not in result.stderr, "passthrough args must reach the tool untouched"


class TestABrokenRegistryIsNeverIgnored:
    """An unreadable registry must fail, not fall back to the legacy store.

    The registry is what binds every name to a URL. When it cannot be read there
    is no evidence about where the saved default points, so continuing on the
    legacy paths runs the command against the legacy gateway while the user
    believes their saved default is in force.
    """

    @pytest.fixture
    def broken_registry(self, adp, adp_home):
        write_adp_session(adp_home, username="github_alice")
        registry = adp_home / ".adp"
        registry.mkdir(parents=True, exist_ok=True)
        return adp, registry / "deployments.json"

    def test_an_unreadable_registry_stops_the_command(self, broken_registry) -> None:
        adp, path = broken_registry
        path.write_text("{not json at all")

        result = adp(["status"])

        assert result.returncode != 0, "a corrupt registry must not resolve to the legacy gateway"

    def test_a_newer_schema_stops_the_command_and_says_so(self, broken_registry) -> None:
        """The documented forward-compatibility path must not silently downgrade."""
        adp, path = broken_registry
        path.write_text(json.dumps({"schema_version": 99, "default": "prod", "deployments": {"prod": {"id": "d1", "gateway_url": PREPROD_URL}}}))

        result = adp(["status"])

        assert result.returncode != 0
        assert "adp update" in result.stderr

    def test_a_machine_with_no_registry_still_uses_the_legacy_store(self, adp, adp_home) -> None:
        """Absent is not broken: an existing single-deployment user is untouched."""
        write_adp_session(adp_home, username="github_alice")

        result = adp(["status"])

        assert result.returncode == 0, result.stderr
        assert "github_alice" in result.stdout


class TestALoginCannotBindANameToAnotherGateway:
    """A named deployment's registered URL is its identity, not a default.

    The destination for every request is read from the REGISTRY, while the bearer
    comes from the deployment's own store. So a sign-in that went to a different
    gateway under this name left the store holding a token minted by gateway B
    while all traffic went to gateway A — presenting one deployment's live
    credential to another deployment's gateway. Both halves look internally
    consistent, so nothing downstream can notice.
    """

    @pytest.fixture
    def prod(self, adp):
        assert adp(["deployment", "add", "prod", "--url", PREPROD_URL]).returncode == 0
        return adp

    def test_signing_in_to_a_different_gateway_under_a_name_is_refused(self, prod) -> None:
        result = prod(["--deployment", "prod", "login", "--gateway-url", DEV_URL])

        assert result.returncode != 0, "a login aimed at another gateway must not proceed"
        assert "is registered for" in result.stderr
        assert "Starting web sign-in" not in result.stderr, "it must refuse BEFORE contacting anyone"

    def test_the_refusal_names_both_urls_and_how_to_proceed(self, prod) -> None:
        """A user who genuinely re-pointed a deployment needs the way forward."""
        result = prod(["--deployment", "prod", "login", "--gateway-url", DEV_URL])

        assert PREPROD_URL + "/api" in result.stderr
        assert DEV_URL + "/api" in result.stderr
        assert "deployment add prod" in result.stderr

    @pytest.mark.parametrize("form", [PREPROD_URL, PREPROD_URL + "/", PREPROD_URL + "/api", PREPROD_URL + "/api/"])
    def test_every_spelling_of_the_registered_url_is_accepted(self, prod, form) -> None:
        """Alias detection treats these as ONE gateway; so must this check.

        A string comparison would reject a correct URL over a trailing slash while
        still missing a genuinely different host.
        """
        result = prod(["--deployment", "prod", "login", "--gateway-url", form])

        assert "is registered for" not in result.stderr, f"{form} is the registered gateway and must be accepted"

    def test_a_registered_deployment_needs_no_url_on_its_first_login(self, prod) -> None:
        """`deployment add` records the URL and makes no request, so there is no
        config.json until the first sign-in. Demanding a hand-typed URL there is
        what produced the mismatched logins in the first place."""
        result = prod(["--deployment", "prod", "login"])

        output = result.stdout + result.stderr
        assert "No gateway URL known" not in output
        assert PREPROD_URL + "/api" in output, "it must use the URL the registry has held since `add`"

    def test_a_legacy_login_is_left_alone(self, adp, adp_home) -> None:
        """The legacy store, not a registry record, is that deployment's authority."""
        write_adp_session(adp_home, username="github_alice")

        result = adp(["login", "--gateway-url", DEV_URL])

        assert "is registered for" not in result.stderr, "a legacy machine has no registered binding to contradict"


class TestAnImportCannotBindANameToAnotherGateway:
    """`import` establishes a session too, so it gets login's identity check.

    `import` is the headless equivalent of `login`: it adopts an existing refresh
    token and persists the gateway URL it was given. It was dispatched straight
    through to the auth helper with no registry comparison, so
    `adp --deployment prod import --gateway-url <dev>` wrote dev's URL and dev's
    token into PROD's store. Every later prod command then read its destination
    from the registry (prod) and its bearer from the store (dev) — one
    deployment's live credential posted to another deployment's gateway, and a
    refresh rotated dev's refresh token while addressing prod.

    The guard lives in one shared function precisely so these two entry points
    cannot drift apart again.
    """

    @pytest.fixture
    def prod(self, adp):
        assert adp(["deployment", "add", "prod", "--url", PREPROD_URL]).returncode == 0
        return adp

    def test_importing_a_session_from_another_gateway_is_refused(self, prod) -> None:
        result = prod(["--deployment", "prod", "import", "--gateway-url", DEV_URL, "--refresh-token", "r", "--client-id", "c"])

        assert result.returncode != 0, "an import aimed at another gateway must not proceed"
        assert "is registered for" in result.stderr
        assert "Validating refresh token" not in result.stderr, "it must refuse BEFORE contacting Cognito"

    def test_the_refused_import_writes_nothing_to_the_store(self, prod, resolved, mock_aws_cli) -> None:
        """The harm is the persisted crossing, so assert on the store, not the message.

        Cognito is MOCKED here on purpose. Without it the import dies at
        `initiate-auth` and the store stays clean either way — the test would pass
        against the unfixed code and prove nothing. With a working Cognito the only
        thing that can keep dev's token out of prod's store is the refusal.
        """
        result = prod(
            ["--deployment", "prod", "import", "--gateway-url", DEV_URL, "--refresh-token", "r", "--client-id", "c"],
            {"PATH": f"{mock_aws_cli}:{os.environ['PATH']}", "MOCK_COGNITO_RESULT": "ok"},
        )

        assert result.returncode != 0, "the import must be refused, not merely fail to persist"
        store = Path(resolved("prod")["config_dir"])
        assert not (store / "tokens.json").exists(), "a refused import must not leave another gateway's token here"
        if (store / "config.json").exists():
            assert DEV_URL not in (store / "config.json").read_text(), "prod's store must never record dev's URL"

    @pytest.mark.parametrize("form", [PREPROD_URL, PREPROD_URL + "/", PREPROD_URL + "/api"])
    def test_every_spelling_of_the_registered_url_is_accepted(self, prod, form) -> None:
        """A trailing slash is the same gateway; only a different host is a crossing."""
        result = prod(["--deployment", "prod", "import", "--gateway-url", form, "--refresh-token", "r", "--client-id", "c"])

        assert "is registered for" not in result.stderr, f"{form} is the registered gateway and must be accepted"

    def test_a_legacy_import_is_left_alone(self, adp, adp_home) -> None:
        """The legacy store is its own authority, with no registered binding to contradict."""
        write_adp_session(adp_home, username="github_alice")

        result = adp(["import", "--gateway-url", DEV_URL, "--refresh-token", "r", "--client-id", "c"])

        assert "is registered for" not in result.stderr


class TestAnInheritedStoreCannotBeAimedAtTheLegacyName:
    """Only the legacy deployment reads its store location from the environment.

    Every named deployment derives its path from its stable id, so it simply
    overrides whatever `BG_CONFIG_DIR` it inherited — selecting a named deployment
    from inside another deployment's session is legitimate and must keep working.
    The legacy record is the exception: its store IS `BG_CONFIG_DIR`. So inside
    `adp --deployment dev claude` (which exports dev's store), a nested
    `adp --deployment default <verb>` resolved 'default' onto DEV's directory —
    printing dev's URL, handing out dev's token, and letting `logout` destroy dev's
    session while the real legacy store sat untouched.

    _reject_crossed_context could not catch this: a legacy record's config_dir is
    derived FROM BG_CONFIG_DIR, so it was comparing the inherited path with itself.
    """

    @pytest.fixture
    def legacy_and_dev(self, adp, adp_home, resolved, seed_session):
        write_adp_session(adp_home, username="github_alice")
        assert adp(["deployment", "add", "dev", "--url", DEV_URL]).returncode == 0
        seed_session(Path(resolved("dev")["config_dir"]), DEV_URL)
        return adp, Path(resolved("dev")["config_dir"])

    @pytest.mark.parametrize("route", ["flag", "environment", "pin"])
    def test_selecting_legacy_with_another_deployments_store_inherited_is_refused(self, legacy_and_dev, route) -> None:
        """All three selection routes, because the crossing is equally harmful on each."""
        adp, dev_store = legacy_and_dev
        env = {"BG_CONFIG_DIR": str(dev_store)}
        args = ["status"]
        if route == "flag":
            args = ["--deployment", "default", "status"]
        elif route == "environment":
            env["ADP_DEPLOYMENT"] = "default"
        else:
            env["ADP_DEPLOYMENT_ID"] = "default"

        result = adp(args, env)

        assert result.returncode != 0, f"a crossed context via {route} must not proceed"
        assert "mixed deployment context" in result.stderr

    def test_a_named_deployment_selected_from_another_session_still_works(self, legacy_and_dev, adp_home) -> None:
        """The guard must not break the ordinary reason BG_CONFIG_DIR is inherited."""
        adp, dev_store = legacy_and_dev
        assert adp(["deployment", "add", "preprod", "--url", PREPROD_URL]).returncode == 0

        result = adp(["--deployment", "preprod", "status"], {"BG_CONFIG_DIR": str(dev_store)})

        assert "mixed deployment context" not in result.stderr, "switching to a named deployment is legitimate"

    def test_the_legacy_store_itself_is_not_a_crossing(self, legacy_and_dev, adp_home) -> None:
        """A user who exports BG_CONFIG_DIR for the auth helper's own sake is fine."""
        adp, _ = legacy_and_dev

        result = adp(["--deployment", "default", "status"], {"BG_CONFIG_DIR": str(adp_home / ".bedrock-gateway")})

        assert "mixed deployment context" not in result.stderr


class TestLegacyAliasesRetainTheirRegisteredBinding:
    """Rebinding the original store must not silently rebind a named alias."""

    @pytest.fixture
    def aliased(self, adp, adp_home, resolved):
        write_adp_session(adp_home, username="github_alice")
        # `deployment add` MUTATES, so it adopts the legacy store, which requires a
        # private directory. Read-only resolution tolerates 0755 (the legacy
        # exemption); adoption does not.
        (adp_home / ".bedrock-gateway").chmod(0o700)
        from .conftest import ADP_GATEWAY_URL

        result = adp(["deployment", "add", "aaa", "--url", ADP_GATEWAY_URL])
        assert result.returncode == 0, result.stderr
        assert "another name for" in result.stdout, f"the fixture must create an ALIAS, not a second deployment: {result.stdout}"
        return adp

    def test_alias_keeps_its_url_and_refuses_rebound_credentials(self, aliased, adp_home, resolved) -> None:
        before = resolved("aaa")["gateway_url"]
        (adp_home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": PREPROD_URL + "/api"}))
        assert resolved("aaa")["gateway_url"] == before
        result = aliased(["--deployment", "aaa", "token"])
        assert result.returncode != 0
        assert result.stdout == ""
        assert "another gateway" in result.stderr

    def test_legacy_name_still_follows_original_store(self, aliased, adp_home, resolved) -> None:
        (adp_home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": PREPROD_URL + "/api"}))
        assert resolved("default")["gateway_url"] == PREPROD_URL + "/api"
        assert resolved("aaa")["config_dir"] == resolved("default")["config_dir"]


class TestTheLegacyUrlIsReadFromItsStoreNotASnapshot:
    """Adoption records the legacy URL, but the STORE stays the authority.

    The legacy deployment is adopted in place, and `adp login --gateway-url
    <other>` rewrites that store's URL and token together without touching the
    registry. Trusting the adoption-time snapshot made the two disagree, and since
    the destination comes from the registry and the bearer from the store, the
    command sent a token minted by the new gateway to the old one.
    """

    @pytest.fixture
    def adopted(self, adp, adp_home, resolved):
        write_adp_session(adp_home, username="github_alice")
        # A mutating command persists the registry, snapshotting the legacy URL.
        assert adp(["deployment", "add", "other", "--url", INT_URL]).returncode == 0
        assert (adp_home / ".adp" / "deployments.json").exists()
        return adp

    def test_a_rebound_legacy_store_moves_the_destination_with_it(self, adopted, adp_home, resolved) -> None:
        config = adp_home / ".bedrock-gateway" / "config.json"
        config.write_text(json.dumps({"gateway_url": PREPROD_URL + "/api"}))

        assert resolved()["gateway_url"] == PREPROD_URL + "/api", "the live store must win over the adoption-time snapshot"

    def test_the_registry_file_is_not_rewritten_to_agree(self, adopted, adp_home, resolved) -> None:
        """Refreshing is a read-time derivation; a status command must not write."""
        config = adp_home / ".bedrock-gateway" / "config.json"
        config.write_text(json.dumps({"gateway_url": PREPROD_URL + "/api"}))
        before = (adp_home / ".adp" / "deployments.json").read_text()

        assert resolved()["gateway_url"] == PREPROD_URL + "/api"
        assert (adp_home / ".adp" / "deployments.json").read_text() == before

    def test_an_unreadable_legacy_url_keeps_the_recorded_one(self, adopted, adp_home, resolved) -> None:
        """A truncated config must not blank the destination mid-session."""
        from .conftest import ADP_GATEWAY_URL

        (adp_home / ".bedrock-gateway" / "config.json").write_text("{truncated")

        assert resolved()["gateway_url"] == ADP_GATEWAY_URL


class TestResolverDiagnosticsAreNotExecuted:
    """Only the resolver's stdout may be eval'd by the front door.

    stderr used to be merged into the captured exports, so one warning line from a
    wrapped python3 on an OTHERWISE SUCCESSFUL resolve was concatenated with the
    exports and passed to `eval` — failing with "command not found". The resolve
    exited 0, so no status check could catch it.
    """

    def test_a_warning_on_stderr_does_not_break_the_command(self, adp, adp_home, tmp_path) -> None:
        write_adp_session(adp_home, username="github_alice")
        shim = tmp_path / "shim"
        shim.mkdir()
        real_python = shutil.which("python3")
        assert real_python, "the shim has to delegate to a real interpreter"
        (shim / "python3").write_text(f'#!/bin/bash\necho "WARNING: a warning" >&2\nexec {real_python} "$@"\n')
        (shim / "python3").chmod(0o755)

        result = adp(["status"], {"PATH": f"{shim}:{os.environ['PATH']}"})

        assert result.returncode == 0, f"a stderr warning must not be eval'd: {result.stderr}"
        assert "command not found" not in result.stderr
