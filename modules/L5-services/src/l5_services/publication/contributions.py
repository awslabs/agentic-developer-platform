"""Freeze scoped result lineage before attempting Context delivery."""

import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from l5_services.models.publication import Publication


def create_publication(run, result, detail, findings, *, now):
    if (run.tenant_id != result.tenant_id or run.run_id != result.run_id
            or detail.tenant_id != result.tenant_id or detail.result_id != result.result_id
            or any(finding.tenant_id != result.tenant_id or finding.result_id != result.result_id
                   for finding in findings)):
        raise ValueError("result lineage does not match its run")
    scope = detail.payload["scope"]
    scope_valid = all(scope.get(field) for field in (
        "tenant_id", "product_id", "project_id", "relationship_revision", "owner_id", "visibility"))
    visibility = scope.get("visibility", "only_me")
    if (scope["tenant_id"], scope["product_id"], scope["project_id"]) != (
            run.tenant_id, run.product_id, run.project_id):
        raise ValueError("result scope does not match its run")
    source_refs = run.input.get("sources") or []
    if any(source not in source_refs for finding in findings for source in finding.payload["source_refs"]):
        raise ValueError("finding cites an unpinned source")
    manifest_ref = detail.payload.get("manifest_ref") or (run.manifest or {}).get("archive")
    identity = f"{run.tenant_id}/{result.result_id}/{result.version}/product-context/1.0"
    publication_id = str(uuid5(NAMESPACE_URL, identity))
    claims = [{"finding_id": finding.finding_id, "native_id": finding.native_id,
               "kind": finding.kind, "claim": finding.payload["claim"],
               "provider_severity": finding.payload["provider_severity"],
               "validation": finding.payload["validation"], "source_refs": finding.payload["source_refs"],
               "evidence": finding.payload["evidence"], "visibility": visibility,
               "locations": finding.payload["locations"], "assumptions": finding.payload["assumptions"],
               "mitigations": finding.payload["mitigations"]}
              for finding in sorted(findings, key=lambda row: row.finding_id)]
    payload = {
        "schema_version": "1.0", "contribution_id": publication_id, "scope": scope,
        "result_ref": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                       "source_connection_id": "l5-security", "record_type": "security_result",
                       "record_id": result.result_id, "record_revision": str(result.version)},
        "run_id": run.run_id, "native_job_ref": detail.payload["native_job_ref"],
        "manifest_ref": manifest_ref, "source_refs": source_refs,
        "classification": "confidential" if visibility == "only_me" else "internal",
        "validation": claims[0]["validation"] if claims and len({item["validation"] for item in claims}) == 1
        else "unverified",
        "observed_at": detail.payload["observed_at"], "causal_origin": run.run_id,
        "evidence": [reference for claim in claims for reference in claim["evidence"]],
        "invalidation_watermark": 0, "claims": claims,
    }
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Publication(tenant_id=run.tenant_id, publication_id=publication_id,
                       result_id=result.result_id, result_version=result.version,
                       destination="product-context", schema_version="1.0", contribution_hash=digest,
                       payload=payload, state="pending" if manifest_ref is not None and scope_valid
                       else "unavailable",
                       next_due_at=now,
                       attempts=0, revision=1, invalidation_watermark=0, applied_watermark=0)


def create_devops_publications(run, result, detail, findings, *, now):
    """Retain result delivery with collection, without inventing ungrounded native findings."""
    from l5_services.devops.context import translate_result

    if (run.service_id != "devops" or run.tenant_id != result.tenant_id
            or run.run_id != result.run_id or detail.tenant_id != result.tenant_id
            or detail.result_id != result.result_id
            or detail.payload.get("run_id") != run.run_id
            or detail.payload.get("revision") != result.version
            or detail.payload.get("native_job_ref") != run.native_job_ref):
        raise ValueError("DevOps result lineage does not match its run")
    scope = deepcopy(detail.payload["scope"])
    if (any(scope.get(field) != getattr(run, field) for field in (
            "tenant_id", "product_id", "project_id")) or scope != run.admission.get("scope")):
        raise ValueError("DevOps result scope does not match its run")
    sources = deepcopy((run.manifest or {}).get("sources", [])
                       if run.input.get("capability") == "release_readiness"
                       else run.input.get("sources", []))
    if any(not isinstance(source, dict)
           or source.get("tenant_id") != run.tenant_id or source.get("project_id") != run.project_id
           or any(not isinstance(source.get(field), str) or not source[field] for field in (
               "source_connection_id", "record_type", "record_id", "record_revision"))
           or source["record_revision"].lower() in {"head", "latest", "null"} for source in sources):
        raise ValueError("DevOps contribution requires pinned scoped sources")
    findings = sorted(findings, key=lambda item: item.finding_id)
    selected = set()
    for finding in findings:
        if finding.tenant_id != run.tenant_id or finding.result_id != result.result_id:
            raise ValueError("DevOps finding belongs to another result")
        refs = finding.payload.get("source_refs") or []
        if any(source not in sources for source in refs):
            raise ValueError("DevOps finding cites an unpinned source")
        if refs:
            selected.add(finding.finding_id)
    translated = translate_result(detail, findings, selected_finding_ids=selected)
    evidence_refs = [deepcopy(reference) for finding in translated["findings"]
                     for reference in finding["evidence"]]
    for field in ("evidence_ref", "manifest_ref"):
        if detail.payload.get(field):
            evidence_refs.append({"kind": field.removesuffix("_ref"), "artifact": deepcopy(detail.payload[field])})
    grounded = bool(sources and evidence_refs and len(selected) == len(findings))
    publications = []
    for destination in ("product-context", "engineering-insights"):
        identity = (f"{run.tenant_id}/{run.product_id}/{run.project_id}/"
                    f"{result.result_id}/{result.version}/{destination}/1.0")
        publication_id = str(uuid5(NAMESPACE_URL, identity))
        payload = {
            "schema_version": "1.0", "kind": "devops_result", "contribution_id": publication_id,
            "scope": deepcopy(scope),
            "result_ref": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                           "source_connection_id": "l5-devops", "record_type": "devops_result",
                           "record_id": result.result_id, "record_revision": str(result.version)},
            "run_id": run.run_id, "result": deepcopy(translated),
            "source_refs": deepcopy(sources), "evidence_refs": deepcopy(evidence_refs),
        }
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        publications.append(Publication(
            tenant_id=run.tenant_id, publication_id=publication_id,
            result_id=result.result_id, result_version=result.version,
            destination=destination, schema_version="1.0", contribution_hash=digest,
            payload=payload, state="pending" if grounded else "unavailable", next_due_at=now,
            last_error=None if grounded else "source_evidence_unavailable",
            attempts=0, revision=1, invalidation_watermark=0, applied_watermark=0))
    return publications


def create_finops_publications(run, result, detail, *, now):
    if (run.service_id != "finops" or run.tenant_id != result.tenant_id
            or run.run_id != result.run_id or detail.tenant_id != result.tenant_id
            or detail.result_id != result.result_id
            or detail.payload.get("run_id") != run.run_id
            or detail.payload.get("revision") != result.version):
        raise ValueError("cost result lineage does not match its run")
    scope = deepcopy(detail.payload["scope"])
    if any(scope.get(field) != getattr(run, field) for field in (
            "tenant_id", "product_id", "project_id")):
        raise ValueError("cost result scope does not match its run")
    cost = deepcopy({field: value for field, value in detail.payload.items()
                     if field not in {"result_id", "revision", "scope"}})
    if (cost.get("kind") != "finops_cost_inquiry" or cost.get("query") != run.input.get("query")
            or cost.get("binding_generation") != run.binding_generation
            or cost.get("scope_digest") != run.admission["billing_scope"]["scope_digest"]
            or cost.get("native", {}).get("answer_artifact") != (
                run.provider_readback_ref or {}).get("answer_artifact")):
        raise ValueError("cost contribution differs from the pinned result")
    evidence_refs = [{"kind": "native_answer", "artifact": cost["native"]["answer_artifact"]},
                     {"kind": "scope_snapshot", "artifact": cost["billing_scope"]["snapshot_artifact"]}]
    evidence_refs.extend({"kind": "billing_page", "artifact": page} for page in cost["oracle"]["pages"])
    publications = []
    for destination in ("product-context", "engineering-insights"):
        identity = (f"{run.tenant_id}/{run.product_id}/{run.project_id}/"
                    f"{result.result_id}/{result.version}/{destination}/1.0")
        publication_id = str(uuid5(NAMESPACE_URL, identity))
        payload = {
            "schema_version": "1.0", "kind": "finops_cost_result",
            "contribution_id": publication_id, "scope": deepcopy(scope),
            "result_ref": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                           "source_connection_id": "l5-finops", "record_type": "finops_cost_result",
                           "record_id": result.result_id, "record_revision": str(result.version)},
            "run_id": run.run_id, "cost": deepcopy(cost), "evidence_refs": deepcopy(evidence_refs),
        }
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        publications.append(Publication(
            tenant_id=run.tenant_id, publication_id=publication_id,
            result_id=result.result_id, result_version=result.version,
            destination=destination, schema_version="1.0", contribution_hash=digest,
            payload=payload, state="pending", next_due_at=now,
            attempts=0, revision=1, invalidation_watermark=0, applied_watermark=0))
    return publications


def create_transform_publications(run, result, detail, *, now):
    if (run.service_id != "transform" or run.tenant_id != result.tenant_id
            or run.run_id != result.run_id or detail.tenant_id != result.tenant_id
            or detail.result_id != result.result_id
            or detail.payload.get("run_id") != run.run_id
            or detail.payload.get("result_id") != result.result_id
            or detail.payload.get("revision") != result.version):
        raise ValueError("Transform result lineage does not match its run")
    scope = detail.payload["scope"]
    if any(scope.get(field) != getattr(run, field) for field in (
            "tenant_id", "product_id", "project_id")):
        raise ValueError("Transform result scope does not match its run")
    if detail.payload.get("source_ref") != run.input.get("sources"):
        raise ValueError("Transform result source differs from the pinned run")
    retained = detail.payload.get("retained_artifacts")
    if not isinstance(retained, dict) or "original_patch" not in retained:
        raise ValueError("Transform result lacks retained original evidence")
    expires = detail.payload.get("retention_expires_at")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        expiry = None
    eligible = (all(scope.get(field) for field in ("relationship_revision", "owner_id", "visibility"))
                and expiry is not None and expiry.tzinfo is not None and expiry > now)
    evidence_refs = [{"kind": name, "artifact": deepcopy(reference)}
                     for name, reference in sorted(retained.items())]
    publications = []
    for destination in ("product-context", "engineering-insights"):
        identity = (f"{run.tenant_id}/{run.product_id}/{run.project_id}/"
                    f"{result.result_id}/{result.version}/{destination}/1.0")
        publication_id = str(uuid5(NAMESPACE_URL, identity))
        payload = {
            "schema_version": "1.0", "kind": "transform_result", "contribution_id": publication_id,
            "scope": deepcopy(scope),
            "result_ref": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                           "source_connection_id": "l5-transform", "record_type": "transform_result",
                           "record_id": result.result_id, "record_revision": str(result.version)},
            "run_id": run.run_id, "result": deepcopy(detail.payload),
            "goal": deepcopy(run.input.get("transform_input", {}).get("goal")),
            "source_refs": deepcopy(run.input["sources"]), "evidence_refs": evidence_refs,
        }
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        publications.append(Publication(
            tenant_id=run.tenant_id, publication_id=publication_id,
            result_id=result.result_id, result_version=result.version,
            destination=destination, schema_version="1.0", contribution_hash=digest,
            payload=payload, state="pending" if eligible else "unavailable", next_due_at=now,
            attempts=0, revision=1, invalidation_watermark=0, applied_watermark=0))
    return publications


def create_wa_publications(run, result, detail, *, now, clients=None):
    if (run.service_id != "well_architected" or run.tenant_id != result.tenant_id
            or run.run_id != result.run_id or detail.tenant_id != result.tenant_id
            or detail.result_id != result.result_id
            or detail.payload.get("revision") != result.version
            or detail.payload.get("review_mode") != "source_only"):
        raise ValueError("architecture publication requires a matching source-only result")
    scope = detail.payload["scope"]
    if any(scope.get(field) != getattr(run, field) for field in (
            "tenant_id", "product_id", "project_id")):
        raise ValueError("architecture publication scope differs from original run")
    sources = run.input.get("sources") or []
    if (result.version > 1 and detail.payload.get("based_on_result_id") is None
            or detail.payload.get("manifest_ref") != (run.manifest or {}).get("archive")):
        raise ValueError("architecture publication lacks pinned result lineage")
    publications = []
    for destination in ("product-context", "engineering-insights"):
        identity = (f"{run.tenant_id}/{result.result_id}/{result.version}/{destination}/1.0")
        publication_id = str(uuid5(NAMESPACE_URL, identity))
        payload = {
            "schema_version": "1.0", "kind": "well_architected_result",
            "contribution_id": publication_id, "scope": deepcopy(scope),
            "result_ref": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                           "source_connection_id": "l5-well-architected",
                           "record_type": "well_architected_result",
                           "record_id": result.result_id, "record_revision": str(result.version)},
            "run_id": run.run_id, "result": deepcopy(detail.payload),
            "source_refs": deepcopy(sources), "evidence_refs": [deepcopy(run.manifest["archive"])],
            "invalidation_watermark": 0,
        }
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        publication = Publication(tenant_id=run.tenant_id, publication_id=publication_id,
                                  result_id=result.result_id, result_version=result.version,
                                  destination=destination, schema_version="1.0", contribution_hash=digest,
                                  payload=payload, state="unavailable", last_error="consumer_schema_unadmitted",
                                  next_due_at=now, attempts=0, revision=1,
                                  invalidation_watermark=0, applied_watermark=0)
        client = (clients or {}).get(destination)
        if (result.version > 1 and client is not None and client.can_publish(publication)
                and all(item["comparison"]["status"] != "not_checked"
                        for item in detail.payload["recommendations"])):
            publication.state = "pending"
            publication.last_error = None
        publications.append(publication)
    return publications


def contribution_pages(publication):
    groups = [[]]
    evidence_count = 0
    for claim in publication.payload["claims"]:
        if len(groups[-1]) == 100 or evidence_count + len(claim["evidence"]) > 100:
            groups.append([])
            evidence_count = 0
        groups[-1].append(claim)
        evidence_count += len(claim["evidence"])
    if len(groups) == 1:
        return [publication.payload]
    return [{**publication.payload, "claims": claims,
             "evidence": [reference for claim in claims for reference in claim["evidence"]],
             "claim_page": {"index": index, "count": len(groups),
                            "total_claims": len(publication.payload["claims"]),
                            "contribution_hash": publication.contribution_hash}}
            for index, claims in enumerate(groups)]


def validate_receipt(publication, receipt):
    if not isinstance(receipt, dict) or any(
            not isinstance(receipt.get(field), str) or not receipt[field]
            for field in ("receipt_id", "contribution_id", "admitted_schema", "consumer_revision")):
        raise ValueError("Context did not return a durable receipt")
    result_contribution = publication.payload.get("kind") in {
        "devops_result", "finops_cost_result", "transform_result", "well_architected_result"}
    if (receipt["contribution_id"] != publication.publication_id
            or receipt["admitted_schema"] != publication.schema_version
            or not isinstance(receipt.get("record_refs"), list) or len(receipt["record_refs"]) > 100
            or (result_contribution and receipt["record_refs"] != [publication.payload["result_ref"]])
            or (not result_contribution and publication.payload["claims"] and not receipt["record_refs"])):
        raise ValueError("Context receipt does not cover this contribution")
    scope = publication.payload["scope"]
    if any(not isinstance(reference, dict) or reference.get("tenant_id") != scope["tenant_id"]
           or reference.get("project_id") != scope["project_id"]
           or any(not isinstance(reference.get(field), str) or not reference[field] for field in (
               "source_connection_id", "record_type", "record_id", "record_revision"))
           for reference in receipt["record_refs"]):
        raise ValueError("Context receipt contains foreign records")
    if (not result_contribution and
            (len(publication.payload["claims"]) > 100 or len(publication.payload["evidence"]) > 100)) and (
            receipt.get("contribution_hash") != publication.contribution_hash
            or type(receipt.get("claim_count")) is not int
            or receipt["claim_count"] != len(publication.payload["claims"])):
        raise ValueError("Context receipt does not cover every claim page")
    return receipt


def acknowledge(publication, receipt):
    validate_receipt(publication, receipt)
    if publication.state not in {"pending", "publishing", "retry_wait"}:
        raise ValueError("publication cannot be acknowledged in this state")
    publication.receipt = receipt
    publication.state = "published"
    publication.revision += 1
