"""Tests for cross-tenant trigger policy enforcement in identity_resolver.py.

Issue #3134: Verifies that per-tenant trigger_policy is correctly enforced:
- Default (absent/any_adp_user) allows cross-tenant as before.
- home_tenant_only blocks users not in member_org_ids.
- home_tenant_only allows users whose member_org_ids includes the repo org.
- Fail-closed: missing member_org_ids under home_tenant_only → deny.
- Bot rows (user_kind=bot, org=repo tenant) always pass.
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

INSTALLATION_ID = 55555
SENDER_ID = 20402445
USER_ID = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"

# Tenant with default policy (absent trigger_policy attr)
TENANT_ITEM_DEFAULT = {
    "identity_type": "github_installation_id",
    "identity_value": str(INSTALLATION_ID),
    "org_id": "target-org",
    "user_provisioning_mode": "strict",
}

# Tenant with home_tenant_only policy
TENANT_ITEM_LOCKED = {
    "identity_type": "github_installation_id",
    "identity_value": str(INSTALLATION_ID),
    "org_id": "target-org",
    "user_provisioning_mode": "strict",
    "trigger_policy": "home_tenant_only",
}

# User in a DIFFERENT org (cross-tenant)
USER_ITEM_CROSS = {
    "provider": "github",
    "provider_user_id": str(SENDER_ID),
    "user_id": USER_ID,
    "org_id": "home-org",
}

# User with member_org_ids that includes target-org
USER_ITEM_MEMBER = {
    "provider": "github",
    "provider_user_id": str(SENDER_ID),
    "user_id": USER_ID,
    "org_id": "home-org",
    "member_org_ids": ["home-org", "target-org"],
}

# User in the SAME org (no cross-tenant)
USER_ITEM_SAME = {
    "provider": "github",
    "provider_user_id": str(SENDER_ID),
    "user_id": USER_ID,
    "org_id": "target-org",
}

# Bot user in the repo's tenant
BOT_USER_ITEM = {
    "provider": "github",
    "provider_user_id": "77777777",
    "user_id": "b0b0b0b0-1111-2222-3333-444444444444",
    "org_id": "target-org",
    "user_kind": "bot",
    "bot_kind": "agent-developer",
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


class TestDefaultPolicyAllowsCrossTenant:
    """Policy absent + cross-tenant → allowed (today's behavior preserved)."""

    def test_absent_policy_allows_cross_tenant(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_DEFAULT,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": USER_ITEM_CROSS,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.org_id == "target-org"
        # CrossTenantMismatch metric emitted (not denied)
        mock_cw.put_metric_data.assert_called_once()
        metric = mock_cw.put_metric_data.call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "CrossTenantMismatch"


class TestHomeTenantOnlySameOrgAllowed:
    """home_tenant_only + commenter's home org = repo org → allowed."""

    def test_same_org_always_allowed(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_LOCKED,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": USER_ITEM_SAME,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.org_id == "target-org"
        # No cross-tenant metric (same org)
        mock_cw.put_metric_data.assert_not_called()


class TestHomeTenantOnlyMemberAllowed:
    """home_tenant_only + repo org ∈ member_org_ids → allowed."""

    def test_member_of_target_org_allowed(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_LOCKED,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": USER_ITEM_MEMBER,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.org_id == "target-org"
        # CrossTenantMismatch emitted (allowed cross-tenant)
        mock_cw.put_metric_data.assert_called_once()
        metric = mock_cw.put_metric_data.call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "CrossTenantMismatch"


class TestHomeTenantOnlyNonMemberDenied:
    """home_tenant_only + repo org ∉ member_org_ids → cross_tenant_denied."""

    def test_non_member_denied(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_LOCKED,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": USER_ITEM_CROSS,  # member_org_ids absent
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert result is None
        assert reason == "cross_tenant_denied"
        # CrossTenantDenied metric emitted
        mock_cw.put_metric_data.assert_called_once()
        metric = mock_cw.put_metric_data.call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "CrossTenantDenied"


class TestHomeTenantOnlyMissingMemberOrgIdsDenied:
    """home_tenant_only + member_org_ids attr missing → denied (fail-closed).

    When member_org_ids is absent, defaults to [user's home org_id]. If that
    doesn't match the target org, the user is denied.
    """

    def test_missing_member_org_ids_fail_closed(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        # User has no member_org_ids attr and home org != target org
        user_item_no_members = {
            "provider": "github",
            "provider_user_id": str(SENDER_ID),
            "user_id": USER_ID,
            "org_id": "different-org",
        }

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_LOCKED,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": user_item_no_members,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert result is None
        assert reason == "cross_tenant_denied"


class TestBotInHomeTenantAllowedUnderLockedPolicy:
    """Bot row (user_kind=bot, org = repo tenant) under home_tenant_only → allowed.

    Bot rows are seeded per-tenant with org_id = that tenant, so home-tenant
    always matches — no cross-tenant path triggered.
    """

    def test_bot_same_tenant_allowed(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM_LOCKED,
            },
            "adp-dev-user-identity-index": {
                "github|77777777": BOT_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, 77777777)

        assert reason == "ok"
        assert result is not None
        assert result.user_kind == "bot"
        assert result.org_id == "target-org"
        # No cross-tenant metric (bot is same tenant)
        mock_cw.put_metric_data.assert_called_once()
        metric = mock_cw.put_metric_data.call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "BotActionTriggered"


class TestExplicitAnyAdpUserPolicyAllows:
    """Explicit trigger_policy=any_adp_user + cross-tenant → allowed."""

    def test_explicit_any_adp_user_allows(self):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        tenant_item_explicit = {
            **TENANT_ITEM_DEFAULT,
            "trigger_policy": "any_adp_user",
        }
        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": tenant_item_explicit,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": USER_ITEM_CROSS,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                identity_resolver._cloudwatch = None
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
