# tests/cli/test_adp_setup.py
"""Tests for the `adp` wrapper's verbs — Issue #4852, Phase 1.

`adp` is a thin wrapper: every auth verb delegates to the sibling
`bg-cognito-auth.sh`, which has its own tests. So what is worth testing here is
the genuinely new behaviour, and specifically the two failure modes the issue
called out as costly:

- **`claude setup` must MERGE**, never overwrite. Clobbering a user's
  `~/.claude/settings.json` loses their permissions, hooks and MCP servers.
- **both `setup` verbs must be idempotent**, because the /setup page tells users
  to just re-run them, and because `adp update` re-running them must be a no-op.

Plus the one-login invariant: `claude setup` then `codex setup` share a single
session, so neither may trigger a sign-in. That is asserted by poisoning the
core script's `login` path and proving it is never reached.

These drive the real shell scripts against a sandboxed HOME, like the
`test_bg_cognito_auth_*` suites.
"""

import json
import shlex
from pathlib import Path

import pytest

from .conftest import ADP_GATEWAY_URL as GATEWAY_URL
from .conftest import write_adp_config as _write_config
from .conftest import write_adp_session as _write_session

# The exact provider block from the design (D3). Byte-for-byte, because Codex
# silently ignores a provider it cannot parse and the failure looks like an
# unrelated auth error.
EXPECTED_CODEX_BLOCK = """[model_providers.adp-gateway]
name = "adp-gateway"
base_url = "http://127.0.0.1:9191/openai/v1"
wire_api = "responses"
env_key = "ADP_GATEWAY_DUMMY"
"""


class TestStatus:
    """`status` is the day-1 confirmation and the day-N "am I still good?"."""

    def test_empty_store_says_not_signed_in_and_exits_non_zero(self, run_adp) -> None:
        result = run_adp(["status"])

        assert result.returncode != 0, "must be usable as a script-level check"
        assert "not signed in" in result.stdout.lower()

    def test_config_without_tokens_still_reports_not_signed_in(self, run_adp, adp_home: Path) -> None:
        """install.sh writes a config before any login — that is not a session."""
        _write_config(adp_home)

        result = run_adp(["status"])

        assert result.returncode != 0
        assert "not signed in" in result.stdout.lower()

    def test_reports_user_gateway_and_remaining_validity(self, run_adp, adp_home: Path) -> None:
        _write_session(adp_home, username="github_alice", ttl=3600)

        result = run_adp(["status"])

        assert result.returncode == 0, result.stderr
        assert "github_alice" in result.stdout
        assert GATEWAY_URL in result.stdout
        # ~60 minutes left; the exact minute is timing-dependent, the units are not.
        assert "minutes remaining" in result.stdout

    def test_reports_gateway_refresh_mode(self, run_adp, adp_home: Path) -> None:
        """`login --web` sets refresh_via=gateway (#4846); status surfaces it."""
        _write_session(adp_home)
        _write_config(adp_home, refresh_via="gateway")

        result = run_adp(["status"])

        assert result.returncode == 0
        assert "gateway" in result.stdout

    def test_expired_token_is_not_an_error(self, run_adp, adp_home: Path) -> None:
        """An expired ACCESS token is normal — it refreshes on next use."""
        _write_session(adp_home, ttl=-60)

        result = run_adp(["status"])

        assert result.returncode == 0
        assert "expired" in result.stdout.lower()


class TestCodexSetup:
    def test_refuses_without_a_session_and_writes_nothing(self, run_adp, adp_home: Path) -> None:
        result = run_adp(["codex", "setup"])

        assert result.returncode != 0
        assert "adp login" in result.stderr
        assert not (adp_home / ".codex" / "config.toml").exists()

    def test_writes_the_provider_block_pointed_at_the_local_proxy(self, run_adp, adp_home: Path) -> None:
        _write_session(adp_home)

        result = run_adp(["codex", "setup"])

        assert result.returncode == 0, result.stderr
        content = (adp_home / ".codex" / "config.toml").read_text()
        assert 'model_provider = "adp-gateway"' in content
        assert EXPECTED_CODEX_BLOCK in content

    def test_written_config_is_parseable_toml(self, run_adp, adp_home: Path) -> None:
        """A malformed config.toml is the documented blast radius for this verb."""
        import tomllib

        _write_session(adp_home)
        assert run_adp(["codex", "setup"]).returncode == 0

        parsed = tomllib.loads((adp_home / ".codex" / "config.toml").read_text())

        assert parsed["model_provider"] == "adp-gateway"
        assert parsed["model_providers"]["adp-gateway"]["base_url"] == "http://127.0.0.1:9191/openai/v1"

    def test_is_idempotent(self, run_adp, adp_home: Path) -> None:
        _write_session(adp_home)
        config = adp_home / ".codex" / "config.toml"

        assert run_adp(["codex", "setup"]).returncode == 0
        first = config.read_text()
        assert run_adp(["codex", "setup"]).returncode == 0

        assert config.read_text() == first, "second run must be a no-op diff"

    def test_preserves_an_existing_codex_config(self, run_adp, adp_home: Path) -> None:
        """Merge, not overwrite: another provider, MCP servers and unrelated
        top-level keys must all survive."""
        import tomllib

        _write_session(adp_home)
        codex_dir = adp_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "openai.gpt-5.6-sol"\n'
            'model_provider = "someone-elses-provider"\n'
            'approval_policy = "on-request"\n'
            "\n"
            "[model_providers.some-other]\n"
            'name = "keep me"\n'
            "\n"
            "[mcp_servers.foo]\n"
            'command = "bar"\n'
        )

        assert run_adp(["codex", "setup"]).returncode == 0

        parsed = tomllib.loads((codex_dir / "config.toml").read_text())
        assert parsed["model"] == "openai.gpt-5.6-sol"
        assert parsed["approval_policy"] == "on-request"
        assert parsed["model_providers"]["some-other"] == {"name": "keep me"}
        assert parsed["mcp_servers"] == {"foo": {"command": "bar"}}
        # …and ours replaced theirs rather than being appended alongside.
        assert parsed["model_provider"] == "adp-gateway"

    def test_model_provider_is_not_captured_by_a_preceding_table(self, run_adp, adp_home: Path) -> None:
        """In TOML a bare key after a [section] header belongs to that table. If
        `model_provider` were appended at the end of a file ending in
        [mcp_servers.foo], it would define mcp_servers.foo.model_provider and
        Codex would never see it — a silent misconfiguration."""
        import tomllib

        _write_session(adp_home)
        codex_dir = adp_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text('[mcp_servers.foo]\ncommand = "bar"\n')

        assert run_adp(["codex", "setup"]).returncode == 0

        parsed = tomllib.loads((codex_dir / "config.toml").read_text())
        assert parsed["model_provider"] == "adp-gateway"
        assert "model_provider" not in parsed["mcp_servers"]["foo"]

    def test_refuses_when_the_proxy_is_missing(self, run_adp, adp_home: Path, adp_bin: Path) -> None:
        """Codex cannot work without the proxy `adp serve` runs, so wiring the
        config while it is absent would produce a config that cannot connect."""
        _write_session(adp_home)
        (adp_bin / "bg-gateway-proxy.py").unlink()

        result = run_adp(["codex", "setup"])

        assert result.returncode != 0
        assert "bg-gateway-proxy.py" in result.stderr
        assert not (adp_home / ".codex" / "config.toml").exists()

    def test_prints_how_to_launch_codex(self, run_adp, adp_home: Path) -> None:
        """#4863 replaced the two-terminal `adp serve` hint with `adp codex`.

        The closing hint is the last thing a user reads before their first launch,
        so it must name the command that actually works in one step — and must not
        send them back to the dance the launcher exists to remove.
        """
        _write_session(adp_home)

        result = run_adp(["codex", "setup"])

        assert "adp codex" in result.stdout
        assert "ADP_GATEWAY_DUMMY=unused codex" not in result.stdout


class TestClaudeSetup:
    def test_refuses_without_a_session_and_writes_nothing(self, run_adp, adp_home: Path) -> None:
        result = run_adp(["claude", "setup"])

        assert result.returncode != 0
        assert "adp login" in result.stderr
        assert not (adp_home / ".claude" / "settings.json").exists()

    def test_sets_the_api_key_helper_to_an_absolute_adp_token_path(self, run_adp, adp_home: Path, adp_bin: Path) -> None:
        """Absolute, because Claude Code may invoke the helper from a non-login
        shell where the install dir is not on PATH."""
        _write_session(adp_home)

        assert run_adp(["claude", "setup"]).returncode == 0

        settings = json.loads((adp_home / ".claude" / "settings.json").read_text())
        assert settings["apiKeyHelper"].endswith(f"{adp_bin}/adp token")
        assert "ADP_DEPLOYMENT_ID=default" in settings["apiKeyHelper"]
        assert Path(shlex.split(settings["apiKeyHelper"])[-2]).is_absolute()
        assert settings["apiKeyHelperTtlMs"] == 3300000

    def test_sets_the_bedrock_env_the_setup_page_documents(self, run_adp, adp_home: Path) -> None:
        _write_session(adp_home)

        assert run_adp(["claude", "setup"]).returncode == 0

        env = json.loads((adp_home / ".claude" / "settings.json").read_text())["env"]
        assert env["ANTHROPIC_BEDROCK_BASE_URL"] == GATEWAY_URL
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
        assert env["AWS_REGION"] == "us-east-1"

    def test_merges_into_an_existing_settings_file(self, run_adp, adp_home: Path) -> None:
        """The documented worst case for this verb: a user's other Claude config
        must not be lost. Seed unrelated keys and assert every one survives."""
        _write_session(adp_home)
        claude_dir = adp_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["WebSearch", "WebFetch"]},
                    "hooks": {"Stop": [{"matcher": "x"}]},
                    "model": "global.anthropic.claude-opus-4-6-v1",
                    "env": {"MY_OWN_VAR": "keep-me"},
                }
            )
        )

        assert run_adp(["claude", "setup"]).returncode == 0

        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["permissions"] == {"allow": ["WebSearch", "WebFetch"]}
        assert settings["hooks"] == {"Stop": [{"matcher": "x"}]}
        assert settings["model"] == "global.anthropic.claude-opus-4-6-v1"
        # env is merged key-by-key, not replaced wholesale.
        assert settings["env"]["MY_OWN_VAR"] == "keep-me"
        assert settings["env"]["ANTHROPIC_BEDROCK_BASE_URL"] == GATEWAY_URL

    def test_keeps_a_users_own_aws_region(self, run_adp, adp_home: Path) -> None:
        """us-east-1 is a default, not an override — a user on another region
        has presumably set it deliberately."""
        _write_session(adp_home)
        claude_dir = adp_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"env": {"AWS_REGION": "eu-west-1"}}))

        assert run_adp(["claude", "setup"]).returncode == 0

        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["env"]["AWS_REGION"] == "eu-west-1"

    def test_is_idempotent(self, run_adp, adp_home: Path) -> None:
        _write_session(adp_home)
        settings_file = adp_home / ".claude" / "settings.json"

        assert run_adp(["claude", "setup"]).returncode == 0
        first = settings_file.read_text()
        assert run_adp(["claude", "setup"]).returncode == 0

        assert settings_file.read_text() == first

    def test_refuses_to_touch_malformed_json(self, run_adp, adp_home: Path) -> None:
        """Overwriting a file we cannot parse would destroy content the user may
        only have mistyped."""
        _write_session(adp_home)
        claude_dir = adp_home / ".claude"
        claude_dir.mkdir()
        broken = '{"permissions": {"allow": ["WebSearch"]'
        (claude_dir / "settings.json").write_text(broken)

        result = run_adp(["claude", "setup"])

        assert result.returncode != 0
        assert (claude_dir / "settings.json").read_text() == broken


class TestOneLoginIsSharedByBothTools:
    def test_setting_up_both_tools_never_triggers_a_login(self, run_adp, adp_home: Path, adp_bin: Path) -> None:
        """The corrected design model: exactly one auth verb. Poison the core
        script so ANY delegation to it fails loudly, then prove both `setup`
        verbs still succeed against the single seeded session."""
        _write_session(adp_home)
        core = adp_bin / "bg-cognito-auth.sh"
        core.write_text("#!/usr/bin/env bash\necho 'core must not be called by setup' >&2\nexit 99\n")
        core.chmod(0o755)

        claude_result = run_adp(["claude", "setup"])
        codex_result = run_adp(["codex", "setup"])

        assert claude_result.returncode == 0, claude_result.stderr
        assert codex_result.returncode == 0, codex_result.stderr
        assert "core must not be called" not in (claude_result.stderr + codex_result.stderr)
        # Both tools ended up pointed at the same session.
        assert (adp_home / ".claude" / "settings.json").exists()
        assert (adp_home / ".codex" / "config.toml").exists()


class TestDelegationToTheCore:
    """The wrapper adds no auth capability; it forwards. What matters is that it
    forwards the right verb with the right flags and does not mangle output."""

    @pytest.fixture
    def spy_core(self, adp_bin: Path, tmp_path: Path) -> Path:
        """Replace the core with a recorder that logs its argv and exits 0."""
        log = tmp_path / "core-argv.log"
        core = adp_bin / "bg-cognito-auth.sh"
        core.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nprintf "core-stdout"\n')
        core.chmod(0o755)
        return log

    @pytest.mark.parametrize("verb", ["logout", "token", "refresh", "import", "serve"])
    def test_passes_the_verb_straight_through(self, run_adp, spy_core: Path, verb: str) -> None:
        result = run_adp([verb])

        assert result.returncode == 0, result.stderr
        assert spy_core.read_text().strip() == verb

    def test_token_output_is_not_decorated(self, run_adp, spy_core: Path) -> None:
        """`adp token` is the Claude Code / proxy hot path — Claude Code uses
        stdout verbatim as the credential, so a stray newline or log line breaks
        every request."""
        result = run_adp(["token"])

        assert result.stdout == "core-stdout"

    def test_login_maps_to_the_web_flow_with_the_stored_gateway_url(self, run_adp, spy_core: Path, adp_home: Path) -> None:
        """There is one auth verb, and it is browser approval. The URL comes
        from the config install.sh wrote, so first login needs no flags."""
        _write_config(adp_home)

        result = run_adp(["login"])

        assert result.returncode == 0, result.stderr
        assert spy_core.read_text().strip() == f"login --web --gateway-url {GATEWAY_URL}"

    def test_login_forwards_no_browser(self, run_adp, spy_core: Path, adp_home: Path) -> None:
        _write_config(adp_home)

        assert run_adp(["login", "--no-browser"]).returncode == 0

        assert spy_core.read_text().strip().endswith("--no-browser")

    def test_explicit_gateway_url_wins_over_the_stored_one(self, run_adp, spy_core: Path, adp_home: Path) -> None:
        _write_config(adp_home)

        assert run_adp(["login", "--gateway-url", "https://other.example.com/api"]).returncode == 0

        assert "https://other.example.com/api" in spy_core.read_text()

    def test_login_without_any_known_url_says_how_to_fix_it(self, run_adp, spy_core: Path) -> None:
        result = run_adp(["login"])

        assert result.returncode != 0
        assert "--gateway-url" in result.stderr
        assert not spy_core.exists(), "must not invoke the core with an empty URL"

    def test_missing_core_script_is_an_actionable_error(self, run_adp, adp_bin: Path) -> None:
        (adp_bin / "bg-cognito-auth.sh").unlink()

        result = run_adp(["token"])

        assert result.returncode != 0
        assert "install.sh" in result.stderr


class TestHelpAndVersion:
    def test_help_lists_every_verb(self, run_adp) -> None:
        result = run_adp(["help"])

        assert result.returncode == 0
        for verb in ("login", "status", "logout", "token", "refresh", "import", "serve", "codex setup", "claude setup", "update"):
            assert verb in result.stdout, f"help omits {verb}"

    def test_help_shows_the_day_one_walkthrough(self, run_adp) -> None:
        result = run_adp(["help"])

        assert "install.sh" in result.stdout
        assert "adp login" in result.stdout

    def test_no_command_exits_non_zero_with_usage(self, run_adp) -> None:
        result = run_adp([])

        assert result.returncode != 0
        assert "Usage" in result.stdout

    def test_unknown_command_exits_non_zero(self, run_adp) -> None:
        result = run_adp(["definitely-not-a-verb"])

        assert result.returncode != 0

    def test_version_prints_a_version(self, run_adp) -> None:
        result = run_adp(["version"])

        assert result.returncode == 0
        assert result.stdout.startswith("adp ")


class TestUpdate:
    def test_rollback_restores_the_previous_copies(self, run_adp, adp_bin: Path) -> None:
        """The mitigation for "adp update pulled a broken CLI"."""
        (adp_bin / "adp.prev").write_text("#!/usr/bin/env bash\necho previous-version\n")

        result = run_adp(["update", "--rollback"])

        assert result.returncode == 0, result.stderr
        assert "previous-version" in (adp_bin / "adp").read_text()
        assert not (adp_bin / "adp.prev").exists()

    def test_rollback_without_a_previous_version_fails_clearly(self, run_adp) -> None:
        result = run_adp(["update", "--rollback"])

        assert result.returncode != 0
        assert "previous" in result.stderr.lower()

    def test_update_without_a_known_gateway_says_how_to_reinstall(self, run_adp) -> None:
        result = run_adp(["update"])

        assert result.returncode != 0
        assert "install.sh" in result.stderr
