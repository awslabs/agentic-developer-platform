"""Exercise real authorization-bound operation storage with fake external tools."""

import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws
from adp_tools.storage import OperationRepository, task_ops_partition
from cyber_tools.operations import CyberOperations
from cyber_tools.common_crawl_scan import CommonCrawlTools
from cyber_tools.url_contract import validate_url_payload
from common_crawl import CrawlConfig
from test_service_boundary import authorization, make_table


class Evidence:
    def __init__(self):
        self.contents = []

    def put_run_artifact(self, **kwargs):
        assert hashlib.sha256(kwargs["content"]).hexdigest() == kwargs["digest"]
        self.contents.append(kwargs["content"])
        return SimpleNamespace(
            artifact_id="art_" + str(uuid.uuid4()),
            content_type=kwargs["content_type"],
            content_sha256=kwargs["digest"],
        )


class Athena:
    def __init__(self):
        self.state, self.starts, self.stops = "RUNNING", [], []
        self.host = "example.com"

    def get_work_group(self, **kw):
        return {
            "WorkGroup": {
                "State": "ENABLED",
                "Configuration": {
                    "EnforceWorkGroupConfiguration": True,
                    "BytesScannedCutoffPerQuery": 1024**3,
                    "ResultConfiguration": {"OutputLocation": "s3://results/"},
                },
            }
        }

    def start_query_execution(self, **kw):
        self.starts.append(kw)
        return {"QueryExecutionId": "query-1"}

    def get_query_execution(self, **kw):
        return {
            "QueryExecution": {
                "Status": {"State": self.state},
                "Statistics": {"DataScannedInBytes": 123},
            }
        }

    def stop_query_execution(self, **kw):
        self.stops.append(kw)
        self.state = "CANCELLED"

    def get_query_results(self, **kw):
        names = ["url_host_name", "url", "fetch_time", "crawl", "warc_filename"]
        values = [
            self.host,
            "https://" + self.host,
            "2026-01-01",
            "CC-MAIN-2026-01",
            "private-coordinates",
        ]
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": n} for n in names]},
                "Rows": [
                    {"Data": [{"VarCharValue": n} for n in row]}
                    for row in [names, values]
                ],
            }
        }


def setup_service(tools):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    make_table(ddb)
    verified = authorization()
    repo = OperationRepository(ddb, "cyber-operations", lambda: verified)
    backend = SimpleNamespace(
        url_tools=tools,
        _client=lambda name: ddb if name == "dynamodb" else tools.client,
    )
    evidence = Evidence()
    service = CyberOperations(
        repo, evidence, backend, revalidate=lambda: verified.identity
    )

    def call(operation, payload, operation_id=None):
        return service.execute(
            verified.identity, operation_id or str(uuid.uuid4()), operation, payload
        )

    return verified, service, call, evidence


@mock_aws
def test_scan_async_dedup_poll_and_owned_captures():
    athena = Athena()
    tools = CommonCrawlTools(
        athena, CrawlConfig("archive", "ccindex", "bounded", ("CC-MAIN-2026-01",))
    )
    verified, service, call, evidence = setup_service(tools)
    first = call("common_crawl_scan", {"url": "https://example.com"})
    scan_id = first["result"]["scan_id"]
    assert first["result"]["status"] == "pending"
    assert (
        call("common_crawl_scan", {"url": "https://example.com"})["result"]
        == first["result"]
    )
    assert len(athena.starts) == 1
    assert len(athena.starts[0]["ClientRequestToken"]) == 64
    assert (
        call("common_crawl_result", {"scan_id": scan_id})["result"]["status"]
        == "pending"
    )
    athena.state = "SUCCEEDED"
    result = call("common_crawl_result", {"scan_id": scan_id})["result"]
    assert result["found"] and result["bytes_scanned"] == 123
    assert result["captures"][0]["capture_id"] == "capture-001"
    assert "warc_filename" not in result["captures"][0]
    stored = service.repo._get(
        task_ops_partition(verified.identity.task_id), "CC_SCAN#" + scan_id
    )
    assert stored["captures"][0]["warc_filename"] == "private-coordinates"
    with pytest.raises(HTTPException):
        call("common_crawl_result", {"scan_id": "f" * 64})
    with pytest.raises(HTTPException):
        call("common_crawl_scan", {"url": "https://other.example"})


@mock_aws
def test_scan_timeout_and_cancellation_fence():
    athena = Athena()
    clock = [time.time()]
    tools = CommonCrawlTools(
        athena,
        CrawlConfig("archive", "ccindex", "bounded", ("CC-MAIN-2026-01",)),
        clock=lambda: clock[0],
    )
    verified, service, call, _ = setup_service(tools)
    scan_id = call("common_crawl_scan", {"url": "https://example.com"})["result"][
        "scan_id"
    ]
    clock[0] += 1000
    assert (
        call("common_crawl_result", {"scan_id": scan_id})["result"]["reason"]
        == "query_deadline_exceeded"
    )
    assert len(athena.stops) == 1
    call("cancel_jobs", {})
    with pytest.raises(HTTPException):
        call("common_crawl_scan", {"url": "https://example.com"})


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("common_crawl_scan", {"url": "https://example.com", "query": "SELECT secret"}),
        (
            "common_crawl_read",
            {
                "scan_id": "a" * 64,
                "capture_id": "capture-001",
                "warc_filename": "arbitrary",
            },
        ),
        (
            "browser_step",
            {
                "session_id": "a" * 64,
                "view_id": "v",
                "action": "execute",
                "url": "https://other.example",
            },
        ),
        (
            "browser_step",
            {"session_id": "a" * 64, "view_id": "v", "action": "wait", "seconds": True},
        ),
    ],
)
def test_reject_untrusted_tool_options(operation, payload):
    with pytest.raises(HTTPException):
        validate_url_payload(operation, payload)


@pytest.mark.parametrize("content_type", ["text/html", "application/xml"])
@mock_aws
def test_selected_archive_read_preserves_original_and_extracts_inert_text(content_type):
    import gzip
    import io
    from adp_tools.storage import serialize

    html = b'<html><title>Archived</title><body>Historical page<script>throw "never execute"</script></body></html>'
    response = (
        f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n\r\n".encode() + html
    )
    raw = gzip.compress(
        (
            f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: https://example.com\r\nWARC-Date: 2026-01-01T00:00:00Z\r\nContent-Length: {len(response)}\r\n\r\n"
        ).encode()
        + response
        + b"\r\n\r\n"
    )

    class S3:
        def get_object(self, **kw):
            assert kw == {
                "Bucket": "commoncrawl",
                "Key": "crawl-data/CC-MAIN-2026-01/segments/123/warc/example.warc.gz",
                "Range": f"bytes=0-{len(raw) - 1}",
            }
            return {
                "ContentLength": len(raw),
                "ContentRange": f"bytes 0-{len(raw) - 1}/{len(raw)}",
                "Body": io.BytesIO(raw),
            }

    athena = Athena()
    tools = CommonCrawlTools(
        athena,
        CrawlConfig("archive", "ccindex", "bounded", ("CC-MAIN-2026-01",)),
        s3=S3(),
    )
    verified, service, call, evidence = setup_service(tools)
    scan_id = call("common_crawl_scan", {"url": "https://example.com"})["result"][
        "scan_id"
    ]
    athena.state = "SUCCEEDED"
    call("common_crawl_result", {"scan_id": scan_id})
    key = {
        "event_id": task_ops_partition(verified.identity.task_id),
        "arrived_at": "CC_SCAN#" + scan_id,
    }
    row = service.repo._get(key["event_id"], key["arrived_at"])
    row["captures"][0].update(
        warc_filename="crawl-data/CC-MAIN-2026-01/segments/123/warc/example.warc.gz",
        warc_record_offset="0",
        warc_record_length=str(len(raw)),
    )
    service.repo._client.put_item(
        TableName=service.repo.table_name, Item=serialize(row)
    )
    result = call(
        "common_crawl_read", {"scan_id": scan_id, "capture_id": "capture-001"}
    )["result"]
    if content_type == "text/html":
        assert "Historical page" in result["text_preview"]
    else:
        assert result["status"] == "partial"
        assert result["reason"] == "archive_content_not_extractable"
        assert result["metadata"]["content_type"] == "application/xml"
        assert "text_preview" not in result
    # The partial result is durable and can be replayed without another range read.
    assert (
        call("common_crawl_read", {"scan_id": scan_id, "capture_id": "capture-001"})[
            "result"
        ]
        == result
    )
    assert result["original"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["original"]["media_type"] == "application/warc+gzip"


def test_blob_chunks_reassemble_with_matching_digest():
    from adp_tools.evidence import put_blob, CHUNK_BYTES
    import base64

    evidence = Evidence()
    content = b"bounded binary\x00\xff" * (CHUNK_BYTES // 8)
    result = put_blob(
        evidence, authorization().identity, content, "application/octet-stream"
    )
    chunks = [json.loads(v) for v in evidence.contents[:-1]]
    restored = b"".join(base64.b64decode(c["data"]) for c in chunks)
    assert restored == content
    assert result["sha256"] == hashlib.sha256(restored).hexdigest()
    assert all(len(value) < 1048576 for value in evidence.contents)


@mock_aws
def test_archive_read_limit_returns_durable_partial_result():
    from adp_tools.storage import serialize

    athena = Athena()
    tools = CommonCrawlTools(
        athena, CrawlConfig("archive", "ccindex", "bounded", ("CC-MAIN-2026-01",))
    )
    verified, service, call, evidence = setup_service(tools)
    scan_id = call("common_crawl_scan", {"url": "https://example.com"})["result"][
        "scan_id"
    ]
    athena.state = "SUCCEEDED"
    call("common_crawl_result", {"scan_id": scan_id})
    key = {
        "event_id": task_ops_partition(verified.identity.task_id),
        "arrived_at": "CC_SCAN#" + scan_id,
    }
    service.repo._client.update_item(
        TableName=service.repo.table_name,
        Key=serialize(key),
        UpdateExpression="SET read_count = :max",
        ExpressionAttributeValues=serialize({":max": 8}),
    )
    payload = {"scan_id": scan_id, "capture_id": "capture-001"}
    result = call("common_crawl_read", payload)
    assert result["operation_status"] == "confirmed"
    assert result["result"]["reason"] == "archive_read_limit_reached"
    assert call("common_crawl_read", payload)["result"] == result["result"]
    assert service.repo._get(key["event_id"], key["arrived_at"])["read_count"] == 8
