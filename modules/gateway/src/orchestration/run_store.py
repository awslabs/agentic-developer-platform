"""Worker run records written by the engine after SQL commit, before SQS publish.

The existing worker and budget boundary require this row. These records report
execution; the append-only SQL plan/decision remains the authority for dispatch.
"""

from __future__ import annotations

import os
import time
from typing import Any

from botocore.config import Config
from botocore.exceptions import ClientError


class EngineRunStore:
    def __init__(self, table: Any):
        self.table = table

    @classmethod
    def from_env(cls) -> EngineRunStore:
        import boto3

        name = os.environ.get("WEBHOOK_EVENTS_TABLE", "").strip()
        if not name:
            raise RuntimeError("WEBHOOK_EVENTS_TABLE is required for engine worker runs")
        return cls(
            boto3.resource(
                "dynamodb",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}),
            ).Table(name)
        )

    @staticmethod
    def build_item(envelope: dict) -> dict:
        source = envelope["source_ref"]
        graph = envelope["orchestration"]
        correlation = envelope["correlation"]
        return {
            "event_id": envelope["message_id"],
            "arrived_at": envelope["arrived_at"],
            "GSI1PK": envelope["tenant_id"],
            "GSI1SK": envelope["arrived_at"],
            "tenant_id": envelope["tenant_id"],
            "user_id": envelope["actor"]["user_id"],
            "channel": "orchestration",
            "event_type": "engine_dispatch",
            "action": "dispatch",
            "status": "webhook_received",
            "status_updated_at": envelope["arrived_at"],
            "expires_at": int(time.time()) + 30 * 86400,
            "repo": source["repo"],
            "issue_number": source["issue"],
            # Same bound as webhook activity. Older envelopes still identify
            # their issue instead of creating another untitled invocation.
            "topic": ((graph.get("title") or "").strip() or f"{source['repo']}#{source['issue']}")[:120],
            "installation_id": str(source["installation_id"]),
            "persona": envelope["persona"],
            "correlation_id": correlation["correlation_id"],
            "root_human_id": correlation["root_human_id"],
            "is_human_rooted": True,
            "chain_depth": 0,
            "graph_address": graph["graph_address"],
            "engine_node_id": graph["node_id"],
            "engine_attempt": graph["attempt"],
            "source_url": f"https://github.com/{source['repo']}/issues/{source['issue']}",
        }

    def register(self, envelope: dict) -> None:
        item = self.build_item(envelope)
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(event_id)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = self.get(item["event_id"], item["arrived_at"])
            # A retry must preserve terminal status and cannot rebind identity.
            keys = ("tenant_id", "user_id", "root_human_id", "engine_node_id", "engine_attempt", "repo", "issue_number")
            if existing is None or any(existing.get(k) != item[k] for k in keys):
                raise RuntimeError("engine run identity conflicts with existing record") from exc

    def get(self, run_id: str, arrived_at: str) -> dict | None:
        return self.table.get_item(Key={"event_id": run_id, "arrived_at": arrived_at}, ConsistentRead=True).get("Item")
