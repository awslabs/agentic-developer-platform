"""Tests for bot identity resolution path in identity_resolver.py.

Issue #780: Verifies that bot rows in DDB resolve correctly, emit the
BotActionTriggered metric, and default to 'human' when user_kind is absent.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add common/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset module state and set base env vars."""
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "false")

    # Clear module caches
    mods_to_clear = [
        k
        for k in sys.modules
        if k.startswith("common.identity_resolver")
        or k.startswith("common.gateway_client")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]
    yield
    mods_to_clear = [
        k
        for k in sys.modules
        if k.startswith("common.identity_resolver")
        or k.startswith("common.gateway_client")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INSTALLATION_ID = 99999
BOT_SENDER_ID = 77777777
HUMAN_SENDER_ID = 20402445
BOT_USER_ID = "b0b0b0b0-1111-2222-3333-444444444444"
HUMAN_USER_ID = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"

TENANT_ITEM = {
    "identity_type": "github_installation_id",
    "identity_value": str(INSTALLATION_ID),
    "org_id": "sophos-test",
    "user_provisioning_mode": "strict",
}

BOT_USER_ITEM = {
    "provider": "github",
    "provider_user_id": str(BOT_SENDER_ID),
    "user_id": BOT_USER_ID,
    "org_id": "sophos-test",
    "user_kind": "bot",
    "bot_kind": "agent-developer",
}

HUMAN_USER_ITEM = {
    "provider": "github",
    "provider_user_id": str(HUMAN_SENDER_ID),
    "user_id": HUMAN_USER_ID,
    "org_id": "sophos-test",
}

# Legacy item without user_kind attribute (pre-migration)
LEGACY_USER_ITEM_NO_KIND = {
    "provider": "github",
    "provider_user_id": str(HUMAN_SENDER_ID),
    "user_id": HUMAN_USER_ID,
    "org_id": "sophos-test",
    # No user_kind attribute — should default to 'human'
}


def _mock_ddb_get_item(items_by_table):
    """Create a mock DynamoDB resource that returns items based on table/key."""
    mock_resource = MagicMock()

    def make_table(table_name):
        mock_table = MagicMock()

        def get_item(Key=None):  # noqa: N803
            table_items = items_by_table.get(table_name, {})
            key_str = "|".join(str(v) for v in Key.values())
            item = table_items.get(key_str)
            return {"Item": item} if item else {}

        mock_table.get_item = get_item
        return mock_table

    mock_resource.Table = make_table
    return mock_resource


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBotResolveReturnsUserKindBot:
    """Bot row in DDB resolves with user_kind='bot'."""

    def test_bot_resolves_with_user_kind_bot(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{BOT_SENDER_ID}": BOT_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(
                    INSTALLATION_ID, BOT_SENDER_ID
                )

        assert reason == "ok"
        assert result is not None
        assert result.user_kind == "bot"
        assert result.user_id == BOT_USER_ID


class TestBotActionTriggeredMetricEmitted:
    """BotActionTriggered metric is emitted for bot resolutions, not for human."""

    def test_metric_emitted_for_bot(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{BOT_SENDER_ID}": BOT_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(
                    INSTALLATION_ID, BOT_SENDER_ID
                )

        assert reason == "ok"
        # Verify BotActionTriggered metric was emitted
        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "ADP/IdentityResolver"
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "BotActionTriggered"
        dimensions = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dimensions["bot_kind"] == "agent-developer"
        assert dimensions["org_id"] == "sophos-test"

    def test_metric_not_emitted_for_human(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{HUMAN_SENDER_ID}": HUMAN_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(
                    INSTALLATION_ID, HUMAN_SENDER_ID
                )

        assert reason == "ok"
        assert result.user_kind == "human"
        # No metric emitted for humans
        mock_cw.put_metric_data.assert_not_called()


class TestMissingUserKindDefaultsToHuman:
    """DDB row missing user_kind attribute defaults to 'human' (back-compat)."""

    def test_missing_user_kind_defaults_to_human(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{HUMAN_SENDER_ID}": LEGACY_USER_ITEM_NO_KIND,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(
                    INSTALLATION_ID, HUMAN_SENDER_ID
                )

        assert reason == "ok"
        assert result is not None
        assert result.user_kind == "human"
        # No metric emitted
        mock_cw.put_metric_data.assert_not_called()
