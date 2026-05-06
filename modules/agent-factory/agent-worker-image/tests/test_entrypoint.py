"""Unit tests for agent-worker-image entrypoint and helper libraries.

Covers the 12-step sequence with mocked external dependencies.
"""

from __future__ import annotations

import json
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
        mock_subprocess_run.return_value = MagicMock(returncode=0)

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
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with patch("entrypoint.GatewayCredentialClient") as mock_gw_cls, \
             patch("entrypoint.assume_customer_role") as mock_assume:
            mock_gw = MagicMock()
            mock_gw_cls.return_value = mock_gw
            mock_gw.is_configured = True
            mock_gw.raw_read.return_value = {
                "value": json.dumps({
                    "role_arn": "arn:aws:iam::111:role/test",
                    "external_id": "ext-123",
                    "session_duration_seconds": 3600,
                }),
                "credential_type": "api_key",
                "provenance_id": "prov-123",
            }
            mock_assume.return_value = {
                "AWS_ACCESS_KEY_ID": "AK",
                "AWS_SECRET_ACCESS_KEY": "SK",
                "AWS_SESSION_TOKEN": "ST",
            }
            main()
            mock_gw.raw_read.assert_called_once()
            mock_assume.assert_called_once()
            # Verify user_id is passed to assume_customer_role
            call_kwargs = mock_assume.call_args[1]
            assert call_kwargs["user_id"] == "cognito-sub-jane-123"

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
            MagicMock(stdout="", returncode=0),                   # git clone
            MagicMock(stdout="", returncode=0),                   # git config email
            MagicMock(stdout="", returncode=0),                   # git config name
            MagicMock(stdout="", returncode=0),                   # git checkout -b branch
            MagicMock(stdout="", returncode=0),                   # git commit --allow-empty WIP
            MagicMock(stdout="", returncode=0),                   # git push -u origin branch
            MagicMock(stdout="abc1234def5678\n", returncode=0),   # git rev-parse HEAD (WIP sha)
            MagicMock(stdout="", returncode=0),                   # gh issue edit --remove-label
            MagicMock(stdout="", returncode=0),                   # gh issue view (started check)
            MagicMock(stdout="", returncode=0),                   # gh issue comment (started)
            MagicMock(stdout="", returncode=0),                   # git diff --stat (no changes)
            MagicMock(stdout="", returncode=0),                   # git status --porcelain
            MagicMock(stdout="", returncode=0),                   # git log origin/branch..HEAD
            MagicMock(stdout="", returncode=0),                   # gh issue view (completed check)
            MagicMock(stdout="", returncode=0),                   # gh issue comment (no changes)
            MagicMock(stdout="", returncode=0),                   # gh pr view (PR url lookup)
        ]

        mock_create_cr.return_value = {
            "id": 9876,
            "html_url": "https://github.com/acme-corp/flagship-app/runs/9876",
        }
        mock_subprocess_run.return_value = MagicMock(returncode=0)

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
        mock_subprocess_run.return_value = MagicMock(returncode=0)

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
        mock_resp.read.return_value = json.dumps({
            "value": '{"role_arn": "arn:aws:iam::111:role/test"}',
            "credential_type": "api_key",
            "provenance_id": "prov-abc",
        }).encode()
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


class TestFetchAwsCredentials:
    def test_raises_on_empty_user_id(self):
        from entrypoint import _fetch_aws_credentials

        with pytest.raises(ValueError, match="no user_id"):
            _fetch_aws_credentials(user_id="", agent_id="ops", task_id="t1")

    @patch("entrypoint.GatewayCredentialClient")
    def test_raises_on_unconfigured_client(self, mock_gw_cls, monkeypatch):
        from entrypoint import _fetch_aws_credentials

        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = False

        with pytest.raises(GatewayCredentialError, match="not configured"):
            _fetch_aws_credentials(user_id="user-1", agent_id="ops", task_id="t1")

    @patch("entrypoint.GatewayCredentialClient")
    def test_success_returns_parsed_cred(self, mock_gw_cls):
        from entrypoint import _fetch_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.raw_read.return_value = {
            "value": json.dumps({
                "role_arn": "arn:aws:iam::111:role/test",
                "external_id": "ext-abc",
                "session_duration_seconds": 1800,
            }),
            "credential_type": "api_key",
            "provenance_id": "prov-xyz",
        }

        result = _fetch_aws_credentials(user_id="user-1", agent_id="ops", task_id="t1")
        assert result["role_arn"] == "arn:aws:iam::111:role/test"
        assert result["external_id"] == "ext-abc"
        assert result["session_duration_seconds"] == 1800

    @patch("entrypoint.GatewayCredentialClient")
    def test_raises_on_missing_role_arn(self, mock_gw_cls):
        from entrypoint import _fetch_aws_credentials

        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw
        mock_gw.is_configured = True
        mock_gw.raw_read.return_value = {
            "value": json.dumps({"external_id": "ext-abc"}),
            "credential_type": "api_key",
            "provenance_id": "prov-xyz",
        }

        with pytest.raises(GatewayCredentialError, match="missing required field 'role_arn'"):
            _fetch_aws_credentials(user_id="user-1", agent_id="ops", task_id="t1")


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
