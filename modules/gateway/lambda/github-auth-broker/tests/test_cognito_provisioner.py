"""Tests for cognito_provisioner (Issue #600 — custom:org_id write).

Coverage:
  - write_org_id_attribute calls admin_update_user_attributes with correct params
  - write_org_id_attribute skips when org_id is empty
  - _update_user_attributes includes custom:org_id when provided
  - _update_user_attributes omits custom:org_id when not provided
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# The cognito_provisioner module uses boto3 directly, so we mock at that level.


class TestWriteOrgIdAttribute:
    """Tests for the standalone write_org_id_attribute function."""

    @patch("cognito_provisioner.boto3.client")
    def test_writes_org_id_to_cognito(self, mock_boto_client):
        """When org_id is non-empty, admin_update_user_attributes is called."""
        from cognito_provisioner import write_org_id_attribute

        mock_cognito = MagicMock()
        mock_boto_client.return_value = mock_cognito

        write_org_id_attribute(
            user_pool_id="us-east-1_TestPool",
            username="GitHub_12345",
            org_id="org-acme",
        )

        mock_cognito.admin_update_user_attributes.assert_called_once_with(
            UserPoolId="us-east-1_TestPool",
            Username="GitHub_12345",
            UserAttributes=[
                {"Name": "custom:org_id", "Value": "org-acme"},
            ],
        )

    @patch("cognito_provisioner.boto3.client")
    def test_skips_when_org_id_empty(self, mock_boto_client):
        """When org_id is empty, no Cognito call is made."""
        from cognito_provisioner import write_org_id_attribute

        mock_cognito = MagicMock()
        mock_boto_client.return_value = mock_cognito

        write_org_id_attribute(
            user_pool_id="us-east-1_TestPool",
            username="GitHub_12345",
            org_id="",
        )

        mock_cognito.admin_update_user_attributes.assert_not_called()

    @patch("cognito_provisioner.boto3.client")
    def test_handles_client_error_gracefully(self, mock_boto_client):
        """ClientError is caught and logged, not raised."""
        from botocore.exceptions import ClientError

        from cognito_provisioner import write_org_id_attribute

        mock_cognito = MagicMock()
        mock_boto_client.return_value = mock_cognito
        mock_cognito.admin_update_user_attributes.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": "User not found"}},
            "AdminUpdateUserAttributes",
        )

        # Should not raise
        write_org_id_attribute(
            user_pool_id="us-east-1_TestPool",
            username="GitHub_99999",
            org_id="org-acme",
        )


class TestUpdateUserAttributesOrgId:
    """Tests that _update_user_attributes includes custom:org_id when provided."""

    @patch("cognito_provisioner.boto3.client")
    def test_includes_org_id_when_provided(self, mock_boto_client):
        """_update_user_attributes includes custom:org_id in attributes list."""
        from cognito_provisioner import _update_user_attributes

        mock_cognito = MagicMock()

        _update_user_attributes(
            client=mock_cognito,
            user_pool_id="us-east-1_TestPool",
            username="GitHub_12345",
            email="user@example.com",
            name="Test User",
            github_login="testuser",
            avatar_url="https://github.com/testuser.png",
            org_id="org-acme",
        )

        call_args = mock_cognito.admin_update_user_attributes.call_args
        attributes = call_args[1]["UserAttributes"] if call_args[1] else call_args.kwargs["UserAttributes"]
        attr_names = [a["Name"] for a in attributes]
        assert "custom:org_id" in attr_names
        org_attr = next(a for a in attributes if a["Name"] == "custom:org_id")
        assert org_attr["Value"] == "org-acme"

    @patch("cognito_provisioner.boto3.client")
    def test_omits_org_id_when_empty(self, mock_boto_client):
        """_update_user_attributes does NOT include custom:org_id when org_id is empty."""
        from cognito_provisioner import _update_user_attributes

        mock_cognito = MagicMock()

        _update_user_attributes(
            client=mock_cognito,
            user_pool_id="us-east-1_TestPool",
            username="GitHub_12345",
            email="user@example.com",
            name="Test User",
            github_login="testuser",
            avatar_url="https://github.com/testuser.png",
            org_id="",
        )

        call_args = mock_cognito.admin_update_user_attributes.call_args
        attributes = call_args[1]["UserAttributes"] if call_args[1] else call_args.kwargs["UserAttributes"]
        attr_names = [a["Name"] for a in attributes]
        assert "custom:org_id" not in attr_names

    @patch("cognito_provisioner.boto3.client")
    def test_omits_org_id_when_not_specified(self, mock_boto_client):
        """_update_user_attributes does NOT include custom:org_id by default."""
        from cognito_provisioner import _update_user_attributes

        mock_cognito = MagicMock()

        _update_user_attributes(
            client=mock_cognito,
            user_pool_id="us-east-1_TestPool",
            username="GitHub_12345",
            email="user@example.com",
            name="Test User",
            github_login="testuser",
            avatar_url="https://github.com/testuser.png",
        )

        call_args = mock_cognito.admin_update_user_attributes.call_args
        attributes = call_args[1]["UserAttributes"] if call_args[1] else call_args.kwargs["UserAttributes"]
        attr_names = [a["Name"] for a in attributes]
        assert "custom:org_id" not in attr_names
