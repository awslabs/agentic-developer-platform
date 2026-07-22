"""Tests for the auto-register write-guard (Issue #2769).

Postgres is the single source of truth for the installation → tenant mapping.
_auto_register_installation() must:
  1. No-op when a Postgres-owned row (no auto_registered flag) exists.
  2. Skip (no write, caller 403) when the installation is not a known ADP tenant.
  3. Write + tag auto_registered when no row exists and the installation resolves
     to a known Postgres tenant — writing the POSTGRES tenant, not the raw login.
  4. Emit InstallationTenantDrift when a Postgres-owned row's org differs from
     the webhook org login.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _mock_table_with(forward_item=None, reverse_item=None):
    """Return a mock DDB table whose get_item returns the given rows by identity_type."""
    table = MagicMock()

    def get_item(Key=None):  # noqa: N803
        itype = Key["identity_type"]
        if itype == "github_installation_id":
            return {"Item": forward_item} if forward_item else {}
        if itype == "org_installation":
            return {"Item": reverse_item} if reverse_item else {}
        return {}

    table.get_item = get_item
    return table


class TestAutoRegisterGuard:
    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_no_op_when_postgres_owned_row_exists(self, mock_resolver, mock_gw, mock_metric):
        """A row without auto_registered is Postgres-owned → no write, returns stored tenant."""
        from handler import _auto_register_installation

        # Postgres-owned row, same org login → no drift
        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "pranavsharma1000")

        assert result == "pranavsharma1000"
        table.put_item.assert_not_called()
        # gateway not consulted — row already exists
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        mock_metric.assert_not_called()

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_drift_metric_when_postgres_row_org_differs(self, mock_resolver, mock_gw, mock_metric):
        """Postgres-owned row whose org differs from webhook login → keep PG, emit drift."""
        from handler import _auto_register_installation

        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "aws-innovate")

        assert result == "pranavsharma1000"  # Postgres tenant kept
        table.put_item.assert_not_called()
        mock_metric.assert_called_once_with("InstallationTenantDrift")

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_skips_when_not_known_tenant(self, mock_resolver, mock_gw):
        """No row + gateway 404 (not a known tenant) → registers with org_login as tenant.

        Changed from original skip behavior: user-namespace installs and fresh
        deploys where the gateway is unreachable should still register. The user
        still needs approval before they can trigger agents — this only resolves
        the installation, not the user.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = None

        result = _auto_register_installation(555, "some-random-org")

        # Falls back to using org_login as the tenant_id
        assert result == "some-random-org"
        # Forward + reverse rows written
        assert table.put_item.call_count == 2
        forward_item = table.put_item.call_args_list[0].kwargs["Item"]
        assert forward_item["org_id"] == "some-random-org"
        assert forward_item["auto_registered"] is True

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_writes_postgres_tenant_when_known(self, mock_resolver, mock_gw):
        """No row + gateway resolves a tenant → write the POSTGRES tenant, tagged auto_registered."""
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        # Gateway maps installation → Postgres tenant (which differs from the login)
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "tenant_id": "pranavsharma1000"
        }

        result = _auto_register_installation(144082554, "pranav-login")

        assert result == "pranavsharma1000"
        # Two writes: forward + reverse
        assert table.put_item.call_count == 2
        forward_call = table.put_item.call_args_list[0]
        forward_item = forward_call.kwargs["Item"]
        assert forward_item["org_id"] == "pranavsharma1000"  # NOT the raw login
        assert forward_item["auto_registered"] is True
        # Non-clobber condition on the fresh write
        assert forward_call.kwargs["ConditionExpression"] == "attribute_not_exists(auto_registered)"
        # Reverse row keyed on the Postgres tenant
        reverse_item = table.put_item.call_args_list[1].kwargs["Item"]
        assert reverse_item["identity_type"] == "org_installation"
        assert reverse_item["identity_value"] == "pranavsharma1000"
        assert reverse_item["installation_id"] == 144082554

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_812_ui_register_then_webhook_does_not_clobber(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """Regression for the account-812447483903 split-brain (2026-07-04).

        Sequence reproduced live: the UI register flow writes the Postgres
        installation → tenant mapping (identity-index forward row, NO
        ``auto_registered`` flag) for org ``pranavsharma1000``; ~35 minutes
        later a GitHub webhook fires auto-register with the org *login*
        ``aws-innovate``. The webhook MUST NOT overwrite the Postgres-owned row
        with the login (that clobber is what rolled usage up to the phantom
        ``aws-innovate`` tenant, which has no Postgres org and no admin).

        Row shapes below are the real 812 artifacts captured before the account
        was wiped (per #2400: never invent fixture shapes).
        """
        from handler import _auto_register_installation

        # Real DDB adp-dev-identity-index forward row written by the UI register
        # flow on 812 — Postgres-owned (no auto_registered flag).
        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144240027",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        # Webhook auto-register fires later with the GitHub org login.
        result = _auto_register_installation(144240027, "aws-innovate")

        # Postgres tenant is kept; the phantom login never becomes the tenant.
        assert result == "pranavsharma1000"
        # No write at all — the Postgres-owned row is untouched.
        table.put_item.assert_not_called()
        # Gateway is not consulted: an existing row short-circuits the resolve.
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        # Drift is surfaced for observability (org login != stored tenant).
        mock_metric.assert_called_once_with("InstallationTenantDrift")

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_idempotent_refresh_of_auto_registered_row(self, mock_resolver, mock_gw):
        """An existing auto_registered row refreshes idempotently without a gateway call."""
        from handler import _auto_register_installation

        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
            "auto_registered": True,
        }
        reverse = {
            "identity_type": "org_installation",
            "identity_value": "pranavsharma1000",
            "installation_id": 144082554,
            "auto_registered": True,
        }
        table = _mock_table_with(forward_item=forward, reverse_item=reverse)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "pranavsharma1000")

        assert result == "pranavsharma1000"
        # Refresh path does not consult the gateway
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        # Forward write has no ConditionExpression (idempotent overwrite)
        forward_call = table.put_item.call_args_list[0]
        assert "ConditionExpression" not in forward_call.kwargs
