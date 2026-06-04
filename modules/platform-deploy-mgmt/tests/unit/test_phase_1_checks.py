"""Unit tests for Phase 1 checks — mock boto3 calls and verify PASS/FAIL logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from platform_deploy_mgmt.checks.boto_helpers import Context
from platform_deploy_mgmt.checks.phase_1 import (
    check_1_1_state_bucket_exists,
    check_1_2_versioning_enabled,
    check_1_3_encryption_enabled,
    check_1_4_public_access_blocked,
    check_1_5_lock_table_exists,
    check_1_6_lock_table_has_lock_id_key,
    check_1_7_backend_tfvars_substituted,
    check_1_8_state_bucket_not_empty,
)
from platform_deploy_mgmt.checks.shape import Result, Severity


def _make_ctx(account_id: str = "443458828159") -> Context:
    """Create a test context with a mock customer session."""
    return Context(
        customer_account_id=account_id,
        region="us-east-1",
        environment="dev",
        customer_session=MagicMock(),
        platform_session=MagicMock(),
    )


def _client_error(code: str, message: str = "Error") -> ClientError:
    """Create a ClientError with the given error code."""
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


class TestCheck_1_1_StateBucketExists:
    """1.1: State bucket exists in target account."""

    def test_pass_when_bucket_exists(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.head_bucket.return_value = {}

        result = check_1_1_state_bucket_exists(ctx)
        assert result.result == Result.PASS
        assert result.severity == Severity.HARD
        assert "adp-terraform-state-443458828159" in result.detail
        mock_s3.head_bucket.assert_called_once_with(Bucket="adp-terraform-state-443458828159")

    def test_fail_when_bucket_missing(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.head_bucket.side_effect = _client_error("404", "Not Found")

        result = check_1_1_state_bucket_exists(ctx)
        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD
        assert "404" in result.detail

    def test_fail_when_access_denied(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.head_bucket.side_effect = _client_error("403", "Forbidden")

        result = check_1_1_state_bucket_exists(ctx)
        assert result.result == Result.FAIL
        assert "403" in result.detail


class TestCheck_1_2_VersioningEnabled:
    """1.2: State bucket versioning enabled."""

    def test_pass_when_versioning_enabled(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_versioning.return_value = {"Status": "Enabled"}

        result = check_1_2_versioning_enabled(ctx)
        assert result.result == Result.PASS

    def test_fail_when_versioning_suspended(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_versioning.return_value = {"Status": "Suspended"}

        result = check_1_2_versioning_enabled(ctx)
        assert result.result == Result.FAIL
        assert "Suspended" in result.detail

    def test_fail_when_versioning_never_set(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_versioning.return_value = {}

        result = check_1_2_versioning_enabled(ctx)
        assert result.result == Result.FAIL
        assert "Disabled" in result.detail


class TestCheck_1_3_EncryptionEnabled:
    """1.3: State bucket encryption enabled."""

    def test_pass_when_aes256(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            }
        }

        result = check_1_3_encryption_enabled(ctx)
        assert result.result == Result.PASS
        assert "AES256" in result.detail

    def test_pass_when_kms(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
            }
        }

        result = check_1_3_encryption_enabled(ctx)
        assert result.result == Result.PASS

    def test_fail_when_no_encryption(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_bucket_encryption.side_effect = _client_error(
            "ServerSideEncryptionConfigurationNotFoundError", "Not found"
        )

        result = check_1_3_encryption_enabled(ctx)
        assert result.result == Result.FAIL


class TestCheck_1_4_PublicAccessBlocked:
    """1.4: State bucket public access blocked."""

    def test_pass_when_all_blocked(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_public_access_block.return_value = {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

        result = check_1_4_public_access_blocked(ctx)
        assert result.result == Result.PASS

    def test_fail_when_partial_block(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_public_access_block.return_value = {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

        result = check_1_4_public_access_blocked(ctx)
        assert result.result == Result.FAIL

    def test_fail_when_no_config(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.get_public_access_block.side_effect = _client_error("NoSuchPublicAccessBlockConfiguration", "Not found")

        result = check_1_4_public_access_blocked(ctx)
        assert result.result == Result.FAIL


class TestCheck_1_5_LockTableExists:
    """1.5: DynamoDB lock table exists and active."""

    def test_pass_when_active(self):
        ctx = _make_ctx()
        mock_ddb = MagicMock()
        ctx.customer_session.client.return_value = mock_ddb
        mock_ddb.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}

        result = check_1_5_lock_table_exists(ctx)
        assert result.result == Result.PASS
        mock_ddb.describe_table.assert_called_once_with(TableName="adp-terraform-locks")

    def test_fail_when_creating(self):
        ctx = _make_ctx()
        mock_ddb = MagicMock()
        ctx.customer_session.client.return_value = mock_ddb
        mock_ddb.describe_table.return_value = {"Table": {"TableStatus": "CREATING"}}

        result = check_1_5_lock_table_exists(ctx)
        assert result.result == Result.FAIL
        assert "CREATING" in result.detail

    def test_fail_when_not_found(self):
        ctx = _make_ctx()
        mock_ddb = MagicMock()
        ctx.customer_session.client.return_value = mock_ddb
        mock_ddb.describe_table.side_effect = _client_error("ResourceNotFoundException", "Not found")

        result = check_1_5_lock_table_exists(ctx)
        assert result.result == Result.FAIL


class TestCheck_1_6_LockTableHasLockIdKey:
    """1.6: Lock table has LockID partition key."""

    def test_pass_when_lock_id_is_hash_key(self):
        ctx = _make_ctx()
        mock_ddb = MagicMock()
        ctx.customer_session.client.return_value = mock_ddb
        mock_ddb.describe_table.return_value = {
            "Table": {"KeySchema": [{"AttributeName": "LockID", "KeyType": "HASH"}]}
        }

        result = check_1_6_lock_table_has_lock_id_key(ctx)
        assert result.result == Result.PASS

    def test_fail_when_wrong_key_name(self):
        ctx = _make_ctx()
        mock_ddb = MagicMock()
        ctx.customer_session.client.return_value = mock_ddb
        mock_ddb.describe_table.return_value = {"Table": {"KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}]}}

        result = check_1_6_lock_table_has_lock_id_key(ctx)
        assert result.result == Result.FAIL
        assert "id" in result.detail


class TestCheck_1_7_BackendTfvarsSubstituted:
    """1.7: backend.tfvars contains account ID."""

    def test_pass_when_account_id_present(self, tmp_path):
        ctx = _make_ctx("443458828159")
        tfvars_dir = tmp_path / "environments" / "dev"
        tfvars_dir.mkdir(parents=True)
        (tfvars_dir / "backend.tfvars").write_text('bucket = "adp-terraform-state-443458828159"\n')
        with patch.dict("os.environ", {"GITHUB_WORKSPACE": str(tmp_path)}):
            result = check_1_7_backend_tfvars_substituted(ctx)
        assert result.result == Result.PASS

    def test_fail_when_placeholder_present(self, tmp_path):
        ctx = _make_ctx("443458828159")
        tfvars_dir = tmp_path / "environments" / "dev"
        tfvars_dir.mkdir(parents=True)
        (tfvars_dir / "backend.tfvars").write_text('bucket = "adp-terraform-state-ACCOUNT_ID"\n')
        with patch.dict("os.environ", {"GITHUB_WORKSPACE": str(tmp_path)}):
            result = check_1_7_backend_tfvars_substituted(ctx)
        assert result.result == Result.FAIL
        assert result.severity == Severity.SOFT

    def test_fail_when_file_missing(self, tmp_path):
        ctx = _make_ctx("443458828159")
        with patch.dict("os.environ", {"GITHUB_WORKSPACE": str(tmp_path)}):
            result = check_1_7_backend_tfvars_substituted(ctx)
        assert result.result == Result.FAIL


class TestCheck_1_8_StateBucketNotEmpty:
    """1.8: State bucket contains state files."""

    def test_pass_when_objects_present(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.list_objects_v2.return_value = {"KeyCount": 5}

        result = check_1_8_state_bucket_not_empty(ctx)
        assert result.result == Result.PASS
        assert result.severity == Severity.SOFT

    def test_fail_when_empty(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.list_objects_v2.return_value = {"KeyCount": 0}

        result = check_1_8_state_bucket_not_empty(ctx)
        assert result.result == Result.FAIL

    def test_fail_on_error(self):
        ctx = _make_ctx()
        mock_s3 = MagicMock()
        ctx.customer_session.client.return_value = mock_s3
        mock_s3.list_objects_v2.side_effect = _client_error("AccessDenied", "Forbidden")

        result = check_1_8_state_bucket_not_empty(ctx)
        assert result.result == Result.FAIL


class TestCheckRegistry:
    """Verify the CHECKS registry is well-formed."""

    def test_all_checks_have_unique_ids(self):
        from platform_deploy_mgmt.checks.phase_1 import CHECKS

        ids = [c[0] for c in CHECKS]
        assert len(ids) == len(set(ids))

    def test_all_checks_are_callable(self):
        from platform_deploy_mgmt.checks.phase_1 import CHECKS

        for check_id, name, fn, severity, cost_class in CHECKS:
            assert callable(fn)
            assert isinstance(severity, Severity)
            assert isinstance(cost_class, str) or hasattr(cost_class, "value")

    def test_eight_checks_defined(self):
        from platform_deploy_mgmt.checks.phase_1 import CHECKS

        assert len(CHECKS) == 8

    def test_ids_are_sequential(self):
        from platform_deploy_mgmt.checks.phase_1 import CHECKS

        expected_ids = [f"1.{i}" for i in range(1, 9)]
        actual_ids = [c[0] for c in CHECKS]
        assert actual_ids == expected_ids
