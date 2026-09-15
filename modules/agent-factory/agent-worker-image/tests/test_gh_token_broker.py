"""Tests for the GitHub-token gatekeeper path in entrypoint.py (issue #4272).

The defect being closed: the platform GitHub App private key was read from the
tenant's Secrets Manager secret in-pod and exported to the agent subprocess as
GH_APP_PRIVATE_KEY on every run. A prompt-injected agent could read it out of its
own environment and mint installation tokens for any org that installed the App.

These tests assert OUTCOMES, not source text:
  * with the broker on, GH_APP_PRIVATE_KEY is absent from the env dict actually
    handed to subprocess.run
  * with the broker on, the vault read and the in-pod mint are never called at
    all — the dead path is provably dead, not merely unused
  * ADP_GH_TOKEN_BROKER_ENABLED is exported, because both TS initTokenManager
    call sites gate on "have a key OR broker mode"; without the flag they go
    false and the run silently dies at the 1-hour token expiry
  * a broker that cannot mint fails the run loudly — there is deliberately no
    in-pod fallback, since falling back would put the key back in pod memory
  * with the flag off, the old behavior is byte-for-byte unchanged
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.test_entrypoint import (
    SAMPLE_ENVELOPE,
    _subprocess_side_effect_fresh_branch,
)

_BROKERED_TOKEN = "ghs_brokered_from_gateway"
# The App ID is a public identifier, not a credential. In broker mode it comes
# back FROM the gatekeeper rather than from a vault read, because the run still
# needs it for the bot commit identity and for GH_APP_ID.
_BROKERED_APP_ID = "99001"
_BROKER_RESULT = (_BROKERED_TOKEN, _BROKERED_APP_ID, "2099-01-01T00:00:00Z")
_INSTALLATION_ID = SAMPLE_ENVELOPE["source_ref"]["installation_id"]
_REPO = SAMPLE_ENVELOPE["source_ref"]["repo"]


def _prepare(monkeypatch, tmp_path, entrypoint, *, broker: str | None):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(entrypoint, "BootstrapLogger", MagicMock())
    monkeypatch.setattr(entrypoint, "_start_sigv4_proxy", MagicMock())
    monkeypatch.setattr(entrypoint, "update_invocation_status", MagicMock())
    monkeypatch.setattr(entrypoint, "_write_outbound_correlation", MagicMock())
    if broker is None:
        monkeypatch.delenv("ADP_GH_TOKEN_BROKER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", broker)

    work_dir = tmp_path / "repo"
    work_dir.mkdir(parents=True)
    monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
    monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")


def _agent_env(mock_subprocess_run) -> dict:
    call = mock_subprocess_run.call_args
    return call.kwargs.get("env") or call[1].get("env")


class TestFlagParsing:
    """The kill-switch is off unless explicitly turned on."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "True"])
    def test_truthy_values_enable(self, value):
        from entrypoint import _gh_token_broker_enabled

        assert _gh_token_broker_enabled({"ADP_GH_TOKEN_BROKER_ENABLED": value}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_disables(self, value):
        from entrypoint import _gh_token_broker_enabled

        assert _gh_token_broker_enabled({"ADP_GH_TOKEN_BROKER_ENABLED": value}) is False

    def test_authority_forces_broker_even_when_optional_flag_is_disabled(self):
        from entrypoint import _gh_token_broker_enabled
        assert _gh_token_broker_enabled({"ADP_AGENT_AUTHORITY_ENABLED": "true", "ADP_GH_TOKEN_BROKER_ENABLED": "false"})

    def test_absent_disables(self):
        """Default off: an environment that has never heard of this flag is unchanged."""
        from entrypoint import _gh_token_broker_enabled

        assert _gh_token_broker_enabled({}) is False


class TestBrokerModeKeyNotExported:
    """The core assertion of #4272: no private key in the agent's environment."""

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._broker_installation_token")
    def test_private_key_absent_from_agent_env(
        self,
        mock_broker,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker="1")
        # Seed a key into the ambient pod env. Merely *not setting* it is not
        # enough: the agent subprocess env is os.environ.copy(), so an inherited
        # value (leftover from an earlier path, a projected Secret, an operator
        # debugging by hand) would reach the agent and make the flag silently
        # ineffective. The invariant must hold regardless of how the pod env was
        # populated.
        monkeypatch.setenv("GH_APP_KEY", "inherited-key-alias")
        monkeypatch.setenv("GH_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nambient\n-----END RSA PRIVATE KEY-----")
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-1")
        mock_broker.return_value = _BROKER_RESULT
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        main()

        agent_env = _agent_env(mock_subprocess_run)
        assert "GH_APP_KEY" not in agent_env
        assert "GH_APP_PRIVATE_KEY" not in agent_env, (
            "the whole point of #4272: a prompt-injected agent must not be able to "
            "read the platform App key out of its own environment"
        )

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._broker_installation_token")
    def test_broker_flag_and_installation_id_are_exported(
        self,
        mock_broker,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Without the flag, the TS token manager never initialises.

        agent-worker.ts and agent-pm.ts both gate initTokenManager on having a
        key. With the key gone and nothing else to key off, both predicates go
        false, no refresh is ever scheduled, and the run dies silently at ~1h with
        401 Bad credentials — a regression that looks like a flaky agent, not a
        config error. The flag is what keeps both predicates true.
        """
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker="1")
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-2")
        mock_broker.return_value = _BROKER_RESULT
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        main()

        agent_env = _agent_env(mock_subprocess_run)
        assert agent_env["ADP_GH_TOKEN_BROKER_ENABLED"] == "1"
        # The re-mint must be pinned to THIS run's installation, not installations[0].
        assert agent_env["GH_APP_INSTALLATION_ID"] == str(_INSTALLATION_ID)
        # GH_APP_ID must be non-empty for the same reason the flag must be set:
        # canInitTokenManager() requires it in BOTH modes, so an empty value is the
        # identical silent 1-hour death. It is public, so the gatekeeper returns it
        # rather than the pod reading the vault for it.
        assert agent_env["GH_APP_TOKEN"] == _BROKERED_TOKEN
        assert agent_env["GH_APP_TOKEN_EXPIRES_AT"] == "2099-01-01T00:00:00Z"
        assert agent_env["GH_APP_ID"] == _BROKERED_APP_ID

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._broker_installation_token")
    def test_bot_commit_identity_uses_the_brokered_app_id(
        self,
        mock_broker,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """git user.email is built from the App ID — an empty one is malformed.

        Step 6 configures `<app_id>+adp-agent[bot]@users.noreply.github.com`. With
        an empty app_id that becomes `+adp-agent[bot]@…`, which GitHub does not
        associate with the App, so commits show as an unknown author. Silent, and
        only visible in the commit trailer after the fact.
        """
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker="1")
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-email")
        mock_broker.return_value = _BROKER_RESULT
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        main()

        emails = [
            c.args[0][3]
            for c in mock_run_cmd.call_args_list
            if c.args and c.args[0][:3] == ["git", "config", "user.email"]
        ]
        assert emails, "expected step 6 to configure a git commit identity"
        assert emails[0] == f"{_BROKERED_APP_ID}+adp-agent[bot]@users.noreply.github.com"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._broker_installation_token")
    def test_vault_read_and_local_mint_never_happen(
        self,
        mock_broker,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Not-exported is not enough — the key must never enter the process.

        If the vault read still ran, the key would sit in pod memory (and in any
        crash dump or debug log) even though it is absent from the subprocess env.
        The bootstrap token must come through the gatekeeper too, which is what
        makes the in-pod mint genuinely dead code in broker mode.
        """
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker="1")
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-3")
        mock_broker.return_value = _BROKER_RESULT
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        main()

        mock_vault_cls.assert_not_called()
        mock_mint.assert_not_called()
        mock_broker.assert_called_once()
        kwargs = mock_broker.call_args.kwargs
        assert kwargs["installation_id"] == _INSTALLATION_ID
        assert f"{kwargs['repo_owner']}/{kwargs['repo_name']}" == _REPO


class TestBrokerFailureIsLoud:
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._fail_bootstrap_status")
    @patch("entrypoint._broker_installation_token")
    def test_unreachable_broker_fails_the_run_without_falling_back(
        self,
        mock_broker,
        mock_fail_status,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """A gatekeeper outage must not silently degrade to the in-pod mint.

        Falling back would keep the key in pod memory and quietly undo the fix —
        the failure mode would be invisible, so the regression would ship. Loud
        failure at bootstrap is the intended behavior.
        """
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker="1")
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-4")
        mock_broker.side_effect = RuntimeError("gateway unreachable")
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        with pytest.raises(RuntimeError, match="gateway unreachable"):
            main()

        # No silent fallback to the in-pod key path.
        mock_vault_cls.assert_not_called()
        mock_mint.assert_not_called()
        # And the operator gets a bootstrap status explaining why.
        mock_fail_status.assert_called_once()
        assert "gatekeeper" in mock_fail_status.call_args.args[2]

    def test_broker_helper_refuses_when_gateway_not_configured(self):
        """No endpoint configured is a refusal, not a shrug-and-mint-locally."""
        from entrypoint import _broker_installation_token

        client = MagicMock()
        client.is_configured = False

        with pytest.raises(RuntimeError, match="ADP_GH_TOKEN_BROKER_ENABLED"):
            _broker_installation_token(
                installation_id=1,
                repo_owner="acme",
                repo_name="widgets",
                cred_client=client,
            )

        client.github_installation_token.assert_not_called()

    def test_broker_helper_passes_installation_and_repo_through(self):
        from entrypoint import _broker_installation_token

        client = MagicMock()
        client.is_configured = True
        client.github_installation_token.return_value = {
            "token": _BROKERED_TOKEN,
            "expires_at": "2099-01-01T00:00:00Z",
            "app_id": _BROKERED_APP_ID,
        }

        token, app_id, expires_at = _broker_installation_token(
            installation_id=_INSTALLATION_ID,
            repo_owner="acme-corp",
            repo_name="flagship-app",
            cred_client=client,
        )

        assert expires_at == "2099-01-01T00:00:00Z"
        assert token == _BROKERED_TOKEN
        # The public App ID comes back from the gatekeeper: the pod no longer reads
        # the vault, but still needs it for GH_APP_ID and the bot commit identity.
        assert app_id == _BROKERED_APP_ID
        kwargs = client.github_installation_token.call_args.kwargs
        assert kwargs["installation_id"] == _INSTALLATION_ID
        assert kwargs["repo_owner"] == "acme-corp"
        assert kwargs["repo_name"] == "flagship-app"


class TestFlagOffIsUnchanged:
    """No-regression guard: embark1 runs with this flag off on merge day."""

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @patch("entrypoint._broker_installation_token")
    def test_flag_off_still_exports_key_and_uses_local_mint(
        self,
        mock_broker,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        import entrypoint
        from entrypoint import main

        _prepare(monkeypatch, tmp_path, entrypoint, broker=None)
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-broker-off")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {
            "app_id": "99001",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        }
        mock_mint.return_value = "ghs_local_mint"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        main()

        agent_env = _agent_env(mock_subprocess_run)
        assert agent_env["GH_APP_ID"] == "99001"
        assert agent_env["GH_APP_PRIVATE_KEY"].startswith("-----BEGIN RSA PRIVATE KEY-----")
        assert "ADP_GH_TOKEN_BROKER_ENABLED" not in agent_env
        mock_broker.assert_not_called()
        mock_mint.assert_called_once()


@pytest.mark.parametrize("expires_at", ["", "invalid", "2000-01-01T00:00:00Z", "2099-01-01T00:00:00"])
def test_bootstrap_refuses_unknown_or_expired_provider_token(expires_at):
    from entrypoint import _broker_installation_token
    client = MagicMock()
    client.github_installation_token.return_value = {"token": "fixture", "app_id": "1", "expires_at": expires_at}
    with pytest.raises(RuntimeError, match="unusable token or expiry"):
        _broker_installation_token(installation_id=1, repo_owner="acme", repo_name="repo", cred_client=client)
