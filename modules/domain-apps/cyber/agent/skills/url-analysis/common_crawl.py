"""Bounded Athena lookup of Common Crawl index metadata, never target content.

The index describes historical fetches. The model weighs these leads alongside
browser evidence to decide the verdict. Raw query results stay in the configured
S3 workgroup.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from case_contract import digest, redact_url, sanitize, utcnow

IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}\Z")
CRAWL = re.compile(r"CC-MAIN-20\d{2}-\d{2}\Z")
MAX_ROWS = 30
MAX_QUERY_SECONDS = 45
MAX_SCAN_BYTES = 1024**3


@dataclass(frozen=True)
class CrawlConfig:
    database: str
    table: str
    workgroup: str
    crawls: tuple[str, ...]
    region: str = "us-east-1"
    catalog: str = "AwsDataCatalog"

    def validate(self):
        if not all(
            IDENTIFIER.fullmatch(v) for v in (self.database, self.table, self.catalog)
        ):
            raise ValueError("Invalid Common Crawl catalog identifier")
        if not re.fullmatch(r"[\w.-]{1,128}", self.workgroup):
            raise ValueError("Invalid Athena workgroup")
        if not 1 <= len(self.crawls) <= 3 or not all(
            CRAWL.fullmatch(c) for c in self.crawls
        ):
            raise ValueError("Configure one to three explicit Common Crawl partitions")
        return self

    @classmethod
    def from_env(cls):
        database = os.environ.get("CYBER_CC_DATABASE", "")
        workgroup = os.environ.get("CYBER_CC_WORKGROUP", "")
        crawls = tuple(filter(None, os.environ.get("CYBER_CC_CRAWLS", "").split(",")))
        if not database or not workgroup or not crawls:
            return None
        return cls(
            database=database,
            table=os.environ.get("CYBER_CC_TABLE", "ccindex"),
            workgroup=workgroup,
            crawls=crawls,
            region=os.environ.get("CYBER_CC_REGION", "us-east-1"),
        ).validate()


def domain_of(url):
    host = (urlsplit(url).hostname or "").rstrip(".").encode("idna").decode().lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Common Crawl discovery requires a domain, not an IP literal")
    if (
        len(host) > 253
        or "." not in host
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        )
    ):
        raise ValueError("Invalid domain")
    return host


def query_for(config, domain):
    config.validate()
    if domain_of("https://" + domain + "/") != domain:
        raise ValueError("Query domain must be canonical")
    labels = domain.split(".")
    parents = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    # Only validated identifiers are interpolated; values use Athena parameters.
    sql = f"""SELECT crawl, url_host_name, url, fetch_time, fetch_status,
       content_mime_type, content_languages, content_digest,
       warc_filename, warc_record_offset, warc_record_length
FROM "{config.database}"."{config.table}"
WHERE subset = 'warc' AND crawl IN ({", ".join("?" for _ in config.crawls)})
  AND url_host_tld = ?
  AND url_host_registered_domain IN ({", ".join("?" for _ in parents)})
  AND (url_host_name = ? OR url_host_name LIKE ?)
ORDER BY fetch_time DESC, url ASC
LIMIT {MAX_ROWS}"""
    # These sorted index columns enable Parquet pruning. Candidate ancestor
    # registrations avoid guessing an eTLD+1; the final host filter stays exact.
    values = [*config.crawls, labels[-1], *parents, domain, "%." + domain]
    # Domain validation excludes quotes and LIKE wildcards.
    return sql, ["'" + v + "'" for v in values]


def lookup_common_crawl(
    url, *, config=None, client=None, clock=time.monotonic, sleep=time.sleep
):
    record = {
        "kind": "archive_index",
        "source": "common_crawl_athena",
        "checked_at": utcnow(),
        "status": "unavailable",
        "verdict_effect": "model_assessed",
        "limitations": [
            "Historical index metadata, not page content, reputation, or current behavior.",
            "Coverage is limited to the selected crawls and exact hostname plus subdomains.",
            "No match does not establish safety, domain age, or absence from all Common Crawl history.",
            "At most 30 newest matching records are returned; samples do not measure prevalence.",
        ],
    }
    query_id = None
    terminal = False
    try:
        domain = domain_of(url)
        record["subject"] = domain
        config = config or CrawlConfig.from_env()
        if config is None:
            return {
                **record,
                "status": "skipped",
                "reason": "Common Crawl Athena configuration is unavailable",
            }
        config.validate()
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "athena",
                region_name=config.region,
                config=Config(
                    connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}
                ),
            )
        workgroup = client.get_work_group(WorkGroup=config.workgroup)["WorkGroup"]
        settings = workgroup["Configuration"]
        cutoff = settings.get("BytesScannedCutoffPerQuery", 0)
        if (
            workgroup.get("State") != "ENABLED"
            or not settings.get("EnforceWorkGroupConfiguration")
            or not 10_000_000 <= cutoff <= MAX_SCAN_BYTES
            or not settings.get("ResultConfiguration", {})
            .get("OutputLocation", "")
            .startswith("s3://")
        ):
            raise ValueError(
                "Athena workgroup must enforce S3 results and a scan cutoff of at most 1 GiB"
            )
        sql, params = query_for(config, domain)
        response = client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={
                "Database": config.database,
                "Catalog": config.catalog,
            },
            WorkGroup=config.workgroup,
            ExecutionParameters=params,
        )
        query_id = response["QueryExecutionId"]
        record.update(
            query_id=query_id,
            query_cleanup={},
            crawls=list(config.crawls),
            workgroup=config.workgroup,
            database=config.database,
            table=config.table,
            region=config.region,
        )
        deadline = clock() + MAX_QUERY_SECONDS
        while clock() < deadline:
            execution = client.get_query_execution(QueryExecutionId=query_id)[
                "QueryExecution"
            ]
            state = execution["Status"]["State"]
            record["query_state"] = state
            record["bytes_scanned"] = execution.get("Statistics", {}).get(
                "DataScannedInBytes", 0
            )
            if state in {"FAILED", "CANCELLED"}:
                terminal = True
                return {
                    **record,
                    "reason": "Athena query " + state.lower(),
                    "athena_error_category": execution["Status"]
                    .get("AthenaError", {})
                    .get("ErrorCategory"),
                }
            if state == "SUCCEEDED":
                terminal = True
                break
            sleep(min(1, max(0, deadline - clock())))
        if not terminal:
            return {**record, "reason": "Common Crawl query exceeded its time budget"}
        result = client.get_query_results(
            QueryExecutionId=query_id, MaxResults=MAX_ROWS + 1
        )
        result_set = result["ResultSet"]
        columns = [c["Name"] for c in result_set["ResultSetMetadata"]["ColumnInfo"]]
        rows = result_set.get("Rows", [])
        # Athena SELECT results include the column-name header as their first row.
        if (
            rows
            and [c.get("VarCharValue", "") for c in rows[0].get("Data", [])] == columns
        ):
            rows = rows[1:]
        captures = []
        for row in rows[:MAX_ROWS]:
            item = dict(
                zip(columns, [c.get("VarCharValue", "") for c in row.get("Data", [])])
            )
            host = item.get("url_host_name", "").lower()
            if host != domain and not host.endswith("." + domain):
                raise ValueError("Athena returned an out-of-scope hostname")
            if item.get("url"):
                item["url_sha256"] = digest(item["url"])
                item["url"] = redact_url(item["url"])
            item["capture_id"] = f"capture-{len(captures) + 1:03d}"
            captures.append(sanitize(item))
        return {
            **record,
            "status": "available",
            "found": bool(captures),
            "sample_limit_reached": len(captures) == MAX_ROWS,
            "capture_count_returned": len(captures),
            "captures": captures,
        }
    except Exception as error:
        # Preserve provenance without leaking query text, credentials, or URLs.
        return {
            **record,
            "reason": "Common Crawl lookup could not complete",
            "error_type": type(error).__name__,
        }
    finally:
        if query_id and not terminal:
            try:
                client.stop_query_execution(QueryExecutionId=query_id)
                record["query_cleanup"]["cancellation_requested"] = True
            except Exception:
                record["query_cleanup"]["cancellation_requested"] = False
