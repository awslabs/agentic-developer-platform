"""Offline wire tests for the unregistered Jira provider primitives."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.internal import credential_routes
from src.internal import jira_provider as jira

SITE_A = "00000000-0000-4000-8000-000000000001"
SITE_B = "00000000-0000-4000-8000-000000000002"
TOKEN = "fixture-private-jira-token"
SUMMARY = "Dedicated fixture sentinel"
ACCOUNT = "fixture-account:12345678-abcd-4000-8000-000000000001"


def resource(cloud_id=SITE_A, scopes=None, **extra):
    return {
        "id": cloud_id,
        "scopes": ["read:jira-work"] if scopes is None else scopes,
        "url": "https://ignored.example.test",
        "name": "Not an account identity",
        **extra,
    }


def issue(**extra):
    return {"id": "10001", "key": "FIX-1", "fields": {"summary": SUMMARY}, **extra}


def profile(**extra):
    return {"account_id": ACCOUNT, "account_type": "atlassian", "account_status": "active", **extra}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def wire(monkeypatch):
    settings = SimpleNamespace(vault_proxy_require_https=True, vault_proxy_host_allowlist="api.atlassian.com")
    monkeypatch.setattr(jira, "get_settings", lambda: settings)
    dns = []

    def resolve(host, port):
        dns.append((host, port))
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(credential_routes.socket, "getaddrinfo", resolve)
    state = SimpleNamespace(requests=[], responses=[], dns=dns, settings=settings)

    async def dispatch(request):
        state.requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "8.8.8.8" and request.url.scheme == "https"
        assert request.headers["host"] == "api.atlassian.com"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["sni_hostname"] == "api.atlassian.com"
        response = state.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client_class = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"].connect == 3 and kwargs["timeout"].read == 10
        return client_class(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(jira.httpx, "AsyncClient", client)
    return state


def protected_site(cloud_id=SITE_A):
    return jira.JiraSiteObservation(cloud_id, ("read:jira-work",))


async def read(wire, response, *, cloud_id=SITE_A, identifier="FIX-1", site=None):
    wire.responses.append(response)
    return await jira.read_jira_issue(TOKEN, protected_site=protected_site() if site is None else site, cloud_id=cloud_id, issue_id_or_key=identifier)


def assert_error(error, code, status, **metadata):
    assert error.value.status_code == status
    assert error.value.detail == {"error": code, **metadata}
    assert TOKEN not in str(error.value)


async def test_observed_resource_scopes_are_separate_from_vendor_user_identity(wire):
    wire.responses.append(
        httpx.Response(
            200,
            json=[
                resource(SITE_A, ["write:jira-work", "read:jira-work"], accountId="untrusted-account"),
                resource(SITE_B),
                resource(SITE_A, ["read:confluence-space.summary"]),
            ],
        )
    )
    sites = await jira.observe_jira_sites(TOKEN)
    assert sites == (jira.JiraSiteObservation(SITE_A, ("read:jira-work", "write:jira-work")), protected_site(SITE_B))
    assert vars(sites[0]) == {"cloud_id": SITE_A, "scopes": ("read:jira-work", "write:jira-work")}
    assert jira.select_jira_site(sites, SITE_B) == protected_site(SITE_B)
    assert len(wire.requests) == 1
    assert wire.requests[0].url.path == "/oauth/token/accessible-resources"
    assert not wire.requests[0].url.query
    assert wire.dns == [("api.atlassian.com", None)]


async def test_user_identity_comes_from_authenticated_me_and_drops_other_profile_fields(wire):
    wire.responses.append(
        httpx.Response(200, json=profile(email="fixture@example.test", name="Fixture", picture="https://ignored.example.test/avatar"))
    )
    observed = await jira.observe_jira_user(TOKEN)
    assert vars(observed) == {"account_id": ACCOUNT}
    assert len(wire.requests) == 1
    assert wire.requests[0].url.path == "/me"
    assert not wire.requests[0].url.query
    assert wire.dns == [("api.atlassian.com", None)]


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        resource(account_id=ACCOUNT),
        issue(reporter={"accountId": ACCOUNT}, assignee={"accountId": ACCOUNT}),
        profile(account_id=None),
        profile(account_id=123),
        profile(account_id=""),
        profile(account_id=" padded "),
        profile(account_id="user\nother"),
        profile(account_id="a" * 129),
        profile(account_id="user/other"),
        profile(account_id="user\u202eother"),
        profile(account_type="app"),
        profile(account_type=None),
        profile(account_status="closed"),
        profile(account_status=None),
    ],
)
async def test_identity_never_inferred_from_site_issue_or_invalid_profile(wire, body):
    wire.responses.append(httpx.Response(200, json=body))
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_user(TOKEN)
    # A literal empty HTTP response is invalid JSON; every valid JSON shape is
    # refused specifically as identity evidence.
    assert error.value.status_code == 502
    assert error.value.detail["error"] in {"jira_invalid_response", "jira_invalid_identity_evidence"}
    assert len(wire.requests) == 1


@pytest.mark.parametrize("status,code", [(401, "jira_unauthorized"), (403, "jira_forbidden"), (429, "jira_throttled")])
async def test_identity_permission_denials_are_preserved_without_retry_or_fallback(wire, status, code):
    wire.responses.append(httpx.Response(status, text=TOKEN))
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_user(TOKEN)
    assert_error(error, code, status, provider_status=status)
    assert len(wire.requests) == 1


async def test_identity_token_echo_in_discarded_profile_field_is_refused(wire):
    wire.responses.append(httpx.Response(200, json=profile(email=TOKEN)))
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_user(TOKEN)
    assert_error(error, "jira_response_refused", 502)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("identifier", ["10001", "FIX-1"])
async def test_exact_site_and_issue_read_returns_only_bounded_selected_fields(wire, identifier):
    result = await read(
        wire,
        httpx.Response(
            200,
            json=issue(
                self="https://ignored.example.test/admin",
                fields={"summary": SUMMARY, "description": "private unrequested field"},
                changelog={"histories": ["never released"]},
            ),
        ),
        identifier=identifier,
    )
    assert result == {"id": "10001", "key": "FIX-1", "fields": {"summary": SUMMARY}}
    assert len(wire.requests) == 1
    assert wire.requests[0].url.path == f"/ex/jira/{SITE_A}/rest/api/3/issue/{identifier}"
    assert dict(wire.requests[0].url.params) == {"fields": "summary"}


@pytest.mark.parametrize("cloud_id", [SITE_B, "ABCDEF00" + SITE_A[8:], "../user", SITE_A + "?expand=all", "https://example.test", None])
async def test_unselected_or_invalid_site_never_dispatches(wire, cloud_id):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, json=issue()), cloud_id=cloud_id)
    assert error.value.status_code in {400, 403}
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize(
    "identifier",
    [
        "../10001",
        "%2e%2e%2f10001",
        "FIX-1?fields=*all",
        "FIX-1/other",
        "https://example.test",
        "fix-1",
        "0",
        "-1",
        "1e4",
        10001,
        None,
        "A" * 129 + "-1",
        "1" * 21,
    ],
)
async def test_issue_path_injection_or_unbounded_identifier_never_dispatches(wire, identifier):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, json=issue()), identifier=identifier)
    assert_error(error, "jira_invalid_issue_id", 400)
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize(
    "site",
    [
        None,
        {},
        jira.JiraSiteObservation(SITE_A, ()),
        jira.JiraSiteObservation(SITE_A, ("read:user",)),
        jira.JiraSiteObservation(SITE_A, None),
        jira.JiraSiteObservation(SITE_A, ["read:jira-work"]),
        jira.JiraSiteObservation(SITE_A, ("read:jira-work", "read:jira-work")),
    ],
)
async def test_missing_or_malformed_protected_scope_evidence_never_dispatches(wire, site):
    with pytest.raises(HTTPException) as error:
        await jira.read_jira_issue(TOKEN, protected_site=site, cloud_id=SITE_A, issue_id_or_key="FIX-1")
    assert error.value.status_code == 403
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [resource(scopes=None, id="not-a-cloud-id")],
        [resource(scopes=[None])],
        [resource(scopes=["read:jira-work"] * 2)],
        [resource(scopes="read:jira-work")],
        [resource(scopes=["read:jira-work", "scope "])],
        [resource()] * 129,
    ],
)
async def test_malformed_or_excessive_resource_evidence_is_rejected(wire, body):
    wire.responses.append(httpx.Response(200, json=body))
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_sites(TOKEN)
    assert error.value.status_code == 502
    assert len(wire.requests) == 1


async def test_duplicate_jira_sites_are_ambiguous_and_non_jira_scopes_grant_nothing(wire):
    wire.responses.extend(
        [httpx.Response(200, json=[resource(), resource()]), httpx.Response(200, json=[resource(scopes=["read:confluence-content.all"])])]
    )
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_sites(TOKEN)
    assert_error(error, "jira_ambiguous_site_evidence", 502)
    sites = await jira.observe_jira_sites(TOKEN)
    assert sites == ()
    with pytest.raises(HTTPException) as error:
        jira.select_jira_site(sites, SITE_A)
    assert_error(error, "jira_site_denied", 403)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "jira_unauthorized"),
        (403, "jira_forbidden"),
        (404, "jira_not_found"),
        (429, "jira_throttled"),
        (400, "jira_http_error"),
        (503, "jira_http_error"),
        (302, "jira_http_error"),
    ],
)
@pytest.mark.parametrize("operation", ["sites", "issue"])
async def test_provider_failures_keep_status_without_body_or_retry_dispatch(wire, status, code, operation):
    stream = Chunks([TOKEN.encode() * 100000])
    response = httpx.Response(status, headers={"location": "https://evil.example.test/", "retry-after": "7"}, stream=stream)
    with pytest.raises(HTTPException) as error:
        if operation == "sites":
            wire.responses.append(response)
            await jira.observe_jira_sites(TOKEN)
        else:
            await read(wire, response)
    assert_error(error, code, status if status != 302 else 502, provider_status=status)
    assert error.value.headers == ({"Retry-After": "7"} if status == 429 else None)
    assert stream.reads == 0 and stream.closed
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "values,expected",
    [
        (["0"], "0"),
        (["86400"], "86400"),
        (["086"], "86"),
        (["86401"], None),
        (["7", "8"], None),
        (["-1"], None),
        (["1.5"], None),
        (["Sun, 18 Oct 2026 00:00:00 GMT"], None),
        ([TOKEN], None),
    ],
)
async def test_throttle_retry_guidance_is_bounded_without_inventing_a_shorter_wait(wire, values, expected):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(429, headers=[("retry-after", value) for value in values]))
    assert_error(error, "jira_throttled", 429, provider_status=429)
    assert error.value.headers == ({"Retry-After": expected} if expected is not None else None)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("field,value", [("error", "private error"), ("errorMessages", ["private error"]), ("errors", {"summary": "private error"})])
async def test_api_level_errors_cannot_be_successful_issue_reads(wire, field, value):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, json=issue(**{field: value})))
    assert_error(error, "jira_api_error", 502, provider_status=200)


@pytest.mark.parametrize(
    "payload",
    [b'{"id":"10001","id":"10002"}', b'{"id":NaN}', b"\xff", b"[" * 2000 + b"]" * 2000, b'{"ignored":"\\ud800"}'],
    ids=["duplicate-key", "nan", "invalid-utf8", "deep-json", "unpaired-surrogate"],
)
async def test_invalid_json_is_sanitized(wire, payload):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, content=payload))
    assert_error(error, "jira_invalid_response", 502)


@pytest.mark.parametrize("escaped", [False, True])
async def test_token_echo_in_even_unused_provider_data_is_refused(wire, escaped):
    payload = json.dumps(issue(unused={"token": TOKEN})).encode()
    if escaped:
        payload = payload.replace(TOKEN.encode(), b"fixture-private-jira-\\u0074oken")
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, content=payload))
    assert_error(error, "jira_response_refused", 502)


async def test_response_stream_stops_at_byte_limit_and_closes(wire):
    stream = Chunks([b" " * jira.MAX_RESPONSE, b"x", TOKEN.encode()])
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, stream=stream))
    assert_error(error, "jira_response_too_large", 502)
    assert stream.reads == 2 and stream.closed


async def test_unrequested_compression_is_refused_before_reading(wire):
    stream = Chunks([b"not read"])
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream))
    assert_error(error, "jira_encoded_response_refused", 502)
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize(
    "body",
    [
        [],
        issue(id=10001),
        issue(id="0"),
        issue(key="other"),
        issue(fields={}),
        issue(fields={"summary": ""}),
        issue(fields={"summary": "x" * 4097}),
        issue(fields={"summary": "first\nsecond"}),
        issue(key="FIX-2"),
    ],
)
async def test_issue_shape_and_requested_identity_must_match(wire, body):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, json=body))
    assert error.value.status_code == 502
    assert error.value.detail["error"] in {"jira_invalid_issue_response", "jira_issue_mismatch"}


async def test_immutable_requested_id_mismatch_is_refused(wire):
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, json=issue(id="10002")), identifier="10001")
    assert_error(error, "jira_issue_mismatch", 502)


@pytest.mark.parametrize("token", [None, "", "token\r\nother", "tökén", "t" * 16385])
async def test_invalid_internal_credentials_never_dispatch(wire, token):
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_sites(token)
    assert_error(error, "jira_credential_unavailable", 403)
    assert not wire.requests and not wire.dns


@pytest.mark.parametrize("problem", [httpx.ReadTimeout(TOKEN), httpx.ConnectError(TOKEN), TimeoutError(TOKEN)])
async def test_transport_exceptions_never_expose_tokens_or_retry(wire, problem):
    with pytest.raises(HTTPException) as error:
        await read(wire, problem)
    assert_error(error, "jira_transport_unavailable", 503)
    assert error.value.__suppress_context__
    assert len(wire.requests) == 1


async def test_total_timeout_bounds_a_response_that_never_finishes(wire, monkeypatch):
    deadline = None

    class Stalled(Chunks):
        async def __aiter__(self):
            self.reads += 1
            assert deadline is not None
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
            yield b"never reached"

    timeout = asyncio.timeout

    def bounded_timeout(seconds):
        nonlocal deadline
        assert seconds == 15
        deadline = timeout(None)
        return deadline

    monkeypatch.setattr(jira.asyncio, "timeout", bounded_timeout)
    stream = Stalled([])
    with pytest.raises(HTTPException) as error:
        await read(wire, httpx.Response(200, stream=stream))
    assert_error(error, "jira_transport_unavailable", 503)
    assert deadline.expired()
    assert stream.reads == 1 and stream.closed
    assert len(wire.requests) == 1


@pytest.mark.parametrize("problem", ["denied_host", "private_dns", "dns_failure"])
async def test_existing_egress_pinning_rejection_is_preserved_without_dispatch(wire, monkeypatch, problem):
    if problem == "denied_host":
        wire.settings.vault_proxy_host_allowlist = "other.example.test"
    elif problem == "private_dns":
        monkeypatch.setattr(credential_routes.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("127.0.0.1", 443))])
    else:

        def fail(*args):
            raise credential_routes.socket.gaierror(TOKEN)

        monkeypatch.setattr(credential_routes.socket, "getaddrinfo", fail)
    with pytest.raises(HTTPException) as error:
        await jira.observe_jira_sites(TOKEN)
    assert_error(error, "jira_egress_unavailable", 503)
    assert not wire.requests
