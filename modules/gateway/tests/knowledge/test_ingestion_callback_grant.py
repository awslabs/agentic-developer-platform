"""The ingestion callback grant is unforgeable and non-transferable (#5663, A09).

The status-callback route's whole authorization argument rests on one claim: the
token it verifies could only have been produced by the gateway, for that asset.
These tests attack that claim directly, at the level where a break would be
silent — the route-level tests would keep passing against a verifier that accepted
a tampered payload, because they mint their tokens with the real minter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.knowledge.ingestion_callback_grant import (
    GRANT_VERSION,
    MAX_GRANT_TTL_SECONDS,
    GrantError,
    mint_ingestion_grant,
    try_mint_ingestion_grant,
    verify_ingestion_grant,
)

_ENV = {"AGENT_RUN_CREDENTIAL_KEY": "k" * 48}
_OTHER = {"AGENT_RUN_CREDENTIAL_KEY": "different" * 8}
_ASSET = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_a_minted_grant_round_trips_with_its_asset_and_tenant():
    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env=_ENV)
    grant = verify_ingestion_grant(token, env=_ENV)
    assert (grant.asset_id, grant.tenant_id) == (_ASSET, "t1")
    assert grant.is_shared_scope is False


def test_a_shared_grant_carries_null_tenant_as_a_positive_fact():
    """None is a supported value, not an absence — see the module docstring."""
    grant = verify_ingestion_grant(mint_ingestion_grant(asset_id=_ASSET, tenant_id=None, env=_ENV), env=_ENV)
    assert grant.tenant_id is None
    assert grant.is_shared_scope is True


def test_a_grant_signed_with_another_key_does_not_verify():
    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env=_OTHER)
    with pytest.raises(GrantError):
        verify_ingestion_grant(token, env=_ENV)


@pytest.mark.parametrize("field,value", [("asset_id", "other-asset"), ("tenant_id", "attacker-tenant")])
def test_editing_the_payload_invalidates_the_grant(field, value):
    """The MAC covers the payload, so retargeting requires the key.

    Encoded rather than asserted abstractly: decode the payload, change the one
    field an attacker would want to change, re-encode, and confirm it is refused.
    Without this, a verifier that read claims before checking the MAC would pass
    every other test in the suite.
    """
    import base64
    import json

    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env=_ENV)
    version, payload_b64, mac = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    payload[field] = value
    tampered = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()

    with pytest.raises(GrantError):
        verify_ingestion_grant(f"{version}.{tampered}.{mac}", env=_ENV)


def test_a_grant_whose_tenant_claim_was_removed_does_not_verify_as_shared():
    """The failure mode that would quietly reopen the hole.

    If a stripped tenant claim verified as tenant_id=None, an attacker holding any
    valid tenant grant could downgrade it to a shared grant — and then the route's
    `tenant_id IS NULL` branch would be reachable without the gateway ever having
    issued shared authority. Presence is therefore required even though null is a
    legal value. (The MAC already prevents this; the check is belt-and-braces for a
    future refactor that regenerates the payload.)
    """
    import base64
    import json

    payload = {
        "v": GRANT_VERSION,
        "asset_id": _ASSET,
        "issued_at": "2026-01-01T00:00:00Z",
        "not_before": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    import hashlib
    import hmac

    mac = hmac.new(_ENV["AGENT_RUN_CREDENTIAL_KEY"].encode(), GRANT_VERSION.encode() + b"." + body, hashlib.sha256).digest()
    token = ".".join(
        [
            GRANT_VERSION,
            base64.urlsafe_b64encode(body).rstrip(b"=").decode(),
            base64.urlsafe_b64encode(mac).rstrip(b"=").decode(),
        ]
    )

    with pytest.raises(GrantError):
        verify_ingestion_grant(token, env=_ENV)


def test_a_run_credential_is_not_a_callback_grant():
    """Domain separation under the shared key, at the module level.

    A worker DOES hold a run credential, so if the version prefix were not part of
    the MAC input this would be a real cross-domain forgery: present the credential
    as a grant and inherit authority over an asset.
    """
    from src.agentauth.run_credential import mint_credential

    token = mint_credential(invocation_id="inv-1", attempt=1, tenant_id="t1", env=_ENV)
    with pytest.raises(GrantError):
        verify_ingestion_grant(token, env=_ENV)


def test_a_callback_grant_is_not_a_run_credential():
    """And the reverse direction, which is the more dangerous one.

    A grant is long-lived and reaches the gateway from a pod. If it verified as a
    run credential it would become an execution identity, which is a far larger
    authority than the one row it is supposed to name.
    """
    from src.agentauth.run_credential import CredentialError, verify_credential

    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env=_ENV)
    with pytest.raises(CredentialError):
        verify_credential(token, env=_ENV)


def test_an_expired_grant_is_refused():
    minted_at = datetime.now(UTC) - timedelta(seconds=MAX_GRANT_TTL_SECONDS + 60)
    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", now=minted_at, env=_ENV)
    with pytest.raises(GrantError):
        verify_ingestion_grant(token, env=_ENV)


def test_the_ttl_outlives_queue_retention_plus_the_longest_run():
    """Not arbitrary: SQS retains ingestion messages for 4 days and a repo run may
    take an hour, so a short-lived grant would expire in the queue and turn every
    backlogged asset into a refused callback."""
    assert MAX_GRANT_TTL_SECONDS >= 4 * 24 * 3600 + 3600


def test_a_grant_still_valid_after_four_days_in_the_queue():
    """The concrete case the TTL exists for, executed rather than asserted."""
    minted_at = datetime.now(UTC) - timedelta(days=4, hours=1)
    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", now=minted_at, env=_ENV)
    assert verify_ingestion_grant(token, env=_ENV).asset_id == _ASSET


def test_a_ttl_longer_than_the_cap_is_capped_not_honoured():
    token = mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", ttl_seconds=10**9, env=_ENV)
    grant = verify_ingestion_grant(token, env=_ENV)
    assert (grant.expires_at - grant.issued_at).total_seconds() == MAX_GRANT_TTL_SECONDS


def test_minting_without_a_key_fails_loudly_rather_than_unsigned():
    """A missing key must never degrade to "unsigned grants are fine"."""
    with pytest.raises(GrantError):
        mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env={})


def test_try_mint_returns_none_when_no_key_is_configured():
    """Dispatch must not start failing where the signing secret is absent.

    The gateway mounts AGENT_RUN_CREDENTIAL_KEY with optional: true, so an
    environment without agent-authority-signing would otherwise have every asset
    registration raise. Losing the binding for that message is safe — the route
    treats an ungranted callback as unbound — whereas fabricating one would not be.
    """
    assert try_mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env={}) is None
    assert try_mint_ingestion_grant(asset_id=_ASSET, tenant_id="t1", env=_ENV) is not None


@pytest.mark.parametrize("token", ["", "garbage", "adpk1.only-two", "x" * 5000, "adpr1.abc.def"])
def test_malformed_tokens_are_refused_without_crashing(token):
    with pytest.raises(GrantError):
        verify_ingestion_grant(token, env=_ENV)


def test_mint_requires_an_asset():
    with pytest.raises(GrantError):
        mint_ingestion_grant(asset_id="", tenant_id="t1", env=_ENV)
