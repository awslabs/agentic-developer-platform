"""Wire contract shared by the gateway mediator and both durable consumers."""

import hashlib
import json

KINDS = {"security_result", "devops_result", "finops_cost_result", "transform_result",
         "well_architected_result"}
SUFFIXES = {"security_result": "l5-contributions", "devops_result": "l5-devops-contributions",
            "finops_cost_result": "l5-cost-contributions", "transform_result": "l5-transform-contributions",
            "well_architected_result": "l5-wa-contributions"}
DESTINATIONS = {"context": "product-context", "insights": "engineering-insights"}
REVISION = "l5-consumer-v1"
MAX_BYTES = 4 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def kind_of(payload):
    return payload.get("kind") or payload.get("result_ref", {}).get("record_type")


def storage_key(scope, result_ref, destination, schema_version):
    """Semantic uniqueness includes the immutable result revision and destination."""
    return digest({"scope": scope, "result_ref": result_ref, "destination": destination,
                   "schema_version": schema_version})


def validate_payload(payload, authority):
    if (not isinstance(payload, dict) or len(canonical(payload)) > MAX_BYTES
            or kind_of(payload) not in KINDS or payload.get("schema_version") != "1.0"
            or payload.get("scope") != authority["scope"]
            or payload.get("result_ref") != authority["result_ref"]
            or payload.get("contribution_id") != authority["contribution_id"]
            or payload.get("run_id") != authority["run_id"]
            or kind_of(payload) != authority["kind"]):
        raise ValueError("contribution differs from admitted immutable lineage")
    scope, reference = payload["scope"], payload["result_ref"]
    if (any(not isinstance(scope.get(field), str) or not scope[field] for field in (
            "tenant_id", "product_id", "project_id", "relationship_revision", "owner_id", "visibility"))
            or reference.get("tenant_id") != scope["tenant_id"]
            or reference.get("project_id") != scope["project_id"]
            or reference.get("record_type") != authority["kind"]
            or any(not isinstance(reference.get(field), str) or not reference[field] for field in (
                "source_connection_id", "record_id", "record_revision"))):
        raise ValueError("invalid result scope")
    if "claim_page" not in payload and digest(payload) != authority["contribution_hash"]:
        raise ValueError("contribution digest differs from durable producer")
    if payload.get("invalidation_watermark", 0) != 0:
        raise ValueError("admission cannot clear or import a tombstone")
