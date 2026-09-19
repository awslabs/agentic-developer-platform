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
    def _base_item(envelope: dict, *, event_type: str, action: str) -> dict:
        """The fields every engine-produced run row carries, whatever summoned it.

        Factored out so the two builders below cannot drift on the parts other
        readers depend on: `tenant_id` and `status` are what
        `draft_binding.resolve_draft_tenant` binds an internal-scope registration to,
        and `_PROJECTION` in `budget/run_binding.py` reads `user_id`, `tenant_id`,
        `root_human_id`, `is_human_rooted`, `correlation_id`, `status` and
        `arrived_at`. A row missing any of those is a run that cannot spend or
        register, so neither builder gets to omit them independently.
        """
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
            "event_type": event_type,
            "action": action,
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
            "source_url": f"https://github.com/{source['repo']}/issues/{source['issue']}",
        }

    @staticmethod
    def build_item(envelope: dict) -> dict:
        graph = envelope["orchestration"]
        return {
            **EngineRunStore._base_item(envelope, event_type="engine_dispatch", action="dispatch"),
            "graph_address": graph["graph_address"],
            "engine_node_id": graph["node_id"],
            "engine_attempt": graph["attempt"],
        }

    @staticmethod
    def build_authoring_item(envelope: dict) -> dict:
        """The run row for one AI-DLC amendment-authoring run (#4529).

        A separate builder rather than a tolerant `build_item`, because the three
        graph fields are not optional for dispatch: `results.collect` refuses a run
        record whose `engine_node_id`/`engine_attempt` do not match the node it is
        collecting, and `register`'s retry check treats them as identity. Letting
        them go absent to accommodate this path would turn "this row belongs to no
        node" from a shape that cannot be built into a shape those readers must
        remember to reject.

        The absence here is deliberate and load-bearing: an authoring run owns no
        graph node, so it gets no `graph_address`, and without one its model calls
        cannot be attributed to a node's spend (`model_identity` captures the
        address from the validated scope, and `validate_authoring_authority` returns
        none). It carries the assignment instead — the request it answers and the
        base revision it was asked against — so an operator reading this table can
        tell which replan a run came from without joining back to SQL.
        """
        assignment = envelope["orchestration"]
        item = {
            **EngineRunStore._base_item(envelope, event_type="engine_replan_authoring", action="author_amendment"),
            "engine_request_id": assignment["request_id"],
            "engine_flow_id": assignment["flow_id"],
        }
        base_version = assignment.get("base_plan_version")
        if base_version is not None:
            item["engine_base_plan_version"] = base_version
        return item

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
