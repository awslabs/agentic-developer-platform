"""
Unit tests for per-pod installation auth (issue #2088).

Covers:
- Asset with installation_id → mints token for THAT installation, clones with it
- Asset with installation_id NULL (public/shared) → clones unauthenticated, no per-install mint
- Mint returns 404/410 (revoked) → asset marked failed: access_revoked, pod aborts, NO fallback
- Worker never calls a "first installation" / central-App resolver for tenant content

Tests both github_auth.mint_installation_token() and the sqs-worker main() flow.
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add the ingestion directory to sys.path for import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))


# ---------------------------------------------------------------------------
# Tests for github_auth.mint_installation_token
# ---------------------------------------------------------------------------


class TestMintInstallationToken:
    """Verify mint_installation_token() per-installation behavior."""

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_mints_token_for_specific_installation(self, mock_settings, mock_run):
        """Token is minted for the exact installation_id passed — no discovery."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

        # Clear env
        env_backup = {k: os.environ.pop(k, None) for k in ("GIT_ASKPASS", "GIT_TERMINAL_PROMPT")}
        try:
            from github_auth import mint_installation_token

            result = mint_installation_token(installation_id=99887)

            assert result is True
            assert os.environ.get("GIT_ASKPASS") == "/app/git-credential-helper.sh"
            assert os.environ.get("GIT_TERMINAL_PROMPT") == "0"

            # Verify the command passes --installation-id and NOT --owner
            cmd = mock_run.call_args[0][0]
            assert "--installation-id" in cmd
            assert "99887" in cmd
            assert "--owner" not in cmd
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_raises_revoked_on_exit_code_2(self, mock_settings, mock_run):
        """Exit code 2 from github-app-token.py → InstallationRevokedError."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(
            returncode=2,
            stdout=b"",
            stderr=b"Installation 99887 not found (HTTP 404).",
        )

        from github_auth import InstallationRevokedError, mint_installation_token

        with pytest.raises(InstallationRevokedError) as exc_info:
            mint_installation_token(installation_id=99887)

        assert exc_info.value.installation_id == 99887
        assert (
            "revoked" in str(exc_info.value).lower() or "not found" in str(exc_info.value).lower()
        )

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_raises_runtime_error_on_other_failures(self, mock_settings, mock_run):
        """Non-zero exit code other than 2 → RuntimeError (transient error)."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.aws_region = "us-east-1"

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"AccessDeniedException",
        )

        from github_auth import mint_installation_token

        with pytest.raises(RuntimeError, match="exit code 1"):
            mint_installation_token(installation_id=12345)

    @patch("github_auth.settings")
    def test_raises_if_secrets_not_configured(self, mock_settings):
        """No app secrets → RuntimeError (not silent False like the global mint)."""
        mock_settings.github_app_id_secret = ""
        mock_settings.github_app_key_secret = ""

        from github_auth import mint_installation_token

        with pytest.raises(RuntimeError, match="secrets not configured"):
            mint_installation_token(installation_id=12345)

    @patch("subprocess.run")
    @patch("github_auth.settings")
    def test_raises_runtime_error_on_timeout(self, mock_settings, mock_run):
        """Timeout → RuntimeError."""
        mock_settings.github_app_id_secret = "adp/gh-app-ops-id"
        mock_settings.github_app_key_secret = "adp/gh-app-ops-key"
        mock_settings.aws_region = "us-east-1"

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="python", timeout=30)

        from github_auth import mint_installation_token

        with pytest.raises(RuntimeError, match="timed out"):
            mint_installation_token(installation_id=12345)


# ---------------------------------------------------------------------------
# Tests for sqs-worker main() per-installation auth flow
#
# sqs-worker.py uses a hyphen in its name, making it unimportable via normal
# Python import. We use importlib.util.spec_from_file_location to load it.
# Additionally, it imports heavy modules at module level (boto3, opentelemetry),
# so we mock those at import time and test only the auth routing logic.
# ---------------------------------------------------------------------------

_SQS_WORKER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "images", "ingestion", "sqs-worker.py"
)


def _import_sqs_worker():
    """Import sqs-worker.py as a module (it has a hyphen in the filename)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sqs_worker", _SQS_WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sqs_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSqsWorkerPerInstallationAuth:
    """Verify sqs-worker routes per-installation auth correctly."""

    def _make_sqs_message(self, installation_id=None, content_type="repo", source="org/repo"):
        """Helper to build an SQS message body."""
        msg = {
            "content_type": content_type,
            "source": source,
            "tags": {},
            "triggered_by": "api",
            "steps": ["clone"],
            "registry_asset_id": "asset-uuid-123",
            "scope": {"visibility": "tenant", "tenant_id": "t1"},
        }
        if installation_id is not None:
            msg["installation_id"] = installation_id
        return msg

    def test_installation_id_present_mints_per_installation(self):
        """When installation_id is in the message, per-installation mint is called."""
        sqs_worker = _import_sqs_worker()
        msg = self._make_sqs_message(installation_id=55555)

        mock_per_install = MagicMock(return_value=True)
        mock_global = MagicMock()

        with (
            patch.object(sqs_worker, "SQS_QUEUE_URL", "https://sqs.example.com/queue"),
            patch.object(sqs_worker, "receive_sqs_message", return_value=(msg, "r1")),
            patch.object(sqs_worker, "_mint_per_installation_token", mock_per_install),
            patch.object(sqs_worker, "_mint_github_token", mock_global),
            patch.object(sqs_worker, "delete_sqs_message"),
            patch.object(sqs_worker, "update_dynamo_status"),
            patch.object(sqs_worker, "write_dynamo_run_record"),
            patch.object(sqs_worker, "emit_status_callback"),
            patch.object(sqs_worker, "ingest_repo"),
            patch.object(sqs_worker, "safe_emit"),
        ):
            sqs_worker.main()

        # Per-installation mint called with the correct ID
        mock_per_install.assert_called_once_with(55555)
        # Global mint NOT called
        mock_global.assert_not_called()

    def test_no_installation_id_uses_global_mint(self):
        """When installation_id is absent, global mint (unauthenticated fallback) is used."""
        sqs_worker = _import_sqs_worker()
        msg = self._make_sqs_message(installation_id=None)

        mock_per_install = MagicMock()
        mock_global = MagicMock(return_value=False)

        with (
            patch.object(sqs_worker, "SQS_QUEUE_URL", "https://sqs.example.com/queue"),
            patch.object(sqs_worker, "receive_sqs_message", return_value=(msg, "r2")),
            patch.object(sqs_worker, "_mint_per_installation_token", mock_per_install),
            patch.object(sqs_worker, "_mint_github_token", mock_global),
            patch.object(sqs_worker, "delete_sqs_message"),
            patch.object(sqs_worker, "update_dynamo_status"),
            patch.object(sqs_worker, "write_dynamo_run_record"),
            patch.object(sqs_worker, "emit_status_callback"),
            patch.object(sqs_worker, "ingest_repo"),
            patch.object(sqs_worker, "safe_emit"),
        ):
            sqs_worker.main()

        # Global mint called (public/shared path)
        mock_global.assert_called_once()
        # Per-installation mint NOT called
        mock_per_install.assert_not_called()

    def test_revoked_installation_marks_failed_and_aborts(self):
        """Revoked installation → asset marked failed: access_revoked, pod exits."""
        from github_auth import InstallationRevokedError

        sqs_worker = _import_sqs_worker()
        msg = self._make_sqs_message(installation_id=77777)

        mock_per_install = MagicMock(
            side_effect=InstallationRevokedError(installation_id=77777, detail="HTTP 404")
        )
        mock_global = MagicMock()
        mock_dynamo = MagicMock()
        mock_run_record = MagicMock()
        mock_callback = MagicMock()
        mock_delete = MagicMock()

        with (
            patch.object(sqs_worker, "SQS_QUEUE_URL", "https://sqs.example.com/queue"),
            patch.object(sqs_worker, "receive_sqs_message", return_value=(msg, "r3")),
            patch.object(sqs_worker, "_mint_per_installation_token", mock_per_install),
            patch.object(sqs_worker, "_mint_github_token", mock_global),
            patch.object(sqs_worker, "delete_sqs_message", mock_delete),
            patch.object(sqs_worker, "update_dynamo_status", mock_dynamo),
            patch.object(sqs_worker, "write_dynamo_run_record", mock_run_record),
            patch.object(sqs_worker, "emit_status_callback", mock_callback),
            patch.object(sqs_worker, "safe_emit") as mock_safe_emit,
            pytest.raises(SystemExit) as exc_info,
        ):
            sqs_worker.main()

        assert exc_info.value.code == 1

        # DynamoDB marked as failed with access_revoked
        mock_dynamo.assert_called_once()
        dynamo_args = mock_dynamo.call_args
        assert dynamo_args[0][2] == "failed"  # status positional arg
        assert "access_revoked" in dynamo_args[1]["error"]

        # Run record written with access_revoked error
        mock_run_record.assert_called_once()
        run_args = mock_run_record.call_args
        assert run_args[0][2] == "failed"
        assert "access_revoked" in run_args[1]["error"]

        # Status callback emitted via safe_emit with access_revoked
        # safe_emit wraps emit_status_callback — check that it was called
        # with the correct args (safe_emit(fn, *args, **kwargs))
        safe_emit_calls = mock_safe_emit.call_args_list
        # Find the callback call with "failed" status
        callback_call = None
        for c in safe_emit_calls:
            if len(c[0]) >= 3 and c[0][1] == "asset-uuid-123" and c[0][2] == "failed":
                callback_call = c
                break
        assert callback_call is not None, (
            f"Expected safe_emit call with 'failed', got: {safe_emit_calls}"
        )
        assert callback_call[1]["status_detail"]["reason"] == "access_revoked"

        # Message deleted (terminal failure — won't retry)
        mock_delete.assert_called_once_with("r3")

        # Global mint NEVER called as fallback
        mock_global.assert_not_called()

    def test_non_repo_content_type_skips_all_github_auth(self):
        """Non-repo content types (url, doc, infra) skip GitHub auth entirely."""
        sqs_worker = _import_sqs_worker()
        msg = self._make_sqs_message(
            installation_id=11111, content_type="url", source="https://example.com"
        )

        mock_per_install = MagicMock()
        mock_global = MagicMock()

        with (
            patch.object(sqs_worker, "SQS_QUEUE_URL", "https://sqs.example.com/queue"),
            patch.object(sqs_worker, "receive_sqs_message", return_value=(msg, "r4")),
            patch.object(sqs_worker, "_mint_per_installation_token", mock_per_install),
            patch.object(sqs_worker, "_mint_github_token", mock_global),
            patch.object(sqs_worker, "delete_sqs_message"),
            patch.object(sqs_worker, "update_dynamo_status"),
            patch.object(sqs_worker, "write_dynamo_run_record"),
            patch.object(sqs_worker, "emit_status_callback"),
            patch.object(sqs_worker, "ingest_url"),
            patch.object(sqs_worker, "safe_emit"),
        ):
            sqs_worker.main()

        # Neither mint function called for non-repo content
        mock_per_install.assert_not_called()
        mock_global.assert_not_called()
