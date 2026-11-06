"""Finite, explicit knowledge publication selection and immutable source admission."""

from copy import deepcopy
from hashlib import sha256

import rfc8785

from l5_services.admission import _verify_snapshot
from l5_services.devops.knowledge_contract import MAPPING_VERSION, native_contract


MAX_SOURCES = 20
MAX_SOURCE_BYTES = 1024 * 1024
WORKER_GAP = "knowledge_worker_unqualified"
WORKER_ACTION = ("Install the current DevOps worker package, review separate native knowledge consent for this "
                 "original operation, then use its reconcile action to resume the retained work. "
                 "No native assets were changed by admission. The owner may instead cancel this operation "
                 "before any native effect starts, including after source consent expires.")
REF_FIELDS = {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}


def digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def publication_selection(policy, tenant_id, project_id):
    """No inferred publication kind, target or cleanup from an empty selection."""
    if not isinstance(policy, dict) or policy.get("refresh") != "manual":
        raise ValueError("knowledge_manual_selection_required")
    if policy.get("visibility") != "product_team":
        raise ValueError("knowledge_private_visibility_unqualified")
    publications, allowed = policy.get("publications"), policy.get("source_refs")
    if (not isinstance(publications, list) or not 1 <= len(publications) <= MAX_SOURCES
            or not isinstance(allowed, list) or len(allowed) != len(publications)):
        raise ValueError("knowledge_selection_missing")
    result, seen, targets = [], set(), set()
    for item in publications:
        if not isinstance(item, dict) or set(item) != {"source_ref", "kind", "scope", "active", "native_metadata"}:
            raise ValueError("knowledge_selection_invalid")
        reference = item["source_ref"]
        if (not isinstance(reference, dict) or set(reference) != REF_FIELDS
                or any(not isinstance(value, str) or not value or len(value) > 512 for value in reference.values())
                or (reference["tenant_id"], reference["project_id"]) != (tenant_id, project_id)
                or reference["record_type"] not in {"document", "runbook", "skill"}
                or reference["record_revision"].casefold() in {"head", "latest", "null"}
                or reference not in allowed):
            raise ValueError("knowledge_source_outside_selection")
        identity = digest({field: value for field, value in reference.items() if field != "record_revision"})
        if identity in seen:
            raise ValueError("knowledge_duplicate_source")
        seen.add(identity)
        _, _, metadata, _ = native_contract(item["kind"], item["scope"], item["active"], policy["visibility"], item["native_metadata"])
        if item["kind"] == "instruction":
            target = metadata["agent_type"]
            if target in targets:
                raise ValueError("knowledge_duplicate_instruction_target")
            targets.add(target)
        result.append({**deepcopy(item), "native_metadata": metadata})
    if any(reference not in [item["source_ref"] for item in result] for reference in allowed):
        raise ValueError("knowledge_source_outside_selection")
    return {"mapping_version": MAPPING_VERSION, "visibility": "product_team", "publications": result,
            "policy_hash": digest(policy), "removals": []}


def admitted_selection(selection, snapshots, deadline):
    publications = selection["publications"]
    if not isinstance(snapshots, list) or len(snapshots) != len(publications):
        raise ValueError("knowledge_source_admission_missing")
    seen = set()
    for publication, snapshot in zip(publications, snapshots):
        _verify_snapshot(snapshot, publication["source_ref"], [], deadline)
        _, _, _, limit = native_contract(publication["kind"], publication["scope"], publication["active"],
                                         selection["visibility"], publication["native_metadata"])
        if (snapshot["snapshot_id"] in seen or snapshot["file_count"] != 1
                or snapshot["content"]["bytes"] > limit or snapshot["file_manifest"]["bytes"] > MAX_SOURCE_BYTES):
            raise ValueError("knowledge_source_admission_invalid")
        seen.add(snapshot["snapshot_id"])
    return {**deepcopy(selection), "source_snapshots": deepcopy(snapshots), "retain_until": deadline.isoformat()}
