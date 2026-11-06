"""Read back Transform consumer acknowledgements and independent later-task use."""

import json
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace

from l5_services.publication.contributions import validate_receipt


DESTINATIONS = {"product-context", "engineering-insights"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _current(expires_at, now):
    try:
        expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return expiration.tzinfo is not None and expiration > now


async def probe_reuse(driver, *, run_id, result_id, scope, now):
    _require(isinstance(scope, dict) and all(isinstance(scope.get(key), str) and scope[key]
             for key in ("tenant_id", "product_id", "project_id", "relationship_revision", "owner_id", "visibility"))
             and all(isinstance(value, str) and value for value in (run_id, result_id))
             and isinstance(now, datetime) and now.tzinfo is not None,
             "reuse selection is incomplete")
    run = await driver.read_run(run_id)
    result = await driver.read_result(result_id)
    _require(isinstance(run, dict) and isinstance(result, dict)
             and run.get("run_id") == result.get("run_id") == run_id
             and result.get("result_id") == result_id and result.get("service_id") == "transform"
             and run.get("scope") == result.get("scope") == scope
             and type(result.get("revision")) is int and result["revision"] > 0
             and isinstance(run.get("input"), dict)
             and isinstance(run["input"].get("sources"), list)
             and isinstance(run["input"].get("transform_input", {}).get("goal"), dict)
             and _current(result.get("retention_expires_at"), now),
             "selected result lacks current scoped lineage")
    retained = result.get("retained_artifacts")
    _require(isinstance(retained, dict) and "original_patch" in retained
             and all(isinstance(reference, dict)
                     and reference.get("tenant_id") == scope["tenant_id"]
                     and reference.get("project_id") == scope["project_id"]
                     and isinstance(reference.get("sha256"), str)
                     and len(reference["sha256"]) == 64
                     and not reference.get("revoked_at")
                     and _current(reference.get("expires_at"), now)
                     for reference in retained.values()),
             "retained citation evidence is unavailable")
    expected_reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                          "source_connection_id": "l5-transform", "record_type": "transform_result",
                          "record_id": result_id, "record_revision": str(result["revision"])}
    evidence = [{"kind": name, "artifact": reference}
                for name, reference in sorted(retained.items())]
    rows = await driver.read_publications(result_id)
    _require(isinstance(rows, list) and len(rows) == 2
             and {row.get("destination") for row in rows if isinstance(row, dict)} == DESTINATIONS,
             "both scoped consumer publications are required")
    references = []
    for row in rows:
        payload = row.get("payload")
        _require(isinstance(payload, dict) and row.get("state") == "published"
                 and row.get("tenant_id") == scope["tenant_id"]
                 and row.get("result_id") == result_id
                 and row.get("result_version") == result["revision"]
                 and row.get("schema_version") == payload.get("schema_version") == "1.0"
                 and row.get("invalidation_watermark") == 0
                 and row.get("applied_watermark") == 0
                 and payload.get("kind") == "transform_result"
                 and payload.get("contribution_id") == row.get("publication_id")
                 and payload.get("run_id") == run_id
                 and payload.get("scope") == scope and payload.get("result") == result
                 and payload.get("result_ref") == expected_reference
                 and payload.get("source_refs") == run["input"]["sources"]
                 and payload.get("goal") == run["input"]["transform_input"]["goal"]
                 and payload.get("evidence_refs") == evidence,
                 "publication differs from scoped original evidence")
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        _require(row.get("contribution_hash") == digest, "publication digest does not match its payload")
        publication = SimpleNamespace(**row)
        validate_receipt(publication, row.get("receipt"))
        for kind, method in (("receipt", driver.read_consumer_receipt),
                             ("projection", driver.read_consumer_projection)):
            observation = await method(row)
            _require(isinstance(observation, dict)
                     and observation.get("source") == row["destination"]
                     and observation.get("status") == 200
                     and isinstance(observation.get("evidence_ref"), str)
                     and bool(observation["evidence_ref"]),
                     "consumer readback is not independently attributable")
            if kind == "receipt":
                _require(observation.get("data") == row["receipt"],
                         "consumer did not acknowledge the persisted receipt")
                validate_receipt(publication, observation["data"])
            else:
                _require(observation.get("data") == {**payload, "contribution_hash": digest},
                         "consumer projection differs from the original digest")
            references.append(observation["evidence_ref"])
    _require(len(set(references)) == 4, "consumer observations reuse an evidence reference")
    task = await driver.read_later_task(result_id)
    citation = {"result_ref": expected_reference, "evidence_refs": evidence}
    authorization = task.get("entitlement") if isinstance(task, dict) else None
    _require(isinstance(task, dict) and task.get("state") == "succeeded"
             and task.get("scope") == scope
             and isinstance(task.get("task_id"), str) and bool(task["task_id"])
             and isinstance(task.get("run_id"), str) and bool(task["run_id"])
             and task["run_id"] != run_id
             and isinstance(task.get("evidence_ref"), str) and bool(task["evidence_ref"])
             and isinstance(authorization, dict) and authorization.get("decision") == "allowed"
             and authorization.get("scope") == scope
             and isinstance(authorization.get("evidence_ref"), str) and bool(authorization["evidence_ref"])
             and isinstance(task.get("input"), dict)
             and task["input"].get("citation") == citation
             and isinstance(result.get("source_trees"), dict)
             and task["input"].get("candidate_tree") == result["source_trees"].get("candidate_tree")
             and bool(task["input"]["candidate_tree"])
             and isinstance(task.get("result"), dict)
             and task["result"].get("cited_result_ref") == expected_reference,
             "later task did not independently cite and use this result")
    for kind in ("foreign", "revoked"):
        denial = await driver.read_reuse_denial(result_id, kind)
        fencing = denial.get("entitlement") if isinstance(denial, dict) else None
        actor_scope = denial.get("actor_scope") if isinstance(denial, dict) else None
        _require(isinstance(denial, dict) and denial.get("kind") == kind
                 and denial.get("status") in {403, 404}
                 and denial.get("body") in (None, {}, {"detail": {"code": "not_found"}})
                 and denial.get("effect_count") == 0
                 and isinstance(denial.get("actor_id"), str) and bool(denial["actor_id"])
                 and isinstance(fencing, dict) and fencing.get("decision") == "denied"
                 and isinstance(fencing.get("evidence_ref"), str) and bool(fencing["evidence_ref"])
                 and ((kind == "foreign" and isinstance(actor_scope, dict)
                       and actor_scope.get("tenant_id") != scope["tenant_id"])
                      or (kind == "revoked" and actor_scope == scope
                          and type(fencing.get("watermark")) is int and fencing["watermark"] > 0
                          and type(fencing.get("applied_watermark")) is int
                          and fencing["applied_watermark"] >= fencing["watermark"]))
                 and isinstance(denial.get("evidence_ref"), str) and bool(denial["evidence_ref"]),
                 "foreign or revoked reuse disclosed a result or caused an effect")
    return {"phase": "consumer-and-reuse-observations", "live_qualified": False,
            "result_ref": expected_reference, "consumer_refs": references,
            "later_task_ref": task["evidence_ref"], "citation": citation,
            "denials": ["foreign", "revoked"]}
