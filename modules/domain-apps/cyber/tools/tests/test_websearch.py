"""Search route, authority, paid-operation fence and citation integration (mocked AWS provider)."""

import hashlib
import json
import uuid
from types import SimpleNamespace

import boto3
import pytest
from cyber_tools import handler, websearch
from moto import mock_aws
from test_service_boundary import (
    CALLER,
    ENDPOINT,
    PROOFS,
    ROLE,
    attempt,
    authorization,
    make_table,
)

GATEWAY = "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
CONFIG = {
    "ADP_WEBSEARCH_GATEWAY_URL": GATEWAY,
    "ADP_WEBSEARCH_REGION": "us-east-1",
    "ADP_WEBSEARCH_TARGET": "search-target",
    "ADP_WEBSEARCH_CONNECTOR_VERSION": "1.2.0",
}


def provider(calls, *, results=None, fail=False):
    def invoke(endpoint, region, method, params):
        calls.append((method, params))
        assert endpoint == GATEWAY and region == "us-east-1"
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "search-target___WebSearch",
                        "inputSchema": {
                            "properties": {
                                "query": {"type": "string"},
                                "maxResults": {"type": "integer"},
                                "filters": {
                                    "type": "object",
                                    "properties": {
                                        "domainFilter": {},
                                        "publishedDateFilter": {},
                                    },
                                },
                            }
                        },
                    }
                ]
            }
        if fail:
            if fail == "malformed":
                raise ValueError("malformed provider response")
            raise TimeoutError("response lost after paid invocation")
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "id": "provider-id",
                            "results": results
                            if results is not None
                            else [
                                {
                                    "url": "https://source.example/report",
                                    "title": "Report",
                                    "publishedDate": "2026-09-01",
                                    "text": "External evidence",
                                }
                            ],
                        }
                    ),
                }
            ],
        }

    return invoke


def test_provider_uses_target_name_filters_and_bounded_sources():
    calls = []
    payload = {
        "query": "example.org indicator",
        "maxResults": 1,
        "filters": {
            "domainFilter": {"include": ["source.example"]},
            "publishedDateFilter": {"from": "2026-01-01T00:00:00Z"},
        },
    }
    result = websearch.search(payload, call=provider(calls), environ=CONFIG)
    assert calls[1] == (
        "tools/call",
        {"params": {"name": "search-target___WebSearch", "arguments": payload}},
    )
    assert result["results"][0]["url"] == "https://source.example/report"
    assert result["query_count"] == 1 and result["estimated_search_usd"] == 0.007
    assert (
        websearch.search(
            {"query": "none"}, call=provider([], results=[]), environ=CONFIG
        )["status"]
        == "empty"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "x" * 201},
        {"query": "   "},
        {"query": "a", "maxResults": 0},
        {"query": "a", "maxResults": True},
        {"query": "a", "tenant": "forged"},
        {"query": "a", "filters": {"domainFilter": {"include": ["invalid..example"]}}},
        {"query": "a", "filters": {"publishedDateFilter": {"from": "yesterday"}}},
    ],
)
def test_invalid_inputs_refused_without_provider(payload):
    with pytest.raises(ValueError):
        websearch.search(
            payload, call=lambda *args: pytest.fail("provider invoked"), environ=CONFIG
        )


@pytest.mark.parametrize("failure", ["timeout", "malformed"])
@mock_aws
def test_handler_durable_authority_artifact_and_unknown_no_replay(monkeypatch, failure):
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    make_table(dynamodb)
    verified = authorization()
    grants = []
    calls = []
    artifacts = []

    class Authority:
        def __init__(self, endpoint, headers, **kwargs):
            assert endpoint == ENDPOINT and headers == PROOFS

        def authorize(self, **kwargs):
            grants.append(kwargs)
            if kwargs["attempt"] != attempt(verified):
                from fastapi import HTTPException

                raise HTTPException(403, "Attempt mismatch")
            if denied[0]:
                from fastapi import HTTPException

                raise HTTPException(403, "Grant revoked")
            return verified

        def put_run_artifact(self, **kwargs):
            artifacts.append(json.loads(kwargs["content"]))
            digest = hashlib.sha256(kwargs["content"]).hexdigest()
            return SimpleNamespace(
                artifact_id="art_" + str(uuid.uuid4()),
                content_type="application/json",
                content_sha256=digest,
            )

        def close(self):
            pass

    class Backend:
        def _client(self, name):
            assert name == "dynamodb"
            return dynamodb

    denied = [False]
    monkeypatch.setattr(handler, "TaskAuthorityClient", Authority)
    monkeypatch.setattr(handler, "CyberBackends", Backend)
    original = websearch.search
    monkeypatch.setattr(
        websearch,
        "search",
        lambda payload, **kwargs: original(
            payload, call=provider(calls, fail=lost[0]), environ=CONFIG, **kwargs
        ),
    )
    lost = [False]
    monkeypatch.setenv("CYBER_TOOLS_WORKER_ROLES", ROLE)
    monkeypatch.setenv("CYBER_TOOLS_TABLE", "cyber-operations")
    monkeypatch.setenv("ADP_TASK_AUTHORITY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "true")
    body = {
        "schema_version": "1.0",
        "attempt": attempt(verified),
        "operation_id": str(uuid.uuid4()),
        "operation": "search",
        "payload": {"query": "reported domain"},
    }
    event = {
        "httpMethod": "POST",
        "resource": "/tools/websearch",
        "headers": PROOFS,
        "requestContext": {"identity": {"userArn": CALLER}},
        "body": json.dumps(body),
    }
    response = handler.lambda_handler(event, None)
    receipt = json.loads(response["body"])
    assert response["statusCode"] == 200 and receipt["operation_status"] == "confirmed"
    assert (
        artifacts[0]["result"]["results"][0]["url"] == "https://source.example/report"
    )
    assert grants and all(grant["tool"] == "websearch.search" for grant in grants)
    assert handler.lambda_handler(event, None)["body"] == response["body"]
    assert len([method for method, _ in calls if method == "tools/call"]) == 1
    from cyber_tools.task_report import render_report
    from lib.task_host import TaskHost
    from lib.task_run_client import TaskRunClient

    monkeypatch.setenv(
        "ADP_AGENT_CONTROL_ENDPOINT", "https://api.example/internal/v1/agent"
    )
    monkeypatch.setenv(
        "ADP_TASK_TOOL_ROUTES",
        '{"websearch.search":"https://api.example/dev/tools/websearch"}',
    )
    client = TaskRunClient()

    def signed_route(action, request, **kwargs):
        assert action == "cyber" and kwargs["tool_endpoint"].endswith(
            "/tools/websearch"
        )
        routed = {**event, "body": json.dumps(request)}
        answer = handler.lambda_handler(routed, None)
        assert answer["statusCode"] == 200
        return json.loads(answer["body"])

    monkeypatch.setattr(client, "_post", signed_route)
    host = TaskHost.__new__(TaskHost)
    host.client = client
    host._report_renderer = render_report
    host._report_context = {
        "task_id": verified.identity.task_id,
        "inputs": {},
        "steps": [],
    }
    frame = {
        "type": "tool.request",
        "tool": "websearch.search",
        "request_id": body["operation_id"],
        "payload": body["payload"],
    }
    delivered = host._cyber(
        SimpleNamespace(task_id=verified.identity.task_id), body["attempt"], frame
    )
    assert delivered["operation_status"] == "confirmed" and delivered["artifact"][
        "artifact_id"
    ].startswith("art_")
    html = render_report(
        report={
            "summary": "Verdict: inconclusive",
            "findings": [
                {
                    "statement": "External report",
                    "evidence_refs": [delivered["artifact"]["artifact_id"]],
                }
            ],
            "uncertainties": [],
        },
        context=host._report_context,
    )["content"].decode()
    assert (
        "https://source.example/report" in html
        and "External report" in html
        and "estimated search charge" in html
    )
    before = len([method for method, _ in calls if method == "tools/call"])
    event["resource"] = "/tools/cyber"
    assert handler.lambda_handler(event, None)["statusCode"] == 404
    event["resource"] = "/tools/websearch"
    monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "false")
    assert handler.lambda_handler(event, None)["statusCode"] == 503
    monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "true")
    foreign = {
        **body,
        "attempt": {**body["attempt"], "runtime_attempt_id": str(uuid.uuid4())},
    }
    event["body"] = json.dumps(foreign)
    assert handler.lambda_handler(event, None)["statusCode"] == 403
    event["body"] = json.dumps(body)
    assert len([method for method, _ in calls if method == "tools/call"]) == before
    denied[0] = True
    body["operation_id"] = str(uuid.uuid4())
    event["body"] = json.dumps(body)
    assert handler.lambda_handler(event, None)["statusCode"] == 403
    denied[0] = False
    lost[0] = failure
    body["payload"]["query"] = "second search"
    event["body"] = json.dumps(body)
    assert handler.lambda_handler(event, None)["statusCode"] == 503
    retry = handler.lambda_handler(event, None)
    assert json.loads(retry["body"])["operation_status"] == "unknown"
    assert json.loads(retry["body"])["result"]["potential_query_count"] == 1
    assert len([method for method, _ in calls if method == "tools/call"]) == 2
    event["requestContext"]["identity"]["userArn"] = CALLER.replace(
        "approved-worker", "forged-worker"
    )
    assert handler.lambda_handler(event, None)["statusCode"] == 403
    assert len([method for method, _ in calls if method == "tools/call"]) == 2


def test_gateway_transport_signs_exact_region_and_refuses_untrusted_endpoint():
    from botocore.credentials import Credentials
    from fastapi import HTTPException

    class Response:
        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            pass

        content = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'

        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self.content)

    class Session:
        trust_env = True

        def post(self, endpoint, *, data, headers, timeout, allow_redirects, stream):
            assert stream is True
            assert endpoint == GATEWAY and timeout == (2, 12) and not allow_redirects
            assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            assert b'"method":"tools/list"' in data
            return Response()

    session = Session()
    assert websearch.gateway_call(
        GATEWAY,
        "us-east-1",
        "tools/list",
        {},
        session=session,
        credentials=Credentials("test-access", "test-secret"),
    ) == {"tools": []}
    assert session.trust_env is False
    with pytest.raises(HTTPException):
        websearch.gateway_call(
            "https://outside.example/mcp",
            "us-east-1",
            "tools/list",
            {},
            session=session,
        )
    with pytest.raises(HTTPException):
        websearch.gateway_call(GATEWAY, "eu-west-1", "tools/list", {}, session=session)


def test_large_provider_results_remain_within_artifact_bound():
    sources = [
        {
            "text": "e" * 5000,
            "url": "https://source.example/" + str(index),
            "title": "Source " + str(index),
        }
        for index in range(25)
    ]
    result = websearch.search(
        {"query": "indicator", "maxResults": 25},
        call=provider([], results=sources),
        environ=CONFIG,
    )
    assert result["results_truncated"] is True
    assert 0 < len(result["results"]) < 25
    assert len(json.dumps(result).encode()) < 25000


def test_wrong_discovered_schema_refuses_paid_search():
    calls = []

    def no_filters(endpoint, region, method, params):
        calls.append(method)
        return {
            "tools": [
                {
                    "name": "search-target___WebSearch",
                    "inputSchema": {"properties": {"query": {}, "maxResults": {}}},
                }
            ]
        }

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        websearch.search({"query": "indicator"}, call=no_filters, environ=CONFIG)
    assert calls == ["tools/list"]


def test_gateway_accepts_bounded_mcp_event_stream_without_replay():
    from botocore.credentials import Credentials

    class Response:
        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            pass

        content = b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'

        def __init__(self):
            self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def raise_for_status(self):
            pass

    class Session:
        def post(self, *args, **kwargs):
            assert "text/event-stream" in kwargs["headers"]["Accept"]
            return Response()

    result = websearch.gateway_call(
        GATEWAY,
        "us-east-1",
        "tools/list",
        {},
        session=Session(),
        credentials=Credentials("test-access", "test-secret"),
    )
    assert result == {"tools": []}


def test_report_escapes_provider_source_metadata():
    from cyber_tools.task_report import render_report

    artifact_id = "art_" + str(uuid.uuid4())
    context = {
        "inputs": {},
        "steps": [
            {
                "tool": "websearch.search",
                "operation_status": "confirmed",
                "result": {
                    "status": "completed",
                    "results": [
                        {
                            "url": 'https://source.example/"bad',
                            "title": "<script>alert(1)</script>",
                            "text": "<img src=x onerror=alert(1)>",
                            "publishedDate": "2026-09-01",
                        }
                    ],
                    "query_count": 1,
                    "estimated_search_usd": 0.007,
                },
                "artifact": {"artifact_id": artifact_id},
                "finished_at": "2026-09-27T00:00:00Z",
                "started_at": "2026-09-27T00:00:00Z",
            }
        ],
    }
    html = render_report(
        report={
            "summary": "Verdict: inconclusive",
            "findings": [
                {"statement": "External reference", "evidence_refs": [artifact_id]}
            ],
        },
        context=context,
    )["content"].decode()
    assert "<script>alert(1)</script>" not in html and "onerror=alert(1)" in html
    assert "&lt;script&gt;" in html and "&quot;bad" in html
    assert artifact_id in html and "https://source.example/" in html


def test_discovery_follows_cursor_before_invoking_target():
    actual = provider([])
    calls = []

    def paged(endpoint, region, method, params):
        calls.append((method, params))
        if method == "tools/list" and not params:
            return {"tools": [], "nextCursor": "second-page"}
        return actual(endpoint, region, method, params)

    result = websearch.search({"query": "example"}, call=paged, environ=CONFIG)
    assert calls[1] == ("tools/list", {"params": {"cursor": "second-page"}})
    assert result["query_count"] == 1


def test_snippet_truncation_is_disclosed():
    result = websearch.search(
        {"query": "example"},
        call=provider([], results=[{"text": "x" * 1300}]),
        environ=CONFIG,
    )
    assert result["results_truncated"] is True


def test_revocation_after_discovery_prevents_paid_query():
    from fastapi import HTTPException

    calls = []

    def revoked():
        raise HTTPException(403, "Grant revoked")

    with pytest.raises(HTTPException):
        websearch.search(
            {"query": "example"},
            call=provider(calls),
            environ=CONFIG,
            before_search=revoked,
        )
    assert [method for method, _ in calls] == ["tools/list"]


def test_stream_response_limit_stops_reading_and_closes_response():
    from botocore.credentials import Credentials

    reads = []
    closed = []

    class Response:
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            for i in range(100):
                reads.append(i)
                yield b"x" * 8192

        def close(self):
            closed.append(True)

    class Session:
        def post(self, *args, **kwargs):
            assert kwargs["stream"] is True
            return Response()

    with pytest.raises(ValueError, match="Oversized"):
        websearch.gateway_call(
            GATEWAY,
            "us-east-1",
            "tools/list",
            {},
            session=Session(),
            credentials=Credentials("test", "test"),
        )
    assert len(reads) == 33 and closed == [True]
