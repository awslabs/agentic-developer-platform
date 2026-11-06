"""Controlled public-shape regressions, not authenticated provider acceptance.

ROVO-999 documents OAuth's account_id and optional user profile fields:
https://jira.atlassian.com/browse/ROVO-999
MCP structuredContent is optional; JSON may appear in a text content block.
"""

import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from tool_adapters.atlassian_observe import SITES_TOOL, SUBJECT_TOOL, collect_observation

PROFILE = {
    "account_id": "fixture-subject",
    "displayName": "Controlled test user",
    "emailAddress": "fixture@example.invalid",
    "timezone": "UTC",
    "locale": "en-US",
    "avatarUrls": {"24x24": "https://example.invalid/avatar"},
}
RESOURCES = [
    {
        "id": "fixture-site",
        "scopes": ["read:jira:agent-interface"],
        "name": "Controlled test site",
        "url": "https://example.invalid",
        "avatarUrl": "https://example.invalid/avatar",
    }
]
SCHEMA_SHA = hashlib.sha256(b"{}").hexdigest()


def text_result(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


def observe(subject, sites=None, *, schema_sha=SCHEMA_SHA, scope_resources=None):
    calls = []

    class Transport:
        def __init__(self, *, deadline):
            pass

        def send(self, method, params, token):
            assert token == "controlled-transient-token"
            calls.append((method, params))
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {"result": {"tools": [{"name": name, "inputSchema": {}} for name in (SUBJECT_TOOL, SITES_TOOL)]}}
            assert method == "tools/call" and params["arguments"] == {}
            value = subject if params["name"] == SUBJECT_TOOL else sites or text_result(RESOURCES)
            return {"result": value}

    settings = SimpleNamespace(subject_schema_sha256=schema_sha, sites_schema_sha256=SCHEMA_SHA)

    def scope_fetcher(token, *, deadline):
        assert token == "controlled-transient-token" and deadline > time.monotonic()
        assert scope_resources is not None, "Legacy MCP scopes must not trigger another provider request"
        calls.append(("scope-readback", {}))
        return scope_resources

    return collect_observation(
        "controlled-transient-token", settings, transport_factory=Transport, scope_fetcher=scope_fetcher
    ), calls


@pytest.mark.parametrize("representation", ["text", "structured", "both"])
def test_documented_oauth_profile_and_site_metadata(representation):
    subject = text_result(PROFILE)
    sites = text_result(RESOURCES)
    if representation in {"structured", "both"}:
        subject["structuredContent"] = PROFILE
        sites["structuredContent"] = {"resources": RESOURCES}
    if representation == "structured":
        subject.pop("content")
        sites.pop("content")
    (account, observed_sites, scopes), calls = observe(subject, sites)
    assert account == "fixture-subject"
    assert [site.model_dump() for site in observed_sites] == [
        {"site_id": "fixture-site", "scopes": ("read:jira:agent-interface",)}
    ]
    assert "read:jira:agent-interface" in scopes
    assert "displayName" not in str(observed_sites) and "example.invalid" not in str(observed_sites)
    assert [params["name"] for method, params in calls if method == "tools/call"] == [SUBJECT_TOOL, SITES_TOOL]


def test_retains_previous_identity_form_and_rejects_conflicting_ids():
    assert observe({"structuredContent": {"accountId": "fixture-subject"}})[0][0] == "fixture-subject"
    assert observe(text_result({**PROFILE, "accountId": PROFILE["account_id"]}))[0][0] == "fixture-subject"
    with pytest.raises(ValueError, match="subject changed"):
        observe(text_result({**PROFILE, "accountId": "another-subject"}))


@pytest.mark.parametrize("representation", ["text", "structured", "both"])
def test_mcp_data_envelope_and_equivalent_unwrapped_identity(representation):
    subject = text_result({"data": {"accountId": "fixture-subject"}})
    if representation in {"structured", "both"}:
        subject["structuredContent"] = PROFILE
    if representation == "structured":
        subject = {"structuredContent": {"data": PROFILE}}
    assert observe(subject)[0][0] == "fixture-subject"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"error": "denied", "accountId": "fixture-subject"},
        {"accountId": "another-subject"},
        {"accountId": "fixture-subject", "account_id": "another-subject"},
    ],
)
def test_malformed_or_conflicting_data_envelope_is_refused(data):
    with pytest.raises(ValueError, match="subject changed"):
        observe(text_result({**PROFILE, "data": data}))


@pytest.mark.parametrize("value", [None, "", 1, True, [], {}, "white space", "bad\x00subject", "a" * 513])
def test_invalid_wrapped_subject_is_refused(value):
    with pytest.raises(ValueError, match="subject changed"):
        observe(text_result({"data": {"accountId": value}}))


@pytest.mark.parametrize("value", [None, "", 1, True, [], {}, "white space", "bad\x00subject", "a" * 513])
def test_missing_or_invalid_provider_subject_is_refused(value):
    with pytest.raises(ValueError):
        observe(text_result({**PROFILE, "account_id": value}))


@pytest.mark.parametrize("is_error", [True, "false", "true", 0, None])
def test_error_flag_cannot_be_coerced_to_success(is_error):
    with pytest.raises(ValueError):
        observe({**text_result(PROFILE), "isError": is_error})


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"content": []},
        {"structuredContent": None},
        {"content": "not-a-list"},
        {"content": [{"type": "image", "text": json.dumps(PROFILE)}]},
        {"content": [{"type": "text", "text": None}]},
        {"content": [{"type": "text", "text": "not JSON"}]},
        {"content": [{"type": "text", "text": '{"account_id":"one","account_id":"two"}'}]},
        {"content": [{"type": "text", "text": '{"account_id":"one","unused":NaN}'}]},
        {"content": [{"type": "text", "text": " " * 65537}]},
        {"content": [{"type": "text", "text": json.dumps(PROFILE)}] * 2},
        {"structuredContent": PROFILE, **text_result({**PROFILE, "account_id": "other"})},
        text_result({**PROFILE, "error": True}),
    ],
)
def test_ambiguous_or_invalid_mcp_identity_results_are_refused(result):
    with pytest.raises(ValueError):
        observe(result)


@pytest.mark.parametrize(
    "resources",
    [
        [],
        RESOURCES * 2,
        [{"id": "fixture-site"}],
        [{"scopes": []}],
        [{"id": "fixture-site", "scopes": "read:jira:agent-interface"}],
        [{"id": "fixture-site", "scopes": [1]}],
        [{"id": "fixture-site", "scopes": ["read:jira:agent-interface"] * 2}],
        [{"id": "bad site", "scopes": []}],
        [{**RESOURCES[0], "error": True}],
        {"resources": RESOURCES, "nextCursor": "unread-page"},
        {"result": RESOURCES},
    ],
)
def test_missing_duplicate_or_incomplete_resources_are_refused(resources):
    with pytest.raises(ValueError):
        observe(text_result(PROFILE), text_result(resources))


def test_text_and_structured_site_scopes_must_agree():
    changed = copy.deepcopy(RESOURCES)
    changed[0]["scopes"].append("write:jira:agent-interface")
    with pytest.raises(ValueError, match="ambiguous"):
        observe(text_result(PROFILE), {"structuredContent": {"resources": RESOURCES}, **text_result(changed)})


def modern_sites(*, access="read-only"):
    return {
        "data": {
            "resources": [
                {
                    "cloudId": "fixture-site",
                    "products": [
                        {"id": "jira", "access": access},
                        {"id": "confluence", "access": "read-only"},
                    ],
                }
            ]
        }
    }


@pytest.mark.parametrize("access", ["read-only", "read-write"])
def test_product_access_uses_actual_scopes_and_never_invents_confluence_grants(access):
    resources = [{"id": "fixture-site", "scopes": ["read:jira:agent-interface", "write:jira:agent-interface"]}]
    (_, sites, scopes), calls = observe(
        text_result({"data": PROFILE}), text_result(modern_sites(access=access)), scope_resources=resources
    )
    expected = {"read:jira:agent-interface"} | ({"write:jira:agent-interface"} if access == "read-write" else set())
    assert set(sites[0].scopes) == expected
    assert set(scopes) == expected | {"read:me", "read:account"}
    assert [method for method, _ in calls].count("scope-readback") == 1


@pytest.mark.parametrize(
    "resources", [[], [{"id": "different-site", "scopes": []}], RESOURCES * 2, modern_sites(), [{"id": "fixture-site"}]]
)
def test_product_access_cannot_substitute_for_verified_site_scope_evidence(resources):
    with pytest.raises(ValueError):
        observe(text_result(PROFILE), text_result(modern_sites()), scope_resources=resources)


@pytest.mark.parametrize(
    "change",
    ["error", "id", "scopes", "empty-products", "duplicate-product", "unknown-access", "bad-site", "nested-error"],
)
def test_malformed_modern_resources_are_refused(change):
    value = modern_sites()
    site = value["data"]["resources"][0]
    if change in {"error", "id", "scopes"}:
        site[change] = "ambiguous"
    elif change == "empty-products":
        site["products"] = []
    elif change == "duplicate-product":
        site["products"] *= 2
    elif change == "unknown-access":
        site["products"][0]["access"] = "admin"
    elif change == "bad-site":
        site["cloudId"] = "bad site"
    else:
        value["data"]["error"] = "denied"
    with pytest.raises(ValueError):
        observe(text_result(PROFILE), text_result(value), scope_resources=RESOURCES)


def test_public_response_support_does_not_bypass_schema_pin():
    with pytest.raises(ValueError, match="unverified provider schema"):
        observe(text_result(PROFILE), schema_sha="0" * 64)


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"account_id":"one","account_id":"two"}}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"account_id":"one","extra":NaN}}}',
    ],
)
def test_transport_rejects_ambiguous_structured_json_before_it_is_normalized(monkeypatch, content_type, payload):
    from tool_adapters import atlassian_observe

    body = payload if content_type == "application/json" else b"data: " + payload + b"\n\n"
    sock = SimpleNamespace(settimeout=lambda _: None)

    class Connection:
        def __init__(self, host, timeout):
            assert host == "mcp.atlassian.com"

        def request(self, method, path, body, headers):
            assert method == "POST" and path == "/v2/mcp?tools=all"

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                getheader=lambda name, default=None: content_type if name == "Content-Type" else default,
                read=lambda maximum: body[:maximum],
            )

        def close(self):
            pass

    monkeypatch.setattr(atlassian_observe.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(
        atlassian_observe.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))]
    )
    monkeypatch.setattr(atlassian_observe.socket, "create_connection", lambda *args, **kwargs: sock)
    monkeypatch.setattr(
        atlassian_observe.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=lambda *args, **kwargs: sock),
    )
    with pytest.raises(ValueError, match="provider observation"):
        atlassian_observe.MCPTransport(deadline=time.monotonic() + 10).send(
            "tools/call", {"name": SUBJECT_TOOL, "arguments": {}}, "controlled-transient-token"
        )
