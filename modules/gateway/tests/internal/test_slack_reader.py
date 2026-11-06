"""Offline intercepted-wire coverage; no Slack calls or message writes."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.internal import credential_routes
from src.internal import slack_reader as slack

TOKEN = "xoxp-fixture-private-token"
OWNER = "U00000001"
TARGET = "U00000002"
TEAM = "T00000001"


def identity(**overrides):
    return {"ok": True, "user_id": OWNER, "team_id": TEAM, "user": "unused-owner-name", "url": "https://ignored.example.test/", **overrides}


def profile(**overrides):
    return {"ok": True, "user": {"id": TARGET, "name": "fixture-reader", "profile": {"email": "private@example.test"}}, **overrides}


def response(body, *, scopes="users:read", status=200, headers=None):
    return httpx.Response(status, json=body, headers={"x-oauth-scopes": scopes, **(headers or {})})


def binding(**overrides):
    return slack.SlackIdentityObservation(**{"user_id": OWNER, "team_id": TEAM, "scopes": ("users:read",), **overrides})


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def wire(monkeypatch):
    settings = SimpleNamespace(vault_proxy_require_https=True, vault_proxy_host_allowlist="slack.com")
    monkeypatch.setattr(slack, "get_settings", lambda: settings)
    state = SimpleNamespace(requests=[], responses=[], dns=[], settings=settings)

    def resolve(host, port):
        state.dns.append((host, port))
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(credential_routes.socket, "getaddrinfo", resolve)

    async def dispatch(request):
        state.requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "8.8.8.8"
        assert request.headers["host"] == "slack.com" and request.extensions["sni_hostname"] == "slack.com"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        assert request.content == b""
        if request.url.path == "/api/auth.test":
            assert request.method == "POST" and not request.url.query
        else:
            assert request.url.path == "/api/users.info" and request.method == "GET"
            assert dict(request.url.params) == {"user": TARGET}
        reply = state.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client_class = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"].connect == 3 and kwargs["timeout"].read == 10
        return client_class(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(slack.httpx, "AsyncClient", client)
    return state


async def read(wire, *, observed=None, user_reply=None, protected=None, user_id=TARGET):
    wire.responses.extend([response(identity()) if observed is None else observed, response(profile()) if user_reply is None else user_reply])
    return await slack.read_slack_user(TOKEN, protected_identity=binding() if protected is None else protected, user_id=user_id)


def assert_error(error, code, status, **metadata):
    assert error.value.status_code == status
    assert error.value.detail == {"error": code, **metadata}
    assert TOKEN not in str(error.value)


async def test_observes_actual_user_workspace_and_grants_without_copying_extra_fields(wire):
    wire.responses.append(response(identity(extra="not identity"), scopes="users:read, chat:write"))
    result = await slack.observe_slack_identity(TOKEN)
    assert result == binding(scopes=("chat:write", "users:read"))
    assert vars(result) == {"user_id": OWNER, "team_id": TEAM, "scopes": ("chat:write", "users:read")}
    assert len(wire.requests) == 1 and wire.dns == [("slack.com", None)]


async def test_read_reobserves_protected_identity_before_exact_visible_user_read(wire):
    result = await read(wire)
    assert result == {"id": TARGET, "name": "fixture-reader"}
    assert [request.url.path for request in wire.requests] == ["/api/auth.test", "/api/users.info"]
    assert wire.dns == [("slack.com", None)] * 2
    assert TARGET != OWNER  # The visible requested user is not the connected owner.


async def test_extra_granted_write_scope_does_not_remove_reads_or_create_a_write_dispatch(wire):
    result = await read(
        wire,
        observed=response(identity(), scopes="chat:write, users:read"),
        user_reply=response(profile(), scopes="users:read,chat:write"),
        protected=binding(scopes=("chat:write", "users:read")),
    )
    assert result["id"] == TARGET
    assert len(wire.requests) == 2


@pytest.mark.parametrize("change", [{"bot_id": "B00000001"}, {"bot_id": None}, {"is_bot": True}, {"is_bot": 0}, {"is_bot": None}])
async def test_bot_tokens_cannot_reach_user_read_or_fall_back(wire, change):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=response(identity(**change)))
    assert_error(error, "slack_user_token_required", 403)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("change", [{"user_id": "U00000003"}, {"team_id": "T00000002"}])
async def test_foreign_token_identity_cannot_reach_user_read(wire, change):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=response(identity(**change)))
    assert_error(error, "slack_identity_changed", 403)
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "change", [{"user_id": None}, {"user_id": "fixture-alias"}, {"team_id": None}, {"team_id": "E00000001"}, {"user_id": "U" + "A" * 64}]
)
async def test_incomplete_or_unbounded_identity_has_no_substitute(wire, change):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=response(identity(**change)))
    assert_error(error, "slack_identity_evidence_unavailable", 403)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("header", [None, "", "users:read,users:read", "users:read,", "users:read,bad scope", ",".join(["users:read"] * 65)])
async def test_missing_or_malformed_actual_scope_evidence_is_not_inferred(wire, header):
    headers = {"x-accepted-oauth-scopes": "users:read"}
    if header is not None:
        headers["x-oauth-scopes"] = header
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, json=identity(), headers=headers))
    assert_error(error, "slack_scope_evidence_unavailable", 502)
    assert len(wire.requests) == 1


async def test_duplicate_actual_scope_headers_are_ambiguous(wire):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, json=identity(), headers=[("x-oauth-scopes", "users:read")] * 2))
    assert_error(error, "slack_scope_evidence_unavailable", 502)
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "stage,scopes,code",
    [
        ("identity", "chat:write", "slack_read_scope_missing"),
        ("identity", "users:read,chat:write", "slack_identity_changed"),
        ("user", "chat:write", "slack_read_scope_missing"),
        ("user", "users:read,chat:write", "slack_scope_changed"),
    ],
)
async def test_missing_or_changed_grants_never_release_user_data(wire, stage, scopes, code):
    replies = {"observed": response(identity(), scopes=scopes)} if stage == "identity" else {"user_reply": response(profile(), scopes=scopes)}
    with pytest.raises(HTTPException) as error:
        await read(wire, **replies)
    assert_error(error, code, 403)
    assert len(wire.requests) == (1 if stage == "identity" else 2)


@pytest.mark.parametrize(
    "protected",
    [
        {},
        binding(user_id=None),
        binding(team_id="other"),
        binding(scopes=[]),
        binding(scopes=("chat:write",)),
        binding(scopes=("users:read", "users:read")),
        binding(scopes=("users:read", "chat:write")),
        binding(scopes=(None,)),
    ],
)
async def test_bad_protected_identity_is_refused_before_any_dispatch(wire, protected):
    with pytest.raises(HTTPException) as error:
        await read(wire, protected=protected)
    assert_error(error, "slack_identity_evidence_unavailable", 403)
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize(
    "user_id", [None, 1, "", "@person", "U00000002&include_locale=true", "../users.list", "https://example.test", "U" + "A" * 64]
)
async def test_caller_id_cannot_change_method_or_path_or_expand_the_read(wire, user_id):
    with pytest.raises(HTTPException) as error:
        await read(wire, user_id=user_id)
    assert_error(error, "slack_invalid_user_id", 400)
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize(
    "vendor,status",
    [
        ("invalid_auth", 401),
        ("token_revoked", 401),
        ("missing_scope", 403),
        ("no_permission", 403),
        ("user_not_found", 404),
        ("ratelimited", 429),
        ("internal_error", 502),
        ("private unrecognized error", 502),
        (None, 502),
        ({}, 502),
    ],
)
@pytest.mark.parametrize("stage", ["identity", "user"])
async def test_http_200_ok_false_is_a_sanitized_tool_failure_not_success(wire, vendor, status, stage):
    reply = httpx.Response(200, json={"ok": False, "error": vendor}, headers={"retry-after": "7"})
    with pytest.raises(HTTPException) as error:
        await read(wire, **({"observed": reply} if stage == "identity" else {"user_reply": reply}))
    known = isinstance(vendor, str) and vendor in slack.API_ERRORS
    assert_error(error, "slack_api_error", status, provider_status=200, provider_error=vendor if known else "unrecognized_error")
    assert error.value.headers == ({"Retry-After": "7"} if vendor == "ratelimited" else None)
    assert len(wire.requests) == (1 if stage == "identity" else 2)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "slack_unauthorized"),
        (403, "slack_forbidden"),
        (404, "slack_not_found"),
        (429, "slack_throttled"),
        (503, "slack_http_error"),
        (302, "slack_http_error"),
    ],
)
async def test_http_failure_body_is_not_read_or_redirected_or_retried(wire, status, code):
    stream = Chunks([TOKEN.encode() * 100000])
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(status, headers={"location": "https://evil.example.test"}, stream=stream))
    assert_error(error, code, status if status != 302 else 502, provider_status=status)
    assert stream.closed and stream.reads == 0 and len(wire.requests) == 1


@pytest.mark.parametrize(
    "values,expected",
    [
        (["7"], "7"),
        (["86400"], "86400"),
        (["86401"], None),
        (["7", "8"], None),
        (["-1"], None),
        (["1.2"], None),
        ([TOKEN], None),
        (["Sun, 18 Oct 2026 00:00:00 GMT"], None),
    ],
)
async def test_retry_guidance_is_safe_and_never_shortens_the_provider_wait(wire, values, expected):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(429, headers=[("retry-after", value) for value in values]))
    assert_error(error, "slack_throttled", 429, provider_status=429)
    assert error.value.headers == ({"Retry-After": expected} if expected is not None else None)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("body", [[], {}, identity(ok=1), identity(ok="true"), identity(error="inconsistent")])
async def test_invalid_or_contradictory_success_shape_is_refused(wire, body):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=response(body))
    assert_error(error, "slack_invalid_response", 502)


@pytest.mark.parametrize(
    "user",
    [
        None,
        {},
        {"id": OWNER, "name": "wrong person"},
        {"id": TARGET, "name": ""},
        {"id": TARGET, "name": "x" * 81},
        {"id": TARGET, "name": "first\nsecond"},
        {"id": TARGET, "name": 3},
    ],
)
async def test_wrong_requested_user_or_unbounded_profile_is_refused(wire, user):
    with pytest.raises(HTTPException) as error:
        await read(wire, user_reply=response(profile(user=user)))
    assert_error(error, "slack_invalid_user_response", 502)
    assert len(wire.requests) == 2


@pytest.mark.parametrize(
    "payload",
    [b'{"ok":true,"ok":false}', b'{"ok":NaN}', b"\xff", b"[" * 2000 + b"]" * 2000, b'{"ignored":"\\ud800"}'],
    ids=["duplicate", "nan", "utf8", "depth", "surrogate"],
)
async def test_bad_json_is_bounded_and_sanitized(wire, payload):
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, content=payload))
    assert_error(error, "slack_invalid_response", 502)


@pytest.mark.parametrize("escaped", [False, True])
async def test_token_echo_in_unused_provider_data_never_leaves_adapter(wire, escaped):
    payload = json.dumps(identity(unused={"secret": TOKEN})).encode()
    if escaped:
        payload = payload.replace(TOKEN.encode(), b"xoxp-fixture-private-\\u0074oken")
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, content=payload))
    assert_error(error, "slack_response_refused", 502)


@pytest.mark.parametrize("stage", ["identity", "user"])
async def test_token_echo_cannot_become_observed_scope_evidence(wire, stage):
    reply = response(identity() if stage == "identity" else profile(), scopes="users:read," + TOKEN)
    with pytest.raises(HTTPException) as error:
        await read(wire, **({"observed": reply} if stage == "identity" else {"user_reply": reply}))
    assert_error(error, "slack_response_refused", 502)
    assert len(wire.requests) == (1 if stage == "identity" else 2)


@pytest.mark.parametrize("encoding", [None, "gzip"])
async def test_oversized_or_encoded_stream_is_closed_without_consuming_rest(wire, encoding):
    stream = Chunks([b" " * slack.MAX_RESPONSE, b"x", TOKEN.encode()])
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, stream=stream, headers={"content-encoding": encoding} if encoding else {}))
    assert_error(error, "slack_encoded_response_refused" if encoding else "slack_response_too_large", 502)
    assert stream.closed and stream.reads == (0 if encoding else 2)


@pytest.mark.parametrize("token", [None, "", "token\r\nother", "tökén", "t" * 16385])
async def test_invalid_internal_credentials_never_dispatch(wire, token):
    with pytest.raises(HTTPException) as error:
        await slack.observe_slack_identity(token)
    assert_error(error, "slack_credential_unavailable", 403)
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize("stage", ["identity", "user"])
async def test_transport_loss_is_sanitized_and_never_retried(wire, stage):
    with pytest.raises(HTTPException) as error:
        await read(wire, **({"observed": httpx.ReadTimeout(TOKEN)} if stage == "identity" else {"user_reply": httpx.ReadTimeout(TOKEN)}))
    assert_error(error, "slack_transport_unavailable", 503)
    assert error.value.__suppress_context__
    assert len(wire.requests) == (1 if stage == "identity" else 2)


@pytest.mark.parametrize("deadline", [15, 25])
async def test_per_request_and_whole_read_deadlines_close_stalled_stream(wire, monkeypatch, deadline):
    class Stalled(Chunks):
        async def __aiter__(self):
            self.reads += 1
            await asyncio.Event().wait()
            yield b"unreachable"

    timeout = asyncio.timeout
    deadlines = []

    def bounded(seconds):
        deadlines.append(seconds)
        assert seconds in {15, 25}
        return timeout(0.05 if seconds == deadline else 1)

    monkeypatch.setattr(slack.asyncio, "timeout", bounded)
    stream = Stalled([])
    with pytest.raises(HTTPException) as error:
        await read(wire, observed=httpx.Response(200, stream=stream))
    assert_error(error, "slack_transport_unavailable", 503)
    assert deadlines == [25, 15]
    assert stream.reads == 1 and stream.closed and len(wire.requests) == 1


@pytest.mark.parametrize("problem", ["denied_host", "private_dns", "dns_failure"])
async def test_existing_egress_validator_refuses_unsafe_destination_before_dispatch(wire, monkeypatch, problem):
    if problem == "denied_host":
        wire.settings.vault_proxy_host_allowlist = "other.example.test"
    elif problem == "private_dns":
        monkeypatch.setattr(credential_routes.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("127.0.0.1", 443))])
    else:

        def fail(*args):
            raise credential_routes.socket.gaierror(TOKEN)

        monkeypatch.setattr(credential_routes.socket, "getaddrinfo", fail)
    with pytest.raises(HTTPException) as error:
        await slack.observe_slack_identity(TOKEN)
    assert_error(error, "slack_egress_unavailable", 503)
    assert not wire.requests
