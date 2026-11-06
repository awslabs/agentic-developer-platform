"""Translate selected DevOps readback into scoped, non-authoritative Context evidence."""

from copy import deepcopy


def _source_in_scope(reference, scope):
    return (isinstance(reference, dict)
            and reference.get("tenant_id") == scope["tenant_id"]
            and reference.get("project_id") == scope["project_id"]
            and all(reference.get(field) for field in (
                "source_connection_id", "record_type", "record_id", "record_revision")))


def translate_result(result, findings, *, selected_finding_ids, native_assets=(), topology=None):
    """Prepare an immutable result selection; transport and receipts belong to publication."""
    detail = result.payload
    scope = detail.get("scope") or {}
    if (detail.get("service_id", "devops") != "devops"
            or not all(scope.get(field) for field in ("tenant_id", "product_id", "project_id"))
            or scope.get("tenant_id") != result.tenant_id
            or detail.get("result_id") != result.result_id
            or not detail.get("run_id") or not detail.get("revision")
            or not detail.get("native_job_ref") or not detail.get("observed_at")
            or scope.get("visibility") not in {"only_me", "product_team"}):
        raise ValueError("incomplete scoped DevOps result")
    if not isinstance(selected_finding_ids, (set, frozenset)):
        raise ValueError("explicit finding selection required")

    selected = []
    for finding in findings:
        if finding.finding_id not in selected_finding_ids:
            continue
        payload = finding.payload
        references = payload.get("source_refs")
        if (finding.tenant_id != result.tenant_id or finding.result_id != result.result_id
                or payload.get("native_id") != finding.native_id
                or payload.get("native_job_ref") != detail["native_job_ref"]
                or not isinstance(references, list) or not references
                or not all(_source_in_scope(ref, scope) for ref in references)):
            raise ValueError("selected finding lacks scoped provenance")
        selected.append({"finding_id": finding.finding_id, "native_id": finding.native_id,
                         "kind": finding.kind, "claim": payload["claim"],
                         "validation": payload["validation"],
                         "confidence": payload.get("confidence"),
                         "source_refs": deepcopy(references),
                         "evidence": deepcopy(payload.get("evidence", []))})
    if {item["finding_id"] for item in selected} != selected_finding_ids:
        raise ValueError("selected finding not found in result")

    assets = []
    for asset in native_assets:
        if (asset.get("kind") not in {"memory", "skill"}
                or not asset.get("asset_id") or not asset.get("version")
                or not _source_in_scope(asset.get("source_ref"), scope)
                or asset.get("visibility") != scope["visibility"]):
            raise ValueError("native asset lacks scoped provenance")
        if asset.get("export_status") != "verified" or not asset.get("content_ref"):
            assets.append({"asset_id": asset["asset_id"], "version": asset["version"],
                           "kind": asset["kind"], "status": "unavailable",
                           "reason": "native_export_unqualified"})
            continue
        assets.append({"asset_id": asset["asset_id"], "version": asset["version"],
                       "kind": asset["kind"], "status": "available",
                       "source_ref": deepcopy(asset["source_ref"]),
                       "content_ref": deepcopy(asset["content_ref"]),
                       "confidence": asset.get("confidence"),
                       "visibility": asset["visibility"]})

    available_topology = (isinstance(topology, dict) and topology.get("export_status") == "verified"
                          and topology.get("version") and topology.get("content_ref")
                          and _source_in_scope(topology.get("source_ref"), scope))
    topology_view = ({"status": "available", "version": topology["version"],
                      "content_ref": deepcopy(topology["content_ref"]),
                      "source_ref": deepcopy(topology["source_ref"])} if available_topology
                     else {"status": "unavailable", "reason": "native_export_unqualified"})
    return {"kind": "devops_investigation", "authority": "provider_observation",
            "scope": deepcopy(scope), "visibility": scope["visibility"],
            "result_id": result.result_id, "result_version": detail["revision"],
            "run_id": detail["run_id"], "native_job_ref": deepcopy(detail["native_job_ref"]),
            "observed_at": detail["observed_at"], "findings": selected,
            "topology": topology_view, "native_assets": assets}
