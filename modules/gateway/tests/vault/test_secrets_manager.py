"""Unit tests for the Secrets Manager helper (mock boto3).

Issue #134: Vault Phase 1
"""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.shared.services.secrets_manager import (
    MAX_SECRET_SIZE_BYTES,
    SecretsManagerHelper,
    SecretTooLargeError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sm_client():
    """A mock boto3 Secrets Manager client."""
    client = MagicMock()
    client.create_secret.return_value = {
        "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/users/sub123/github-abcd1234-XyZaB",
        "Name": "adp/users/sub123/github-abcd1234",
    }
    client.get_secret_value.return_value = {
        "SecretString": '{"token": "ghp_xxx"}',
    }
    client.update_secret.return_value = {}
    client.delete_secret.return_value = {}
    return client


@pytest.fixture
def helper(mock_sm_client):
    return SecretsManagerHelper(client=mock_sm_client)


# ---------------------------------------------------------------------------
# create_secret
# ---------------------------------------------------------------------------


class TestCreateSecret:
    def test_returns_arn(self, helper, mock_sm_client):
        arn = helper.create_secret("sub123", "github", "my-token", {"token": "ghp_xxx"})
        assert arn.startswith("arn:aws:secretsmanager:")
        mock_sm_client.create_secret.assert_called_once()

    def test_passes_correct_name_prefix(self, helper, mock_sm_client):
        helper.create_secret("user-sub", "openai", "key", "sk-123")
        call_kwargs = mock_sm_client.create_secret.call_args.kwargs
        assert call_kwargs["Name"].startswith("adp/users/user-sub/openai-")

    def test_serialises_dict_payload(self, helper, mock_sm_client):
        helper.create_secret("sub", "svc", "lbl", {"key": "val"})
        call_kwargs = mock_sm_client.create_secret.call_args.kwargs
        assert json.loads(call_kwargs["SecretString"]) == {"key": "val"}

    def test_string_payload_passed_directly(self, helper, mock_sm_client):
        helper.create_secret("sub", "svc", "lbl", "raw-secret")
        call_kwargs = mock_sm_client.create_secret.call_args.kwargs
        assert call_kwargs["SecretString"] == "raw-secret"

    def test_tags_applied(self, helper, mock_sm_client):
        helper.create_secret("sub123", "github", "my-label", "secret")
        call_kwargs = mock_sm_client.create_secret.call_args.kwargs
        tags = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
        assert tags["adp:user_sub"] == "sub123"
        assert tags["adp:service"] == "github"
        assert tags["adp:label"] == "my-label"

    def test_rejects_oversized_payload(self, helper):
        big = "x" * (MAX_SECRET_SIZE_BYTES + 1)
        with pytest.raises(SecretTooLargeError):
            helper.create_secret("sub", "svc", "lbl", big)

    def test_accepts_exactly_max_payload(self, helper, mock_sm_client):
        exact = "x" * MAX_SECRET_SIZE_BYTES
        arn = helper.create_secret("sub", "svc", "lbl", exact)
        assert arn is not None


# ---------------------------------------------------------------------------
# get_secret
# ---------------------------------------------------------------------------


class TestGetSecret:
    def test_returns_secret_string(self, helper, mock_sm_client):
        result = helper.get_secret("arn:aws:secretsmanager:us-east-1:123:secret:test")
        assert result == '{"token": "ghp_xxx"}'
        mock_sm_client.get_secret_value.assert_called_once_with(SecretId="arn:aws:secretsmanager:us-east-1:123:secret:test")


# ---------------------------------------------------------------------------
# update_secret
# ---------------------------------------------------------------------------


class TestUpdateSecret:
    def test_update_string(self, helper, mock_sm_client):
        helper.update_secret("arn:fake", "new-value")
        mock_sm_client.update_secret.assert_called_once_with(SecretId="arn:fake", SecretString="new-value")

    def test_update_dict_serialised(self, helper, mock_sm_client):
        helper.update_secret("arn:fake", {"new": "data"})
        call_kwargs = mock_sm_client.update_secret.call_args.kwargs
        assert json.loads(call_kwargs["SecretString"]) == {"new": "data"}

    def test_rejects_oversized_update(self, helper):
        with pytest.raises(SecretTooLargeError):
            helper.update_secret("arn:fake", "x" * (MAX_SECRET_SIZE_BYTES + 1))


# ---------------------------------------------------------------------------
# delete_secret
# ---------------------------------------------------------------------------


class TestDeleteSecret:
    def test_delete_with_force(self, helper, mock_sm_client):
        helper.delete_secret("arn:to-delete")
        mock_sm_client.delete_secret.assert_called_once_with(
            SecretId="arn:to-delete",
            ForceDeleteWithoutRecovery=True,
        )

    def test_delete_without_force(self, helper, mock_sm_client):
        helper.delete_secret("arn:to-delete", force=False)
        mock_sm_client.delete_secret.assert_called_once_with(SecretId="arn:to-delete")

    def test_delete_already_gone(self, helper, mock_sm_client):
        """ResourceNotFoundException should not raise."""
        mock_sm_client.delete_secret.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "DeleteSecret",
        )
        # Should not raise
        helper.delete_secret("arn:gone")

    def test_delete_retries_on_transient_error(self, helper, mock_sm_client):
        """Non-ResourceNotFound errors are retried up to 3 times."""
        error = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "oops"}},
            "DeleteSecret",
        )
        mock_sm_client.delete_secret.side_effect = [error, error, {}]
        helper.delete_secret("arn:retry")
        assert mock_sm_client.delete_secret.call_count == 3

    def test_delete_raises_after_max_retries(self, helper, mock_sm_client):
        error = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "oops"}},
            "DeleteSecret",
        )
        mock_sm_client.delete_secret.side_effect = error
        with pytest.raises(ClientError):
            helper.delete_secret("arn:fail")
        assert mock_sm_client.delete_secret.call_count == 3


# ---------------------------------------------------------------------------
# Size cap edge cases
# ---------------------------------------------------------------------------


class TestSizeCap:
    def test_64kb_boundary_string(self, helper, mock_sm_client):
        """Exactly 65536 bytes should be accepted."""
        helper.create_secret("sub", "svc", "lbl", "a" * MAX_SECRET_SIZE_BYTES)
        mock_sm_client.create_secret.assert_called_once()

    def test_64kb_plus_one_rejected(self, helper):
        with pytest.raises(SecretTooLargeError):
            helper.create_secret("sub", "svc", "lbl", "a" * (MAX_SECRET_SIZE_BYTES + 1))

    def test_multibyte_chars_counted_as_bytes(self, helper):
        """Unicode chars take >1 byte; ensure we check byte length."""
        # Each emoji is 4 bytes in UTF-8
        emojis = "\U0001f600" * (MAX_SECRET_SIZE_BYTES // 4 + 1)
        with pytest.raises(SecretTooLargeError):
            helper.create_secret("sub", "svc", "lbl", emojis)

    def test_bytes_payload_accepted(self, helper, mock_sm_client):
        """bytes input should work too."""
        data = b"binary-content"
        # SecretsManagerHelper._validate_payload_size handles bytes directly
        from src.shared.services.secrets_manager import SecretsManagerHelper

        validated = SecretsManagerHelper._validate_payload_size(data)
        assert validated == data
