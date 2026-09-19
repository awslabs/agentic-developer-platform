"""Protected markers never sign worker-selected identity or recover shared keys."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from lib import correlation_marker, run_service_client, marker_signing
from lib.status_gateway_client import StatusGatewayError

FIELDS = {
    "correlation_id": "protected-flow",
    "root_human_id": "protected-human",
    "is_human_rooted": "true",
    "invocation_id": "protected-run",
    "chain_depth": "2",
    "signature": "a" * 43,
}


def test_client_sends_no_identity(monkeypatch):
    post = Mock(return_value=FIELDS)
    monkeypatch.setattr(run_service_client, "_post", post)
    assert run_service_client.own_marker_fields() == FIELDS
    post.assert_called_once_with("/marker", {})


@pytest.mark.parametrize(
    "delta",
    [
        {"signature": "bad"},
        {"root_human_id": "injected --> text"},
        {"is_human_rooted": "maybe"},
        {"chain_depth": "-1"},
        {"unknown": "secret"},
        {"invocation_id": ""},
        {"root_human_id": "x" * 256},
    ],
)
def test_client_refuses_invalid_contract(monkeypatch, delta):
    monkeypatch.setattr(run_service_client, "_post", Mock(return_value={**FIELDS, **delta}))
    with pytest.raises(StatusGatewayError):
        run_service_client.own_marker_fields()


def test_protected_marker_uses_only_service_identity(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    for key in ("ADP_CORRELATION_ID", "ADP_ROOT_HUMAN_ID", "ADP_MESSAGE_ID", "ADP_CHAIN_DEPTH"):
        monkeypatch.setenv(key, "another-user")
    monkeypatch.setattr(run_service_client, "own_marker_fields", Mock(return_value=FIELDS))
    sign = Mock(side_effect=AssertionError("local signer must not run"))
    monkeypatch.setattr(correlation_marker, "compute_signature", sign)
    result = correlation_marker.prepend_correlation_marker("result", dispatch_persona="reviewer")
    assert "another-user" not in result
    assert "adp-root-human:protected-human" in result
    assert "adp-dispatch:reviewer" in result
    assert f"adp-sig:{FIELDS['signature']}" in result
    assert result.endswith("\nresult")
    sign.assert_not_called()


def test_protected_marker_refreshes_stale_prefix(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr(run_service_client, "own_marker_fields", Mock(return_value=FIELDS))
    result = correlation_marker.prepend_correlation_marker(
        "<!-- adp-correlation:old adp-root-human:someone-else -->\nresult"
    )
    assert "someone-else" not in result and "old" not in result
    assert result.count("<!-- adp-correlation:") == 1


def test_unavailable_service_never_falls_back_to_shared_key(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_MARKER_SIGNING_KEY_SECRET", "shared-platform-secret")
    monkeypatch.setattr(
        run_service_client, "own_marker_fields", Mock(side_effect=StatusGatewayError("unavailable"))
    )
    sm = Mock(side_effect=AssertionError("no shared secret lookup"))
    monkeypatch.setattr(marker_signing.boto3, "client", sm)
    assert correlation_marker.prepend_correlation_marker("result") == "result"
    sm.assert_not_called()
