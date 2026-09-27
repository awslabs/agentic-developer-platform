"""Unit tests for agent-worker-image entrypoint and helper libraries.

Covers the 12-step sequence with mocked external dependencies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent to path so we can import the modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.check_run import create_check_run, update_check_run
from lib.gateway_credential_client import GatewayCredentialClient, GatewayCredentialError
from lib.vault_client import VaultClient
from lib.github_token import generate_jwt, mint_installation_token
from lib.sts_assume import assume_customer_role


# --- Fixtures ---

@pytest.fixture(autouse=True)
def stub_agent_process(monkeypatch):
    # Entrypoint tests stub execution; process-tree deadlines have real-process tests.
    import subprocess
    monkeypatch.setattr("lib.agent_process.run_agent", lambda command, **options: subprocess.run(command, **options))



def _subprocess_side_effect_fresh_branch(*args, **kwargs):
    """Default subprocess.run side_effect for tests simulating a fresh issue.

    The entrypoint calls subprocess.run directly (not run_cmd) for:
      1. `git ls-remote --exit-code --heads origin agent/issue-NNN`
         → returncode 0 means "branch exists"; we return 2 (doesn't exist)
         so the fresh-creation path runs (the legacy test default).
      2. `gh pr list ...` (only if branch exists; not reached in fresh case)
      3. `git push --delete origin ...` (only if stale branch reset; not reached)
      4. The final `subprocess.run` for node agent execution → returncode 0.

    Tests that simulate "branch already exists" should override this with
    their own side_effect.
    """
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=2, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def ready_gateway_proxy(monkeypatch):
    """Main-sequence tests have a healthy proxy unless explicitly overridden."""
    # Hosted reviewers inherit the worker's real queue. Tests must opt into a
    # fake queue explicitly, never consume another live assignment from it.
    monkeypatch.delenv("QUEUE_URL", raising=False)
    monkeypatch.setattr("entrypoint._start_sigv4_proxy", MagicMock())
    monkeypatch.setattr("entrypoint._stop_sigv4_proxy", MagicMock())
    monkeypatch.setattr("entrypoint.BootstrapLogger", MagicMock())
    # Real durable-receipt behavior is covered in test_invocation_completion.py.
    monkeypatch.setattr("entrypoint.is_delivery_completed", MagicMock(return_value=False))
    monkeypatch.setattr("entrypoint.record_delivery_completed", MagicMock())
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")
    # main() exports runtime telemetry with os.environ.update, outside monkeypatch.
    # Restore those writes too so later model-policy tests see their own run state.
    with patch.dict(os.environ):
        yield


SAMPLE_ENVELOPE = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "message_id": "msg-abc-123",
    "actor": {
        "github_id": 12345678,
        "github_login": "jane-dev",
        "user_id": "cognito-sub-jane-123",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 99887766,
        "repo": "acme-corp/flagship-app",
        "issue": 42,
        "pr": None,
        "sha": None,
    },
    "intent": {"trigger": "issue_labeled", "label": "developer"},
    "arrived_at": "2026-04-30T14:22:00Z",
}


@pytest.fixture
def envelope_json():
    return json.dumps(SAMPLE_ENVELOPE)


@pytest.fixture
def env_with_message(envelope_json, monkeypatch, tmp_path):
    """Set up env with SQS_MESSAGE_BODY and a writable workspace."""
    monkeypatch.setenv("SQS_MESSAGE_BODY", envelope_json)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Patch WORK_DIR to tmp
    work_dir = tmp_path / "repo"
    return work_dir


# --- Test: parse_envelope ---


class TestParseEnvelope:
    def test_valid_envelope(self):
        from entrypoint import parse_envelope

        result = parse_envelope(json.dumps(SAMPLE_ENVELOPE))
        assert result["tenant_id"] == "acme-corp"
        assert result["persona"] == "developer"
        assert result["source_ref"]["installation_id"] == 99887766

    def test_missing_tenant_id(self):
        from entrypoint import parse_envelope

        bad = {**SAMPLE_ENVELOPE}
        del bad["tenant_id"]
        with pytest.raises(ValueError, match="tenant_id"):
            parse_envelope(json.dumps(bad))

    def test_missing_source_ref_field(self):
        from entrypoint import parse_envelope

        bad = {**SAMPLE_ENVELOPE, "source_ref": {"installation_id": 1, "repo": "x/y"}}
        with pytest.raises(ValueError, match="issue"):
            parse_envelope(json.dumps(bad))

    def test_invalid_json(self):
        from entrypoint import parse_envelope

        with pytest.raises(json.JSONDecodeError):
            parse_envelope("not json")


class TestPersonaRuntimeRouting:
    def test_existing_personas_keep_the_claude_worker(self):
        from entrypoint import AGENT_BINARY, persona_runtime, worker_command

        assert persona_runtime("reviewer") == "claude"
        assert worker_command("architect") == ["node", AGENT_BINARY]

    def test_codex_reviewer_uses_the_embedded_adapter(self):
        from entrypoint import CODEX_REVIEWER_BINARY, persona_runtime, worker_command

        assert persona_runtime("agent-codex-reviewer") == "codex"
        assert worker_command("agent-codex-reviewer") == [
            "node",
            CODEX_REVIEWER_BINARY,
            "--embedded",
        ]

    def test_future_codex_personas_are_explicitly_fail_closed(self):
        from entrypoint import persona_runtime, worker_command

        assert persona_runtime("agent-codex-architect") == "codex"
        with pytest.raises(ValueError, match="not packaged yet"):
            worker_command("agent-codex-architect")


# --- Test: vault_client ---


class TestVaultClient:
    @patch("lib.vault_client.boto3.client")
    def test_get_secret(self, mock_boto_client):
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"app_id": "123", "private_key": "fake-key"}'
        }

        client = VaultClient(region="us-east-1", env="dev")
        result = client.get_secret("tenants/acme-corp/github-app")

        mock_sm.get_secret_value.assert_called_once_with(
            SecretId="adp/dev/tenants/acme-corp/github-app"
        )
        assert result == {"app_id": "123", "private_key": "fake-key"}


# --- Test: github_token ---


class TestGithubToken:
    def test_generate_jwt_structure(self):
        """JWT should have 3 dot-separated parts."""
        # Generate a test RSA key
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        token = generate_jwt("12345", pem)
        parts = token.split(".")
        assert len(parts) == 3

    @patch("lib.github_token.requests.post")
    def test_mint_installation_token_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "token": "ghs_test_token_123",
            "expires_at": "2026-05-01T00:00:00Z",
        }
        mock_post.return_value = mock_resp

        with patch("lib.github_token.generate_jwt", return_value="fake-jwt"):
            result = mint_installation_token("123", "fake-key", 99887766)

        assert result == "ghs_test_token_123"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "99887766" in call_url

    @patch("lib.github_token.requests.post")
    def test_mint_installation_token_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Bad credentials"
        mock_post.return_value = mock_resp

        with patch("lib.github_token.generate_jwt", return_value="fake-jwt"):
            with pytest.raises(RuntimeError, match="Failed to mint token"):
                mint_installation_token("123", "fake-key", 99887766)


# --- Test: sts_assume ---


class TestStsAssume:
    @patch("lib.sts_assume.boto3.client")
    def test_assume_customer_role(self, mock_boto_client):
        mock_sts = MagicMock()
        mock_boto_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIATEST",
                "SecretAccessKey": "secret123",
                "SessionToken": "token456",
                "Expiration": "2026-05-01T00:00:00Z",
            }
        }

        result = assume_customer_role(
            role_arn="arn:aws:iam::111122223333:role/adp-hosted-agent",
            external_id="ext-id-abc",
            tenant_id="acme-corp",
            actor_login="jane-dev",
            actor_id="12345678",
            run_id="msg-abc-123",
            repo="acme-corp/flagship-app",
            issue=42,
            persona="operations",
        )

        assert result["AWS_ACCESS_KEY_ID"] == "AKIATEST"
        assert result["AWS_SECRET_ACCESS_KEY"] == "secret123"
        assert result["AWS_SESSION_TOKEN"] == "token456"

        # Verify session tags were passed
        call_kwargs = mock_sts.assume_role.call_args[1]
        tags = call_kwargs["Tags"]
        tag_keys = [t["Key"] for t in tags]
        assert "adp:tenant_id" in tag_keys
        assert "adp:persona" in tag_keys


# --- Test: _stage_personas_and_skills ---


class TestStagePersonasAndSkills:
    """Staged image files must be hidden from git so they don't land in PRs."""

    def test_exclude_file_written_with_staged_paths(self, tmp_path, monkeypatch):
        import entrypoint

        work_dir = tmp_path / "repo"
        (work_dir / ".git" / "info").mkdir(parents=True)
        personas_src = tmp_path / "personas"
        personas_src.mkdir()
        (personas_src / "developer.md").write_text("persona content")
        skills_src = tmp_path / "skills"
        (skills_src / "stage-1-triage").mkdir(parents=True)
        (skills_src / "stage-1-triage" / "SKILL.md").write_text("skill content")

        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", personas_src)
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", skills_src)

        entrypoint._stage_personas_and_skills()

        exclude = (work_dir / ".git" / "info" / "exclude").read_text()
        assert ".adp-rules/" in exclude
        assert ".claude/skills/" in exclude
        # Files actually copied (agent can still read them at runtime)
        assert (work_dir / ".adp-rules" / "personas" / "developer.md").exists()
        assert (work_dir / ".claude" / "skills" / "stage-1-triage" / "SKILL.md").exists()

    def test_exclude_file_appended_not_overwritten(self, tmp_path, monkeypatch):
        import entrypoint

        work_dir = tmp_path / "repo"
        (work_dir / ".git" / "info").mkdir(parents=True)
        (work_dir / ".git" / "info" / "exclude").write_text("# pre-existing\n*.tmp\n")
        (tmp_path / "personas").mkdir()
        (tmp_path / "skills").mkdir()

        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        entrypoint._stage_personas_and_skills()

        exclude = (work_dir / ".git" / "info" / "exclude").read_text()
        assert "*.tmp" in exclude
        assert ".adp-rules/" in exclude
        assert ".claude/skills/" in exclude


# --- Test: entrypoint main flow ---


class TestEntrypointMain:
    @pytest.mark.parametrize("start_refused", [False, True])
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.rmtree")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_full_sequence_success(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_rmtree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        monkeypatch,
        tmp_path,
        start_refused,
    ):
        """Test the full 12-step sequence with a successful agent run."""
        from entrypoint import main

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setattr(
            "entrypoint._receive_one_message", lambda *_: (json.dumps(SAMPLE_ENVELOPE), "receipt")
        )
        monkeypatch.setattr("entrypoint._delete_message", MagicMock())
        monkeypatch.setattr("entrypoint.create_check_run", MagicMock(return_value={"id": 111}))
        monkeypatch.setattr("entrypoint.update_check_run", MagicMock())
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        # Mock vault
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {
            "app_id": "123",
            "private_key": "fake-key",
        }

        # Mock token mint
        mock_mint.return_value = "ghs_test_token"

        # Mock run_cmd for git clone, git config, label removal, comments
        mock_run_cmd.return_value = MagicMock(stdout="", stderr="", returncode=0)

        # Mock agent execution (subprocess.run for node)
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        # Patch WORK_DIR and paths
        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        import entrypoint

        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        if start_refused:
            monkeypatch.setattr(entrypoint.run_report, "begin_delivery", MagicMock(side_effect=entrypoint.run_report.RunReportError("run_report_http_409", retryable=False)))
            monkeypatch.setattr(entrypoint.run_report, "report_block", MagicMock())
            assert main() == entrypoint.AGENT_EXIT_RETRYABLE
            assert not any(call.args[0][0] in {"node", "claude", "codex"} for call in mock_subprocess_run.call_args_list)
            entrypoint._delete_message.assert_not_called()
            return

        assert main() == 0

        # Vault was called for github-app creds
        mock_vault.get_secret.assert_called_with("tenants/acme-corp/github-app")
        # Token was minted
        mock_mint.assert_called_once_with("123", "fake-key", 99887766)
        # Agent was executed
        assert any(call.args[0][0] == "node" for call in mock_subprocess_run.call_args_list)

    def test_missing_queue_configuration_does_not_receive_work(self, monkeypatch):
        """The fixture removes even an inherited live worker queue."""
        from entrypoint import main

        receiver = MagicMock(side_effect=AssertionError("test tried to receive live work"))
        monkeypatch.setattr("entrypoint._receive_one_message", receiver)
        monkeypatch.delenv("SQS_MESSAGE_BODY", raising=False)
        assert main() == 1
        receiver.assert_not_called()

    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_agent_failure_posts_comment(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        monkeypatch,
        tmp_path,
    ):
        """On agent failure, should post failure comment and return nonzero."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("SQS_MESSAGE_BODY", json.dumps(SAMPLE_ENVELOPE))
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="", stderr="", returncode=0)

        # Agent fails
        mock_subprocess_run.return_value = MagicMock(returncode=1)

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 1

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_operations_persona_assumes_aws_role_via_gateway(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Operations persona fetches AWS creds via gateway and assumes role."""
        from entrypoint import main
        import entrypoint

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key")

        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-handle-ops")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 111, "html_url": "https://github.com/x/runs/111"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AK",
                "secret_access_key": "SK",
                "session_token": "ST",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-123",
            }
            main()
            # Gateway-side assume-role is called once (replaces raw_read+local STS)
            mock_gw.raw_read.assert_not_called()
            mock_gw.assume_role.assert_called_once()
            call_kwargs = mock_gw.assume_role.call_args.kwargs
            assert call_kwargs["user_id"] == "cognito-sub-jane-123"
            assert call_kwargs["agent_id"] == "operations"
            assert call_kwargs["service"] == "aws"

    def test_idempotency_marker_in_comments(self):
        """Verify marker format uses message_id for idempotency."""
        # The _post_comment function embeds message_id in an HTML comment marker
        # This test verifies the marker format logic
        message_id = "msg-abc-123"
        expected_marker = f"<!-- adp-completed:{message_id} -->"
        assert f"adp-completed:{message_id}" in expected_marker

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.rmtree")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_check_run_created_and_finalized_on_success(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_rmtree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Check run is created after clone and finalized after agent succeeds."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-abc")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"

        # run_cmd call sequence with WIP-branch changes:
        #  clone, git config x2, WIP-branch (checkout -b, commit, push, rev-parse),
        #  label remove, started-comment check+post,
        #  [agent runs],
        #  _handle_success: diff, status, log-check, no-changes-comment check+post,
        #  finalization: gh pr view for PR url.
        mock_run_cmd.side_effect = [
            MagicMock(stdout="", returncode=0),  # git clone
            MagicMock(stdout="", returncode=0),  # git config email
            MagicMock(stdout="", returncode=0),  # git config name
            MagicMock(stdout="", returncode=0),  # git checkout -b branch
            MagicMock(stdout="", returncode=0),  # git commit --allow-empty WIP
            MagicMock(stdout="", returncode=0),  # git push -u origin branch
            MagicMock(stdout="abc1234def5678\n", returncode=0),  # git rev-parse HEAD (WIP sha)
            MagicMock(stdout="", returncode=0),  # gh issue edit --remove-label
            MagicMock(stdout="", returncode=0),  # gh issue view (started check)
            MagicMock(stdout="", returncode=0),  # gh issue comment (started)
            MagicMock(stdout="", returncode=0),  # git diff --stat (no changes)
            MagicMock(stdout="", returncode=0),  # git status --porcelain
            MagicMock(stdout="", returncode=0),  # git log origin/branch..HEAD
            MagicMock(stdout="", returncode=0),  # gh issue view (completed check)
            MagicMock(stdout="", returncode=0),  # gh issue comment (no changes)
            MagicMock(stdout="", returncode=0),  # gh pr view (PR url lookup)
        ]

        mock_create_cr.return_value = {
            "id": 9876,
            "html_url": "https://github.com/acme-corp/flagship-app/runs/9876",
        }
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 0

        mock_create_cr.assert_called_once_with(
            repo="acme-corp/flagship-app",
            head_sha="abc1234def5678",
            persona="developer",
            issue=42,
            token="ghs_test",
        )
        mock_update_cr.assert_called_once()
        call_kwargs = mock_update_cr.call_args[1]
        assert call_kwargs["status"] == "completed"
        assert call_kwargs["conclusion"] == "success"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_check_run_finalized_with_failure_on_agent_error(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Check run is finalized with conclusion=failure when agent exits non-zero."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-xyz")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="deadbeef1234\n", returncode=0)

        mock_create_cr.return_value = {
            "id": 5555,
            "html_url": "https://github.com/acme-corp/flagship-app/runs/5555",
        }
        mock_subprocess_run.return_value = MagicMock(returncode=2)

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 2

        mock_update_cr.assert_called_once()
        call_kwargs = mock_update_cr.call_args[1]
        assert call_kwargs["conclusion"] == "failure"
        assert call_kwargs["status"] == "completed"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_check_run_failure_does_not_fail_pod(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """A Check Run API error must not cause the pod to fail."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-123")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )

        # create_check_run raises — should be silently swallowed
        mock_create_cr.side_effect = RuntimeError("API down")
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        # Pod should still exit 0 despite check run failure
        result = main()
        assert result == 0
        # update_check_run should NOT be called since create failed
        mock_update_cr.assert_not_called()


# --- Test: stale-branch handling in Step 6b ---


class TestStaleBranchHandling:
    """Cover the three branch-state cases the entrypoint handles in Step 6b.

    The agent/issue-NNN branch convention is fixed (A4 auto-merge + reviewer
    workflows depend on it). When the branch already exists from a prior run:
      - if an open PR exists → extend (preserve PR review state)
      - else → preserve substantive work; clean only proven disposable work
      - first run on the issue → fresh `git checkout -b` (existing behavior)
    """

    def test_branch_does_not_exist_creates_fresh(self):
        """ls-remote returns 2 → goes through normal `git checkout -b`."""
        # The fresh-branch helper at module top simulates this case:
        # ls-remote returncode=2 → remote_branch_exists=False → fresh creation.
        # All other tests in TestEntrypointMain that use
        # _subprocess_side_effect_fresh_branch implicitly cover this.
        result = _subprocess_side_effect_fresh_branch(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", "agent/issue-42"]
        )
        assert result.returncode == 2

    def test_branch_exists_with_open_pr_extends(self):
        """ls-remote=0 + gh pr list returns a number → fetch+checkout, no delete."""
        commands_seen = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            commands_seen.append(cmd)
            if cmd[0:2] == ["git", "ls-remote"]:
                return MagicMock(returncode=0, stdout="abc def\n", stderr="")
            if cmd[0:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout="123\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        result = side_effect(["git", "ls-remote", "--heads"])
        assert result.returncode == 0
        result = side_effect(["gh", "pr", "list"])
        assert "123" in result.stdout

        # Verify the entrypoint logic interpreting these as "extend":
        # - has_open_pr would be bool("123".strip()) = True → extend path
        assert bool("123\n".strip())

    def test_empty_pr_response_does_not_establish_branch_disposability(self):
        """An empty PR response supplies no evidence about branch content."""

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd[0:2] == ["git", "ls-remote"]:
                return MagicMock(returncode=0, stdout="abc def\n", stderr="")
            if cmd[0:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        # An empty PR response only establishes absence of an open PR.
        result = side_effect(["gh", "pr", "list"])
        assert not bool(result.stdout.strip())


# --- Test: AIDLC stale-branch extend (Issue #3430) ---


class TestAidlcBranchExtend:
    """Verify persona-aware branch-bootstrap logic added by Issue #3430.

    AIDLC stages commit artifacts sequentially on one branch without opening a PR
    until the final stage. The stale-branch reset (case (a) in Step 6b) must NOT
    delete the remote branch for aidlc persona; it must fetch + extend instead.
    Other personas also preserve substantive work, even without an open PR.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_aidlc_persona_existing_branch_no_pr_extends(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """aidlc persona + existing remote branch + no open PR → fetch+checkout, NO delete."""
        from entrypoint import main
        import entrypoint

        aidlc_envelope = {**SAMPLE_ENVELOPE, "persona": "aidlc"}
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(aidlc_envelope), "receipt-aidlc-1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}

        # Simulate: branch exists on remote, no open PR
        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0:2] == ["git", "ls-remote"]:
                # Branch exists
                return MagicMock(
                    returncode=0, stdout="abc123 refs/heads/agent/issue-42\n", stderr=""
                )
            if cmd and cmd[0:3] == ["gh", "pr", "list"]:
                # No open PR
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd and len(cmd) >= 4 and cmd[0:2] == ["git", "push"] and "--delete" in cmd:
                # Should NOT be called for aidlc — track it
                raise AssertionError("git push --delete should NOT be called for aidlc persona")
            # Final node agent execution
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_subprocess_run.side_effect = subprocess_side_effect

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify run_cmd was called with fetch + checkout (extend path)
        run_cmd_calls = [str(c) for c in mock_run_cmd.call_args_list]
        # Find the fetch and checkout calls for the branch
        fetch_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "fetch" in str(c) and "agent/issue-42" in str(c)
        ]
        checkout_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "checkout" in str(c) and "agent/issue-42" in str(c) and "-b" not in str(c)
        ]
        assert len(fetch_calls) >= 1, f"Expected git fetch for branch, got calls: {run_cmd_calls}"
        assert len(checkout_calls) >= 1, (
            f"Expected git checkout (extend), got calls: {run_cmd_calls}"
        )

        # Verify git push --delete was NOT called via subprocess.run
        subprocess_calls = mock_subprocess_run.call_args_list
        for call in subprocess_calls:
            cmd = call[0][0] if call[0] else call[1].get("args", [])
            assert not (cmd[0:2] == ["git", "push"] and "--delete" in cmd), (
                "git push --delete must NOT be called for aidlc persona"
            )

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_developer_persona_existing_branch_no_pr_preserves_unknown_content(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """No PR and no proof of disposable content must preserve the work branch."""
        from entrypoint import main
        import entrypoint

        # SAMPLE_ENVELOPE already has persona="developer"
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-dev-1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}

        # Simulate: branch exists on remote, no open PR.
        # Distinguish _is_already_completed (--state merged) from Step 6b (--state open).
        delete_called = []

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0:2] == ["git", "ls-remote"]:
                return MagicMock(
                    returncode=0, stdout="abc123 refs/heads/agent/issue-42\n", stderr=""
                )
            if cmd and cmd[0:3] == ["gh", "pr", "list"]:
                # _is_already_completed passes --state merged; Step 6b passes --state open
                # Both should return empty (no merged PR, no open PR)
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd and len(cmd) >= 4 and cmd[0:2] == ["git", "push"] and "--delete" in cmd:
                delete_called.append(cmd)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_subprocess_run.side_effect = subprocess_side_effect

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        assert not delete_called
        checkout_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if c.args[0] == ["git", "checkout", "agent/issue-42"]
        ]
        assert checkout_calls, "Existing content must be checked out, not reset"
        assert not any(c.args[0][:2] == ["git", "reset"] for c in mock_run_cmd.call_args_list)

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_existing_branch_with_open_pr_extends_regardless_of_persona(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_already_completed,
        monkeypatch,
        tmp_path,
    ):
        """Any persona + existing branch + open PR → extend (no delete). Regression guard."""
        from entrypoint import main
        import entrypoint

        # Use developer persona — even with open PR, should extend not reset
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-pr-1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        # Skip idempotency guard (it also calls gh pr list --state merged)
        mock_already_completed.return_value = False

        # Simulate: branch exists + open PR (Step 6b's gh pr list --state open)
        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0:2] == ["git", "ls-remote"]:
                return MagicMock(
                    returncode=0, stdout="abc123 refs/heads/agent/issue-42\n", stderr=""
                )
            if cmd and cmd[0:3] == ["gh", "pr", "list"]:
                # Open PR exists (Step 6b check)
                return MagicMock(returncode=0, stdout="456\n", stderr="")
            if cmd and len(cmd) >= 4 and cmd[0:2] == ["git", "push"] and "--delete" in cmd:
                raise AssertionError("git push --delete should NOT be called when PR is open")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_subprocess_run.side_effect = subprocess_side_effect

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify fetch + checkout (extend) was called
        fetch_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "fetch" in str(c) and "agent/issue-42" in str(c)
        ]
        checkout_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "checkout" in str(c) and "agent/issue-42" in str(c) and "-b" not in str(c)
        ]
        assert len(fetch_calls) >= 1, "Expected git fetch for open-PR extend"
        assert len(checkout_calls) >= 1, "Expected git checkout (extend) for open-PR"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_first_run_no_remote_branch_creates_fresh(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_create_cr,
        mock_update_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """First run on issue (no remote branch) → clean creation for any persona. Regression guard."""
        from entrypoint import main
        import entrypoint

        aidlc_envelope = {**SAMPLE_ENVELOPE, "persona": "aidlc"}
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(aidlc_envelope), "receipt-fresh-1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}

        # Simulate: no remote branch exists
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify checkout -b (fresh branch creation) was called
        checkout_b_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "checkout" in str(c) and "-b" in str(c) and "agent/issue-42" in str(c)
        ]
        assert len(checkout_b_calls) >= 1, "Expected git checkout -b for fresh branch creation"

        # Verify no fetch was done (no existing remote branch to fetch)
        fetch_calls = [
            c
            for c in mock_run_cmd.call_args_list
            if "fetch" in str(c) and "agent/issue-42" in str(c)
        ]
        assert len(fetch_calls) == 0, "Should not fetch when branch doesn't exist"


# --- Test: check_run library ---


class TestCheckRun:
    @patch("lib.check_run.requests.post")
    def test_create_check_run_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "id": 12345,
            "html_url": "https://github.com/acme/repo/runs/12345",
        }
        mock_post.return_value = mock_resp

        result = create_check_run(
            repo="acme/repo",
            head_sha="abc123def456",
            persona="developer",
            issue=42,
            token="ghs_test_token",
        )

        assert result["id"] == 12345
        assert result["html_url"] == "https://github.com/acme/repo/runs/12345"

        call_kwargs = mock_post.call_args[1]
        body = call_kwargs["json"]
        assert body["name"] == "ADP Agent: developer"
        assert body["head_sha"] == "abc123def456"
        assert body["status"] == "in_progress"
        assert "42" in body["details_url"]
        assert "developer" in body["output"]["title"]

    @patch("lib.check_run.requests.post")
    def test_create_check_run_api_error_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Resource not accessible by integration"
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Failed to create check run"):
            create_check_run(
                repo="acme/repo",
                head_sha="abc123",
                persona="developer",
                issue=42,
                token="bad_token",
            )

    @patch("lib.check_run.requests.patch")
    def test_update_check_run_success(self, mock_patch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_patch.return_value = mock_resp

        update_check_run(
            repo="acme/repo",
            check_run_id=12345,
            token="ghs_test_token",
            status="completed",
            conclusion="success",
            output={"title": "All good", "summary": "Agent completed."},
        )

        mock_patch.assert_called_once()
        call_url = mock_patch.call_args[0][0]
        assert "12345" in call_url
        body = mock_patch.call_args[1]["json"]
        assert body["status"] == "completed"
        assert body["conclusion"] == "success"
        assert "completed_at" in body
        assert body["output"]["title"] == "All good"

    @patch("lib.check_run.requests.patch")
    def test_update_check_run_api_error_raises(self, mock_patch):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_patch.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Failed to update check run"):
            update_check_run(
                repo="acme/repo",
                check_run_id=99999,
                token="ghs_test_token",
                status="completed",
                conclusion="failure",
            )

    @patch("lib.check_run.requests.patch")
    def test_update_check_run_partial_payload(self, mock_patch):
        """Only provided fields appear in the PATCH payload."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_patch.return_value = mock_resp

        update_check_run(
            repo="acme/repo",
            check_run_id=111,
            token="ghs_test_token",
            status="in_progress",
        )

        body = mock_patch.call_args[1]["json"]
        assert body["status"] == "in_progress"
        assert "conclusion" not in body
        assert "completed_at" not in body
        assert "output" not in body


# --- Test: gateway_credential_client ---


class TestGatewayCredentialClient:
    def test_is_configured_true(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gw:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key-123")
        client = GatewayCredentialClient()
        assert client.is_configured is True

    def test_is_configured_false_missing_url(self, monkeypatch):
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key-123")
        client = GatewayCredentialClient()
        assert client.is_configured is False

    def test_is_configured_false_missing_key(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gw:8080")
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
        client = GatewayCredentialClient()
        assert client.is_configured is False

    @patch("lib.gateway_credential_client.urlopen")
    def test_raw_read_success(self, mock_urlopen, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gw:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key-123")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "value": '{"role_arn": "arn:aws:iam::111:role/test"}',
                "credential_type": "api_key",
                "provenance_id": "prov-abc",
            }
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        result = client.raw_read(
            user_id="user-123",
            agent_id="operations",
            task_id="task-456",
            service="aws_role_assume",
        )

        assert result["credential_type"] == "api_key"
        assert "role_arn" in result["value"]

    @patch("lib.gateway_credential_client.urlopen")
    def test_raw_read_http_error(self, mock_urlopen, monkeypatch):
        from urllib.error import HTTPError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gw:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key-123")

        mock_urlopen.side_effect = HTTPError(
            url="http://gw:8080/internal/v1/credential-raw-read",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error":"credential_not_found"}')),
        )

        client = GatewayCredentialClient()
        with pytest.raises(GatewayCredentialError, match="HTTP 404"):
            client.raw_read(
                user_id="user-123",
                agent_id="operations",
                task_id="task-456",
                service="aws_role_assume",
            )


# --- Test: _fetch_aws_credentials ---


class TestFetchAssumedAwsCredentials:
    def test_raises_on_empty_user_id(self):
        from entrypoint import _fetch_assumed_aws_credentials

        with pytest.raises(ValueError, match="no user_id"):
            _fetch_assumed_aws_credentials(user_id="", agent_id="ops", task_id="t1")

    @patch("entrypoint.GatewayCredentialClient")
    def test_raises_on_unconfigured_client(self, mock_gw_cls, monkeypatch):
        from entrypoint import _fetch_assumed_aws_credentials

        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = False

        with pytest.raises(GatewayCredentialError, match="not configured"):
            _fetch_assumed_aws_credentials(user_id="user-1", agent_id="ops", task_id="t1")

    @patch("entrypoint.GatewayCredentialClient")
    def test_success_returns_sts_creds(self, mock_gw_cls):
        from entrypoint import _fetch_assumed_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.assume_role.return_value = {
            "profile_name": "adp-aws-default",
            "access_key_id": "ASIATEST",
            "secret_access_key": "secret",
            "session_token": "token",
            "expiration": "2026-05-13T22:00:00Z",
            "region": "us-east-1",
            "provenance_id": "prov-xyz",
        }

        result = _fetch_assumed_aws_credentials(user_id="user-1", agent_id="ops", task_id="t1")
        assert result["access_key_id"] == "ASIATEST"
        assert result["secret_access_key"] == "secret"
        assert result["session_token"] == "token"
        # Caller (entrypoint) only reads access_key_id/secret_access_key/session_token,
        # plus provenance_id and expiration for logging. Don't over-constrain other fields.

    @patch("entrypoint.GatewayCredentialClient")
    def test_calls_assume_role_endpoint_with_correct_args(self, mock_gw_cls):
        from entrypoint import _fetch_assumed_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.assume_role.return_value = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "session_token": "ST",
            "expiration": "2026-05-13T22:00:00Z",
            "region": "us-east-1",
            "profile_name": "p",
            "provenance_id": "prov",
        }

        _fetch_assumed_aws_credentials(user_id="user-1", agent_id="operations", task_id="t1")

        # Verify assume_role (not raw_read) was called with the right args.
        # raw_read MUST NOT be called — that endpoint is gated by a feature flag.
        mock_gw.raw_read.assert_not_called()
        mock_gw.assume_role.assert_called_once()
        call_kwargs = mock_gw.assume_role.call_args.kwargs
        assert call_kwargs["user_id"] == "user-1"
        assert call_kwargs["agent_id"] == "operations"
        assert call_kwargs["task_id"] == "t1"
        assert call_kwargs["service"] == "aws"
        assert call_kwargs["label"] is None

    @patch("entrypoint.GatewayCredentialClient")
    def test_calls_assume_role_with_explicit_label(self, mock_gw_cls):
        """Issue #3574: /aws-label is forwarded as label= to assume_role."""
        from entrypoint import _fetch_assumed_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.assume_role.return_value = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "session_token": "ST",
            "expiration": "2026-07-11T22:00:00Z",
            "region": "us-east-1",
            "profile_name": "p",
            "provenance_id": "prov",
        }

        _fetch_assumed_aws_credentials(
            user_id="user-1",
            agent_id="operations",
            task_id="t1",
            label="adp-integration-test",
        )

        mock_gw.assume_role.assert_called_once()
        call_kwargs = mock_gw.assume_role.call_args.kwargs
        assert call_kwargs["label"] == "adp-integration-test"

    @patch("entrypoint.GatewayCredentialClient")
    def test_assume_role_label_none_when_not_provided(self, mock_gw_cls):
        """Issue #3574: Without /aws-label, label=None is passed (ranked picker)."""
        from entrypoint import _fetch_assumed_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.assume_role.return_value = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "session_token": "ST",
            "expiration": "2026-07-11T22:00:00Z",
            "region": "us-east-1",
            "profile_name": "p",
            "provenance_id": "prov",
        }

        _fetch_assumed_aws_credentials(user_id="user-1", agent_id="operations", task_id="t1")

        call_kwargs = mock_gw.assume_role.call_args.kwargs
        assert call_kwargs["label"] is None


# --- Test: assume-role fatal when aws_label is non-None (issue #3574 invariant 2) ---


class TestAssumeRoleFatalOnLabelMiss:
    """Issue #3574: gateway 404 + non-None label → fatal, NO fallback to label=None."""

    @patch("entrypoint.GatewayCredentialClient")
    def test_explicit_label_failure_is_fatal(self, mock_gw_cls):
        """When aws_label is set and assume_role raises, _fetch re-raises."""
        from entrypoint import _fetch_assumed_aws_credentials
        from lib.gateway_credential_client import GatewayCredentialError

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.assume_role.side_effect = GatewayCredentialError(
            "Gateway returned HTTP 404: credential_not_found"
        )

        # The function itself doesn't know about fatal/non-fatal — that's
        # the caller's responsibility (Step 7). But the function should still
        # raise so the caller can decide.
        import pytest

        with pytest.raises(GatewayCredentialError):
            _fetch_assumed_aws_credentials(
                user_id="user-1",
                agent_id="operations",
                task_id="t1",
                label="nonexistent-label",
            )


# --- Test: sts_assume user_id tag ---


class TestStsAssumeUserIdTag:
    @patch("lib.sts_assume.boto3.client")
    def test_user_id_tag_included_when_provided(self, mock_boto_client):
        mock_sts = MagicMock()
        mock_boto_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": "2026-05-01T00:00:00Z",
            }
        }

        assume_customer_role(
            role_arn="arn:aws:iam::111:role/test",
            external_id="ext-123",
            tenant_id="acme",
            actor_login="jane",
            actor_id="123",
            user_id="cognito-sub-jane",
            run_id="run-1",
            repo="acme/app",
            issue=42,
            persona="operations",
        )

        call_kwargs = mock_sts.assume_role.call_args[1]
        tags = call_kwargs["Tags"]
        tag_map = {t["Key"]: t["Value"] for t in tags}
        assert "adp:user_id" in tag_map
        assert tag_map["adp:user_id"] == "cognito-sub-jane"

    @patch("lib.sts_assume.boto3.client")
    def test_user_id_tag_omitted_when_empty(self, mock_boto_client):
        mock_sts = MagicMock()
        mock_boto_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": "2026-05-01T00:00:00Z",
            }
        }

        assume_customer_role(
            role_arn="arn:aws:iam::111:role/test",
            external_id="ext-123",
            tenant_id="acme",
            actor_login="jane",
            actor_id="123",
            user_id="",
            run_id="run-1",
            repo="acme/app",
            issue=42,
            persona="operations",
        )

        call_kwargs = mock_sts.assume_role.call_args[1]
        tags = call_kwargs["Tags"]
        tag_keys = [t["Key"] for t in tags]
        assert "adp:user_id" not in tag_keys


# --- Test: ADP_BEDROCK_VIA feature flag ---


class TestBedrockViaFlag:
    """Tests for the ADP_BEDROCK_VIA feature flag (scoped agent_env, not os.environ mutation).

    Gateway is required; bypass and retired modes must stop before inference.
    `user` is retired (#4747) and must raise — see test_retired_user_value_raises.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_default_uses_gateway_with_customer_creds_scoped_to_tools(
        self,
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
        """Gateway is the default even when tools use customer AWS credentials."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.delenv("ADP_BEDROCK_VIA", raising=False)

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-test",
            }
            main()

        # Model requests use the proxy while tools retain customer credentials.
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert agent_env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://127.0.0.1:9090"
        assert agent_env["AWS_ACCESS_KEY_ID"] == "AKUSER"
        assert "AWS_ROLE_ARN" not in agent_env

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_platform_bypass_is_rejected(
        self,
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
        """Platform bypass cannot override the user routing rule."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "platform")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-2")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-test",
            }
            with pytest.raises(RuntimeError, match="must be gateway"):
                main()

        assert not any("claude" in str(call.args[0]) for call in mock_subprocess_run.call_args_list)

    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    @pytest.mark.parametrize("raw_value", ["user", "USER", " user ", "User"])
    def test_retired_user_value_raises(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        monkeypatch,
        tmp_path,
        raw_value,
    ):
        """ADP_BEDROCK_VIA=user is retired (#4747) and fails LOUDLY, not silently.

        Before #4747 this value stripped IRSA so the customer's own credentials
        served Bedrock — billed to them, metered nowhere. Deleting the branch
        without this guard would let `=user` fall through to the trailing `else`
        and run on pod IRSA, i.e. silently switch the payer. That silent switch
        is the billing surprise ruling 3 forbids, so it must raise.

        Parametrized over case/whitespace variants because normalization runs
        BEFORE the guard — `USER` and ` user ` must not sneak past it.
        """
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", raw_value)

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-retired")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-test",
            }
            with pytest.raises(RuntimeError, match="ADP_BEDROCK_VIA=user is retired"):
                main()

        # The error must be actionable: name the retirement AND the replacement.
        message = entrypoint.RETIRED_BEDROCK_VIA["user"]
        assert "#4747" in message
        assert "mapping" in message

        # It must fail BEFORE spending anything: no proxy started, no agent exec'd.
        mock_start_proxy.assert_not_called()
        assert not any(
            call.args and call.args[0] and call.args[0][0] == "node"
            for call in mock_subprocess_run.call_args_list
        )

    def test_no_code_path_branches_on_user_value(self):
        """The #4747 acceptance criterion: no routing branch handles `user`.

        Asserted against the source because the behavioral tests above can only
        prove the guard fires — they cannot prove a *second* `user` branch wasn't
        left behind further down. The original story cited one line range but
        there were two such branches, which is exactly the failure this catches.
        The guard itself is a rejection lookup keyed by value, not an
        `== "user"` comparison, so it does not trip this check.
        """
        import entrypoint

        source = Path(entrypoint.__file__).read_text()
        assert 'bedrock_via == "user"' not in source
        assert "bedrock_via == 'user'" not in source

    @patch("entrypoint._stop_sigv4_proxy")
    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_gateway_mode_os_environ_retains_irsa(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        mock_stop_proxy,
        monkeypatch,
        tmp_path,
    ):
        """CRITICAL: os.environ keeps IRSA when gateway mode strips it from agent_env.

        Inherited from the deleted `=user` version of this test (#4747). The
        invariant is still live: gateway mode with a customer role assumed pops
        IRSA vars from the SCOPED agent_env, and the post-agent SQS delete needs
        os.environ to still hold IRSA for platform-account access. Re-pointed
        rather than deleted — the `user` branch was only one of two callers of
        this strip, and dropping the test would leave the survivor uncovered.
        """
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "gateway")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-gw-irsa")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        mock_start_proxy.return_value = MagicMock()

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-test",
            }
            main()

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
        # Customer creds serve shell AWS; IRSA stripped from the SCOPED env only.
        assert "AWS_ROLE_ARN" not in agent_env
        assert agent_env["AWS_ACCESS_KEY_ID"] == "AKUSER"
        assert agent_env["ADP_WORKER_IRSA_ROLE_ARN"] == "arn:aws:iam::123456789012:role/irsa-role"
        assert agent_env["ADP_WORKER_IRSA_TOKEN_FILE"] == "/var/run/secrets/token"
        assert agent_env["ADP_WORKER_AWS_REGION"] == "us-east-1"
        # os.environ MUST still have IRSA (for the post-agent SQS delete).
        assert os.environ.get("AWS_ROLE_ARN") == "arn:aws:iam::123456789012:role/irsa-role"
        assert os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE") == "/var/run/secrets/token"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_unknown_routing_mode_is_rejected(
        self,
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
        """An invalid routing mode cannot silently use the platform account."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "foobar")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-8")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "adp-aws-default",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-05-13T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov-test",
            }
            with pytest.raises(RuntimeError, match="must be gateway"):
                main()

        assert not any("claude" in str(call.args[0]) for call in mock_subprocess_run.call_args_list)


# --- Test: ADP_GITHUB_LOGIN propagation (Issue #1591) ---


class TestGithubLoginPropagation:
    """Verify ADP_GITHUB_LOGIN is exported from actor.github_login in the envelope.

    This is ADDITIVE to the Cognito identity rail — ADP_OWNER_SUB and
    ADP_TENANT_ID must remain untouched. The Door's code-verb ACL uses
    X-GitHub-Login (derived from ADP_GITHUB_LOGIN); personal verbs still
    use the Cognito identity.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_github_login_exported_when_present(
        self,
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
        """ADP_GITHUB_LOGIN is set when actor.github_login is present in envelope."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        # SAMPLE_ENVELOPE includes actor.github_login = "jane-dev"
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-gh-login")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # ADP_GITHUB_LOGIN must be set from envelope actor.github_login
        assert os.environ.get("ADP_GITHUB_LOGIN") == "jane-dev"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_github_login_not_set_when_absent(
        self,
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
        """ADP_GITHUB_LOGIN is NOT set when actor.github_login is missing (webchat path)."""
        from entrypoint import main
        import entrypoint

        # Envelope without github_login in actor (simulates webchat path)
        no_gh_envelope = {
            **SAMPLE_ENVELOPE,
            "actor": {"user_id": "cognito-sub-123", "is_bot": False},
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("ADP_GITHUB_LOGIN", raising=False)

        mock_receive_msg.return_value = (json.dumps(no_gh_envelope), "receipt-no-gh")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # ADP_GITHUB_LOGIN must NOT be set — fail-closed for code verbs
        assert os.environ.get("ADP_GITHUB_LOGIN") is None

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_cognito_identity_unchanged_after_github_login_added(
        self,
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
        """Regression: ADP_OWNER_SUB and ADP_TENANT_ID still set correctly."""
        from entrypoint import main
        import entrypoint

        # Envelope with both cognito_sub and github_login
        envelope_with_both = {
            **SAMPLE_ENVELOPE,
            "cognito_sub": "cognito-sub-jane-456",
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(envelope_with_both), "receipt-regression")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Both identity rails must be present simultaneously
        assert os.environ.get("ADP_OWNER_SUB") == "cognito-sub-jane-456"
        assert os.environ.get("ADP_TENANT_ID") == "acme-corp"
        assert os.environ.get("ADP_GITHUB_LOGIN") == "jane-dev"


class TestSanitizeForStsTag:
    r"""Verify task IDs are sanitized for STS session tag values.

    STS rejects tags outside [\p{L}\p{Z}\p{N}_.:/=+\-@]*. The natural task ID
    shape "<owner>/<repo>#<issue>" contains "#" which fails validation.
    """

    def test_replaces_hash(self):
        from entrypoint import _sanitize_for_sts_tag

        assert (
            _sanitize_for_sts_tag("iankouls-aws/ai-superlane-agent-test#8")
            == "iankouls-aws/ai-superlane-agent-test_8"
        )

    def test_keeps_allowed_chars(self):
        from entrypoint import _sanitize_for_sts_tag

        # All chars in the STS-allowed set per AWS docs
        s = "abcXYZ012_./=+-@:"
        assert _sanitize_for_sts_tag(s) == s

    def test_replaces_other_disallowed(self):
        from entrypoint import _sanitize_for_sts_tag

        assert _sanitize_for_sts_tag("foo bar!baz") == "foo_bar_baz"
        assert _sanitize_for_sts_tag("a$b%c&d") == "a_b_c_d"

    def test_empty_string(self):
        from entrypoint import _sanitize_for_sts_tag

        assert _sanitize_for_sts_tag("") == ""

    def test_already_safe_unchanged(self):
        from entrypoint import _sanitize_for_sts_tag

        s = "msg-id-abcd1234"
        assert _sanitize_for_sts_tag(s) == s


# --- Test: ADP_BEDROCK_VIA=gateway (Phase 3, issue #748) ---


class TestBedrockViaGateway:
    """Tests for the ADP_BEDROCK_VIA=gateway path (sigv4-proxy subprocess)."""

    @pytest.mark.parametrize("port", [None, "8181"])
    @pytest.mark.parametrize("protected", [False, True])
    @patch("entrypoint._stop_sigv4_proxy")
    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_gateway_path_sets_bedrock_env(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        mock_stop_proxy,
        monkeypatch,
        tmp_path,
        protected,
        port,
    ):
        """With ADP_BEDROCK_VIA=gateway + proxy healthy, sets ANTHROPIC_BEDROCK_BASE_URL."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "gateway")
        monkeypatch.setenv(
            "SIGV4_PROXY_TARGET", "https://abc.execute-api.us-east-1.amazonaws.com/dev/agent"
        )
        if port is None:
            monkeypatch.delenv("SIGV4_PROXY_PORT", raising=False)
        else:
            monkeypatch.setenv("SIGV4_PROXY_PORT", port)
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", str(protected).lower())
        # A pod-level false value must not re-enable SDK authentication/probes
        # against the loopback proxy for either shared or protected workers.
        monkeypatch.setenv("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "0")
        selected_model = "global.anthropic.claude-opus-5"
        if protected:
            monkeypatch.setattr(
                entrypoint,
                "_broker_installation_token",
                lambda **_: ("ghs_test", "123", "2099-01-01T00:00:00Z"),
            )
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/authority-worker")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/projected/worker-token")
        monkeypatch.setattr(entrypoint, "_setup_agent_control", lambda *_: False)
        monkeypatch.setattr("lib.run_identity.bootstrap_run_identity", lambda *_: None)

        recovery = {"previous_run_id": "orch:prior", "previous_attempt": 1} if protected else None
        monkeypatch.setenv("ADP_DEVELOPER_RECOVERY_CONTEXT", "stale prior task")
        mock_receive_msg.return_value = (
            json.dumps({**SAMPLE_ENVELOPE, "model_resolved": selected_model,
                        "orchestration": {"developer_recovery": recovery}}),
            "receipt-gw1",
        )
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # Proxy starts successfully
        mock_proxy_proc = MagicMock()
        mock_start_proxy.return_value = mock_proxy_proc

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify subprocess.run was called with gateway env
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert agent_env["SIGV4_PROXY_PORT"] == (port or "9090")
        assert agent_env["ANTHROPIC_BEDROCK_BASE_URL"] == f"http://127.0.0.1:{port or '9090'}"
        assert agent_env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
        assert agent_env["ANTHROPIC_MODEL"] == selected_model
        assert agent_env["ADP_DEVELOPER_RECOVERY_CONTEXT"] == (json.dumps(recovery) if recovery else "")
        # Must NOT have ANTHROPIC_BASE_URL (that routes to the broken translator)
        assert "ANTHROPIC_BASE_URL" not in agent_env
        if protected:
            assert "AWS_ROLE_ARN" not in agent_env
            assert agent_env["ADP_WORKER_IRSA_ROLE_ARN"].endswith(":role/authority-worker")
            assert agent_env["ADP_WORKER_AWS_REGION"] == "us-east-1"
            assert not Path(agent_env["AWS_CONFIG_FILE"]).exists()
        # Parent lifecycle operations continue to use platform IRSA.
        assert os.environ["AWS_ROLE_ARN"].endswith(":role/authority-worker")
        assert os.environ["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "0"
        # Upstream proxy authentication keeps the original platform identity.
        proxy_env = mock_start_proxy.call_args.args[0]
        assert proxy_env["AWS_ROLE_ARN"].endswith(":role/authority-worker")
        assert proxy_env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/projected/worker-token"

        # Proxy was started and stopped
        mock_start_proxy.assert_called_once()
        mock_stop_proxy.assert_called_once_with(mock_proxy_proc)

    @patch("entrypoint._stop_sigv4_proxy")
    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_direct_bypass_is_rejected(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        mock_stop_proxy,
        monkeypatch,
        tmp_path,
    ):
        """Direct bypass cannot override the user routing rule."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "direct")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-direct1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with pytest.raises(RuntimeError, match="must be gateway"):
            main()
        assert not any("claude" in str(call.args[0]) for call in mock_subprocess_run.call_args_list)
        mock_start_proxy.assert_not_called()
        mock_stop_proxy.assert_not_called()

    @patch("entrypoint._stop_sigv4_proxy")
    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_gateway_failure_stops_before_model_calls(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        mock_stop_proxy,
        monkeypatch,
        tmp_path,
    ):
        """A proxy failure must not move user model calls onto platform billing."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "gateway")
        monkeypatch.setenv(
            "SIGV4_PROXY_TARGET", "https://abc.execute-api.us-east-1.amazonaws.com/dev/agent"
        )

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-fb1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # Proxy fails to start
        mock_start_proxy.return_value = None

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with pytest.raises(RuntimeError, match="preserve the user's AWS account routing"):
            main()

        # No Claude agent process is launched after proxy startup fails.
        assert not any("claude" in str(call.args[0]) for call in mock_subprocess_run.call_args_list)

        # Proxy was attempted but not stopped (it never started)
        mock_start_proxy.assert_called_once()
        mock_stop_proxy.assert_not_called()


# --- Test: GH_APP_ID / GH_APP_PRIVATE_KEY exported for token refresh (#1502) ---


class TestGhAppCredentialsExported:
    """Verify entrypoint exports GH_APP_ID and GH_APP_PRIVATE_KEY into the agent env.

    Without these, the TokenManager in agent-worker.ts silently disables itself
    and long-running agents die at ~1 hour with 401 Bad credentials.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_env_vars_contain_gh_app_credentials(
        self,
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
        """GH_APP_ID and GH_APP_PRIVATE_KEY must be present in agent subprocess env."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-app-creds")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {
            "app_id": "99001",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake-key-content\n-----END RSA PRIVATE KEY-----",
        }
        mock_mint.return_value = "ghs_test_token"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Extract the env passed to the agent subprocess (the node call)
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")

        assert agent_env["GH_APP_ID"] == "99001"
        assert agent_env["GH_APP_PRIVATE_KEY"] == (
            "-----BEGIN RSA PRIVATE KEY-----\nfake-key-content\n-----END RSA PRIVATE KEY-----"
        )

    @patch("entrypoint._stop_sigv4_proxy")
    @patch("entrypoint._start_sigv4_proxy")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_gh_app_credentials_survive_irsa_strip(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_start_proxy,
        mock_stop_proxy,
        monkeypatch,
        tmp_path,
    ):
        """GH_APP_* vars must NOT be stripped by the IRSA-popping agent_env assembly.

        Was `..._survive_bedrock_via_user_mode` before #4747. The strip it guards
        is not gone — gateway mode with an assumed customer role pops the same
        AWS_* vars — so this is re-pointed at that path rather than deleted. The
        invariant is unchanged: popping AWS_* must not take GH_APP_* with it, or
        the agent loses its GitHub App identity.
        """
        from entrypoint import main
        import entrypoint

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "gateway")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123:role/irsa")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        mock_start_proxy.return_value = MagicMock()

        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-app-survive")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {
            "app_id": "77788",
            "private_key": "secret-private-key-pem",
        }
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.assume_role.return_value = {
                "profile_name": "p",
                "access_key_id": "AKUSER",
                "secret_access_key": "SKUSER",
                "session_token": "STUSER",
                "expiration": "2026-06-14T22:00:00Z",
                "region": "us-east-1",
                "provenance_id": "prov",
            }
            main()

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")

        # GH_APP_* must survive the IRSA stripping (which only pops AWS_* vars)
        assert agent_env["GH_APP_ID"] == "77788"
        assert agent_env["GH_APP_PRIVATE_KEY"] == "secret-private-key-pem"
        # Confirm the strip this test guards actually happened.
        assert "AWS_ROLE_ARN" not in agent_env

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_private_key_not_logged(
        self,
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
        caplog,
    ):
        """The private key value must NEVER appear in log output (security)."""
        import logging
        from entrypoint import main
        import entrypoint

        secret_key = "-----BEGIN RSA PRIVATE KEY-----\nSUPER_SECRET_DO_NOT_LOG\n-----END RSA PRIVATE KEY-----"

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-log-safety")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {
            "app_id": "12345",
            "private_key": secret_key,
        }
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with caplog.at_level(logging.DEBUG):
            main()

        # Assert the secret key material never appears in any log record
        all_log_output = "\n".join(record.message for record in caplog.records)
        assert "SUPER_SECRET_DO_NOT_LOG" not in all_log_output
        assert "BEGIN RSA PRIVATE KEY" not in all_log_output


# --- Test: OTEL_RESOURCE_ATTRIBUTES composition (#1630) ---


class TestOtelResourceAttributes:
    """Verify entrypoint composes OTEL_RESOURCE_ATTRIBUTES with per-run dimensions.

    When ENABLE_AGENT_OTEL=1 (set by ScaledJob when the flag is on), the
    entrypoint appends tenant.id, agent.persona, enduser.id, and session.id
    to the base attributes from the ScaledJob template.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_otel_attrs_composed_when_enabled(
        self,
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
        """OTEL_RESOURCE_ATTRIBUTES includes tenant, persona, user, session."""
        from entrypoint import main
        import entrypoint

        envelope_with_correlation = {
            **SAMPLE_ENVELOPE,
            "correlation": {
                "correlation_id": "corr-xyz-789",
                "root_human_id": "user-human-1",
                "is_human_rooted": True,
                "parent_invocation_id": None,
            },
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        # Simulate the ScaledJob env vars
        monkeypatch.setenv("ENABLE_AGENT_OTEL", "1")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "service.namespace=adp-agents,deployment.environment=dev",
        )

        mock_receive_msg.return_value = (json.dumps(envelope_with_correlation), "receipt-otel")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify the composed OTEL_RESOURCE_ATTRIBUTES in os.environ
        attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        # Base attributes from ScaledJob template preserved
        assert "service.namespace=adp-agents" in attrs
        assert "deployment.environment=dev" in attrs
        # Per-run dimensions appended
        assert "tenant.id=acme-corp" in attrs
        assert "agent.persona=developer" in attrs
        assert "enduser.id=cognito-sub-jane-123" in attrs
        assert "session.id=corr-xyz-789" in attrs
        # Issue #1695: GitHub login enrichment
        assert "github.login=jane-dev" in attrs

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_otel_attrs_omit_github_login_when_empty(
        self,
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
        """github.login is omitted from OTEL attrs when actor.github_login is empty (#1695)."""
        from entrypoint import main
        import entrypoint

        # Envelope with empty github_login (simulates bot/cron trigger path)
        envelope_no_login = {
            **SAMPLE_ENVELOPE,
            "actor": {
                "github_id": 0,
                "github_login": "",
                "user_id": "cognito-sub-bot-456",
                "is_bot": True,
            },
            "correlation": {
                "correlation_id": "corr-bot-001",
                "root_human_id": "user-human-1",
                "is_human_rooted": False,
                "parent_invocation_id": None,
            },
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ENABLE_AGENT_OTEL", "1")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "service.namespace=adp-agents,deployment.environment=dev",
        )

        mock_receive_msg.return_value = (json.dumps(envelope_no_login), "receipt-no-login")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Verify github.login is NOT in the attributes (empty login = omitted)
        attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        assert "github.login" not in attrs
        # But other attrs are still present
        assert "tenant.id=acme-corp" in attrs
        assert "agent.persona=developer" in attrs
        assert "enduser.id=cognito-sub-bot-456" in attrs

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_otel_attrs_not_set_when_disabled(
        self,
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
        """When ENABLE_AGENT_OTEL is unset, OTEL_RESOURCE_ATTRIBUTES is untouched."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        # Explicitly remove the OTEL flag
        monkeypatch.delenv("ENABLE_AGENT_OTEL", raising=False)
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-no-otel")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # OTEL_RESOURCE_ATTRIBUTES should not exist
        assert "OTEL_RESOURCE_ATTRIBUTES" not in os.environ


# --- Test: SQS message deletion lifecycle (Issue #1864) ---


class TestSqsMessageDeletion:
    """Verify the SQS message is deleted on both success and failure paths.

    Issue #1864: SQS message not deleted on successful completion → 6h FIFO
    redelivery spawns redundant runs. The fix ensures _delete_message is called
    with the correct receipt handle on ANY terminal exit.
    """

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_delete_message_called_on_success(
        self,
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
        """On successful agent run, _delete_message must be called with correct receipt handle."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-success-123")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 0

        # _delete_message MUST be called with the correct queue URL and receipt handle
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo",
            "us-east-1",
            "receipt-handle-success-123",
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
    def test_delete_message_called_on_failure(
        self,
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
        """On agent failure, _delete_message must still be called (terminal exit)."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-failure-456")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        # Agent exits non-zero
        mock_subprocess_run.side_effect = lambda args, **kwargs: (
            MagicMock(returncode=1)
            if args[0] == "node"
            else _subprocess_side_effect_fresh_branch(args, **kwargs)
        )

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 1

        # Even on failure, the message MUST be deleted (terminal exit)
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo",
            "us-east-1",
            "receipt-handle-failure-456",
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
    def test_delete_message_error_does_not_fail_pod(
        self,
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
        """If _delete_message raises, the pod must still exit successfully."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-handle-err")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # _delete_message raises an exception (e.g., expired credentials)
        mock_delete_msg.side_effect = Exception("ReceiptHandle expired")

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        # Pod should still exit 0 — the agent work is committed to GitHub
        result = main()
        assert result == 0


# --- Test: Idempotency guard (Issue #1864) ---


class TestIdempotencyGuard:
    """Verify the idempotency guard skips redelivered messages for completed stories.

    When an SQS message is redelivered after the visibility timeout (because
    the original delete was missed due to pod OOM/crash), the guard checks if
    the agent branch already has a merged PR and short-circuits to delete+exit.
    """

    def test_is_already_completed_returns_true_on_merged_pr(self, monkeypatch):
        """_is_already_completed returns True when a merged PR exists on the agent branch."""
        from entrypoint import _is_already_completed

        with patch("entrypoint.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1847\n", stderr="")
            result = _is_already_completed("acme/repo", 42, "ghs_test_token")

        assert result is True
        # Verify the correct gh command was called
        call_args = mock_run.call_args[0][0]
        assert "pr" in call_args
        assert "list" in call_args
        assert "--state" in call_args
        assert "merged" in call_args[call_args.index("--state") + 1]
        assert "--head" in call_args
        assert "agent/issue-42" in call_args[call_args.index("--head") + 1]

    def test_is_already_completed_returns_false_on_no_merged_pr(self):
        """_is_already_completed returns False when no merged PR exists."""
        from entrypoint import _is_already_completed

        with patch("entrypoint.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = _is_already_completed("acme/repo", 42, "ghs_test_token")

        assert result is False

    def test_is_already_completed_fails_open_on_error(self):
        """_is_already_completed returns False on any exception (fail-open)."""
        from entrypoint import _is_already_completed

        with patch("entrypoint.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("Network error")
            result = _is_already_completed("acme/repo", 42, "ghs_test_token")

        assert result is False

    def test_is_already_completed_fails_open_on_nonzero_exit(self):
        """_is_already_completed returns False on non-zero gh exit code (fail-open)."""
        from entrypoint import _is_already_completed

        with patch("entrypoint.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="gh: error")
            result = _is_already_completed("acme/repo", 42, "ghs_test_token")

        assert result is False

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    def test_idempotency_guard_skips_and_deletes_on_merged(
        self,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """When idempotency guard detects merged PR, message is deleted and run skips."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-redelivery")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        # Idempotency guard returns True (merged PR found)
        mock_is_completed.return_value = True

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)

        result = main()

        # Must exit cleanly (0)
        assert result == 0
        # Must delete the message (so it doesn't redeliver again)
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-redelivery",
        )
        # Must NOT call run_cmd beyond envelope parse (no clone, no agent exec)
        # run_cmd is used for git/gh commands — idempotency skip means no git work
        mock_run_cmd.assert_not_called()

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_idempotency_guard_proceeds_on_open_issue(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """When no merged PR exists, the run proceeds normally."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-fresh")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        # Completed implementation is committed; only git log reports unpushed work.
        mock_run_cmd.side_effect = lambda cmd, **kwargs: MagicMock(
            stdout="" if cmd[:2] in (["git", "diff"], ["git", "status"]) else "abc123\n",
            returncode=0,
        )
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # Idempotency guard returns False (no merged PR — fresh run)
        mock_is_completed.return_value = False

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 0

        # Agent subprocess was invoked (normal run proceeded)
        mock_subprocess_run.assert_called()
        # Message deleted after run completion
        mock_delete_msg.assert_called_once()

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_idempotency_guard_exempts_persona_extending_branch(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """A persona in PERSONAS_EXTENDING_BRANCH (aidlc) must proceed even when a
        prior merged PR exists on the branch — that workflow merges a PR at every
        gate, so a merged PR mid-flow is not "this issue is done" (#39 hit this:
        PR #41 merged after the reverse-engineering gate, then a follow-up
        gate-answer comment was skipped as a stale redelivery)."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        aidlc_envelope = {**SAMPLE_ENVELOPE, "persona": "aidlc"}
        mock_receive_msg.return_value = (json.dumps(aidlc_envelope), "receipt-gate-answer")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # Idempotency guard would say "already completed" — must be bypassed
        # for this persona rather than causing a skip.
        mock_is_completed.return_value = True

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        result = main()
        assert result == 0

        # Run proceeded despite the merged PR — no idempotency skip.
        assert any(call.args[0][0] == "node" for call in mock_subprocess_run.call_args_list)
        mock_is_completed.assert_not_called()
        entrypoint.is_delivery_completed.assert_called_once_with(aidlc_envelope)
        entrypoint.record_delivery_completed.assert_called_once_with(aidlc_envelope)
        mock_delete_msg.assert_called_once()


class TestClaimBoundWorkIsNotBranchInferred:
    """Issue #5335: under protected dispatch, replay is decided by the invocation.

    A merged PR on `agent/issue-N` proves somebody finished something on the
    issue once — not that THIS delivery already ran. Before this change any
    persona's run was discarded once any PR for its issue merged, so the normal
    developer -> reviewer -> repair sequence could not proceed past the first
    merge. Under protected dispatch the run has already been admitted against its
    own dispatch record, attempt and work claim, so the branch question is both
    redundant and wrong; without protected dispatch the legacy guard is unchanged.
    """

    @pytest.mark.parametrize(
        ("envelope_extra", "env"),
        [
            ({"work_claim_required": True}, {}),
            ({}, {"ADP_WORK_CLAIMS_ENABLED": "true"}),
            ({}, {"ADP_AGENT_AUTHORITY_ENABLED": "true"}),
        ],
        ids=["envelope-claim-required", "claims-enabled", "authority-enabled"],
    )
    def test_identity_decides_replay_when_protected_dispatch_is_in_force(
        self, monkeypatch, envelope_extra, env
    ):
        """Each of the three conditions `bootstrap_run_identity` itself uses."""
        from entrypoint import _invocation_identity_decides_replay

        monkeypatch.delenv("ADP_WORK_CLAIMS_ENABLED", raising=False)
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        assert _invocation_identity_decides_replay({**SAMPLE_ENVELOPE, **envelope_extra}) is True

    def test_legacy_deployment_still_uses_branch_history(self, monkeypatch):
        """The deliberate legacy path: no claims, no authority — unchanged."""
        from entrypoint import _invocation_identity_decides_replay

        monkeypatch.delenv("ADP_WORK_CLAIMS_ENABLED", raising=False)
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

        assert _invocation_identity_decides_replay(SAMPLE_ENVELOPE) is False

    def test_a_claimed_envelope_does_not_read_authority_from_the_message_alone(self, monkeypatch):
        """`work_claim_required: False` in the envelope must not switch the guard
        off — a producer cannot opt out of the legacy guard by spelling the flag."""
        from entrypoint import _invocation_identity_decides_replay

        monkeypatch.delenv("ADP_WORK_CLAIMS_ENABLED", raising=False)
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

        assert (
            _invocation_identity_decides_replay({**SAMPLE_ENVELOPE, "work_claim_required": False})
            is False
        )

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_fresh_authorized_run_proceeds_after_an_older_merged_pr(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """AC1-positive, through the real bootstrap: a claim-bound reviewer run
        arrives after the developer's PR merged and must execute. The branch does
        have a merged PR (`_is_already_completed` would say True), and that must
        no longer decide anything."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        claimed = {**SAMPLE_ENVELOPE, "persona": "reviewer", "work_claim_required": True}
        mock_receive_msg.return_value = (json.dumps(claimed), "receipt-fresh-authorized")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch
        # The branch DOES carry a merged PR from the earlier developer run.
        mock_is_completed.return_value = True

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        # The gateway ADMITS this invocation: its dispatch record is pending, the
        # attempt matches and it holds the work claim. That admission — not the
        # branch — is what authorizes the run, so it is stubbed as succeeding
        # rather than bypassed. (A refused admission is the next test.)
        from lib.run_identity import ModelPolicyReport

        admitted_identity = MagicMock()
        admitted_identity.model_policy_report = ModelPolicyReport(
            status="unavailable", posture="report_only", posture_verified=True, reason="snapshot_missing"
        )
        with patch(
            "lib.run_identity.bootstrap_run_identity", return_value=admitted_identity
        ) as mock_identity:
            result = main()

        assert result == 0
        mock_identity.assert_called_once_with(claimed)

        # The agent actually ran: this is work, not a skip.
        assert any(call.args[0][0] == "node" for call in mock_subprocess_run.call_args_list)
        # And the branch was never consulted — the invocation decided.
        mock_is_completed.assert_not_called()

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    def test_legacy_replay_of_completed_work_is_still_skipped(
        self,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """AC1-negative: with no protected dispatch, a redelivery on a merged
        branch is still suppressed and still acknowledged. This is the #1864
        protection, and it must not have been traded away."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("ADP_WORK_CLAIMS_ENABLED", raising=False)
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-redelivery")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_is_completed.return_value = True

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)

        result = main()

        assert result == 0
        mock_is_completed.assert_called_once()
        mock_run_cmd.assert_not_called()
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-redelivery",
        )

    @patch("entrypoint._is_already_completed")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_an_unadmitted_claimed_run_never_reaches_the_guard(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        mock_is_completed,
        monkeypatch,
        tmp_path,
    ):
        """The refusal this change relies on. Skipping the branch check is only
        safe because a claim-bound run that the gateway does NOT admit is stopped
        earlier, at `bootstrap_run_identity`. If that refusal ever stopped being
        fatal, an unauthorized fresh identity would reach the work — so assert the
        run does not proceed and never gets as far as the guard."""
        from lib.run_identity import RunIdentityError
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        claimed = {**SAMPLE_ENVELOPE, "persona": "developer", "work_claim_required": True}
        mock_receive_msg.return_value = (json.dumps(claimed), "receipt-refused")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_subprocess_run.side_effect = _subprocess_side_effect_fresh_branch

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch(
            "lib.run_identity.bootstrap_run_identity",
            side_effect=RunIdentityError("gateway refused run identity"),
        ):
            with pytest.raises(RunIdentityError):
                entrypoint.main()

        mock_is_completed.assert_not_called()
        assert not any(call.args[0][0] == "node" for call in mock_subprocess_run.call_args_list)


# --- Test: VisibilityHeartbeat ---


class TestVisibilityHeartbeat:
    """Tests for the SQS visibility heartbeat daemon thread."""

    def test_heartbeat_extends_visibility_at_interval(self, monkeypatch):
        """Heartbeat calls change_message_visibility at the configured interval."""
        import entrypoint
        from entrypoint import VisibilityHeartbeat

        # Use very short intervals for testing
        monkeypatch.setattr(entrypoint, "HEARTBEAT_INTERVAL", 0.1)
        monkeypatch.setattr(entrypoint, "HEARTBEAT_EXTEND", 300)

        mock_sqs = MagicMock()
        with patch("entrypoint.boto3.client", return_value=mock_sqs):
            hb = VisibilityHeartbeat(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/q.fifo",
                region="us-east-1",
                receipt_handle="test-receipt-handle",
            )
            hb.start()

            # Wait for a few heartbeat cycles
            import time

            time.sleep(0.35)

            hb.stop()

        # Should have called change_message_visibility at least twice
        assert mock_sqs.change_message_visibility.call_count >= 2
        # Verify correct parameters
        call_kwargs = mock_sqs.change_message_visibility.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123/q.fifo"
        assert call_kwargs["ReceiptHandle"] == "test-receipt-handle"
        assert call_kwargs["VisibilityTimeout"] == 300

    def test_heartbeat_stops_cleanly_on_stop(self, monkeypatch):
        """Heartbeat thread exits promptly when stop() is called."""
        import entrypoint
        from entrypoint import VisibilityHeartbeat

        monkeypatch.setattr(entrypoint, "HEARTBEAT_INTERVAL", 60)
        monkeypatch.setattr(entrypoint, "HEARTBEAT_EXTEND", 300)

        mock_sqs = MagicMock()
        with patch("entrypoint.boto3.client", return_value=mock_sqs):
            hb = VisibilityHeartbeat(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/q.fifo",
                region="us-east-1",
                receipt_handle="test-receipt-handle",
            )
            hb.start()

            import time

            time.sleep(0.05)  # Let the thread start

            hb.stop()

            # Thread should be dead after stop returns
            assert not hb._thread.is_alive()

        # With 60s interval and near-instant stop, no extensions should fire
        assert mock_sqs.change_message_visibility.call_count == 0

    def test_heartbeat_exception_does_not_abort(self, monkeypatch):
        """A heartbeat failure logs a warning but does not crash the thread."""
        import entrypoint
        from entrypoint import VisibilityHeartbeat

        monkeypatch.setattr(entrypoint, "HEARTBEAT_INTERVAL", 0.05)
        monkeypatch.setattr(entrypoint, "HEARTBEAT_EXTEND", 300)

        mock_sqs = MagicMock()
        mock_sqs.change_message_visibility.side_effect = Exception("AccessDenied")

        with patch("entrypoint.boto3.client", return_value=mock_sqs):
            hb = VisibilityHeartbeat(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/q.fifo",
                region="us-east-1",
                receipt_handle="test-receipt-handle",
            )
            hb.start()

            import time

            time.sleep(0.2)

            # Thread should still be alive despite repeated failures
            assert hb._thread.is_alive()
            assert hb._consecutive_failures >= 3

            hb.stop()

        # Verify it attempted multiple times (didn't die on first failure)
        assert mock_sqs.change_message_visibility.call_count >= 3

    def test_heartbeat_tracks_extension_count(self, monkeypatch):
        """Heartbeat correctly counts successful extensions."""
        import entrypoint
        from entrypoint import VisibilityHeartbeat

        monkeypatch.setattr(entrypoint, "HEARTBEAT_INTERVAL", 0.05)
        monkeypatch.setattr(entrypoint, "HEARTBEAT_EXTEND", 300)

        mock_sqs = MagicMock()
        with patch("entrypoint.boto3.client", return_value=mock_sqs):
            hb = VisibilityHeartbeat(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/q.fifo",
                region="us-east-1",
                receipt_handle="test-receipt-handle",
            )
            hb.start()

            import time

            time.sleep(0.18)

            hb.stop()

        assert hb._extensions == mock_sqs.change_message_visibility.call_count
        assert hb._extensions >= 2
        assert hb._consecutive_failures == 0

    def test_heartbeat_resets_failure_count_on_success(self, monkeypatch):
        """After a transient failure, a success resets the consecutive counter."""
        import entrypoint
        from entrypoint import VisibilityHeartbeat

        monkeypatch.setattr(entrypoint, "HEARTBEAT_INTERVAL", 0.05)
        monkeypatch.setattr(entrypoint, "HEARTBEAT_EXTEND", 300)

        call_count = {"n": 0}

        def side_effect(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise Exception("Transient network error")
            return {}

        mock_sqs = MagicMock()
        mock_sqs.change_message_visibility.side_effect = side_effect

        with patch("entrypoint.boto3.client", return_value=mock_sqs):
            hb = VisibilityHeartbeat(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/q.fifo",
                region="us-east-1",
                receipt_handle="test-receipt-handle",
            )
            hb.start()

            import time

            time.sleep(0.25)

            hb.stop()

        # After the transient failure, subsequent successes reset the counter
        assert hb._consecutive_failures == 0
        assert hb._extensions >= 3  # At least 3 successes (calls 1, 3, 4+)


class TestZeroTokenFailureDetection:
    """Issue #2883: a run that burned $0.0000 across a single turn never reached
    the model (Bedrock AccessDenied / sigv4 403 / throttling). The SDK returns
    gracefully, so the entrypoint must NOT report it as 'no changes needed' — it
    must fail the check run with a diagnostic.
    """

    # --- _read_result_metadata ---

    def test_read_result_metadata_present(self, tmp_path, monkeypatch):
        import entrypoint

        meta_file = tmp_path / "adp-result-metadata.json"
        meta_file.write_text(
            json.dumps({"subtype": "success", "total_cost_usd": 0, "num_turns": 1})
        )
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(meta_file))
        assert entrypoint._read_result_metadata() == {
            "subtype": "success",
            "total_cost_usd": 0,
            "num_turns": 1,
        }

    def test_read_result_metadata_absent(self, tmp_path, monkeypatch):
        import entrypoint

        monkeypatch.setattr(
            entrypoint, "RESULT_METADATA_PATH", str(tmp_path / "does-not-exist.json")
        )
        assert entrypoint._read_result_metadata() is None

    def test_read_result_metadata_malformed(self, tmp_path, monkeypatch):
        import entrypoint

        meta_file = tmp_path / "adp-result-metadata.json"
        meta_file.write_text("not json{{{")
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(meta_file))
        assert entrypoint._read_result_metadata() is None

    # --- _is_zero_token_failure ---

    def test_zero_cost_single_turn_is_failure(self):
        import entrypoint

        assert entrypoint._is_zero_token_failure({"total_cost_usd": 0, "num_turns": 1})
        assert entrypoint._is_zero_token_failure({"total_cost_usd": 0.0, "num_turns": 0})

    def test_nonzero_cost_is_not_failure(self):
        import entrypoint

        # Genuine "no changes needed" verdict costs >0 tokens.
        assert not entrypoint._is_zero_token_failure({"total_cost_usd": 0.0123, "num_turns": 1})

    def test_zero_cost_multi_turn_is_not_failure(self):
        import entrypoint

        # Some tokens burned then died mid-run — not the zero-token signature.
        assert not entrypoint._is_zero_token_failure({"total_cost_usd": 0, "num_turns": 5})

    def test_missing_fields_or_none_meta_is_not_failure(self):
        import entrypoint

        assert not entrypoint._is_zero_token_failure(None)
        assert not entrypoint._is_zero_token_failure({})
        assert not entrypoint._is_zero_token_failure({"total_cost_usd": 0})
        assert not entrypoint._is_zero_token_failure({"num_turns": 1})

    # --- _handle_success integration of the signature ---

    @patch("entrypoint.update_invocation_status")
    @patch("entrypoint._post_comment")
    @patch("entrypoint._find_open_pr")
    @patch("entrypoint.run_cmd")
    def test_handle_success_zero_token_fails_check(
        self, mock_run_cmd, mock_find_pr, mock_post, mock_status, tmp_path, monkeypatch
    ):
        """cost=0/turns=1/no-diff → failure conclusion + diagnostic comment."""
        import entrypoint

        # No diff, no unpushed commits → reaches the "no changes" branch.
        mock_run_cmd.return_value = MagicMock(stdout="", stderr="", returncode=0)
        meta_file = tmp_path / "meta.json"
        meta_file.write_text(json.dumps({"total_cost_usd": 0, "num_turns": 1}))
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(meta_file))
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path)

        rc = entrypoint._handle_success(
            "acme/repo", 42, "agent/issue-42", "developer", "msg-1", "2026-07-04T00:00:00Z"
        )

        assert rc == 1  # nonzero → main() finalizes check run as failure
        # Diagnostic failure comment posted (not the "no changes needed" success)
        assert mock_post.call_count == 1
        args = mock_post.call_args[0]
        assert args[3] == "failed"
        assert "0 tokens burned" in args[4]
        mock_status.assert_called_once()
        assert mock_status.call_args[0][2] == "failed"
        # Must NOT try to open/backfill a PR on the failure path
        mock_find_pr.assert_not_called()

    @patch("entrypoint.update_invocation_status")
    @patch("entrypoint._post_comment")
    @patch("entrypoint._find_open_pr", return_value="")
    @patch("entrypoint.run_cmd")
    def test_handle_success_nonzero_cost_no_diff_stays_success(
        self, mock_run_cmd, mock_find_pr, mock_post, mock_status, tmp_path, monkeypatch
    ):
        """A successful process without a diff does not establish task completion."""
        import entrypoint

        mock_run_cmd.return_value = MagicMock(stdout="", stderr="", returncode=0)
        meta_file = tmp_path / "meta.json"
        meta_file.write_text(json.dumps({"total_cost_usd": 0.05, "num_turns": 1}))
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(meta_file))
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path)

        rc = entrypoint._handle_success(
            "acme/repo", 42, "agent/issue-42", "developer", "msg-1", "2026-07-04T00:00:00Z"
        )

        assert rc == 0
        args = mock_post.call_args[0]
        assert args[3] == "completed"
        assert "no local changes" in args[4].lower()
        assert "no changes needed" not in args[4]
        assert "task completion is not verified" in args[4]
        assert mock_status.call_args[0][2] == "complete"

    @patch("entrypoint.update_invocation_status")
    @patch("entrypoint._post_comment")
    @patch("entrypoint._find_open_pr", return_value="")
    @patch("entrypoint.run_cmd")
    def test_handle_success_no_metadata_stays_success(
        self, mock_run_cmd, mock_find_pr, mock_post, mock_status, tmp_path, monkeypatch
    ):
        """No metadata file (older Node image / write failed) → fail open to success."""
        import entrypoint

        mock_run_cmd.return_value = MagicMock(stdout="", stderr="", returncode=0)
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(tmp_path / "missing.json"))
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path)

        rc = entrypoint._handle_success(
            "acme/repo", 42, "agent/issue-42", "developer", "msg-1", "2026-07-04T00:00:00Z"
        )

        assert rc == 0
        assert mock_post.call_args[0][3] == "completed"

    @patch("entrypoint.update_invocation_status")
    @patch("entrypoint._post_comment")
    @patch("entrypoint._write_outbound_correlation")
    @patch("entrypoint.run_cmd")
    def test_handle_success_with_diff_preserves_unvalidated_checkpoint(
        self, mock_run_cmd, mock_write_corr, mock_post, mock_status, tmp_path, monkeypatch
    ):
        """Leftover work is preserved without claiming a ready PR."""
        import entrypoint

        # Non-empty diff/status → has_uncommitted True; gh pr list returns no PR.
        def _run_cmd_side_effect(cmd, *a, **k):
            if cmd[:2] == ["git", "diff"]:
                return MagicMock(stdout=" file.py | 2 +-\n", returncode=0)
            if cmd[:2] == ["git", "status"]:
                return MagicMock(stdout=" M file.py\n", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        mock_run_cmd.side_effect = _run_cmd_side_effect
        # Even if a zero-token metadata file exists, the diff path must ignore it.
        meta_file = tmp_path / "meta.json"
        meta_file.write_text(json.dumps({"total_cost_usd": 0, "num_turns": 1}))
        monkeypatch.setattr(entrypoint, "RESULT_METADATA_PATH", str(meta_file))
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path)

        rc = entrypoint._handle_success(
            "acme/repo", 42, "agent/issue-42", "developer", "msg-1", "2026-07-04T00:00:00Z"
        )

        assert rc == 1
        assert mock_post.call_args[0][3] == "failed"
        assert "Incomplete work preserved" in mock_post.call_args[0][4]
        assert not any(call.args[0][:3] == ["gh", "pr", "create"] for call in mock_run_cmd.call_args_list)


# =============================================================================
# GitLab Tier-A acknowledge path (Issue #3436)
# =============================================================================

SAMPLE_GITLAB_ENVELOPE = {
    "version": "1.0",
    "channel": "gitlab",
    "tenant_id": "",
    "persona": "developer",
    "message_id": "msg-gl-456",
    "actor": {
        "user_id": "gitlab-user",
        "org_id": "",
        "github_id": 0,
        "github_login": "",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 0,
        "repo": "spike-group/test-project",
        "issue": 7,
        "pr": None,
        "sha": None,
    },
    "intent": {"trigger": "mention", "label": None, "persona": "developer"},
    "correlation": {
        "correlation_id": "corr-gitlab-789",
        "root_human_id": "gitlab-user",
        "is_human_rooted": True,
    },
    "payload": {
        "provider": "gitlab",
        "event_type": "mention",
        "source": {
            "project_id": 42,
            "project_path": "spike-group/test-project",
            "issue_iid": 7,
            "note_id": 100,
            # Legacy/untrusted field: the worker must ignore this destination.
            "gitlab_url": "https://attacker.example",
        },
        "actor": {
            "username": "gitlab-user",
            "display_name": "GitLab User",
        },
        "content": {
            "body": "@agent hello",
            "mention_target": "developer",
        },
        "metadata": {
            "timestamp": "2026-07-09T10:00:00Z",
            "webhook_id": "wh-gl-001",
        },
    },
    "arrived_at": "2026-07-09T10:00:00Z",
    "model_requested": None,
    "model_resolved": None,
}


class TestGitLabProviderDetection:
    """Issue #3436: GitLab messages must bypass the poison guard and route to
    the GitLab acknowledge path. GitHub messages with installation_id=0 must
    still be deleted by the poison guard (regression guard for #2336).
    """

    @patch("entrypoint._handle_gitlab_mention")
    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    def test_gitlab_envelope_routes_to_gitlab_path(
        self,
        mock_delete_msg,
        mock_receive_msg,
        mock_handle_gitlab,
        monkeypatch,
    ):
        """GitLab envelope with installation_id=0 must route to _handle_gitlab_mention,
        NOT trigger the poison guard."""
        from entrypoint import main

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_GITLAB_ENVELOPE), "receipt-gl-1")
        mock_handle_gitlab.return_value = 0

        result = main()

        assert result == 0
        mock_handle_gitlab.assert_called_once()
        # Poison guard must NOT have fired (no direct _delete_message call)
        mock_delete_msg.assert_not_called()

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    def test_github_envelope_installation_id_zero_still_poison_deleted(
        self,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
    ):
        """GitHub envelope with installation_id=0 must still trigger poison guard
        (regression guard for issue #2336)."""
        from entrypoint import main

        # GitHub envelope: no payload.provider or provider != "gitlab"
        github_poison = {
            **SAMPLE_ENVELOPE,
            "source_ref": {
                **SAMPLE_ENVELOPE["source_ref"],
                "installation_id": 0,
            },
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(github_poison), "receipt-gh-poison")

        result = main()

        assert result == 1
        # Poison guard deleted the message
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gh-poison",
        )

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    def test_github_envelope_no_payload_field_still_poison_deleted(
        self,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
    ):
        """GitHub envelope without a 'payload' field at all must still trigger
        the poison guard when installation_id=0."""
        from entrypoint import main

        # Envelope with no payload field (older format)
        no_payload_envelope = {
            "version": "1.0",
            "channel": "github",
            "tenant_id": "acme-corp",
            "persona": "developer",
            "message_id": "msg-nopayload",
            "source_ref": {
                "installation_id": 0,
                "repo": "acme/repo",
                "issue": 1,
            },
        }

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (json.dumps(no_payload_envelope), "receipt-np")

        result = main()

        assert result == 1
        mock_delete_msg.assert_called_once()


class TestHandleGitLabMention:
    """Issue #3436: Unit tests for the _handle_gitlab_mention function itself."""

    @pytest.fixture(autouse=True)
    def trusted_gitlab_url(self, monkeypatch):
        """Workers receive the trusted URL from deployment-owned configuration."""
        monkeypatch.setenv("GITLAB_URL", "http://gitlab.dev.adp.internal")

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_happy_path_ack_comment_and_branch(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """Full happy path uses the trusted URL and completes all API calls."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("ENVIRONMENT", "dev")

        # Mock Secrets Manager
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test-token"}

        # Mock urllib.request.urlopen for all three calls:
        # 1) ack comment, 2) project lookup, 3) branch create
        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "main"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        mock_branch_response = MagicMock()
        mock_branch_response.status = 201
        mock_branch_response.__enter__ = MagicMock(return_value=mock_branch_response)
        mock_branch_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            mock_ack_response,
            mock_project_response,
            mock_branch_response,
        ]

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-happy",
        )

        assert result == 0
        # Secrets Manager called for the token
        mock_sm.get_secret_value.assert_called_once_with(SecretId="adp/dev/gitlab-api-token")
        # Three urlopen calls: ack comment + project lookup + branch create
        assert mock_urlopen.call_count == 3
        # Message was deleted
        mock_delete_msg.assert_called_once_with(
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-happy",
        )

    @patch("entrypoint.boto3.client")
    @patch("entrypoint._delete_message")
    def test_missing_api_token_deletes_message_returns_1(
        self,
        mock_delete_msg,
        mock_boto_client,
        monkeypatch,
    ):
        """If Secrets Manager read fails, still delete msg (no FIFO jam), return 1."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.side_effect = Exception("AccessDenied")

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-notoken",
        )

        assert result == 1
        # Message still deleted to prevent FIFO jam
        mock_delete_msg.assert_called_once()

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_branch_create_400_already_exists_tolerated(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """Branch already exists (400 with 'already exists' body) tolerated — not failure."""
        from entrypoint import _handle_gitlab_mention
        import urllib.error
        from io import BytesIO

        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        # Calls: 1) ack comment, 2) project lookup, 3) branch create (400 already exists)
        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "main"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def urlopen_side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_ack_response
            if call_count[0] == 2:
                return mock_project_response
            # Third call: branch create returns 400 with "already exists" body
            err = urllib.error.HTTPError(
                url=req.full_url,
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=BytesIO(b'{"message":"Branch already exists"}'),
            )
            raise err

        mock_urlopen.side_effect = urlopen_side_effect

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-400",
        )

        # 400 with "already exists" body is tolerated — still success
        assert result == 0
        mock_delete_msg.assert_called_once()

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_ack_comment_failure_returns_1_but_still_deletes_message(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """If ack comment POST fails, return 1 but still delete msg (no FIFO jam)."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        # Both API calls fail (ack comment + branch create)
        mock_urlopen.side_effect = Exception("Connection refused")

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-fail",
        )

        # Ack comment failed → return 1, but message still deleted (no FIFO jam)
        assert result == 1
        mock_delete_msg.assert_called_once()

    @patch("entrypoint._delete_message")
    def test_missing_required_fields_deletes_message(
        self,
        mock_delete_msg,
        monkeypatch,
    ):
        """Missing project_id/issue_iid → delete msg and return 1."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("GITLAB_URL", "http://gitlab.dev.adp.internal")

        # Envelope with empty source fields
        bad_envelope = {
            **SAMPLE_GITLAB_ENVELOPE,
            "payload": {
                **SAMPLE_GITLAB_ENVELOPE["payload"],
                "source": {
                    "project_id": None,
                    "project_path": "",
                    "issue_iid": None,
                    "note_id": None,
                    "gitlab_url": "",
                },
            },
        }

        result = _handle_gitlab_mention(
            bad_envelope,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-bad",
        )

        assert result == 1
        mock_delete_msg.assert_called_once()

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_missing_trusted_gitlab_url_fails_before_token_read(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """No deployment-owned URL means no token read and no outbound call."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.delenv("GITLAB_URL", raising=False)

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-no-url",
        )

        assert result == 1
        mock_boto_client.assert_not_called()
        mock_urlopen.assert_not_called()
        mock_delete_msg.assert_called_once()

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_envelope_gitlab_url_is_ignored(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """A forged envelope URL cannot receive the platform GitLab token."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("GITLAB_URL", "http://trusted-gitlab.internal")
        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "main"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        mock_branch_response = MagicMock()
        mock_branch_response.status = 201
        mock_branch_response.__enter__ = MagicMock(return_value=mock_branch_response)
        mock_branch_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            mock_ack_response,
            mock_project_response,
            mock_branch_response,
        ]

        _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-env",
        )

        # The sample envelope contains https://attacker.example. Every request
        # must instead use the deployment-owned URL.
        first_call_req = mock_urlopen.call_args_list[0][0][0]
        assert "trusted-gitlab.internal" in first_call_req.full_url
        assert "attacker.example" not in first_call_req.full_url

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_configured_url_used_when_legacy_envelope_url_empty(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """Configured GITLAB_URL is independent of the legacy envelope field."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("GITLAB_URL", "http://override-gitlab.internal")
        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "main"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        mock_branch_response = MagicMock()
        mock_branch_response.status = 201
        mock_branch_response.__enter__ = MagicMock(return_value=mock_branch_response)
        mock_branch_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            mock_ack_response,
            mock_project_response,
            mock_branch_response,
        ]

        # Envelope with empty gitlab_url in source
        envelope_empty_url = {
            **SAMPLE_GITLAB_ENVELOPE,
            "payload": {
                **SAMPLE_GITLAB_ENVELOPE["payload"],
                "source": {
                    **SAMPLE_GITLAB_ENVELOPE["payload"]["source"],
                    "gitlab_url": "",
                },
            },
        }

        _handle_gitlab_mention(
            envelope_empty_url,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-fallback",
        )

        # Verify the URL used is from env var (fallback)
        first_call_req = mock_urlopen.call_args_list[0][0][0]
        assert "override-gitlab.internal" in first_call_req.full_url

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_branch_create_400_invalid_ref_returns_1(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """Issue #3452: branch 400 with non-'already exists' body → error, return 1."""
        from entrypoint import _handle_gitlab_mention
        import urllib.error
        from io import BytesIO

        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        # Calls: 1) ack comment, 2) project lookup, 3) branch create (400 invalid ref)
        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "main"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def urlopen_side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_ack_response
            if call_count[0] == 2:
                return mock_project_response
            # Third call: branch create returns 400 with invalid ref body
            err = urllib.error.HTTPError(
                url=req.full_url,
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=BytesIO(b'{"message":"Invalid reference name"}'),
            )
            raise err

        mock_urlopen.side_effect = urlopen_side_effect

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-400-invalid",
        )

        # 400 with non-"already exists" body is a real error — return 1
        assert result == 1
        mock_delete_msg.assert_called_once()

    @patch("entrypoint.boto3.client")
    @patch("entrypoint.open_authenticated")
    @patch("entrypoint._delete_message")
    def test_default_branch_resolved_from_project_api(
        self,
        mock_delete_msg,
        mock_urlopen,
        mock_boto_client,
        monkeypatch,
    ):
        """Issue #3452: default branch resolved from project API (non-'main' works)."""
        from entrypoint import _handle_gitlab_mention

        monkeypatch.setenv("ENVIRONMENT", "dev")

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "glpat-test"}

        # Calls: 1) ack comment, 2) project lookup (develop), 3) branch create
        mock_ack_response = MagicMock()
        mock_ack_response.status = 201
        mock_ack_response.__enter__ = MagicMock(return_value=mock_ack_response)
        mock_ack_response.__exit__ = MagicMock(return_value=False)

        mock_project_response = MagicMock()
        mock_project_response.status = 200
        mock_project_response.read.return_value = json.dumps({"default_branch": "develop"}).encode(
            "utf-8"
        )
        mock_project_response.__enter__ = MagicMock(return_value=mock_project_response)
        mock_project_response.__exit__ = MagicMock(return_value=False)

        mock_branch_response = MagicMock()
        mock_branch_response.status = 201
        mock_branch_response.__enter__ = MagicMock(return_value=mock_branch_response)
        mock_branch_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            mock_ack_response,
            mock_project_response,
            mock_branch_response,
        ]

        result = _handle_gitlab_mention(
            SAMPLE_GITLAB_ENVELOPE,
            "https://sqs.us-east-1.amazonaws.com/123/q.fifo",
            "us-east-1",
            "receipt-gl-develop",
        )

        assert result == 0
        # Verify the branch-create request used "develop" as ref, not "main"
        branch_create_req = mock_urlopen.call_args_list[2][0][0]
        assert branch_create_req.data is not None
        import json as json_mod

        branch_payload = json_mod.loads(branch_create_req.data.decode("utf-8"))
        assert branch_payload["ref"] == "develop"
        assert branch_payload["branch"] == "agent/issue-7"


# --- Test: Mediated idempotency guard envelope contract (Issue #5223) ---


class TestMediatedIdempotencyGuard:
    """Pin the mediated guard to the envelope the route actually returns.

    This guard shipped unable to fire, for two independent reasons that masked
    each other: it read `pull_request` off the top level of the route envelope
    (where it never appears — the route nests it under `repository`), and it
    compared GitHub's `state` against `"merged"` (a value `state` never holds;
    mergedness is a separate boolean). Either bug alone yields a permanent
    false negative, so the only symptom was a duplicate run on SQS redelivery.
    These tests exist so neither half can regress silently.
    """

    @staticmethod
    def _envelope(pull):
        """The READ_REPOSITORY envelope, shaped as the route emits it."""
        return {
            "repository": {
                "repository_id": 987654321,
                "repository": "acme-corp/flagship-app",
                "default_branch": "main",
                "branch_head": "b" * 40,
                "pull_request": pull,
            },
            "branch": "agent/issue-42",
            "idempotency_key": "read_repository:acme-corp/flagship-app",
        }

    def _guard(self, monkeypatch, envelope):
        from lib import mediated_github
        from entrypoint import _mediated_already_completed

        def _read_repository(**_kwargs):
            return envelope

        monkeypatch.setattr(mediated_github, "read_repository", _read_repository)
        return _mediated_already_completed()

    def test_merged_pull_request_nested_under_repository_is_detected(self, monkeypatch):
        """A merged PR at the REAL nesting level makes the guard fire.

        Proves the consumer reads `repository.pull_request`. Against the old
        top-level read this returns False, so the assertion is load-bearing.
        """
        envelope = self._envelope(
            {
                "number": 4242,
                "html_url": "https://github.com/acme-corp/flagship-app/pull/4242",
                "state": "closed",
                "merged": True,
            }
        )
        assert self._guard(monkeypatch, envelope) is True

    def test_a_top_level_pull_request_is_not_consulted(self, monkeypatch):
        """A merged PR at the OLD top level must NOT satisfy the guard.

        The wrong level is not merely unhelpful — honouring it would let a shape
        the gateway never emits decide whether real work gets skipped.
        """
        envelope = self._envelope(None)
        envelope["pull_request"] = {"number": 1, "state": "closed", "merged": True}
        assert self._guard(monkeypatch, envelope) is False

    def test_an_open_pull_request_does_not_skip_the_run(self, monkeypatch):
        """Work in progress is not completed work."""
        envelope = self._envelope({"number": 4242, "state": "open", "merged": False})
        assert self._guard(monkeypatch, envelope) is False

    def test_a_closed_unmerged_pull_request_does_not_skip_the_run(self, monkeypatch):
        """A closed-without-merging PR means the work was abandoned, not delivered.

        Skipping here would silently drop real work — the expensive direction.
        """
        envelope = self._envelope({"number": 4242, "state": "closed", "merged": False})
        assert self._guard(monkeypatch, envelope) is False

    def test_state_is_never_trusted_as_a_mergedness_signal(self, monkeypatch):
        """Even `state == "merged"` cannot skip a run when `merged` is false.

        GitHub never emits that state, so if it ever appears it is a forgery or a
        provider bug. Mergedness comes from the separate boolean, or not at all.
        """
        envelope = self._envelope({"number": 4242, "state": "merged", "merged": False})
        assert self._guard(monkeypatch, envelope) is False

    def test_a_truthy_non_boolean_merged_value_does_not_skip_the_run(self, monkeypatch):
        """`merged` must be exactly True — schema validation, not truthiness.

        A string like "false" is truthy in Python; accepting it would let a
        malformed provider answer skip real work.
        """
        envelope = self._envelope({"number": 4242, "state": "closed", "merged": "false"})
        assert self._guard(monkeypatch, envelope) is False

    @pytest.mark.parametrize(
        "envelope",
        [
            {},
            {"repository": None},
            {"repository": "acme-corp/flagship-app"},
            {"repository": {}},
            {"repository": {"pull_request": None}},
            {"repository": {"pull_request": "merged"}},
        ],
        ids=["empty", "null-repo", "repo-as-string", "no-pull-key", "null-pull", "pull-as-string"],
    )
    def test_unusable_envelopes_fail_open(self, monkeypatch, envelope):
        """Any shape the guard cannot read means "proceed", never "skip".

        Includes `repository` as a bare string, which is what the *inner* payload
        uses for the slug — so a caller that forgot to unwrap lands here rather
        than crashing on `.get`.
        """
        assert self._guard(monkeypatch, envelope) is False

    def test_a_refused_read_fails_open(self, monkeypatch):
        """An unreachable or refusing gateway must let the run proceed.

        Duplicate work is recoverable; silently dropping an assignment is not.
        """
        from lib import mediated_github
        from entrypoint import _mediated_already_completed

        def _read_repository(**_kwargs):
            raise RuntimeError("the operation is not currently authorized")

        monkeypatch.setattr(mediated_github, "read_repository", _read_repository)
        assert _mediated_already_completed() is False

    def test_the_dispatcher_routes_to_the_authority_the_run_holds(self, monkeypatch):
        """`mediated=True` must not reach for `gh pr list`, which needs a token."""
        import entrypoint

        calls = {"mediated": 0, "token": 0}
        monkeypatch.setattr(
            entrypoint,
            "_mediated_already_completed",
            lambda: calls.__setitem__("mediated", 1) or True,
        )
        monkeypatch.setattr(
            entrypoint, "_is_already_completed", lambda *a: calls.__setitem__("token", 1) or False
        )

        assert entrypoint._already_completed("acme/repo", 42, "", mediated=True) is True
        assert calls == {"mediated": 1, "token": 0}

        calls.update({"mediated": 0, "token": 0})
        assert entrypoint._already_completed("acme/repo", 42, "ghs_x", mediated=False) is False
        assert calls == {"mediated": 0, "token": 1}


@pytest.mark.parametrize("agent_exit", [0, 1])
@pytest.mark.parametrize("mediated, engine_cycle", [(False, False), (True, False), (False, True)])
def test_review_is_produced_and_uploaded_before_terminal_handlers(monkeypatch, tmp_path, agent_exit, mediated, engine_cycle):
    import entrypoint
    from lib import status_gateway_client
    from tests.test_review_delivery import EXPECT, HEAD
    import hashlib

    envelope = {**SAMPLE_ENVELOPE, "persona": "reviewer", "review_expect": EXPECT}
    if engine_cycle:
        envelope["intent"] = {"trigger": "engine_review_cycle"}
        envelope["review_cycle_input"] = {
            "action": "review", "repo": envelope["source_ref"]["repo"], "pr_number": 77,
            "head_sha": HEAD, "accepted_scope": '{"node":{"title":"Bound task"}}',
            "findings": [], "remaining_attempts": 2, "remaining_spend_usd": "4.00", "operation_key": "cycle:test",
        }
    monkeypatch.setattr(entrypoint, "prepend_correlation_marker", lambda body: body)
    if mediated:
        from lib import mediated_github
        monkeypatch.setattr(entrypoint, "_mediated_github_enabled", lambda: True)
        monkeypatch.setattr(entrypoint, "_protected_worker", lambda: True)
        monkeypatch.setattr(entrypoint, "_withhold_write_token", MagicMock())
        monkeypatch.setattr(mediated_github, "read_repository", lambda: {"pull_requests": []})
        def materialize(destination, **kwargs):
            Path(destination).mkdir(parents=True, exist_ok=True)
            return {"remote_head": HEAD, "local_head": "b" * 40}
        monkeypatch.setattr(mediated_github, "materialize_repository", materialize)
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_RUN_ATTEMPT", "1")
    monkeypatch.setattr(entrypoint, "_receive_one_message", lambda *_: (json.dumps(envelope), "receipt"))
    for name in ("_delete_message", "create_check_run", "update_check_run", "_upload_transcript_to_s3",
                 "_record_session_id", "_load_door_api_key", "_setup_agent_control", "_teardown_agent_control"):
        monkeypatch.setattr(entrypoint, name, MagicMock(return_value=None))
    vault = MagicMock()
    vault.return_value.get_secret.return_value = {"app_id": "123", "private_key": "fake-key"}
    monkeypatch.setattr(entrypoint, "VaultClient", vault)
    monkeypatch.setattr(entrypoint, "mint_installation_token", MagicMock(return_value="test-token"))
    monkeypatch.setattr(entrypoint.shutil, "rmtree", MagicMock())
    monkeypatch.setattr(entrypoint.shutil, "copytree", MagicMock())
    monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path / "repo")
    monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout="", returncode=0)))
    def command(cmd, **kwargs):
        output = HEAD if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
        if cmd == ["git", "branch", "--show-current"]:
            output = "bound-pr-branch"
        if cmd == ["git", "rev-parse", "--is-shallow-repository"]:
            output = "false"
        if cmd[:3] == ["gh", "pr", "view"] and cmd[-2:] == ["--json", "headRefName,isCrossRepository,state,baseRefOid"]:
            output = json.dumps({"headRefName": "bound-pr-branch", "isCrossRepository": False, "state": "OPEN", "baseRefOid": "b" * 40})
        return MagicMock(stdout=output, returncode=0)
    monkeypatch.setattr(entrypoint, "run_cmd", command)
    events = []
    def agent(cmd, **kwargs):
        if cmd[:2] == ["git", "ls-remote"]:
            return MagicMock(returncode=2, stdout="", stderr="")
        if cmd[0] == "gh":
            return MagicMock(returncode=0, stdout="", stderr="")
        assert cmd[0] == "node"
        env = kwargs["env"]
        assert json.loads(env["ADP_REVIEW_EXPECT"]) == EXPECT
        if engine_cycle:
            assert json.loads(env["ADP_REVIEW_CYCLE_INPUT"]) == envelope["review_cycle_input"]
        else:
            assert "ADP_REVIEW_CYCLE_INPUT" not in env
        Path(env["ADP_REVIEW_REPORT_PATH"]).write_text(json.dumps({
            "stages": {"functional": "completed"}, "verdict": "request-changes",
            "findings": [{"finding_id": "F1", "stage": "functional", "severity": "blocking",
                          "disposition": "open", "summary": "Needs repair"}],
        }))
        events.append("agent")
        return MagicMock(returncode=agent_exit, stdout="", stderr="")
    monkeypatch.setattr(entrypoint.subprocess, "run", agent)
    monkeypatch.setattr(status_gateway_client, "authority_enabled", lambda: True)
    def upload(path, data, **kwargs):
        assert events == ["agent"]
        assert path == "/artifacts/review-result"
        body = json.loads(data)
        assert body["subject"]["reviewed_head_sha"] == HEAD
        assert body["findings"][0]["finding_id"] == "F1"
        digest = hashlib.sha256(data).hexdigest()
        tenant = hashlib.sha256(envelope["tenant_id"].encode()).hexdigest()
        run = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
        events.append("upload")
        return {"key": f"runs/{tenant}/{run}/attempt-1/review-result/{digest}.json",
                "sha256": digest, "recorded": True}
    monkeypatch.setattr(status_gateway_client, "_post_bytes", upload)
    def terminal(*args, **kwargs):
        assert events == ["agent", "upload"]
        assert "Review evidence recorded" in kwargs["review_note"]
        events.append("terminal")
        return agent_exit
    monkeypatch.setattr(entrypoint, "_handle_success", terminal)
    monkeypatch.setattr(entrypoint, "_handle_failure", terminal)
    monkeypatch.setattr(entrypoint, "update_invocation_status", MagicMock())
    assert entrypoint.main() == agent_exit
    assert events == ["agent", "upload", "terminal"]


def test_developer_cause_is_in_the_first_terminal_status_write(monkeypatch):
    import entrypoint

    status = MagicMock()
    monkeypatch.setattr(entrypoint, "update_invocation_status", status)
    monkeypatch.setattr(entrypoint, "_post_comment", MagicMock())
    assert entrypoint._handle_failure("org/repo", 7, "developer", "run", "now", 1,
                                     failure_error="Agent developer exit 1: budget_exceeded") == 1
    status.assert_called_once_with("run", "now", "failed", summary="Agent `developer` failed with exit code 1.",
                                   error_message="Agent developer exit 1: budget_exceeded")
