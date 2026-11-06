"""Door HTTP requests use the fields decoded by pinned internal/json/json.go.

The companion Go auth-proxy tests run that actual pinned JSON server and prove
these options reach Searcher.Search, while legacy top-level num does not.
"""

import asyncio
import json

import httpx
import pytest
import respx

from door.browse_backend import _list_zoekt_files, _read_zoekt_file
from door.search_backend import ZoektSearchBackend
from door.zoekt_transport import MAX_RESPONSE_BYTES, ZoektUnavailable, search_json

ORIGIN = "http://zoekt.example.test:6070"
KEY = "offline-provider-key-" * 4


@pytest.mark.parametrize(
    "operation,documents,whole",
    [
        ("search", 7, False),
        ("browse-list", 500, False),
        ("browse-read", 5, True),
    ],
)
@pytest.mark.asyncio
async def test_all_door_zoekt_paths_send_authenticated_provider_options(
    monkeypatch, operation, documents, whole
):
    monkeypatch.setenv("ZOEKT_API_KEY", KEY)
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(200, json={"Result": {"Files": []}})
        if operation == "search":
            await ZoektSearchBackend(ORIGIN).search("needle", limit=documents)
        elif operation == "browse-list":
            await _list_zoekt_files("org/repo", "src", zoekt_url=ORIGIN)
        else:
            await _read_zoekt_file("org/repo", "src/main.py", zoekt_url=ORIGIN)
        assert route.call_count == 1
        request = route.calls[0].request
        body = json.loads(request.content)
        assert set(body) == {"q", "opts"}
        assert body["opts"] == {
            "MaxWallTime": 10_000_000_000,
            "MaxDocDisplayCount": documents,
            "ShardMaxMatchCount": 100_000,
            "TotalMaxMatchCount": 1_000_000,
            "Whole": whole,
        }
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert KEY.encode() not in request.content


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": True},
        {"timeout": float("inf")},
        {"timeout": -1},
        {"query": "x" * 65536},
    ],
)
@pytest.mark.asyncio
async def test_invalid_bounds_are_refused_before_any_provider_request(arguments):
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ORIGIN + "/api/search").respond(200, json={"Result": {}})
        with pytest.raises(ZoektUnavailable):
            await search_json(
                ORIGIN, **{"query": "needle", "limit": 5, "api_key": KEY, **arguments}
            )
        assert not route.called


@pytest.mark.asyncio
async def test_large_request_limits_are_capped_in_consumed_options():
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(200, json={"Result": {}})
        await search_json(ORIGIN, "needle", limit=10_000, timeout=600, api_key=KEY)
        options = json.loads(route.calls[0].request.content)["opts"]
        assert options["MaxDocDisplayCount"] == 500 and options["MaxWallTime"] == 10_000_000_000


@pytest.mark.asyncio
async def test_oversized_provider_response_is_unknown_without_retry():
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(
            200, content=b"x" * (MAX_RESPONSE_BYTES + 1)
        )
        with pytest.raises(ZoektUnavailable):
            await search_json(ORIGIN, "needle", limit=5, api_key=KEY)
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_unrequested_response_compression_is_refused_without_retry():
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(
            200, headers={"Content-Encoding": "gzip"}
        )
        with pytest.raises(ZoektUnavailable):
            await search_json(ORIGIN, "needle", limit=5, api_key=KEY)
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_wall_deadline_closes_original_slow_response_without_retry():
    started, closed = asyncio.Event(), asyncio.Event()

    class SlowResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'{"Result":'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(200, stream=SlowResponse())
        with pytest.raises(ZoektUnavailable):
            await asyncio.wait_for(
                search_json(ORIGIN, "needle", limit=5, timeout=0.1, api_key=KEY), 3
            )
        assert started.is_set() and closed.is_set()
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_provider_redirect_never_forwards_credentials_or_replays():
    with respx.mock(assert_all_called=False) as router:
        original = router.post(ORIGIN + "/api/search").respond(
            307, headers={"Location": "https://foreign.example.test/search"}
        )
        foreign = router.post("https://foreign.example.test/search").respond(
            200, json={"Result": {}}
        )
        with pytest.raises(ZoektUnavailable):
            await search_json(ORIGIN, "needle", limit=5, api_key=KEY)
        assert original.call_count == 1 and not foreign.called


@pytest.mark.parametrize("read", [False, True])
@pytest.mark.asyncio
async def test_browse_without_backend_auth_never_sends(monkeypatch, read):
    monkeypatch.delenv("ZOEKT_API_KEY", raising=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ORIGIN + "/api/search").respond(200, json={"Result": {}})
        method = _read_zoekt_file if read else _list_zoekt_files
        assert await method("org/repo", "src/main.py", zoekt_url=ORIGIN) == []
        assert not route.called
