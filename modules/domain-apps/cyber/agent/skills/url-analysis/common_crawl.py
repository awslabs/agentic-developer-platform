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
DEFAULT_QUEUE_SECONDS = 300
DEFAULT_EXECUTION_SECONDS = 120
MAX_SCAN_BYTES = 1024**3


@dataclass(frozen=True)
class CrawlConfig:
    database: str
    table: str
    workgroup: str
    crawls: tuple[str, ...]
    region: str = "us-east-1"
    catalog: str = "AwsDataCatalog"
    queue_seconds: int = DEFAULT_QUEUE_SECONDS
    execution_seconds: int = DEFAULT_EXECUTION_SECONDS

    def validate(self):
        if not all(
            IDENTIFIER.fullmatch(v) for v in (self.database, self.table, self.catalog)
        ):
            raise ValueError("Invalid Common Crawl catalog identifier")
        if not re.fullmatch(r"[\w.-]{1,128}", self.workgroup):
            raise ValueError("Invalid Athena workgroup")
        if not 1 <= len(self.crawls) <= 12 or not all(
            CRAWL.fullmatch(c) for c in self.crawls
        ):
            raise ValueError("Configure one to twelve explicit Common Crawl partitions")
        if (
            not 1 <= self.queue_seconds <= 1800
            or not 1 <= self.execution_seconds <= 1800
        ):
            raise ValueError("Query queue/execution budgets must be 1–1800 seconds")
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
            queue_seconds=int(
                os.environ.get("CYBER_CC_QUEUE_SECONDS", DEFAULT_QUEUE_SECONDS)
            ),
            execution_seconds=int(
                os.environ.get("CYBER_CC_EXECUTION_SECONDS", DEFAULT_EXECUTION_SECONDS)
            ),
        ).validate()


def domain_of(url):
    host = (urlsplit(url).hostname or "").rstrip(".").encode("idna").decode().lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return str(address)
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


def query_for(config, domain, *, url=None, match="host"):
    config.validate()
    if match not in {"host", "exact"}:
        raise ValueError("Archive match must be host or exact")
    host_url = (
        "https://[" + domain + "]/" if ":" in domain else "https://" + domain + "/"
    )
    if domain_of(host_url) != domain:
        raise ValueError("Query domain must be canonical")
    try:
        ipaddress.ip_address(domain)
        literal = True
    except ValueError:
        literal = False
    values = list(config.crawls)
    predicates = []
    if not literal:
        labels = domain.split(".")
        parents = [".".join(labels[i:]) for i in range(len(labels) - 1)]
        predicates += [
            "url_host_tld = ?",
            "url_host_registered_domain IN (" + ", ".join("?" for _ in parents) + ")",
        ]
        values += [labels[-1], *parents]
    if match == "exact":
        if not url or domain_of(url) != domain:
            raise ValueError("Exact archive lookup requires a URL on the case hostname")
        predicates += ["url_host_name = ?", "url = ?"]
        values += [domain, url]
    elif literal:
        predicates += ["url_host_name = ?"]
        values += [domain]
    else:
        predicates += ["(url_host_name = ? OR url_host_name LIKE ?)"]
        values += [domain, "%." + domain]
    # Query values are parameters; validated catalog/table identifiers are SQL.
    sql = f"""SELECT crawl, url_host_name, url, fetch_time, fetch_status,
       content_mime_type, content_languages, content_digest,
       warc_filename, warc_record_offset, warc_record_length
FROM "{config.database}"."{config.table}"
WHERE subset = 'warc' AND crawl IN ({", ".join("?" for _ in config.crawls)})
  AND {" AND ".join(predicates)}
ORDER BY fetch_time DESC, url ASC
LIMIT {MAX_ROWS}"""
    return sql, ["'" + v.replace("'", "''") + "'" for v in values]


def lookup_common_crawl(
    url,
    *,
    config=None,
    client=None,
    clock=time.monotonic,
    sleep=time.sleep,
    match="host",
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
        sql, params = query_for(config, domain, url=url, match=match)
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
            match=match,
            queue_budget_seconds=config.queue_seconds,
            execution_budget_seconds=config.execution_seconds,
        )
        started = previous = clock()
        queue_elapsed = execution_elapsed = 0.0
        while True:
            execution = client.get_query_execution(QueryExecutionId=query_id)[
                "QueryExecution"
            ]
            state = execution["Status"]["State"]
            now = clock()
            elapsed = now - previous
            if state == "QUEUED":
                queue_elapsed += elapsed
            else:
                execution_elapsed += elapsed
            previous = now
            record["query_state"] = state
            record["queue_wait_seconds"] = round(queue_elapsed, 3)
            record["execution_wait_seconds"] = round(execution_elapsed, 3)
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
            exceeded = (
                "queue"
                if queue_elapsed >= config.queue_seconds
                else "execution"
                if execution_elapsed >= config.execution_seconds
                else "total"
                if now - started >= config.queue_seconds + config.execution_seconds
                else None
            )
            if exceeded:
                return {
                    **record,
                    "reason": "Common Crawl query exceeded its "
                    + exceeded
                    + " time budget",
                    "budget_exceeded": exceeded,
                }
            sleep(1)
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
