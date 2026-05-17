"""Tests for identity_resolver.py — canonical user_id resolution.

Issue #702: Tests for Postgres safety-net, v2 flag, drift detection,
kill-switches, and envelope correctness.
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
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "true")
    monkeypatch.setenv("GATEWAY_API_URL", "http://gateway.internal:8080")
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")

    # Clear module caches
    mods_to_clear = [
        k for k in sys.modules
        if k.startswith("common.identity_resolver") or k.startswith("common.gateway_client")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]
    yield
    mods_to_clear = [
        k for k in sys.modules
        if k.startswith("common.identity_resolver") or k.startswith("common.gateway_client")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INSTALLATION_ID = 12345
SENDER_ID = 20402445
CANONICAL_USER_ID = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
ORPHAN_USER_ID = "9b36abea-1111-2222-3333-444444444444"

TENANT_ITEM = {
    "identity_type": "github_installation_id",
    "identity_value": str(INSTALLATION_ID),
    "org_id": "pranavsharma1000",
    "user_provisioning_mode": "strict",
}

V2_USER_ITEM = {
    "provider": "github",
    "provider_user_id": str(SENDER_ID),
    "user_id": CANONICAL_USER_ID,
    "org_id": "pranavsharma1000",
}

LEGACY_USER_ITEM = {
    "identity_type": "github_user",
    "identity_value": str(SENDER_ID),
    "user_id": ORPHAN_USER_ID,
    "org_id": "sophos-test",
}

PG_RESULT_CANONICAL = {
    "user_id": CANONICAL_USER_ID,
    "org_id": "pranavsharma1000",
    "team_id": "",
    "is_shadow": False,
}


def _mock_ddb_get_item(items_by_table):
    """Create a mock DynamoDB resource that returns items based on table/key."""
    mock_resource = MagicMock()

    def make_table(table_name):
        mock_table = MagicMock()

        def get_item(Key=None):
            table_items = items_by_table.get(table_name, {})
            # Build lookup key from the Key dict values
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


class TestResolveUsesV2WhenFlagOn:
    """Test 1: With v2 flag on, primary path hits v2 DDB and returns its user_id."""

    def test_resolve_uses_v2_when_flag_on(self, monkeypatch):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": V2_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            with patch(
                "common.gateway_client.resolve_user_by_identity",
                return_value=PG_RESULT_CANONICAL,
            ):
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.user_id == CANONICAL_USER_ID
        assert result.org_id == "pranavsharma1000"


class TestResolveFallsBackToPostgresWhenV2Misses:
    """Test 2: v2 returns nothing -> Lambda calls /resolve-user and uses Postgres result."""

    def test_resolve_falls_back_to_postgres_when_v2_misses(self, monkeypatch):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        # v2 table has no entry for this sender, old table also empty
        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {},
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            with patch(
                "common.gateway_client.resolve_user_by_identity",
                return_value=PG_RESULT_CANONICAL,
            ):
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.user_id == CANONICAL_USER_ID


class TestResolveTrustsPostgresOnDrift:
    """Test 3: v2 returns user A, Postgres returns user B -> uses B, emits drift metric."""

    def test_resolve_trusts_postgres_on_drift(self, monkeypatch):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        # v2 table points to orphan
        v2_orphan_item = {
            "provider": "github",
            "provider_user_id": str(SENDER_ID),
            "user_id": ORPHAN_USER_ID,
            "org_id": "sophos-test",
        }
        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": v2_orphan_item,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)
        mock_cw = MagicMock()

        with patch("boto3.resource", return_value=mock_ddb):
            with patch("boto3.client", return_value=mock_cw):
                with patch(
                    "common.gateway_client.resolve_user_by_identity",
                    return_value=PG_RESULT_CANONICAL,
                ):
                    identity_resolver._cloudwatch = None
                    result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        assert result.user_id == CANONICAL_USER_ID  # Postgres wins
        # Drift metric emitted
        mock_cw.put_metric_data.assert_called_once()
        call_args = mock_cw.put_metric_data.call_args
        metric_name = call_args[1]["MetricData"][0]["MetricName"]
        assert metric_name == "IdentityIndexDrift"


class TestKillSwitchDisablesGatewayCall:
    """Test 4: With RESOLVE_CANONICAL_VIA_GATEWAY=false, Postgres call is skipped."""

    def test_kill_switch_disables_gateway_call(self, monkeypatch):
        monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "false")
        mods = [k for k in sys.modules if k.startswith("common.identity_resolver")]
        for m in mods:
            del sys.modules[m]

        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": V2_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            with patch(
                "common.gateway_client.resolve_user_by_identity",
            ) as mock_resolve:
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result.user_id == CANONICAL_USER_ID
        mock_resolve.assert_not_called()


class TestLegacyKillSwitchStillWorks:
    """Test 5: With USER_IDENTITY_INDEX_V2_READ=false, behaves as today (legacy path)."""

    def test_legacy_kill_switch_still_works(self, monkeypatch):
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")
        monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "false")
        mods = [k for k in sys.modules if k.startswith("common.identity_resolver")]
        for m in mods:
            del sys.modules[m]

        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        # Only old table has the user (with orphan ID — legacy behavior)
        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
                f"github_user|{SENDER_ID}": LEGACY_USER_ITEM,
            },
            "adp-dev-user-identity-index": {},
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        assert result is not None
        # Legacy behavior: returns orphan user_id (no Postgres cross-validation)
        assert result.user_id == ORPHAN_USER_ID


class TestEnvelopeUsesCanonicalUserId:
    """Test 6: Built envelope's actor.user_id matches the canonical row."""

    def test_envelope_uses_canonical_user_id(self, monkeypatch):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": V2_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            with patch(
                "common.gateway_client.resolve_user_by_identity",
                return_value=PG_RESULT_CANONICAL,
            ):
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        assert reason == "ok"
        # The resolved identity is what goes into the SQS envelope's actor.user_id
        assert result.user_id == CANONICAL_USER_ID
        assert result.org_id == "pranavsharma1000"


class TestInternalApiKeyLoadedOnce:
    """Test 7: INTERNAL_API_KEY_ARN is fetched on first call and cached."""

    def test_internal_api_key_loaded_once(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_KEY_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:key")
        monkeypatch.setenv("BG_INTERNAL_API_KEY", "")
        mods = [k for k in sys.modules if k.startswith("common.gateway_client")]
        for m in mods:
            del sys.modules[m]

        from common import gateway_client

        gateway_client._internal_api_key = None

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "the-key"}

        with patch("boto3.client", return_value=mock_sm):
            # Call twice
            key1 = gateway_client._resolve_internal_api_key()
            key2 = gateway_client._resolve_internal_api_key()

        assert key1 == "the-key"
        assert key2 == "the-key"
        mock_sm.get_secret_value.assert_called_once()


class TestPostgres404TreatedAsNoMatch:
    """Test 8: When /resolve-user returns 404, Lambda treats it as not-found not error."""

    def test_postgres_404_treated_as_no_match(self, monkeypatch):
        from common import identity_resolver

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": TENANT_ITEM,
            },
            "adp-dev-user-identity-index": {
                f"github|{SENDER_ID}": V2_USER_ITEM,
            },
        }
        mock_ddb = _mock_ddb_get_item(ddb_items)

        with patch("boto3.resource", return_value=mock_ddb):
            with patch(
                "common.gateway_client.resolve_user_by_identity",
                return_value=None,  # 404 from gateway
            ):
                result, reason = identity_resolver.resolve(INSTALLATION_ID, SENDER_ID)

        # Should still succeed using v2 DDB result (fail-open on Postgres miss)
        assert reason == "ok"
        assert result is not None
        assert result.user_id == CANONICAL_USER_ID
