"""Trusted state and graph sinks with server-owned ingestion selectors."""

import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from fastapi import HTTPException

from src.knowledge.ingestion_authority import deny


def state_command(authority, command, args):
    table = os.environ.get("INGESTION_STATE_TABLE", "")
    if not table:
        raise HTTPException(503, "Ingestion state storage is not configured")
    status = args.get("status")
    if status not in {"processing", "success", "complete", "partial", "failed"}:
        raise deny()
    from src.knowledge.ingestion_catalog import STAGES

    steps = args.get("steps", {})
    if not isinstance(steps, dict) or any(k not in STAGES or v not in {"verified", "failed", "skipped", "running"} for k, v in steps.items()):
        raise deny()
    now = datetime.now(UTC).isoformat()
    item = {
        "source": f"{authority.asset_type}#{authority.asset_id}",
        "record_type": "STATE" if command == "state" else f"RUN#{authority.attempt_id}",
        "content_type": authority.asset_type,
        "status": status,
        "ingestion_status": status,
        "steps": steps,
        "updated_at": now,
        "last_error": str(args.get("error") or "")[:1000],
    }
    if command == "history":
        duration = args.get("duration_sec", 0)
        if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 <= duration <= 7200:
            raise deny()
        item.update(duration_sec=Decimal(str(round(duration, 1))), ttl=int(time.time()) + 30 * 86400, created_at=now, trigger="registered_ingestion")
    boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")).Table(table).put_item(Item=item)


def graph_command(authority, command, args):
    """Parameter-bound graph writes; a parser can never submit executable queries."""
    if authority.owner_sub or authority.product_id or authority.asset_type not in {"repo", "doc"}:
        raise deny()
    endpoint = os.environ.get("INGESTION_NEPTUNE_ENDPOINT", "")
    if not endpoint:
        raise HTTPException(503, "Ingestion graph is not configured")

    def bound_id(local_id):
        if not isinstance(local_id, str) or not local_id or len(local_id) > 4096:
            raise deny()
        return authority.asset_id + ":" + hashlib.sha256(local_id.encode()).hexdigest()

    if command == "clear":
        operations = [("MATCH (n:entity) WHERE n.asset_id=$asset AND n.source='code-index' DETACH DELETE n", {"asset": authority.asset_id})]
    elif command == "put":
        entities, relationships = args.get("entities"), args.get("relationships")
        if not isinstance(entities, list) or not isinstance(relationships, list) or len(entities) > 100 or len(relationships) > 200:
            raise deny()
        nodes = []
        for entity in entities:
            if not isinstance(entity, dict):
                raise deny()
            eid = bound_id(entity.get("entity_id"))
            properties = {k: str(entity[k])[:4096] for k in ("name", "type", "file", "description") if k in entity}
            properties.update(asset_id=authority.asset_id, source="wiki" if entity.get("source") == "wiki" else "code-index")
            if authority.asset_type == "repo":
                properties["repo"] = authority.source
            else:
                properties.update(source_ref=authority.source_ref, content_type="doc")
            if authority.tenant_id:
                properties["tenant_id"] = authority.tenant_id
            if "line" in entity:
                line = entity["line"]
                if type(line) is not int or not 0 <= line <= 100000000:
                    raise deny()
                properties["line"] = line
            nodes.append({"id": eid, "properties": properties})
        groups = {}
        for relation in relationships:
            if not isinstance(relation, dict):
                raise deny()
            label = relation.get("type", "related_to")
            if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", label):
                raise deny()
            groups.setdefault(label, []).append({"from": bound_id(relation.get("from")), "to": bound_id(relation.get("to"))})
        # Validate the entire batch before writes. The only interpolated query
        # fragment is a restricted identifier; every content value is a parameter.
        operations = []
        if nodes:
            operations.append(("UNWIND $nodes AS node MERGE (n:entity {entity_id: node.id}) SET n += node.properties", {"nodes": nodes}))
        for label, edges in groups.items():
            operations.append(
                (
                    f"UNWIND $edges AS edge MATCH (a:entity {{entity_id: edge.from}}), (b:entity {{entity_id: edge.to}}) "
                    f"WHERE a.asset_id=$asset AND b.asset_id=$asset MERGE (a)-[:`{label}`]->(b)",
                    {"edges": edges, "asset": authority.asset_id},
                )
            )
    else:
        raise deny()
    client = boto3.client("neptunedata", endpoint_url=f"https://{endpoint}:8182", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    for query, parameters in operations:
        client.execute_open_cypher_query(openCypherQuery=query, parameters=json.dumps(parameters))
    return True
