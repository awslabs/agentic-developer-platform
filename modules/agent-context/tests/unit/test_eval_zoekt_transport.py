"""Offline evaluation calls exercise the real shared HTTP transport."""

import asyncio
import json

import httpx
import pytest
import respx

from tests.eval import run_eval as evaluation

ORIGIN = "http://zoekt.example.test:6070"
KEY = "offline-evaluation-key-" * 4
UNAVAILABLE = "Zoekt evaluation request unavailable"


def call_path(config, operation):
    if operation == "ingestion":
        return evaluation.check_ingestion(
            config, [evaluation.Repo("fixture", "https://example.test/fixture", "code")]
        )
    if operation == "search":
        return evaluation.query_direct(config, "search_exact", {"query": "needle", "limit": 2})
    return evaluation.query_direct(config, "browse", {"uri": "/fixture/src"})


@pytest.mark.parametrize("result_field", ["Files", "FileMatches"])
@pytest.mark.parametrize(
    "operation,query,documents",
    [("ingestion", "r:fixture", 1), ("search", "needle", 2), ("browse", "r:fixture f:src", 100)],
)
def test_all_eval_paths_authenticate_and_bound_the_provider_request(
    monkeypatch, operation, query, documents, result_field
):
    monkeypatch.setenv("ZOEKT_API_KEY", KEY)
    config = evaluation.EvalConfig(eval_mode="direct", zoekt_url=ORIGIN)
    files = [
        {
            "Repository": "fixture",
            "FileName": "src/b.py",
            "LineMatches": [
                {"Line": "needle one", "LineNumber": 2},
                {"Line": "needle two", "LineNumber": 3},
            ],
        },
        {
            "Repository": "fixture",
            "FileName": "src/a.py",
            "LineMatches": [{"Line": "needle three", "LineNumber": 1}],
        },
    ]
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(
            200, json={"Result": {result_field: files}}
        )
        result = call_path(config, operation)
        assert route.call_count == 1
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert json.loads(request.content) == {
            "q": query,
            "opts": {
                "MaxWallTime": 10_000_000_000,
                "MaxDocDisplayCount": documents,
                "ShardMaxMatchCount": 100_000,
                "TotalMaxMatchCount": 1_000_000,
                "Whole": False,
            },
        }
    if operation == "ingestion":
        assert result == {"fixture": True}
    elif operation == "search":
        assert result == {
            "results": [
                {
                    "file": "fixture/src/b.py", "path": "src/b.py", "repo": "fixture",
                    "content": "needle one", "line": 2,
                },
                {
                    "file": "fixture/src/b.py", "path": "src/b.py", "repo": "fixture",
                    "content": "needle two", "line": 3,
                },
            ]
        }
    else:
        assert result == {"entries": [{"name": "a.py"}, {"name": "b.py"}]}
    assert KEY not in json.dumps(result) and KEY not in repr(config)


@pytest.mark.parametrize("operation", ["ingestion", "search", "browse"])
@pytest.mark.parametrize("key", [None, "short", "PLACEHOLDER_" * 4])
def test_eval_refuses_unconfigured_auth_without_any_request(monkeypatch, operation, key):
    if key is None:
        monkeypatch.delenv("ZOEKT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ZOEKT_API_KEY", key)
    config = evaluation.EvalConfig(eval_mode="direct", zoekt_url=ORIGIN)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ORIGIN + "/api/search").respond(200, json={"Result": {}})
        if operation == "ingestion":
            assert call_path(config, operation) == {"fixture": False}
        else:
            with pytest.raises(RuntimeError, match=UNAVAILABLE):
                call_path(config, operation)
        assert not route.called


def test_eval_deadline_closes_original_response_without_retry(monkeypatch):
    monkeypatch.setenv("ZOEKT_API_KEY", KEY)
    state = {"reads": 0, "closed": False}

    class SlowResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            state["reads"] += 1
            yield b'{"Result":'
            await asyncio.sleep(0.5)
            state["reads"] += 1
            yield b'{"Files":[]}}'

        async def aclose(self):
            state["closed"] = True

    config = evaluation.EvalConfig(eval_mode="direct", zoekt_url=ORIGIN, timeout=0.05)
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(200, stream=SlowResponse())
        with pytest.raises(RuntimeError, match=UNAVAILABLE):
            call_path(config, "search")
        assert route.call_count == 1
        assert json.loads(route.calls[0].request.content)["opts"]["MaxWallTime"] == 50_000_000
    assert state == {"reads": 1, "closed": True}


def test_provider_refusal_is_counted_as_an_error_without_leaking_response(monkeypatch, caplog):
    monkeypatch.setenv("ZOEKT_API_KEY", KEY)
    question = evaluation.GoldenQuestion(
        "offline-question", "fixture", "search_exact", "needle",
        {"files": ["src/b.py"]}, "expected file appears",
    )
    monkeypatch.setattr(evaluation, "load_corpus", lambda: [])
    monkeypatch.setattr(evaluation, "load_golden", lambda: [question])
    config = evaluation.EvalConfig(eval_mode="direct", zoekt_url=ORIGIN)
    with respx.mock as router:
        route = router.post(ORIGIN + "/api/search").respond(503, text="private-provider-detail")
        report = evaluation.run_evaluation(config)
        assert route.call_count == 1
    assert report.total == report.errors == 1
    assert report.passed == report.failed == 0
    assert report.by_verb["search_exact"]["errors"] == 1
    assert report.results[0].error == UNAVAILABLE
    assert KEY not in repr(report) + caplog.text
    assert "private-provider-detail" not in repr(report) + caplog.text
