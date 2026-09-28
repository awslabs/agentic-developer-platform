"""Tests for the unknown-installation negative cache (Issue #4047, #2724 slice C).

The invariant that matters most here is the asymmetry between the three-state
client's outcomes: ``not_found`` is cacheable (authoritative gateway 404),
``error`` is NOT (we could not find out). Caching an ``error`` would convert a
transient gateway outage into a TTL-long lockout of every legitimate new tenant
— the availability failure slice A's three-state split exists to prevent. The
call-site tests in ``test_identity_resolver.py`` and
``github/tests/test_auto_register_guard.py`` pin that at the boundary; these
tests pin the store's own behavior.

Second-most important: DynamoDB TTL deletion is asynchronous, so ``get_item``
can return an already-expired row. The read path must enforce the TTL itself or
the cache silently outlives its configured lifetime.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add common/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Default to an explicit 300s TTL so tests don't depend on the env."""
    monkeypatch.setenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "300")
    for mod in [k for k in sys.modules if k.startswith("common.negative_cache")]:
        del sys.modules[mod]
    yield
    for mod in [k for k in sys.modules if k.startswith("common.negative_cache")]:
        del sys.modules[mod]


def _table_returning(item):
    table = MagicMock()
    table.get_item = MagicMock(return_value={"Item": item} if item else {})
    return table


# ---------------------------------------------------------------------------
# TTL configuration
# ---------------------------------------------------------------------------


class TestTtlConfiguration:
    def test_defaults_to_300_seconds(self, monkeypatch):
        monkeypatch.delenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", raising=False)
        from common import negative_cache

        assert negative_cache.ttl_seconds() == 300
        assert negative_cache.enabled() is True

    def test_ttl_is_configurable(self, monkeypatch):
        monkeypatch.setenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "60")
        from common import negative_cache

        assert negative_cache.ttl_seconds() == 60

    def test_zero_ttl_disables_the_cache(self, monkeypatch):
        """TTL=0 is the ops kill-switch — no second flag needed."""
        monkeypatch.setenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "0")
        from common import negative_cache

        assert negative_cache.enabled() is False

        table = _table_returning({"ttl": int(time.time()) + 300})
        # Read short-circuits without even touching DDB...
        assert negative_cache.is_negative_cached(table, 555) is False
        table.get_item.assert_not_called()
        # ...and writes are suppressed too.
        negative_cache.record_not_found(table, 555)
        negative_cache.invalidate(table, 555)
        table.put_item.assert_not_called()

    def test_malformed_ttl_falls_back_to_default_not_disabled(self, monkeypatch):
        """A typo'd TTL must not silently switch a security mitigation off."""
        monkeypatch.setenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "five-minutes")
        from common import negative_cache

        assert negative_cache.ttl_seconds() == negative_cache.DEFAULT_TTL_SECONDS
        assert negative_cache.enabled() is True


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


class TestIsNegativeCached:
    def test_miss_when_no_row(self):
        from common import negative_cache

        assert negative_cache.is_negative_cached(_table_returning(None), 555) is False

    def test_hit_when_live_row(self):
        from common import negative_cache

        table = _table_returning({"ttl": int(time.time()) + 300})

        assert negative_cache.is_negative_cached(table, 555) is True
        # Keyed on the DISTINCT negative identity_type — never the forward
        # github_installation_id rows dispatch routes on.
        key = table.get_item.call_args.kwargs["Key"]
        assert key["identity_type"] == "github_installation_negative"
        assert key["identity_value"] == "555"

    def test_expired_row_is_a_miss_even_though_ddb_returned_it(self):
        """DDB TTL deletion is lazy — the read path must enforce the TTL itself.

        Without this check the cache outlives its configured TTL by however long
        DDB's reaper takes (documented as up to 48h), delaying legitimate tenant
        onboarding far past the "minutes" this slice promises.
        """
        from common import negative_cache

        table = _table_returning({"ttl": int(time.time()) - 1})

        assert negative_cache.is_negative_cached(table, 555) is False

    @pytest.mark.parametrize(
        "bad_ttl",
        [None, "", "not-a-number", {}],
        ids=["absent", "empty", "text", "dict"],
    )
    def test_row_without_usable_ttl_is_a_miss(self, bad_ttl):
        """Cannot prove the row is live → do not trust it (fail toward re-asking)."""
        from common import negative_cache

        item = {"identity_type": "github_installation_negative"}
        if bad_ttl is not None:
            item["ttl"] = bad_ttl

        assert negative_cache.is_negative_cached(_table_returning(item), 555) is False

    def test_ddb_read_failure_degrades_to_miss(self):
        """A cache is an optimization; it must never fail a webhook."""
        from common import negative_cache

        table = MagicMock()
        table.get_item = MagicMock(side_effect=RuntimeError("DDB throttled"))

        assert negative_cache.is_negative_cached(table, 555) is False

    def test_decimal_ttl_from_ddb_is_honoured(self):
        """boto3's resource API returns numbers as Decimal, not int."""
        from decimal import Decimal

        from common import negative_cache

        table = _table_returning({"ttl": Decimal(int(time.time()) + 300)})

        assert negative_cache.is_negative_cached(table, 555) is True


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestRecordNotFound:
    def test_writes_row_with_ttl(self, monkeypatch):
        monkeypatch.setenv("INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS", "120")
        from common import negative_cache

        table = MagicMock()
        before = int(time.time())
        negative_cache.record_not_found(table, 555)

        item = table.put_item.call_args.kwargs["Item"]
        assert item["identity_type"] == "github_installation_negative"
        assert item["identity_value"] == "555"
        assert before + 120 <= item["ttl"] <= int(time.time()) + 120
        assert "cached_at" in item
        # Must NOT look like a tenant mapping to any existing reader.
        assert "org_id" not in item

    def test_write_failure_is_swallowed(self):
        from common import negative_cache

        table = MagicMock()
        table.put_item = MagicMock(side_effect=RuntimeError("DDB unavailable"))

        negative_cache.record_not_found(table, 555)  # must not raise


class TestInvalidate:
    def test_invalidate_writes_already_expired_row(self):
        """Overwrite-with-past-TTL, not DeleteItem.

        The Lambda's identity-index policy grants GetItem/PutItem only. Adding
        DeleteItem to invalidate a cache row would also authorize deleting the
        live forward tenant rows dispatch routes on — the wrong trade in a
        security-hardening slice. Read-side TTL validation makes the overwrite
        exactly as effective.
        """
        from common import negative_cache

        table = MagicMock()
        negative_cache.invalidate(table, 555)

        item = table.put_item.call_args.kwargs["Item"]
        assert item["identity_type"] == "github_installation_negative"
        assert item["ttl"] <= int(time.time())
        assert item["invalidated"] is True

    def test_invalidated_row_reads_as_a_miss(self):
        """End-to-end: the row invalidate() writes must not be a hit."""
        from common import negative_cache

        write_table = MagicMock()
        negative_cache.invalidate(write_table, 555)
        written = write_table.put_item.call_args.kwargs["Item"]

        table = _table_returning(written)
        assert negative_cache.is_negative_cached(table, 555) is False

    def test_invalidate_failure_is_swallowed(self):
        from common import negative_cache

        table = MagicMock()
        table.put_item = MagicMock(side_effect=RuntimeError("DDB unavailable"))

        negative_cache.invalidate(table, 555)  # must not raise


class TestRoundTrip:
    def test_recorded_row_reads_back_as_a_hit(self):
        """record_not_found() → is_negative_cached() is the whole point."""
        from common import negative_cache

        write_table = MagicMock()
        negative_cache.record_not_found(write_table, 98765)
        written = write_table.put_item.call_args.kwargs["Item"]

        table = _table_returning(written)
        assert negative_cache.is_negative_cached(table, 98765) is True

    def test_int_and_str_installation_ids_share_a_key(self):
        """The handler passes an int, the resolver a str — same row either way."""
        from common import negative_cache

        t1, t2 = MagicMock(), MagicMock()
        negative_cache.record_not_found(t1, 555)
        negative_cache.record_not_found(t2, "555")

        assert (
            t1.put_item.call_args.kwargs["Item"]["identity_value"]
            == t2.put_item.call_args.kwargs["Item"]["identity_value"]
            == "555"
        )
