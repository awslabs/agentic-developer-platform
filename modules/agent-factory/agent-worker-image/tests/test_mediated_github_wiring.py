"""The worker-setup half of mediated GitHub operations (issue #5223).

`lib/mediated_github.py` gives the agent a way to publish without a token, and
`tests/test_mediated_github.py` covers that helper. This file covers the wiring:
whether turning mediation on actually withholds the write token from the agent
subprocess.

Why that needs its own tests. Mediation's entire claim is "no merge-capable
credential is reachable from the agent". The gateway can refuse a merge operation
perfectly and the claim still be false, because an agent holding `$GH_TOKEN` does
not need the gateway's permission to merge — a `contents: write` installation token
authorizes `PUT /repos/{o}/{r}/pulls/{n}/merge` directly. So the property under
test is not "the gateway refuses" but "the token is absent from the environment the
agent runs in".

These tests assert OUTCOMES on the env dict actually handed to `subprocess.run`,
not source text:

  * with mediation on, no GitHub token variable reaches the agent
  * `GIT_ASKPASS` / `ADP_TOKEN_FILE` are gone too — the askpass helper falls back
    to `$GITHUB_TOKEN` and otherwise reads a TokenManager-refreshed file, so
    leaving either would let `git push` authenticate straight past mediation
  * the broker flag is withheld, so the TS TokenManager does not wake up and put
    a token back
  * bootstrap keeps its own token: clone happened before this, and the terminal
    check-run/status calls happen after
  * public identifiers (app id, installation id) survive — the bot commit identity
    is built from the app id, and neither can mint anything
  * customer AWS/vault variables survive, because the v2 semantics accepted for
    those are explicitly out of scope for this change
  * with the flag off, behavior is byte-for-byte unchanged
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.test_entrypoint import (
    SAMPLE_ENVELOPE,
    _subprocess_side_effect_fresh_branch,
)

_BROKERED_TOKEN = "ghs_brokered_from_gateway"
_BROKERED_APP_ID = "99001"
_BROKER_RESULT = (_BROKERED_TOKEN, _BROKERED_APP_ID, "2099-01-01T00:00:00Z")


def _prepare(monkeypatch, tmp_path, entrypoint, *, mediated: str | None):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(entrypoint, "BootstrapLogger", MagicMock())
    monkeypatch.setattr(entrypoint, "_start_sigv4_proxy", MagicMock())
    monkeypatch.setattr(entrypoint, "update_invocation_status", MagicMock())
    monkeypatch.setattr(entrypoint, "_write_outbound_correlation", MagicMock())
    # Broker mode on throughout: mediation is a property of policy-bearing runs,
    # which are exactly the runs that already take their token from the gateway.
    monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "1")
    # Protected-worker authority, for the same reason. Mediation is a policy-bearing
    # path and every mediated request must present the run credential and workload
    # token this cohort carries, so a run without it cannot be mediated — the fixture
    # has to look like the cohort the feature is actually for.
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    # That flag also binds the pod to its protected invocation over the network.
    # Stubbed so these tests stay about mediation wiring; the binding itself is
    # covered in test_run_identity.py.
    monkeypatch.setattr("lib.run_identity.bootstrap_run_identity", MagicMock())
    monkeypatch.setenv("GH_APP_ID", _BROKERED_APP_ID)
    if mediated is None:
        monkeypatch.delenv("ADP_MEDIATED_GITHUB_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ADP_MEDIATED_GITHUB_ENABLED", mediated)

    work_dir = tmp_path / "repo"
    work_dir.mkdir(parents=True)
    monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
    monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")
    if mediated in ("1", "true", "yes"):
        return _stub_mediated_startup(monkeypatch, work_dir)
    return None


def _assert_model_launched(mock_subprocess_run) -> None:
    """The half of the recorded blocker that "no credential was requested" cannot prove.

    The startup failure this branch fixes had two symptoms:
    `raw_token_broker_calls=1` AND `model_subprocess_calls=0`. A test that only
    asserts the absence of credential requests is trivially satisfied by a run that
    dies at bootstrap and gets nowhere at all — a dead run asks for nothing. So the
    absence assertions need this positive counterpart to mean anything.

    Kept as a helper because the graceful-exit case (bootstrap returns non-zero
    instead of raising) otherwise surfaces as `AttributeError: 'NoneType' has no
    attribute 'kwargs'` from `_agent_env`, which reads as a broken test rather than
    as "the model never launched".
    """
    assert mock_subprocess_run.call_args is not None, (
        "the model subprocess never launched: startup exited before spawning the "
        "agent. This is the `model_subprocess_calls=0` half of the recorded mediated "
        "startup blocker — a run that dies at bootstrap requests no credential, so "
        "the absence-of-token assertions pass vacuously without this check"
    )


def _agent_env(mock_subprocess_run) -> dict:
    _assert_model_launched(mock_subprocess_run)
    call = mock_subprocess_run.call_args
    return call.kwargs.get("env") or call[1].get("env")


def _stub_mediated_startup(monkeypatch, work_dir):
    """Stand in for the gateway's read and archive operations.

    A mediated run reaches the protected route during startup — the idempotency read
    and the work-tree materialization — rather than the token broker. Both are
    stubbed here at the helper boundary so these tests stay about *wiring*: which
    authority startup uses, and what ends up in the agent's environment. The
    operations' own behaviour is covered in `test_mediated_github.py`, and the real
    policy/broker boundary is exercised in
    `test_mediated_startup_acceptance.py`.
    """
    from lib import mediated_github

    calls = {"read": 0, "materialize": 0}

    def _read_repository(**_kwargs):
        calls["read"] += 1
        # Shaped exactly like the route's answer: the pull request is nested under
        # `repository`, NOT at the top level. An earlier version of this stub put
        # `pull_request` at the top level, which no route ever produces — that
        # fiction is what let the idempotency guard ship reading the wrong level.
        # No merged PR here: this run is not a redelivery of completed work.
        return {
            "repository": {
                "repository_id": 987654321,
                "repository": "acme-corp/flagship-app",
                "default_branch": "main",
                "branch_head": "b" * 40,
                "pull_request": None,
            },
            "branch": "agent/issue-42",
            "idempotency_key": "read_repository:acme-corp/flagship-app",
        }

    def _materialize(destination, **_kwargs):
        calls["materialize"] += 1
        Path(destination).mkdir(parents=True, exist_ok=True)
        return {"repository": "acme-corp/flagship-app", "remote_head": "a" * 40, "local_head": "b" * 40}

    monkeypatch.setattr(mediated_github, "read_repository", _read_repository)
    monkeypatch.setattr(mediated_github, "materialize_repository", _materialize)
    return calls


def _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, *, receipt: str):
    from entrypoint import main

    mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), receipt)
    mock_broker.return_value = _BROKER_RESULT
    mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
    mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
    mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
    main()


class TestFlagParsing:
    """Default off: the gateway cohort must ship before a worker depends on it."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_truthy_values_enable(self, value):
        from entrypoint import _mediated_github_enabled

        assert _mediated_github_enabled({"ADP_MEDIATED_GITHUB_ENABLED": value}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_disables(self, value):
        from entrypoint import _mediated_github_enabled

        assert _mediated_github_enabled({"ADP_MEDIATED_GITHUB_ENABLED": value}) is False

    def test_absent_disables(self):
        from entrypoint import _mediated_github_enabled

        assert _mediated_github_enabled({}) is False

    def test_broker_mode_alone_does_not_enable_mediation(self):
        """#4272 and #5223 are separate switches.

        Broker mode still hands this pod a real token; mediation is the further
        step of not passing it to the agent. Conflating them would activate
        mediation on every brokered run before the endpoint exists.
        """
        from entrypoint import _mediated_github_enabled

        assert _mediated_github_enabled({"ADP_GH_TOKEN_BROKER_ENABLED": "1", "ADP_AGENT_AUTHORITY_ENABLED": "true"}) is False


class TestWithholdWriteToken:
    """Unit-level: exactly what `_withhold_write_token` removes and keeps."""

    def test_every_token_variable_is_removed(self):
        from entrypoint import _withhold_write_token

        env = {
            "GITHUB_TOKEN": "ghs_x",
            "GH_TOKEN": "ghs_x",
            "GH_APP_TOKEN": "ghs_x",
            "GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----",
            "GH_APP_KEY": "alias",
        }
        _withhold_write_token(env)
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_APP_TOKEN", "GH_APP_PRIVATE_KEY", "GH_APP_KEY"):
            assert var not in env, f"{var} would give the agent a merge-capable credential"

    def test_git_authentication_path_is_removed(self):
        """A token-less env still pushes if git can find a token another way.

        git-askpass-helper reads $ADP_TOKEN_FILE and falls back to $GITHUB_TOKEN.
        Removing the env var but leaving GIT_ASKPASS pointed at a live token file
        would let `git push` succeed outside mediation entirely.
        """
        from entrypoint import _withhold_write_token

        env = {"GIT_ASKPASS": "/usr/local/bin/git-askpass-helper", "ADP_TOKEN_FILE": "/tmp/.adp-gh-token"}
        _withhold_write_token(env)
        assert "GIT_ASKPASS" not in env
        assert "ADP_TOKEN_FILE" not in env

    def test_token_manager_is_not_told_to_refresh(self):
        """The TS TokenManager would re-mint a token into the env it manages."""
        from entrypoint import _withhold_write_token

        env = {"ADP_GH_TOKEN_BROKER_ENABLED": "1"}
        _withhold_write_token(env)
        assert "ADP_GH_TOKEN_BROKER_ENABLED" not in env

    def test_the_token_file_on_disk_is_removed(self, tmp_path, monkeypatch):
        """Unsetting ADP_TOKEN_FILE does not hide the file it pointed at.

        Both shell helpers resolve the path with a DEFAULT, not just the env var:

            TOKEN_FILE="${ADP_TOKEN_FILE:-/tmp/.adp-gh-token}"

        `gh-wrapper` then reads it unconditionally and exports `GH_TOKEN` for the
        `gh` it execs. So an agent whose env we stripped still gets an
        authenticated `gh` — including `gh pr merge` — from the *file*, and
        `git push` gets one the same way if GIT_ASKPASS is reachable. Popping the
        env var is not a control while the bytes are still on disk at the path
        the helpers fall back to.
        """
        from entrypoint import _withhold_write_token

        token_file = tmp_path / ".adp-gh-token"
        token_file.write_text("ghs_live_installation_token")
        monkeypatch.setattr("entrypoint.MEDIATED_TOKEN_FILE_PATHS", (str(token_file),))

        _withhold_write_token({"ADP_TOKEN_FILE": str(token_file)})

        assert not token_file.exists(), "gh-wrapper reads this path even with the env var unset"

    def test_removing_the_token_file_tolerates_absence(self, tmp_path, monkeypatch):
        """PAT mode is the only path that writes the file before the agent starts.

        In brokered mediation the TS TokenManager never runs, so there is usually
        nothing to remove. That must be an ordinary no-op, not a bootstrap crash.
        """
        from entrypoint import _withhold_write_token

        monkeypatch.setattr(
            "entrypoint.MEDIATED_TOKEN_FILE_PATHS", (str(tmp_path / "never-written"),)
        )

        _withhold_write_token({})  # must not raise

    def test_mode_and_flag_are_announced(self):
        from entrypoint import _withhold_write_token

        env = {}
        _withhold_write_token(env)
        assert env["ADP_TOKEN_MODE"] == "mediated"
        assert env["ADP_MEDIATED_GITHUB_ENABLED"] == "true"

    def test_public_identifiers_survive(self):
        """Not credentials, and the bot commit identity needs the app id."""
        from entrypoint import _withhold_write_token

        env = {"GH_APP_ID": "99001", "GH_APP_INSTALLATION_ID": "12345"}
        _withhold_write_token(env)
        assert env["GH_APP_ID"] == "99001"
        assert env["GH_APP_INSTALLATION_ID"] == "12345"

    def test_customer_credentials_survive(self):
        """v2 AWS/vault semantics are explicitly out of scope for #5223."""
        from entrypoint import _withhold_write_token

        env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s", "ADP_USER_ID": "u"}
        _withhold_write_token(env)
        assert env["AWS_ACCESS_KEY_ID"] == "AKIA"
        assert env["AWS_SECRET_ACCESS_KEY"] == "s"
        assert env["ADP_USER_ID"] == "u"


class TestMediatedRunAgentEnvironment:
    """The load-bearing assertion: what the agent process actually receives."""

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
    def test_no_token_reaches_the_agent(
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

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        # Seed tokens into the ambient pod env. Not setting them is not enough:
        # agent_env is os.environ.copy(), so an inherited value would reach the
        # agent and make the flag silently ineffective.
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_ambient")
        monkeypatch.setenv("GH_TOKEN", "ghs_ambient")

        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-mediated-1")

        agent_env = _agent_env(mock_subprocess_run)
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_APP_TOKEN", "GH_APP_PRIVATE_KEY", "GH_APP_KEY"):
            assert var not in agent_env, (
                f"{var} reached the agent — a `contents: write` token authorizes "
                "PUT /pulls/{n}/merge directly, so the human merge gate would be "
                "bypassable without the gateway ever being asked"
            )
        assert _BROKERED_TOKEN not in "".join(str(v) for v in agent_env.values()), (
            "the brokered token value appeared in the agent env under some other name"
        )
        # The long-lived proxy shares the agent's uid/PID namespace. Its initial
        # exec environment must not expose the withheld token through /proc.
        proxy_env = entrypoint._start_sigv4_proxy.call_args.args[0]
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_APP_TOKEN", "GH_APP_PRIVATE_KEY", "GH_APP_KEY"):
            assert var not in proxy_env, f"{var} reached the proxy process"
        assert _BROKERED_TOKEN not in "".join(str(v) for v in proxy_env.values())

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
    def test_git_cannot_authenticate_and_mediation_is_announced(
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

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-mediated-2")

        agent_env = _agent_env(mock_subprocess_run)
        assert "GIT_ASKPASS" not in agent_env
        assert "ADP_TOKEN_FILE" not in agent_env
        assert "ADP_GH_TOKEN_BROKER_ENABLED" not in agent_env, "the TS TokenManager would re-mint a token into this env"
        # The helper reads this to decide it is in mediated mode; without it the
        # agent would have neither a token nor a mediated path and simply fail.
        assert agent_env["ADP_MEDIATED_GITHUB_ENABLED"] == "true"
        assert agent_env["ADP_TOKEN_MODE"] == "mediated"

        # Popping the broker variable is necessary but NOT sufficient, and this
        # records why so it is not mistaken for the whole control:
        # `githubTokenBroker.isBrokerEnabled` also returns true for
        # ADP_AGENT_AUTHORITY_ENABLED, which this env legitimately still carries
        # (the authority transport needs it), and GH_APP_ID / GH_APP_INSTALLATION_ID
        # / REPO_OWNER survive too — which together satisfied every precondition
        # `canInitTokenManager` checked, so the TS side re-minted a token seconds
        # after startup. The guard that actually closes it is `isMediatedRun` in
        # agent/src/mediated-github-config.ts, pinned by
        # agent/src/token-init-guard.test.ts; these two variables are what it reads.
        for survivor in ("GH_APP_ID", "GH_APP_INSTALLATION_ID"):
            assert survivor in agent_env, "public identifiers stay; the TS guard, not their absence, stops the re-mint"

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
    def test_missing_app_id_is_not_exported_as_an_empty_identifier(
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

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        monkeypatch.delenv("GH_APP_ID")
        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-mediated-no-app-id")

        agent_env = _agent_env(mock_subprocess_run)
        assert "GH_APP_ID" not in agent_env, "an absent public identifier must not be exported as an empty value"
        assert agent_env["GH_APP_INSTALLATION_ID"], "the independently available installation id still survives"

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
    def test_bootstrap_requests_no_token_at_all(
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
        """The startup invariant: a mediated run never asks for an installation token.

        This replaces an earlier test that asserted bootstrap *kept* a brokered
        token, which described a design that could not work for the cohort mediation
        exists to serve. `authorize_worker_credential` refuses
        /internal/v1/github-installation-token for a develop assignment whose merge
        gate is human-only — correctly — so a startup path that mints first and
        withholds later dies at bootstrap and never launches the model. Requesting
        the token and having it refused is the bug, not a step on the way.

        Asserted as an outcome on the real call sites: the broker and the vault/mint
        fallback are all mocked, so if any startup step reached for a credential this
        would record it.
        """
        import entrypoint

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-mediated-3")

        import os

        # Both halves of the recorded blocker, in the order they were recorded.
        # `model_subprocess_calls=0` first: if the run died at bootstrap, every
        # assertion below is vacuously true, because a dead run asks for nothing.
        _assert_model_launched(mock_subprocess_run)

        assert not mock_broker.called, (
            "startup asked the broker for an installation token; that request is "
            "refused for a human-merge-gated assignment, so the run would die here"
        )
        assert not mock_mint.called, "startup minted a token from a private key"
        assert not mock_vault_cls.called, "startup read the App private key from the vault"
        # Nor did it leave one behind for a later step to find.
        assert not os.environ.get("GITHUB_TOKEN")
        assert not os.environ.get("GH_TOKEN")
        # No check run: that is a provider write needing `checks: write`, which this
        # run has no credential for. It degrades visibly instead of failing the pod.
        assert not mock_create_cr.called

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
    def test_public_identifiers_and_customer_credentials_reach_the_agent(
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

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        # With no mint, the App id comes from the pod environment the ScaledJob
        # publishes rather than as a by-product of the token response.
        monkeypatch.setenv("GH_APP_ID", _BROKERED_APP_ID)
        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-mediated-4")

        agent_env = _agent_env(mock_subprocess_run)
        assert agent_env["GH_APP_ID"] == _BROKERED_APP_ID, (
            "the public App id must still reach the agent — it is not a credential, "
            "and nothing can be minted from it without the withheld private key"
        )
        assert agent_env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE", (
            "#5223 withholds GitHub tokens only; the v2 customer AWS/vault semantics stay unchanged"
        )


class TestFlagOffIsUnchanged:
    """Dormant by default, so merging this changes no environment's behavior."""

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
    def test_token_still_reaches_the_agent_when_off(
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

        _prepare(monkeypatch, tmp_path, entrypoint, mediated=None)
        _run_main(mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run, receipt="receipt-unmediated-1")

        agent_env = _agent_env(mock_subprocess_run)
        assert agent_env["GITHUB_TOKEN"] == _BROKERED_TOKEN
        assert agent_env["GH_TOKEN"] == _BROKERED_TOKEN
        assert agent_env["GIT_ASKPASS"] == "/usr/local/bin/git-askpass-helper"
        assert agent_env["ADP_TOKEN_MODE"] == "app"
        assert "ADP_MEDIATED_GITHUB_ENABLED" not in agent_env


class TestInstructionsMatchTheHelper:
    """The agent's instructions must name calls that actually exist.

    `agent/src/mediated-github-config.ts` tells the agent to import
    `lib.mediated_github` and call it, because withholding the token makes the
    unsafe path impossible without making the safe path discoverable. Prose that
    teaches a call which does not exist is worse than no prose at all: the agent
    burns the run on an AttributeError rather than on an auth failure, and the
    instruction is the only place it learns the API.

    Nothing imports across the two languages at runtime, so a rename on this side
    would go unnoticed until a live run. These tests read the TypeScript source and
    compare it against the helper's real exports, which is the cheapest place to
    catch the drift.
    """

    PROMPT_SOURCE = (
        Path(__file__).resolve().parents[3] / "agent-factory" / "agent" / "src" / "mediated-github-config.ts"
    )

    @pytest.fixture(scope="class")
    @classmethod
    def prompt_text(cls) -> str:
        if not cls.PROMPT_SOURCE.exists():
            pytest.fail(f"the agent instruction module is missing: {cls.PROMPT_SOURCE}")
        return cls.PROMPT_SOURCE.read_text()

    @pytest.mark.parametrize(
        "func", ["publish_commit", "upsert_pull_request", "publish_review", "read_repository"]
    )
    def test_named_function_exists_and_is_exported(self, prompt_text, func):
        from lib import mediated_github

        assert func in prompt_text, f"instructions do not mention {func}"
        assert callable(getattr(mediated_github, func, None)), f"{func} is not callable on the helper"
        assert func in mediated_github.__all__, f"{func} is not a supported export"

    @pytest.mark.parametrize(
        "error", ["MediatedConflict", "MediatedUnavailable", "MediatedRefused"]
    )
    def test_named_error_class_exists(self, prompt_text, error):
        from lib import mediated_github

        assert error in prompt_text, f"instructions do not mention {error}"
        assert issubclass(getattr(mediated_github, error), Exception)

    def test_the_import_path_in_the_instructions_works(self, prompt_text):
        """The instructions tell the agent to run `from lib import mediated_github`."""
        assert "from lib import mediated_github" in prompt_text

        from lib import mediated_github  # noqa: F401  - the assertion is that this imports

    def test_publish_commit_accepts_the_arguments_the_instructions_pass(self, prompt_text):
        """`publish_commit(repo='.', message=...)` must match the real signature.

        Both are keyword-only in the helper, so a positional example would fail at
        runtime even though the names are right.
        """
        import inspect

        from lib import mediated_github

        assert "publish_commit(repo='.', message=" in prompt_text
        params = inspect.signature(mediated_github.publish_commit).parameters
        for name in ("repo", "message"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_instructions_do_not_promise_a_merge_call(self, prompt_text):
        """Merge is reachable only as a separate operation requiring Action.MERGE.

        An instruction naming a merge helper would be teaching the agent to attempt
        exactly the thing the human gate exists to reserve.
        """
        from lib import mediated_github

        assert not any(name.startswith("merge") for name in mediated_github.__all__)
        assert "merge_pull_request" not in prompt_text

    def test_the_flag_spelling_matches_on_both_sides(self, prompt_text):
        """Python withholds the token; TypeScript tells the agent what to do instead.

        If the two read different variables, a run either holds a token while being
        told it has none, or is told to use a path that is not active.
        """
        from entrypoint import ADP_MEDIATED_GITHUB_ENV

        assert ADP_MEDIATED_GITHUB_ENV in prompt_text


class TestTheEffectiveDecisionIsSingleAndAuthoritative:
    """One decision per run, consulted everywhere — including across the boundary.

    The feature flag is a POD-level environment variable, so "switched on" is a
    property of the deployment, not of a run. Runs that must NOT be mediated share
    the pod with runs that must: a PAT run carries the user's own credential, and a
    run without protected-worker authority cannot authenticate a mediated request at
    all. `_mediated_run` is where that distinction is made.

    These tests exist because the distinction was made once and then re-derived from
    the raw flag at two later sites, which is a defect class rather than a typo: a
    decision consulted in N places is N places that can disagree. The PAT divergence
    was not a degraded mode — the token file written during startup was deleted, the
    env token popped, `GIT_ASKPASS` unset and `ADP_TOKEN_MODE` overwritten, so the
    run had no way to reach GitHub while being told it needed none.
    """

    def test_pat_and_policy_less_runs_are_not_mediated_though_the_flag_is_on(self):
        """The decision itself: the flag alone does not make a run mediated."""
        from entrypoint import _mediated_github_enabled, _protected_worker

        protected = {"ADP_MEDIATED_GITHUB_ENABLED": "true", "ADP_AGENT_AUTHORITY_ENABLED": "true"}
        # Both preconditions hold, so only token_mode decides.
        assert _mediated_github_enabled(protected) and _protected_worker(protected)
        # A run without protected-worker authority cannot present the run credential
        # and workload token a mediated request requires, so it is not mediated.
        assert _protected_worker({"ADP_MEDIATED_GITHUB_ENABLED": "true"}) is False

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
    def test_a_pat_run_keeps_its_token_and_its_prompt_when_the_flag_is_on(
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
        """A PAT run with mediation on globally is byte-for-byte a PAT run.

        This is the regression that matters most in #5223: the flag is deployed
        pod-wide, so this configuration is ordinary operation. Every assertion here
        failed before the single decision was threaded through.
        """
        import entrypoint

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        # An accepted PAT: the user's own credential, with its own accepted scope.
        _pat = "ghp_the_users_own_credential"
        monkeypatch.setattr(
            entrypoint,
            "_resolve_execution_token",
            lambda **_kwargs: entrypoint.PatResolutionResult(
                token_mode="pat", token=_pat, github_login="jane-dev"
            ),
        )
        # Real token file, so a deletion would be observable rather than silent.
        token_file = tmp_path / ".adp-gh-token"
        token_file.write_text("pre-existing")
        monkeypatch.setattr(entrypoint, "MEDIATED_TOKEN_FILE_PATHS", (str(token_file),))

        _run_main(
            mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run,
            receipt="receipt-pat-flag-on-1",
        )

        agent_env = _agent_env(mock_subprocess_run)
        # The token path is unchanged: the credential is present and usable.
        assert agent_env["GITHUB_TOKEN"] == _pat
        assert agent_env["GH_TOKEN"] == _pat
        assert agent_env["GIT_ASKPASS"] == "/usr/local/bin/git-askpass-helper"
        # Token mode still says "pat", so the TS TokenManager adopts the PAT as-is
        # instead of taking the mediated branch and refusing to mint anything.
        assert agent_env["ADP_TOKEN_MODE"] == "pat"
        # The raw flag must NOT reach the agent: the TS side reads exactly this
        # variable to decide whether to inject the "you have NO GitHub token"
        # prompt and to gate every mint. Inherited from the pod, it told a
        # credential-holding run it had none.
        assert "ADP_MEDIATED_GITHUB_ENABLED" not in agent_env
        # The askpass helper's on-disk token survives: deleting it left `git push`
        # with no credential at all, since both helpers default to this path.
        assert token_file.exists()
        # No mediated operation was attempted for a run the gateway would refuse.
        assert mock_broker.called is False

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
    def test_a_run_without_protected_authority_keeps_the_token_path_when_the_flag_is_on(
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
        """A non-protected run still takes its token path with the flag on.

        Mediation is policy-bearing: the gateway refuses every operation for a run
        with no accepted policy, and refuses it with the same opaque 404 it uses for
        a revoked one — so the worker cannot discover the difference by trying. The
        decision must therefore exclude these runs up front. Treating them as
        mediated turned a working run into a bootstrap failure at clone time.
        """
        import entrypoint

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        # No protected-worker authority: this run cannot present the identity a
        # mediated request requires.
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

        _run_main(
            mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run,
            receipt="receipt-unprotected-flag-on-1",
        )

        agent_env = _agent_env(mock_subprocess_run)
        assert agent_env["GITHUB_TOKEN"] == _BROKERED_TOKEN
        assert agent_env["ADP_TOKEN_MODE"] == "app"
        assert agent_env["GIT_ASKPASS"] == "/usr/local/bin/git-askpass-helper"
        assert "ADP_MEDIATED_GITHUB_ENABLED" not in agent_env
        # It took the ordinary token path rather than dying at bootstrap.
        assert mock_broker.called is True

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
    def test_a_mediated_run_still_has_the_flag_and_no_token(
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
        """Narrowing the decision must not disarm mediation for its own cohort.

        The counterpart to the two tests above: a policy-bearing App run is still
        mediated, still holds no token, and still gets the flag the prompt keys off.
        Without this, "no PAT run is mediated" could be satisfied by mediating
        nothing at all.
        """
        import entrypoint

        _prepare(monkeypatch, tmp_path, entrypoint, mediated="true")
        _run_main(
            mock_receive_msg, mock_broker, mock_run_cmd, mock_create_cr, mock_subprocess_run,
            receipt="receipt-mediated-still-on-1",
        )

        agent_env = _agent_env(mock_subprocess_run)
        assert "GITHUB_TOKEN" not in agent_env
        assert "GH_TOKEN" not in agent_env
        assert "GIT_ASKPASS" not in agent_env
        assert agent_env["ADP_TOKEN_MODE"] == "mediated"
        # The prompt and the TS refresh guards key off this variable.
        assert agent_env["ADP_MEDIATED_GITHUB_ENABLED"] == "true"
        assert mock_broker.called is False
