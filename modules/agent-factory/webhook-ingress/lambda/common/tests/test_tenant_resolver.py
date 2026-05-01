"""Tests for tenant resolver."""

from unittest.mock import MagicMock, patch


class TestResolveTenant:
    @patch("common.tenant_resolver._dynamodb", None)
    @patch("common.tenant_resolver.boto3")
    def test_found_tenant(self, mock_boto3: MagicMock) -> None:
        from common.tenant_resolver import resolve_tenant

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "github#installation#12345",
                "tenant_id": "tenant-abc",
                "org_name": "my-org",
                "plan": "pro",
            }
        }

        result = resolve_tenant(12345)

        assert result is not None
        assert result["tenant_id"] == "tenant-abc"
        assert result["org_name"] == "my-org"
        assert result["plan"] == "pro"
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "github#installation#12345"}
        )

    @patch("common.tenant_resolver._dynamodb", None)
    @patch("common.tenant_resolver.boto3")
    def test_not_found(self, mock_boto3: MagicMock) -> None:
        from common.tenant_resolver import resolve_tenant

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {}

        result = resolve_tenant(99999)

        assert result is None

    @patch("common.tenant_resolver._dynamodb", None)
    @patch("common.tenant_resolver.boto3")
    def test_missing_optional_fields(self, mock_boto3: MagicMock) -> None:
        from common.tenant_resolver import resolve_tenant

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "github#installation#111",
                "tenant_id": "t1",
            }
        }

        result = resolve_tenant(111)

        assert result is not None
        assert result["tenant_id"] == "t1"
        assert result["org_name"] == ""
        assert result["plan"] == "free"

    @patch("common.tenant_resolver._dynamodb", None)
    @patch("common.tenant_resolver.boto3")
    def test_ddb_error_returns_none(self, mock_boto3: MagicMock) -> None:
        from common.tenant_resolver import resolve_tenant

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.side_effect = Exception("DDB timeout")

        result = resolve_tenant(12345)

        assert result is None
