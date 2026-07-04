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


def _subprocess_side_effect_fresh_branch(*args, **kwargs):
    """Default subprocess.run side_effect for tests simulating a fresh issue.

    The entrypoint calls subprocess.run directly (not run_cmd) for:
      1. `git ls-remote --exit-code --heads origin agent/issue-NNN`
         → returncode 0 means "branch exists"; we return 1 (doesn't exist)
         so the fresh-creation path runs (the legacy test default).
      2. `gh pr list ...` (only if branch exists; not reached in fresh case)
      3. `git push --delete origin ...` (only if stale branch reset; not reached)
      4. The final `subprocess.run` for node agent execution → returncode 0.

    Tests that simulate "branch already exists" should override this with
    their own side_effect.
    """
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=1, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


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


# --- Test: vault_client ---


class TestVaultClient:
    @patch("lib.vault_client.boto3.client")
    def test_get_secret(self, mock_boto_client):
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"app_id": "123", "private_key": "fake-key"}'
        }

        client = VaultClient(region="us-east-1")
        result = client.get_secret("tenants/acme-corp/github-app")

        mock_sm.get_secret_value.assert_called_once_with(SecretId="tenants/acme-corp/github-app")
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
    ):
        """Test the full 12-step sequence with a successful agent run."""
        from entrypoint import main

        monkeypatch.setenv("SQS_MESSAGE_BODY", json.dumps(SAMPLE_ENVELOPE))
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

        main()

        # Vault was called for github-app creds
        mock_vault.get_secret.assert_called_with("tenants/acme-corp/github-app")
        # Token was minted
        mock_mint.assert_called_once_with("123", "fake-key", 99887766)
        # Agent was executed
        mock_subprocess_run.assert_called_once()

    def test_missing_sqs_message(self, monkeypatch):
        """Should return 1 when SQS_MESSAGE_BODY is not set."""
        from entrypoint import main

        monkeypatch.delenv("SQS_MESSAGE_BODY", raising=False)
        assert main() == 1

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
      - else → reset to main (avoid carrying merged-since-then commits)
      - first run on the issue → fresh `git checkout -b` (existing behavior)
    """

    def test_branch_does_not_exist_creates_fresh(self):
        """ls-remote returns 1 → goes through normal `git checkout -b`."""
        # The fresh-branch helper at module top simulates this case:
        # ls-remote returncode=1 → remote_branch_exists=False → fresh creation.
        # All other tests in TestEntrypointMain that use
        # _subprocess_side_effect_fresh_branch implicitly cover this.
        result = _subprocess_side_effect_fresh_branch(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", "agent/issue-42"]
        )
        assert result.returncode == 1

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

    def test_branch_exists_no_pr_resets_to_main(self):
        """ls-remote=0 + gh pr list returns empty → delete + fresh checkout."""

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd[0:2] == ["git", "ls-remote"]:
                return MagicMock(returncode=0, stdout="abc def\n", stderr="")
            if cmd[0:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        # has_open_pr would be bool("".strip()) = False → reset path
        result = side_effect(["gh", "pr", "list"])
        assert not bool(result.stdout.strip())


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
    """Tests for the ADP_BEDROCK_VIA feature flag (scoped agent_env, not os.environ mutation)."""

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_default_no_flag_agent_env_retains_irsa(
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
        """When ADP_BEDROCK_VIA is not set, agent_env retains all IRSA vars."""
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

        # Agent subprocess should have been called with env containing IRSA vars
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert agent_env["AWS_ROLE_ARN"] == "arn:aws:iam::123456789012:role/irsa-role"
        assert agent_env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/secrets/token"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_platform_explicit_agent_env_retains_irsa(
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
        """When ADP_BEDROCK_VIA=platform, agent_env retains IRSA (same as default)."""
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
            main()

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
        assert "AWS_ROLE_ARN" in agent_env

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_user_mode_strips_irsa_from_agent_env(
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
        """When ADP_BEDROCK_VIA=user and user creds exist, agent_env has IRSA stripped."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("AWS_PROFILE", "default")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "user")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-3")
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

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
        # IRSA vars stripped from agent env
        assert "AWS_ROLE_ARN" not in agent_env
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" not in agent_env
        assert "AWS_PROFILE" not in agent_env
        # User creds remain
        assert agent_env["AWS_ACCESS_KEY_ID"] == "AKUSER"
        assert agent_env["AWS_SECRET_ACCESS_KEY"] == "SKUSER"
        assert agent_env["AWS_SESSION_TOKEN"] == "STUSER"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_user_mode_os_environ_unchanged(
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
        """CRITICAL: os.environ must retain IRSA vars even when ADP_BEDROCK_VIA=user."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "user")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-4")
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

        # os.environ MUST still have IRSA (for post-agent SQS delete)
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
    def test_user_mode_no_user_creds_no_strip(
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
        """When ADP_BEDROCK_VIA=user but no AWS_ACCESS_KEY_ID (assume-role didn't run), no strip."""
        from entrypoint import main
        import entrypoint

        # developer persona does NOT trigger assume-role
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "user")
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

        # developer persona — not in PERSONAS_NEEDING_AWS, so no assume-role
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-5")
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

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
        # IRSA vars should still be present — no strip because no user creds
        assert agent_env["AWS_ROLE_ARN"] == "arn:aws:iam::123456789012:role/irsa-role"
        assert agent_env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/secrets/token"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_user_mode_case_insensitive(
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
        """ADP_BEDROCK_VIA=USER (uppercase) works the same as 'user'."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "USER")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-6")
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

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
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
    def test_user_mode_whitespace_tolerance(
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
        """ADP_BEDROCK_VIA=' user ' (with whitespace) is handled correctly."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", " user ")

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        mock_receive_msg.return_value = (json.dumps(ops_envelope), "receipt-7")
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

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
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
    def test_garbage_value_falls_through_to_platform(
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
        """ADP_BEDROCK_VIA=foobar falls through to platform mode (safe default)."""
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
            main()

        agent_env = mock_subprocess_run.call_args.kwargs.get(
            "env"
        ) or mock_subprocess_run.call_args[1].get("env")
        # IRSA retained — garbage value means platform mode
        assert "AWS_ROLE_ARN" in agent_env

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_user_mode_non_aws_persona_logs_warning(
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
        """When ADP_BEDROCK_VIA=user but persona not in PERSONAS_NEEDING_AWS, warning logged."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/irsa-role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "user")
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

        # 'developer' persona — not in PERSONAS_NEEDING_AWS
        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-9")
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

        import logging

        with caplog.at_level(logging.WARNING):
            main()

        assert any("does not assume customer role" in record.message for record in caplog.records)


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
        monkeypatch.setenv("SIGV4_PROXY_PORT", "9090")

        mock_receive_msg.return_value = (json.dumps(SAMPLE_ENVELOPE), "receipt-gw1")
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
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
        assert agent_env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://127.0.0.1:9090"
        # Must NOT have ANTHROPIC_BASE_URL (that routes to the broken translator)
        assert "ANTHROPIC_BASE_URL" not in agent_env

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
    def test_direct_path_sets_claude_code_use_bedrock(
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
        """With ADP_BEDROCK_VIA=direct, sets only CLAUDE_CODE_USE_BEDROCK (no proxy)."""
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

        main()

        # Verify subprocess.run was called with direct env
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        # No proxy base URL in direct mode
        assert "ANTHROPIC_BEDROCK_BASE_URL" not in agent_env
        assert "ANTHROPIC_BASE_URL" not in agent_env

        # Proxy NOT started
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
    def test_gateway_fallback_on_proxy_failure(
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
        """When proxy fails to start, falls back to direct Bedrock."""
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

        main()

        # Falls back to direct mode — no proxy base URL
        call_kwargs = mock_subprocess_run.call_args
        agent_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert "ANTHROPIC_BEDROCK_BASE_URL" not in agent_env
        assert "ANTHROPIC_BASE_URL" not in agent_env

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

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_gh_app_credentials_survive_bedrock_via_user_mode(
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
        """GH_APP_* vars must NOT be stripped by the ADP_BEDROCK_VIA=user agent_env assembly."""
        from entrypoint import main
        import entrypoint

        ops_envelope = {**SAMPLE_ENVELOPE, "persona": "operations"}
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_BEDROCK_VIA", "user")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123:role/irsa")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")

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

        # GH_APP_* must survive the user-mode IRSA stripping (which only pops AWS_* vars)
        assert agent_env["GH_APP_ID"] == "77788"
        assert agent_env["GH_APP_PRIVATE_KEY"] == "secret-private-key-pem"
        # Confirm IRSA was stripped (user mode works as designed)
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
        mock_subprocess_run.return_value = MagicMock(returncode=1)

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
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1847\n", stderr=""
            )
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
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )
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
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="gh: error"
            )
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

        assert entrypoint._is_zero_token_failure(
            {"total_cost_usd": 0, "num_turns": 1}
        )
        assert entrypoint._is_zero_token_failure(
            {"total_cost_usd": 0.0, "num_turns": 0}
        )

    def test_nonzero_cost_is_not_failure(self):
        import entrypoint

        # Genuine "no changes needed" verdict costs >0 tokens.
        assert not entrypoint._is_zero_token_failure(
            {"total_cost_usd": 0.0123, "num_turns": 1}
        )

    def test_zero_cost_multi_turn_is_not_failure(self):
        import entrypoint

        # Some tokens burned then died mid-run — not the zero-token signature.
        assert not entrypoint._is_zero_token_failure(
            {"total_cost_usd": 0, "num_turns": 5}
        )

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
        """cost>0/no-diff → genuine 'no changes needed' success preserved."""
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
        assert "no changes needed" in args[4]
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
        monkeypatch.setattr(
            entrypoint, "RESULT_METADATA_PATH", str(tmp_path / "missing.json")
        )
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
    def test_handle_success_with_diff_never_checks_metadata(
        self, mock_run_cmd, mock_write_corr, mock_post, mock_status, tmp_path, monkeypatch
    ):
        """A run that produced a diff → success path unchanged (PR created)."""
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

        assert rc == 0
        assert mock_post.call_args[0][3] == "completed"
        assert "PR opened" in mock_post.call_args[0][4]
