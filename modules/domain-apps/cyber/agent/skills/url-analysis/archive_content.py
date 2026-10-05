"""Selected Common Crawl S3 range reads and inert content extraction.

Adapted to cyber evidence from CIP's discover -> range-read -> snapshot -> extract
flow (see docs/common-crawl-investigation.md). No requests reach the target site.
"""

from __future__ import annotations

import re
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin

from case_contract import digest, redact_url, sanitize, utcnow

MAX_RANGE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 40000
MAX_SCRIPT_CHARS = 32768
WARC_KEY = re.compile(
    r"crawl-data/(CC-MAIN-20\d{2}-\d{2})/segments/[\w.-]+/warc/[\w.-]+\.warc\.gz\Z"
)


def coordinates(capture):
    key = capture.get("warc_filename", "")
    match = WARC_KEY.fullmatch(key)
    if not match or match[1] != capture.get("crawl"):
        raise ValueError("Capture has no valid Common Crawl WARC location")
    values = [capture.get(k) for k in ("warc_record_offset", "warc_record_length")]
    if any(not re.fullmatch(r"[0-9]+", str(v)) for v in values):
        raise ValueError("Capture has invalid WARC coordinates")
    offset, length = map(int, values)
    if not 0 < length <= MAX_RANGE_BYTES:
        raise ValueError("Selected archive record exceeds the range-read budget")
    return key, offset, length


def fetch_record(capture, *, client=None):
    """Read one recorded range. Actual target data stays in an AWS runtime."""
    key, offset, length = coordinates(capture)
    if client is None:
        from benchmark import require_aws_runtime

        require_aws_runtime()
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            region_name="us-east-1",
            config=Config(
                connect_timeout=3, read_timeout=15, retries={"max_attempts": 1}
            ),
        )
    response = client.get_object(
        Bucket="commoncrawl", Key=key, Range=f"bytes={offset}-{offset + length - 1}"
    )
    body = response["Body"]
    try:
        # Bound the stream even if the server unexpectedly ignores Range.
        if response.get("ContentLength") != length or not re.fullmatch(
            rf"bytes {offset}-{offset + length - 1}/[0-9]+",
            response.get("ContentRange", ""),
        ):
            raise ValueError("Archive server did not return the requested byte range")
        raw = body.read(length + 1)
        if len(raw) != length:
            raise ValueError("Archive range length differs from the index")
        return raw
    finally:
        body.close()


def inflate(raw, wbits=31):
    decoder = zlib.decompressobj(wbits)
    expanded = decoder.decompress(raw, MAX_RECORD_BYTES + 1)
    if len(expanded) > MAX_RECORD_BYTES or decoder.unconsumed_tail:
        raise ValueError("Archive decompression exceeds the content budget")
    if not decoder.eof or decoder.unused_data.strip(b"\r\n\x00"):
        raise ValueError(
            "Archive compressed stream is incomplete or contains extra members"
        )
    return expanded


def headers(block, encoding="latin-1"):
    first, *lines = block.split(b"\r\n")
    values = {}
    for line in lines:
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        key = name.decode("latin-1").strip().lower()
        if key in values and key in {
            "content-length",
            "content-encoding",
            "transfer-encoding",
            "warc-target-uri",
            "warc-type",
        }:
            raise ValueError("Archive contains ambiguous duplicate headers")
        values[key] = value.decode(encoding).strip()
    return first, values


def parse_record(raw, capture):
    if not raw or len(raw) > MAX_RANGE_BYTES:
        raise ValueError("Archive range is empty or oversized")
    record = inflate(raw)
    envelope, separator, remainder = record.partition(b"\r\n\r\n")
    version, warc = headers(envelope, encoding="utf-8")
    if (
        not separator
        or not version.startswith(b"WARC/")
        or warc.get("warc-type") != "response"
    ):
        raise ValueError("Selected WARC record is not an archived HTTP response")
    target = warc.get("warc-target-uri", "")
    if (
        not target
        or redact_url(target) != capture["url"]
        or (capture.get("url_sha256") and digest(target) != capture["url_sha256"])
    ):
        raise ValueError("WARC target does not match the selected index capture")
    length = int(warc.get("content-length", "-1"))
    if length < 0 or len(remainder) < length or remainder[length:].strip(b"\r\n"):
        raise ValueError("WARC content length is inconsistent")
    http_block, separator, payload = remainder[:length].partition(b"\r\n\r\n")
    status, http = headers(http_block)
    match = re.fullmatch(rb"HTTP/\d(?:\.\d)? ([0-9]{3})(?: .*)?", status)
    if not separator or not match:
        raise ValueError("WARC has no parseable HTTP response")
    return payload, {
        "url": redact_url(target),
        "archived_at": warc.get("warc-date"),
        "http_status": int(match[1]),
        "content_type": http.get("content-type", capture.get("content_mime_type", "")),
        "content_encoding": http.get("content-encoding", "").lower(),
        "transfer_encoding": http.get("transfer-encoding", "").lower(),
        "location": redact_url(urljoin(target, http["location"]))
        if http.get("location")
        else None,
        "archive_truncated": warc.get("warc-truncated"),
    }


class PageEvidence(HTMLParser):
    """Read markup without executing scripts, loading resources or rendering it."""

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.text = []
        self.text_chars = 0
        self.title = []
        self.in_title = False
        self.in_style = False
        self.script = None
        self.form = None
        self.scripts = []
        self.forms = []
        self.links = []
        self.truncated = set()

    def destination(self, value):
        return redact_url(urljoin(self.url, value))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "style":
            self.in_style = True
        if tag == "script":
            self.script = {
                "src": self.destination(attrs["src"]) if attrs.get("src") else None,
                "inline": "",
                "truncated": False,
            }
            if len(self.scripts) < 30:
                self.scripts.append(self.script)
            else:
                self.truncated.add("scripts")
        elif tag == "form":
            self.form = {
                "action": self.destination(attrs.get("action") or self.url),
                "method": attrs.get("method", "GET").upper(),
                "fields": [],
            }
            if len(self.forms) < 30:
                self.forms.append(self.form)
            else:
                self.truncated.add("forms")
        elif tag == "input" and self.form is not None:
            if len(self.form["fields"]) < 50:
                self.form["fields"].append(
                    {"type": (attrs.get("type") or "text")[:100]}
                )
            else:
                self.truncated.add("form_fields")
        elif tag == "a" and attrs.get("href"):
            if len(self.links) < 100:
                self.links.append(
                    {
                        "url": self.destination(attrs["href"]),
                        "download_offer": "download" in attrs,
                    }
                )
            else:
                self.truncated.add("links")

    def handle_endtag(self, tag):
        if tag == "script":
            self.script = None
        elif tag == "form":
            self.form = None
        elif tag == "title":
            self.in_title = False
        elif tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            return
        if self.script is not None:
            remaining = MAX_SCRIPT_CHARS - len(self.script["inline"])
            self.script["inline"] += data[:remaining]
            if len(data) > remaining:
                self.script["truncated"] = True
                self.truncated.add("script_text")
            return
        if self.in_title and sum(map(len, self.title)) < 1000:
            self.title.append(data[:1000])
        remaining = MAX_TEXT_CHARS - self.text_chars
        if remaining:
            self.text.append(data[:remaining])
        self.text_chars += min(remaining, len(data))
        if len(data) > remaining:
            self.truncated.add("text")


def dechunk(payload):
    parts = []
    position = 0
    while position < len(payload):
        end = payload.find(b"\r\n", position)
        if end < 0:
            break
        size = payload[position:end].split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9a-fA-F]+", size):
            break
        size = int(size, 16)
        if size == 0:
            return b"".join(parts)
        position = end + 2
        if payload[position + size : position + size + 2] != b"\r\n":
            break
        parts.append(payload[position : position + size])
        position += size + 2
    raise ValueError("Archived HTTP chunked payload is incomplete")


def extract_content(payload, metadata):
    if metadata["transfer_encoding"] == "chunked":
        payload = dechunk(payload)
    elif metadata["transfer_encoding"] not in {"", "identity"}:
        raise ValueError(
            "Archived HTTP transfer encoding is not supported for extraction"
        )
    encoding = metadata["content_encoding"]
    if encoding in {"gzip", "x-gzip", "deflate"}:
        payload = inflate(payload, 15 if encoding == "deflate" else 31)
    elif encoding not in {"", "identity"}:
        raise ValueError(
            "Archived HTTP content encoding is not supported for extraction"
        )
    content_type = metadata["content_type"].split(";", 1)[0].strip().lower()
    if content_type not in {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/javascript",
        "application/javascript",
        "application/json",
    }:
        raise ValueError("Archived content is not a supported text document")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", metadata["content_type"], re.I)
    charset = charset[1].lower() if charset else "utf-8"
    if charset not in {
        "utf-8",
        "utf8",
        "iso-8859-1",
        "latin-1",
        "windows-1252",
        "ascii",
    }:
        charset = "utf-8"
    markup = payload.decode(charset, errors="replace")
    parser = PageEvidence(metadata["url"])
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser.feed(markup)
        parser.close()
    else:
        parser.handle_data(markup)
    return sanitize(
        {
            "title": " ".join(parser.title)[:1000],
            "text": "\n".join(parser.text)[:MAX_TEXT_CHARS],
            "forms": parser.forms,
            "scripts": parser.scripts,
            "links": parser.links,
            "extraction_truncated": sorted(parser.truncated),
            "extracted_at": utcnow(),
            "limitations": [
                "Historical archived content; scripts were not executed and resources were not fetched.",
                "Extracted HTML text is not a rendered browser view and may include hidden content.",
            ],
        }
    )
