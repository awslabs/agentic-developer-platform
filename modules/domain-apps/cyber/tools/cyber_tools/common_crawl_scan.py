"""Task-owned asynchronous archive discovery; never fetch the live target."""

import hashlib
import time

from fastapi import HTTPException
from botocore.exceptions import ClientError
from adp_tools.storage import base_item, serialize, task_ops_partition
from common_crawl import CrawlConfig, query_for, domain_of, MAX_ROWS, MAX_SCAN_BYTES
from case_contract import digest as content_digest, redact_url, sanitize, utcnow

LIMITATIONS = [
    "Historical evidence from selected crawl partitions; not current site behavior.",
    "No match does not establish safety, domain age, or absence from the archive.",
    "At most 30 newest matching captures are returned.",
]


class CommonCrawlTools:
    def __init__(self, client, config=None, clock=time.time, s3=None):
        self.client, self.config, self.clock, self.s3 = client, config, clock, s3

    def execute(
        self, identity, operation, payload, task, key, repo, evidence, revalidate
    ):
        config = self.config or CrawlConfig.from_env()
        if not config:
            return {"status": "partial", "reason": "common_crawl_not_configured"}
        config.validate()
        partition = task_ops_partition(identity.task_id)
        if operation == "common_crawl_scan":
            settings = self.client.get_work_group(WorkGroup=config.workgroup)[
                "WorkGroup"
            ]
            rules = settings["Configuration"]
            if (
                settings.get("State") != "ENABLED"
                or not rules.get("EnforceWorkGroupConfiguration")
                or not 10_000_000
                <= rules.get("BytesScannedCutoffPerQuery", 0)
                <= MAX_SCAN_BYTES
                or not rules.get("ResultConfiguration", {})
                .get("OutputLocation", "")
                .startswith("s3://")
            ):
                raise HTTPException(503, "Common Crawl workgroup budget unavailable")
            from datetime import datetime

            deadline = min(
                datetime.fromisoformat(
                    task["deadline_at"].replace("Z", "+00:00")
                ).timestamp(),
                self.clock() + config.queue_seconds + config.execution_seconds,
            )
            domain = domain_of(payload["url"])
            sql, params = query_for(
                config, domain, url=payload["url"], match=payload.get("match", "host")
            )
            # A durable operation claim precedes this call; token also fences AWS submission.
            token = hashlib.sha256(
                (identity.task_id + identity.runtime_attempt_id + key).encode()
            ).hexdigest()
            query = self.client.start_query_execution(
                QueryString=sql,
                ExecutionParameters=params,
                QueryExecutionContext={
                    "Database": config.database,
                    "Catalog": config.catalog,
                },
                WorkGroup=config.workgroup,
                ClientRequestToken=token,
            )["QueryExecutionId"]
            row = base_item(
                partition=partition,
                sort_key="CC_SCAN#" + key,
                record_type="TASK_OPS",
                scope=task["scope"],
            )
            row.update(
                identity=identity.model_dump(),
                query_id=query,
                domain=domain,
                deadline=int(deadline),
                crawls=list(config.crawls),
                workgroup=config.workgroup,
                match=payload.get("match", "host"),
            )
            try:
                repo._client.put_item(
                    TableName=repo.table_name,
                    Item=serialize(row),
                    ConditionExpression="attribute_not_exists(event_id)",
                )
            except Exception:
                self.client.stop_query_execution(QueryExecutionId=query)
                raise
            return {"status": "pending", "scan_id": key, "poll_after_seconds": 2}
        row = repo._get(partition, "CC_SCAN#" + payload["scan_id"])
        if not row or row.get("identity") != identity.model_dump():
            raise HTTPException(404, "Archive scan unavailable")
        if operation == "common_crawl_read":
            captures = row.get("captures", [])
            capture = next(
                (c for c in captures if c["capture_id"] == payload["capture_id"]), None
            )
            if capture is None:
                raise HTTPException(404, "Select a completed scan capture")
            # Limit unique reads per scan. Existing operation claims deduplicate the same capture.
            try:
                repo._client.update_item(
                    TableName=repo.table_name,
                    Key=serialize(
                        {"event_id": partition, "arrived_at": row["arrived_at"]}
                    ),
                    UpdateExpression="ADD read_count :one",
                    ConditionExpression="attribute_not_exists(read_count) OR read_count < :max",
                    ExpressionAttributeValues=serialize({":one": 1, ":max": 8}),
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                return {
                    "status": "partial",
                    "reason": "archive_read_limit_reached",
                    "scan_id": payload["scan_id"],
                    "capture_id": payload["capture_id"],
                    "limitations": LIMITATIONS,
                }
            from archive_content import fetch_record, parse_record, extract_content

            # Content validation failures are evidence limitations, not task failures.
            # Storage, authorization and infrastructure exceptions still propagate.
            try:
                raw = fetch_record(capture, client=self.s3)
            except ValueError:
                return {
                    "status": "partial",
                    "reason": "archive_range_unavailable",
                    "scan_id": payload["scan_id"],
                    "capture_id": payload["capture_id"],
                    "limitations": LIMITATIONS,
                }
            from adp_tools.evidence import put_blob

            original = put_blob(evidence, identity, raw, "application/warc+gzip")
            metadata = None
            try:
                content, metadata = parse_record(raw, capture)
                extracted = extract_content(content, metadata)
            except ValueError:
                return {
                    "status": "partial",
                    "reason": "archive_content_not_extractable",
                    "scan_id": payload["scan_id"],
                    "capture_id": payload["capture_id"],
                    "original": original,
                    "metadata": metadata,
                    "limitations": LIMITATIONS
                    + [
                        "This capture could not be safely extracted; do not infer its page content."
                    ],
                }
            # The complete bounded extracted document is an artifact; the model gets a preview.
            import json

            artifact_content = json.dumps(
                {"metadata": metadata, "extracted": extracted}, sort_keys=True
            ).encode()
            artifact = evidence.put_run_artifact(
                attempt=identity,
                content=artifact_content,
                content_type="application/json",
                digest=hashlib.sha256(artifact_content).hexdigest(),
            )
            return {
                "status": "completed",
                "capture_id": payload["capture_id"],
                "scan_id": payload["scan_id"],
                "retrieved_at": utcnow(),
                "capture_time": capture.get("fetch_time"),
                "warc_sha256": hashlib.sha256(raw).hexdigest(),
                "original": original,
                "metadata": metadata,
                "text_preview": extracted.get("text", "")[:6000],
                "artifact_id": artifact.artifact_id,
                "limitations": LIMITATIONS,
            }
        execution = self.client.get_query_execution(QueryExecutionId=row["query_id"])[
            "QueryExecution"
        ]
        state = execution["Status"]["State"]
        info = {
            "scan_id": payload["scan_id"],
            "query_id": row["query_id"],
            "query_state": state,
            "crawls": row["crawls"],
            "workgroup": row["workgroup"],
            "limitations": LIMITATIONS,
            "bytes_scanned": execution.get("Statistics", {}).get(
                "DataScannedInBytes", 0
            ),
        }
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            if self.clock() >= row["deadline"]:
                self.client.stop_query_execution(QueryExecutionId=row["query_id"])
                return {**info, "status": "failed", "reason": "query_deadline_exceeded"}
            return {**info, "status": "pending", "poll_after_seconds": 2}
        if state != "SUCCEEDED":
            return {**info, "status": "failed"}
        result = self.client.get_query_results(
            QueryExecutionId=row["query_id"], MaxResults=MAX_ROWS + 1
        )["ResultSet"]
        columns = [c["Name"] for c in result["ResultSetMetadata"]["ColumnInfo"]]
        rows = result.get("Rows", [])
        if (
            rows
            and [c.get("VarCharValue", "") for c in rows[0].get("Data", [])] == columns
        ):
            rows = rows[1:]
        captures = []
        for raw in rows[:MAX_ROWS]:
            item = dict(
                zip(columns, [c.get("VarCharValue", "") for c in raw.get("Data", [])])
            )
            host = item.get("url_host_name", "").lower()
            if host != row["domain"] and not host.endswith("." + row["domain"]):
                raise HTTPException(503, "Archive returned out-of-scope capture")
            item["url_sha256"] = content_digest(item.get("url", ""))
            item["url"] = redact_url(item.get("url", ""))
            item["capture_id"] = f"capture-{len(captures) + 1:03d}"
            captures.append(sanitize(item))
        repo._client.update_item(
            TableName=repo.table_name,
            Key=serialize({"event_id": partition, "arrived_at": row["arrived_at"]}),
            UpdateExpression="SET captures = :captures",
            ExpressionAttributeValues=serialize({":captures": captures}),
        )
        # Keep the model receipt bounded; preserve complete metadata separately.
        import json

        content = json.dumps({**info, "captures": captures}, sort_keys=True).encode()
        artifact = evidence.put_run_artifact(
            attempt=identity,
            content=content,
            content_type="application/json",
            digest=hashlib.sha256(content).hexdigest(),
        )
        public = [
            {
                "capture_id": c["capture_id"],
                "url": c.get("url", "")[:256],
                "url_truncated": len(c.get("url", "")) > 256,
                "fetch_time": c.get("fetch_time", "")[:64],
                "fetch_status": c.get("fetch_status", "")[:8],
                "content_mime_type": c.get("content_mime_type", "")[:128],
            }
            for c in captures
        ]
        return {
            **info,
            "status": "completed",
            "found": bool(captures),
            "captures": public,
            "artifact_id": artifact.artifact_id,
        }
