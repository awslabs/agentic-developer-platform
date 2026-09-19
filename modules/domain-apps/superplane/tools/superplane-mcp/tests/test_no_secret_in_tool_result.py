"""R9 acc. 4 — no tool result contains a provider secret.

Why this is tested as a boundary rather than per handler: a leaked provider key
is unrecoverable. A model-visible result is copied into the model's context, the
run transcript and the log sink, so the disclosure has happened in three places
before anyone reviews it. Rotating is the only remedy.

The central test here is `test_a_tool_result_containing_a_secret_shape_fails` —
the criterion the issue names: a tool result carrying a provider-secret shape
must fail. It is asserted against a handler that deliberately tries to leak,
proving the *boundary* stops it rather than proving today's handlers happen not
to leak. That distinction is the whole point: today's handlers return no
credentials, so a test that only called them would pass while the boundary was
absent.

`contains_secret()` is used as the detector rather than re-running `redact()`,
so the scrubber is not asserted with itself.
"""

from __future__ import annotations

import json

import pytest
from superplane_mcp import (
    PLACEHOLDER,
    CapacityContract,
    contains_secret,
    dispatch_tool,
    mcp_call_tool,
    redact,
    rest_call,
    server,
)

FULL_HEADERS = {
    "x-adp-principal": "user-1",
    "x-adp-tenant": "tenant-1",
    "x-adp-capabilities": (
        "superplane:capacity:read,"
        "superplane:capacity:allocate,"
        "superplane:capacity:release"
    ),
    "x-adp-workspaces": "ws-alpha",
}

# Representative provider-secret values. Each is a syntactically valid shape for
# its provider and none is a real credential.
SECRET_VALUES = [
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "ghp_1234567890abcdefghijklmnopqrstuvwx",
    "ghs_1234567890abcdefghijklmnopqrstuvwx",
    "xoxb-1234567890-abcdefghijkl",
    "sk-abcdefghijklmnopqrstuvwxyz012345",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\n-----END OPENSSH PRIVATE KEY-----",
    "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
]

# Key names that must be scrubbed regardless of the value's shape — the case
# key-name matching exists for, where the value looks like an ordinary id.
SECRET_KEYS = [
    "secret",
    "api_key",
    "apiKey",
    "password",
    "provider_token",
    "aws_secret_access_key",
    "private_key",
    "client_secret",
    "authorization",
    "session_key",
    "credential",
]


# ---------------------------------------------------------------------------
# The criterion: a tool result carrying a secret shape fails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", SECRET_VALUES)
def test_a_tool_result_containing_a_secret_shape_fails(monkeypatch, secret) -> None:
    """A handler that tries to leak a secret is scrubbed by the boundary.

    Asserts the boundary, not the current handlers: the discovery handler is
    replaced with one that deliberately returns a provider secret under an
    innocuous key, which is the hardest case (key-name matching cannot see it).
    The secret must not survive to the caller.
    """

    def leaky_handler(arguments, contract):
        return {"offers": [], "note": f"connect using {secret}"}

    monkeypatch.setitem(server.HANDLERS, "superplane_discover_capacity", leaky_handler)

    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )

    assert not contains_secret(result), f"secret survived the boundary: {result}"
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_secret_named_keys_are_scrubbed_even_with_innocuous_values(
    monkeypatch, key
) -> None:
    """A credential-named field is withheld whatever it holds.

    Covers the opposite bypass to the test above: an opaque provider token that
    is indistinguishable from a request id by shape alone.
    """

    def leaky_handler(arguments, contract):
        return {"offers": [], key: "an-entirely-ordinary-looking-value"}

    monkeypatch.setitem(server.HANDLERS, "superplane_discover_capacity", leaky_handler)

    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    assert result[key] == PLACEHOLDER
    assert "an-entirely-ordinary-looking-value" not in json.dumps(result)


@pytest.mark.parametrize("secret", SECRET_VALUES)
def test_no_transport_leaks_a_secret(monkeypatch, secret) -> None:
    """Redaction applies on every transport, not just the direct call.

    Redaction lives on the single dispatch path, so this should hold for free —
    which is exactly why it is worth asserting: if a transport ever grew its own
    result-formatting path that bypassed the boundary, this is what catches it.
    """

    def leaky_handler(arguments, contract):
        return {"offers": [], "detail": secret}

    monkeypatch.setitem(server.HANDLERS, "superplane_discover_capacity", leaky_handler)

    arguments = {"workspace": "ws-alpha"}
    via_rest = rest_call(
        {"name": "superplane_discover_capacity", "arguments": arguments}, FULL_HEADERS
    )
    via_mcp = mcp_call_tool("superplane_discover_capacity", arguments, FULL_HEADERS)

    assert not contains_secret(via_rest)
    assert secret not in json.dumps(via_rest)
    # The MCP envelope serializes the result into a text block — check the raw
    # text, since that is what actually reaches the model.
    assert secret not in via_mcp["content"][0]["text"]


def test_nested_and_listed_secrets_are_reached(monkeypatch) -> None:
    """Secrets nested inside lists and dicts are scrubbed, not just top-level.

    A provider response is a tree, so a top-level-only scrub would miss the
    realistic case entirely.
    """

    def leaky_handler(arguments, contract):
        return {
            "offers": [
                {"offer_id": "o1", "credentials": {"secret": "AKIAIOSFODNN7EXAMPLE"}},
                {
                    "offer_id": "o2",
                    "notes": ["harmless", "ghp_1234567890abcdefghijklmnopqrstuvwx"],
                },
            ],
            "meta": {"deep": {"deeper": {"token": "xoxb-1234567890-abcdefghijkl"}}},
        }

    monkeypatch.setitem(server.HANDLERS, "superplane_discover_capacity", leaky_handler)

    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    assert not contains_secret(result), result
    serialized = json.dumps(result)
    for fragment in ("AKIA", "ghp_", "xoxb-"):
        assert fragment not in serialized


# ---------------------------------------------------------------------------
# The real handlers, and the shape of the surface itself
# ---------------------------------------------------------------------------


def test_real_tool_results_carry_no_secret() -> None:
    """The actual tool results are secret-free end to end."""
    contract = CapacityContract()
    discovered = dispatch_tool(
        "superplane_discover_capacity",
        {"workspace": "ws-alpha"},
        FULL_HEADERS,
        contract,
    )
    assert not contains_secret(discovered)

    allocated = dispatch_tool(
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-a1"},
        FULL_HEADERS,
        contract,
    )
    assert not contains_secret(allocated)

    released = dispatch_tool(
        "superplane_release_allocation",
        {
            "workspace": "ws-alpha",
            "allocation_id": allocated["allocation"]["allocation_id"],
        },
        FULL_HEADERS,
        contract,
    )
    assert not contains_secret(released)


def test_capacity_offers_have_no_credential_field() -> None:
    """Discovery's data model carries no credential at all.

    Defence in depth behind redaction: discovery answers "what could I run and
    what would it cost", which never requires a provider secret. If a credential
    field is ever added to this shape, that is a design change to challenge, not
    a value for the scrubber to catch.
    """
    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    for offer in result["offers"]:
        for key in offer:
            assert not any(
                marker in key.lower()
                for marker in ("secret", "token", "credential", "key", "password")
            ), f"capacity offer exposes a credential-ish field: {key}"


def test_denial_reasons_do_not_echo_header_values() -> None:
    """A denial must not reflect caller headers back.

    Otherwise an error message becomes a channel for reading identity values —
    including another tenant's, if a caller can induce a denial that quotes them.
    """
    headers = dict(FULL_HEADERS)
    headers["x-adp-capabilities"] = "superplane:capacity:read"
    result = dispatch_tool(
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-a1"},
        headers,
    )
    serialized = json.dumps(result)
    assert "user-1" not in serialized
    assert "tenant-1" not in serialized


def test_contract_errors_do_not_leak_upstream_payloads(monkeypatch) -> None:
    """A contract error carrying a secret does not reach the caller verbatim."""

    def exploding_handler(arguments, contract):
        # Simulates a handler surfacing an upstream error body that happens to
        # contain a credential.
        return {"error": "upstream said: AKIAIOSFODNN7EXAMPLE"}

    monkeypatch.setitem(
        server.HANDLERS, "superplane_discover_capacity", exploding_handler
    )
    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    assert not contains_secret(result), result


# ---------------------------------------------------------------------------
# Redaction unit behaviour
# ---------------------------------------------------------------------------


def test_redact_preserves_non_secret_data() -> None:
    """Redaction must not damage the numbers the surface exists to report."""
    payload = {
        "instance_type": "g5.xlarge",
        "available": 4,
        "price_per_hour_usd": 1.006,
        "region": "us-east-1",
        "enabled": True,
        "nothing": None,
    }
    assert redact(payload) == payload


def test_quota_fields_named_token_are_not_scrubbed() -> None:
    """`token_budget` is a quota an operator needs, not a credential.

    The allowlist is ordered before the secret-name match precisely so that a
    useful field is not scrubbed into uselessness while `token` still is.
    """
    payload = {"token_budget": 100000, "max_tokens": 4096, "tokens_used": 12}
    assert redact(payload) == payload
    # ...but a bare credential-ish key still goes.
    assert redact({"token": "abc"})["token"] == PLACEHOLDER


def test_quota_name_fragment_does_not_allow_a_secret_field() -> None:
    """A safe quota name inside a credential key must not bypass redaction."""
    payload = {
        "token_budget_api_key": "opaque-provider-value",
        "client_secret_max_tokens": "opaque-provider-value",
        "TOKEN_COUNT_PASSWORD": "opaque-provider-value",
    }
    assert redact(payload) == {key: PLACEHOLDER for key in payload}
    assert redact({"TOKEN_BUDGET": 100, "max_tokens": 42}) == {
        "TOKEN_BUDGET": 100,
        "max_tokens": 42,
    }


def test_redact_handles_tuples_and_scalars() -> None:
    assert redact(("ok", "AKIAIOSFODNN7EXAMPLE")) == ("ok", PLACEHOLDER)
    assert redact(7) == 7
    assert redact(None) is None
    assert redact("plain string") == "plain string"


def test_contains_secret_detects_before_redaction() -> None:
    """The detector is independent of the scrubber and reports honestly."""
    leaky = {"a": {"b": ["ghp_1234567890abcdefghijklmnopqrstuvwx"]}}
    assert contains_secret(leaky)
    assert not contains_secret(redact(leaky))
