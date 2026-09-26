"""Pure owned-document intent and watch evidence; no upload or indexing dispatch."""

import hashlib
import json
import re
import uuid

DOCUMENT = (
    b"# ADP CLI evaluation\n\nThis owned fixture tests knowledge indexing status.\n"
)


def recovery_plan(config):
    fixture = config["knowledge_lifecycle"]
    evaluation = config["evaluation_id"]
    if not isinstance(evaluation, str) or not evaluation.strip():
        raise ValueError("Stable evaluation ID required")
    owner = str(uuid.UUID(fixture["canonical_user_id"]))
    tenant = fixture["tenant_id"]
    bucket = fixture["bucket"]
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("Selected tenant required")
    if not isinstance(bucket, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket
    ):
        raise ValueError("Exact source bucket required")
    identity = json.dumps(
        [
            config["gateway_url"],
            evaluation,
            tenant,
            owner,
            bucket,
            "knowledge-lifecycle-v1",
        ]
    )
    token = hashlib.sha256(identity.encode()).hexdigest()[:32]
    key = f"users/{owner}/cli-evaluation/{token}/source.md"
    return {
        "version": 1,
        "purpose": "knowledge_lifecycle",
        "evaluation_id": evaluation,
        "gateway": config["gateway_url"],
        "tenant_id": tenant,
        "canonical_user_id": owner,
        "login_user_id": fixture["login_user_id"],
        "bucket": bucket,
        "key": key,
        "source_ref": f"s3://{bucket}/{key}",
        "scope": "personal",
        "asset_type": "doc",
        "content_sha256": hashlib.sha256(DOCUMENT).hexdigest(),
        "content_bytes": len(DOCUMENT),
        "registration_key": str(uuid.uuid5(uuid.NAMESPACE_URL, identity + ":register")),
        "reindex_key": str(uuid.uuid5(uuid.NAMESPACE_URL, identity + ":reindex")),
        "maximum_generations": 2,
        "source_receipt": {
            key: fixture.get(key)
            for key in ("source_etag", "source_version_id", "cost_evidence_sha256")
        },
        "cost_bounds": {
            key: fixture.get(key)
            for key in (
                "max_attempts",
                "max_spend_usd",
                "verified_worst_case_usd",
                "runtime_cost_verified",
                "source_upload_verified",
                "owned_mutations_authorized",
            )
        },
        "source_cleanup": "Exact recorded ETag/version only after terminal indexing; retain on uncertainty",
        "registry_cleanup": "Soft delete only; indexing output and graph artifacts retained",
    }


def watch_events(stdout, asset_id):
    """Validate the CLI NDJSON stream without treating timeout as completion."""
    asset_id = str(uuid.UUID(asset_id))
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("Watch event must be an object")
        detail = event.get("detail")
        if (
            event.get("status") not in {"pending", "ok", "failed"}
            or not isinstance(detail, dict)
            or detail.get("asset_id") != asset_id
        ):
            raise ValueError("Watch event lacks correlated asset evidence")
        if events and events[-1]["status"] != "pending":
            raise ValueError("Watch emitted events after terminal status")
        if event["status"] == "ok":
            stages = detail.get("stages")
            success = {"completed", "succeeded", "success", "verified"}
            if (
                detail.get("status")
                not in {"indexed", "complete", "completed", "ready"}
                or detail.get("usable") is not True
                or not detail.get("run_id")
                or detail.get("run_status") not in success | {"complete"}
                or not isinstance(stages, list)
                or not stages
                or not all(isinstance(stage, dict) for stage in stages)
                or not any(stage.get("status") in success for stage in stages)
                or not all(
                    stage.get("status") in success | {"skipped"} for stage in stages
                )
            ):
                raise ValueError("Watch success lacks successful run/stage evidence")
            uuid.UUID(detail["run_id"])
        events.append(event)
    if not events:
        raise ValueError("Watch returned no evidence")
    return events


def validate_dispatch_fixture(fixture):
    """Require caller-verified deployed bounds; these fields do not enforce billing."""
    import math

    for key in (
        "owned_mutations_authorized",
        "source_upload_verified",
        "runtime_cost_verified",
    ):
        if fixture.get(key) is not True:
            raise ValueError("Knowledge source and deployed cost verification required")
    if (
        type(fixture.get("max_attempts")) is not int
        or not 2 <= fixture["max_attempts"] <= 6
    ):
        raise ValueError("Knowledge delivery-attempt bound must be between two and six")
    budget = fixture.get("max_spend_usd")
    estimate = fixture.get("verified_worst_case_usd")
    if (
        not all(
            type(v) in (int, float) and math.isfinite(v) for v in (budget, estimate)
        )
        or not 0 <= estimate <= budget <= 1
        or budget <= 0
    ):
        raise ValueError(
            "Verified worst-case cost must fit explicit spend bound of at most one dollar"
        )
    if not re.fullmatch(r"[a-f0-9]{64}", str(fixture.get("cost_evidence_sha256", ""))):
        raise ValueError("External deployed-cost evidence digest required")
    if not isinstance(fixture.get("source_etag"), str) or not re.fullmatch(
        r'"?[a-f0-9-]{32,80}"?', fixture["source_etag"]
    ):
        raise ValueError("Exact uploaded source ETag required")
    if (
        not isinstance(fixture.get("source_version_id"), str)
        or not 1 <= len(fixture["source_version_id"]) <= 256
    ):
        raise ValueError(
            "Source version receipt required; use null for unversioned object"
        )
