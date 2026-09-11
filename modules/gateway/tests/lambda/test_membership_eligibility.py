"""Tests for the shared membership-eligibility reader (Issue #4849).

Covers the whole verdict surface of ``check_platform_membership``:

- ELIGIBLE      — the projection lists at least one org
- NOT_ELIGIBLE  — the read SUCCEEDED and there is no membership
                  (no row / no attribute / empty list / non-list / empty id)
- UNAVAILABLE   — the projection could not be read at all

The NOT_ELIGIBLE cases are the point of the suite. This reader is deliberately
stricter than webhook-ingress's ``identity_resolver``, which substitutes the
row's own ``org_id`` when ``member_org_ids`` is missing. That substitution is
correct for *its* question and fail-OPEN for ours (an identity row can exist for
a user with no TenantMembership at all), so ``test_row_without_attribute_*``
below is a regression guard against someone "fixing" this module to match the
other reader.
"""

from unittest.mock import MagicMock  # noqa: I001 - see below

import pytest

# Import for its side effect: puts modules/gateway/lambda/shared on sys.path, so
# `membership_eligibility` resolves the same way it does inside the Lambda zip
# (flat, beside the handler). Same mechanism the other lambda tests use for
# pricing_fallback / root_principal. Ordering is load-bearing, hence the I001
# suppression above — isort would hoist the import below this one and break it.
from ._handler_loader import load_handler  # noqa: F401

import membership_eligibility as me  # noqa: E402


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    """Both table names configured, v2 read off — the current dev shape."""
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
    monkeypatch.delenv("USER_IDENTITY_INDEX_V2_READ", raising=False)
    # The module memoises the boto3 resource in a global; leaking it between
    # tests would let one test's stub answer another's read.
    monkeypatch.setattr(me, "_dynamodb", None)
    yield
    monkeypatch.setattr(me, "_dynamodb", None)


def _stub_tables(monkeypatch, *, legacy=None, v2=None):
    """Install a fake boto3 resource whose Table(name) returns a per-table stub.

    ``legacy`` / ``v2`` are either an item dict, ``None`` for "no such row", or
    an ``Exception`` instance to raise from ``get_item``.
    """
    tables: dict[str, MagicMock] = {}

    def _make(result):
        table = MagicMock()

        def _get_item(Key):  # noqa: N803 - boto3's kwarg name
            table.last_key = Key
            if isinstance(result, Exception):
                raise result
            return {"Item": result} if result is not None else {}

        table.get_item.side_effect = _get_item
        return table

    tables["adp-dev-identity-index"] = _make(legacy)
    tables["adp-dev-user-identity-index"] = _make(v2)

    resource = MagicMock()
    resource.Table.side_effect = lambda name: tables[name]
    monkeypatch.setattr(me, "_get_resource", lambda: resource)
    return tables


# ---------------------------------------------------------------------------
# ELIGIBLE
# ---------------------------------------------------------------------------


def test_one_membership_is_eligible(monkeypatch):
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    assert me.check_platform_membership("20402445") == me.ELIGIBLE


def test_multiple_memberships_are_eligible(monkeypatch):
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a", "org-b", "org-c"]})
    assert me.check_platform_membership("20402445") == me.ELIGIBLE


def test_numeric_provider_user_id_is_coerced_to_string(monkeypatch):
    """The DDB key is a string attribute; an int id must not blow up the read."""
    tables = _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    assert me.check_platform_membership(20402445) == me.ELIGIBLE
    assert tables["adp-dev-identity-index"].last_key["identity_value"] == "20402445"


def test_legacy_read_uses_github_user_key_shape(monkeypatch):
    """Keyed on identity_type=github_user + the numeric id, not the login.

    Issue #4849 explicitly rules out reusing signup_allowlist's username key
    shape — a renamed GitHub login must not change the answer.
    """
    tables = _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    me.check_platform_membership("20402445")
    assert tables["adp-dev-identity-index"].last_key == {
        "identity_type": "github_user",
        "identity_value": "20402445",
    }


# ---------------------------------------------------------------------------
# NOT_ELIGIBLE — the read worked, the user has no membership
# ---------------------------------------------------------------------------


def test_no_identity_row_is_not_eligible(monkeypatch):
    _stub_tables(monkeypatch, legacy=None)
    assert me.check_platform_membership("20402445") == me.NOT_ELIGIBLE


def test_row_without_attribute_is_not_eligible_not_home_org(monkeypatch):
    """A row with no member_org_ids must NOT fall back to its own org_id.

    identity_resolver does exactly that fallback. Copying it here would answer
    "eligible" for a user who holds no TenantMembership at all.
    """
    _stub_tables(monkeypatch, legacy={"user_id": "u-1", "org_id": "org-home"})
    assert me.check_platform_membership("20402445") == me.NOT_ELIGIBLE


def test_empty_list_is_not_eligible(monkeypatch):
    """The revoked-last-membership end state: attribute present, list empty."""
    _stub_tables(monkeypatch, legacy={"member_org_ids": []})
    assert me.check_platform_membership("20402445") == me.NOT_ELIGIBLE


def test_list_of_only_empty_strings_is_not_eligible(monkeypatch):
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["", None]})
    assert me.check_platform_membership("20402445") == me.NOT_ELIGIBLE


def test_non_list_attribute_is_not_eligible(monkeypatch):
    """A corrupted/misprojected scalar must deny, not raise and not pass."""
    _stub_tables(monkeypatch, legacy={"member_org_ids": "org-a"})
    assert me.check_platform_membership("20402445") == me.NOT_ELIGIBLE


def test_empty_provider_user_id_is_not_eligible(monkeypatch):
    """No id to look up is a denial, not an outage — nothing was unreachable."""
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    assert me.check_platform_membership("") == me.NOT_ELIGIBLE


def test_empty_provider_user_id_does_not_read_dynamodb(monkeypatch):
    tables = _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    me.check_platform_membership("")
    tables["adp-dev-identity-index"].get_item.assert_not_called()


# ---------------------------------------------------------------------------
# UNAVAILABLE — could not read at all
# ---------------------------------------------------------------------------


def test_dynamodb_error_is_unavailable(monkeypatch):
    """Distinct from NOT_ELIGIBLE so a broken table is attributable (#3986)."""
    _stub_tables(monkeypatch, legacy=RuntimeError("ProvisionedThroughputExceeded"))
    assert me.check_platform_membership("20402445") == me.UNAVAILABLE


def test_unconfigured_legacy_table_is_unavailable(monkeypatch):
    """Terraform not yet applied must read as "could not check", never as a pass."""
    monkeypatch.delenv("IDENTITY_INDEX_TABLE", raising=False)
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    assert me.check_platform_membership("20402445") == me.UNAVAILABLE


def test_unavailable_is_never_eligible(monkeypatch):
    """Guard the fail-closed direction: no read failure yields ELIGIBLE."""
    for boom in (RuntimeError("ddb down"), KeyError("table"), ValueError("bad")):
        _stub_tables(monkeypatch, legacy=boom)
        assert me.check_platform_membership("20402445") != me.ELIGIBLE


# ---------------------------------------------------------------------------
# v2 table / dual-read behaviour (#537 flag)
# ---------------------------------------------------------------------------


def test_v2_not_read_when_flag_off(monkeypatch):
    tables = _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]}, v2={"member_org_ids": ["org-b"]})
    assert me.check_platform_membership("20402445") == me.ELIGIBLE
    tables["adp-dev-user-identity-index"].get_item.assert_not_called()


def test_v2_read_first_when_flag_on(monkeypatch):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    tables = _stub_tables(monkeypatch, legacy=None, v2={"member_org_ids": ["org-b"]})
    assert me.check_platform_membership("20402445") == me.ELIGIBLE
    tables["adp-dev-user-identity-index"].get_item.assert_called_once()
    # v2 hit satisfies the read; no need to touch the legacy table.
    tables["adp-dev-identity-index"].get_item.assert_not_called()


def test_v2_read_uses_provider_key_shape(monkeypatch):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    tables = _stub_tables(monkeypatch, v2={"member_org_ids": ["org-b"]})
    me.check_platform_membership("20402445")
    assert tables["adp-dev-user-identity-index"].last_key == {
        "provider": "github",
        "provider_user_id": "20402445",
    }


def test_v2_miss_falls_back_to_legacy(monkeypatch):
    """Mid-migration users only have a legacy row; a v2 miss is not a denial."""
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    tables = _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]}, v2=None)
    assert me.check_platform_membership("20402445") == me.ELIGIBLE
    tables["adp-dev-identity-index"].get_item.assert_called_once()


def test_v2_error_falls_back_to_legacy(monkeypatch):
    """A v2 fault is non-fatal — legacy is still the authoritative read."""
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]}, v2=RuntimeError("v2 down"))
    assert me.check_platform_membership("20402445") == me.ELIGIBLE


def test_v2_error_and_legacy_error_is_unavailable(monkeypatch):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    _stub_tables(monkeypatch, legacy=RuntimeError("legacy down"), v2=RuntimeError("v2 down"))
    assert me.check_platform_membership("20402445") == me.UNAVAILABLE


def test_v2_unconfigured_table_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    monkeypatch.delenv("USER_IDENTITY_INDEX_TABLE", raising=False)
    _stub_tables(monkeypatch, legacy={"member_org_ids": ["org-a"]})
    assert me.check_platform_membership("20402445") == me.ELIGIBLE


def test_verdicts_are_distinct():
    """Callers branch on these; collapsing any two silently changes semantics."""
    assert len({me.ELIGIBLE, me.NOT_ELIGIBLE, me.UNAVAILABLE}) == 3


# ---------------------------------------------------------------------------
# Client construction — bounded timeouts (PR #4916 review F2)
# ---------------------------------------------------------------------------


def test_dynamodb_resource_uses_bounded_timeouts(monkeypatch):
    """The read runs inside Cognito's 5s non-retryable trigger budget.

    boto3's defaults (60s connect/read, with retries) would let a DDB hang
    outlive the trigger many times over, and the try/except around the read
    cannot catch a hang — only a client that gives up fast keeps the worst case
    (~2s) inside the window. Pins that the resource is built with 1s
    connect/read timeouts and a single attempt.
    """
    captured: dict = {}

    def _fake_resource(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(me.boto3, "resource", _fake_resource)

    me._get_resource()

    assert captured["service_name"] == "dynamodb"
    cfg = captured["config"]
    assert cfg.connect_timeout == 1
    assert cfg.read_timeout == 1
    assert cfg.retries == {"total_max_attempts": 1}


def test_dynamodb_resource_is_memoised(monkeypatch):
    """One client per container — the timeout config must not defeat reuse."""
    calls: list = []
    monkeypatch.setattr(me.boto3, "resource", lambda *a, **k: calls.append(k) or MagicMock())

    first = me._get_resource()
    second = me._get_resource()

    assert first is second
    assert len(calls) == 1
