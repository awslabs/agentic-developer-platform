"""Synthetic WARC fixtures: selected content, provenance, bounded reads and no execution."""

import gzip
import io
import json
from functools import partial

import pytest
import archive_content as archive
import domain_investigation as cli
import live_evaluation as live
from browser_client import BrowserBrokerError
from case_contract import digest, redact_url
from research_case import verify_case

from .test_domain_investigation import live_fixture as browser_fixture
from .test_live_evaluation import ProtocolModel, last_view, row, tool_use

live_fixture = browser_fixture
URL = "https://public.test/products?campaign=fixture"
HTML = b"""<html><title>Fixture security tools</title><body><h1>Endpoint protection</h1>
<form action="https://identity.test/login" method="POST"><input type="password"></form>
<script>fetch('https://collector.test/collect', {method:'POST'});</script>
<a href="/about">About</a><a download href="/sample.exe">Test sample</a></body></html>"""


def fixture(
    payload=HTML,
    *,
    target=URL,
    content_type="text/html; charset=utf-8",
    extra_http="",
    extra_warc="",
):
    http = (
        f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n{extra_http}\r\n".encode()
        + payload
    )
    raw = gzip.compress(
        (
            f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: {target}\r\n"
            f"WARC-Date: 2026-09-17T10:00:00Z\r\nContent-Length: {len(http)}\r\n"
            f"{extra_warc}\r\n"
        ).encode()
        + http
        + b"\r\n\r\n"
    )
    capture = {
        "capture_id": "capture-001",
        "url": redact_url(URL),
        "url_sha256": digest(URL),
        "crawl": "CC-MAIN-2026-39",
        "fetch_time": "2026-09-17 10:00:00",
        "fetch_status": "200",
        "warc_filename": "crawl-data/CC-MAIN-2026-39/segments/123456/warc/example.warc.gz",
        "warc_record_offset": "99",
        "warc_record_length": str(len(raw)),
    }
    return raw, capture


def source(capture):
    return {
        "kind": "archive_index",
        "source": "common_crawl_athena",
        "status": "available",
        "captures": [capture],
    }


def prepared(tmp_path, monkeypatch, capture):
    monkeypatch.setattr(cli, "_lease_path", lambda _: tmp_path / "private.json")
    output = tmp_path / "case"
    cli.prepare(
        output,
        "https://public.test/seed",
        "Investigate archived and live content",
        lookup_fn=lambda *a: source(capture),
    )
    return output


def test_selected_page_is_saved_before_extraction_and_cached(tmp_path, monkeypatch):
    raw, capture = fixture(extra_http="Set-Cookie: first=x\r\nSet-Cookie: second=y\r\n")
    output = prepared(tmp_path, monkeypatch, capture)
    calls = []
    extract = archive.extract_content

    def checked_extract(payload, metadata):
        assert (output / "archive-001.warc.gz").read_bytes() == raw
        assert (output / "archive-001-payload.bin").read_bytes() == HTML
        assert verify_case(output) > 0
        return extract(payload, metadata)

    monkeypatch.setattr(archive, "extract_content", checked_extract)

    def fetch(selected):
        calls.append(selected)
        return raw

    case = cli.archive(
        output,
        "corroboration-001",
        "capture-001",
        "Inspect the product claim",
        fetch_fn=fetch,
    )
    page = case["corroboration"][-1]
    assert (
        page["status"] == "available" and page["archived_at"] == "2026-09-17T10:00:00Z"
    )
    content = json.loads((output / page["content_file"]).read_text())
    assert "Endpoint protection" in content["text"]
    assert content["forms"][0]["method"] == "POST"
    assert content["scripts"][0]["inline"].startswith("fetch(")
    assert content["links"][1]["download_offer"]
    assert page["archive_sha256"] == digest(raw) and page["payload_sha256"] == digest(
        HTML
    )
    cli.archive(output, "corroboration-001", "capture-001", "Reuse", fetch_fn=fetch)
    assert len(calls) == 1
    assert not case["sessions"]  # Static extraction did not start a browser.
    assert verify_case(output) > 0


def test_only_recorded_candidates_can_be_fetched(tmp_path, monkeypatch):
    raw, capture = fixture()
    output = prepared(tmp_path, monkeypatch, capture)
    for sid, cid in [("invented", "capture-001"), ("corroboration-001", "capture-999")]:
        with pytest.raises(ValueError):
            cli.archive(
                output,
                sid,
                cid,
                "Investigate",
                fetch_fn=lambda _: pytest.fail("No fetch"),
            )
    capture["warc_filename"] = "s3://other-bucket/secret"
    with pytest.raises(ValueError, match="WARC location"):
        archive.fetch_record(capture, client=object())


@pytest.mark.parametrize(
    "target", ["https://other.test/", "https://public.test/products?campaign=different"]
)
def test_mismatched_warc_target_never_becomes_page_evidence(target):
    raw, capture = fixture(target=target)
    with pytest.raises(ValueError, match="target does not match"):
        archive.parse_record(raw, capture)


def test_exact_s3_range_and_stream_closure():
    raw, capture = fixture()
    calls = []
    body = io.BytesIO(raw)

    class S3:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "Body": body,
                "ContentLength": len(raw),
                "ContentRange": f"bytes 99-{98 + len(raw)}/99999",
            }

    assert archive.fetch_record(capture, client=S3()) == raw
    assert calls == [
        {
            "Bucket": "commoncrawl",
            "Key": capture["warc_filename"],
            "Range": f"bytes=99-{98 + len(raw)}",
        }
    ]
    assert body.closed


def test_ignored_range_and_compression_bomb_are_bounded(monkeypatch):
    raw, capture = fixture()
    body = io.BytesIO(raw)

    class S3:
        def get_object(self, **kwargs):
            return {"Body": body, "ContentLength": len(raw)}

    with pytest.raises(ValueError, match="requested byte range"):
        archive.fetch_record(capture, client=S3())
    assert body.closed
    monkeypatch.setattr(archive, "MAX_RECORD_BYTES", 100)
    with pytest.raises(ValueError, match="decompression exceeds"):
        archive.inflate(gzip.compress(b"x" * 1000))


def test_actual_archive_reads_are_refused_outside_aws(monkeypatch):
    for key in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CODEBUILD_BUILD_ID",
        "AWS_EXECUTION_ENV",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="local dataset downloads are disabled"):
        archive.fetch_record(fixture()[1])


@pytest.mark.parametrize("encoding", ["gzip", "chunked"])
def test_archived_http_encoding_is_unwrapped_without_execution(encoding):
    if encoding == "gzip":
        raw, capture = fixture(
            gzip.compress(HTML), extra_http="Content-Encoding: gzip\r\n"
        )
    else:
        payload = f"{len(HTML):x}\r\n".encode() + HTML + b"\r\n0\r\n\r\n"
        raw, capture = fixture(payload, extra_http="Transfer-Encoding: chunked\r\n")
    payload, metadata = archive.parse_record(raw, capture)
    content = archive.extract_content(payload, metadata)
    assert "Endpoint protection" in content["text"] and content["scripts"]


def test_unextractable_content_preserves_archive_and_failure(tmp_path, monkeypatch):
    raw, capture = fixture(b"synthetic binary", content_type="application/octet-stream")
    output = prepared(tmp_path, monkeypatch, capture)
    case = cli.archive(
        output,
        "corroboration-001",
        "capture-001",
        "Inspect the selected record",
        fetch_fn=lambda _: raw,
    )
    page = case["corroboration"][-1]
    assert page["status"] == "unavailable" and "supported text" in page["diagnostic"]
    assert (output / page["payload_file"]).read_bytes() == b"synthetic binary"
    assert verify_case(output) > 0


def test_archived_content_supports_assessment_after_live_startup_failure(
    tmp_path, monkeypatch
):
    raw, capture = fixture()
    output = prepared(tmp_path, monkeypatch, capture)
    cli.archive(
        output,
        "corroboration-001",
        "capture-001",
        "Read archived product page",
        fetch_fn=lambda _: raw,
    )
    cli.hypothesize(
        output,
        {
            "hypothesis": "Archived text describes endpoint protection products",
            "source_ids": ["corroboration-002"],
            "limitations": ["Historical page"],
            "next_question": "Does the current page agree?",
        },
    )

    def unavailable(*a):
        raise BrowserBrokerError("Synthetic browser outage")

    with pytest.raises(BrowserBrokerError):
        cli.browse(output, request=unavailable)
    result = cli.finish(
        output,
        {
            "verdict": "no_specific_concern",
            "assessor": "synthetic-model",
            "context_assessment": {
                "risk": "no_specific_concern",
                "findings": [
                    {
                        "basis": "reported",
                        "statement": "The archived page describes endpoint protection.",
                        "source_ids": ["corroboration-002"],
                    }
                ],
                "limitations": [
                    "Live browser startup failed; historical assessment only."
                ],
            },
        },
        "Assess the archived content",
        request=unavailable,
    )
    assert result["assessment"]["verdict"] == "no_specific_concern"
    assert "corroboration-002" in (output / "report.md").read_text()


def test_evaluation_model_can_select_and_inspect_archive_content(
    live_fixture, tmp_path
):
    request, _, _ = live_fixture
    raw, capture = fixture()

    def enrich(output, source_name, reason):
        return cli.enrich(
            output, source_name, reason, lookup_fn=lambda *a, **k: source(capture)
        )

    def choose(turn, kwargs):
        view = last_view(kwargs)
        if turn == 1:
            return [
                tool_use(
                    "enrich",
                    {"source": "common_crawl", "reason": "Check archive coverage"},
                )
            ]
        if turn == 2:
            assert view["archive_candidates"][0]["capture_id"] == "capture-001"
            return [
                tool_use(
                    "archive",
                    {
                        "source_id": "corroboration-001",
                        "capture_id": "capture-001",
                        "reason": "Inspect product content",
                    },
                )
            ]
        if turn == 3:
            return [tool_use("inspect_archive", {"source_id": "corroboration-002"})]
        assert "Endpoint protection" in view["content"]["text"]
        return [
            tool_use(
                "finish",
                {
                    "reason": "Examined archived and live pages",
                    "assessment": {
                        "verdict": "no_specific_concern",
                        "assessor": "synthetic-model",
                    },
                },
            )
        ]

    result = live.investigate(
        tmp_path / "case",
        row(),
        ProtocolModel(choose),
        request=request,
        enrich_fn=enrich,
        archive_fn=partial(cli.archive, fetch_fn=lambda _: raw),
    )
    assert result["model_completed"] and result["verdict"] == "no_specific_concern"
