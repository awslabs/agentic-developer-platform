"""Construct bounded results candidates from admitted native facts."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from insights import projection

from .revision import select_logical_revisions


SNAPSHOT_LIMIT = 2 * 1024 * 1024


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("timezone-aware timestamp required")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timezone-aware timestamp required") from None
    if instant.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return instant


def _reference(reference, tenant_id, project_id):
    identity = projection._identity(reference)
    if identity[:2] != (tenant_id, project_id):
        raise ValueError("foreign Project reference")
    return identity


def _shared(record):
    return record.get("visibility", "shared") == "shared" and record.get("audience", "shared") == "shared"


def build_candidate(*, facts, relationships, allocations, tenant_id, project_id,
                    dataset_version, mapping_versions, as_of, source_scope,
                    evidence_scope):
    """Return a reader-shaped candidate and separate quality report, not a publication.

    The caller supplies S04-admitted shared inputs. The publisher must verify
    current source/evidence policy closure before moving a shared pointer.
    Only actual, work-bound charges belong in facts, never estimated usage.
    """
    if (not all(isinstance(value, str) and value and value not in ("latest", "null")
                for value in (tenant_id, project_id, dataset_version, source_scope, evidence_scope))
            or not isinstance(mapping_versions, (list, tuple)) or not mapping_versions
            or any(not isinstance(value, str) or not value or value in ("latest", "null")
                   for value in mapping_versions)):
        raise ValueError("qualified scope, mapping and version required")
    cutoff = _instant(as_of)
    selected = {}
    for fact in select_logical_revisions(facts, as_of):
        identity = _reference(fact["subject_ref"], tenant_id, project_id)
        if (fact.get("kind") not in ("work", "cost")
                or any(_instant(fact[field]) > cutoff for field in ("effective_at", "recorded_at"))
                or not _shared(fact)):
            raise ValueError("admitted shared work or charge required within as-of")
        if fact["kind"] == "cost":
            if fact.get("cost_basis") != "actual_charge":
                raise ValueError("only actual charges enter cost totals")
            _reference(fact["work_ref"], tenant_id, project_id)
            if fact["work_ref"]["record_type"] != "work":
                raise ValueError("charge must be bound to work")
            try:
                amount = Decimal(str(fact["amount"]))
            except (KeyError, InvalidOperation, ValueError):
                raise ValueError("decimal charge required") from None
            currency = fact.get("currency")
            if (not amount.is_finite() or amount < 0 or not isinstance(currency, str)
                    or len(currency) != 3 or not currency.isascii() or not currency.isalpha()
                    or not currency.isupper()):
                raise ValueError("finite nonnegative charge and ISO currency required")
        if identity in selected and selected[identity] != fact:
            raise ValueError("conflicting copies of native fact")
        selected[identity] = fact
    if not {fact["kind"] for fact in selected.values()} >= {"work", "cost"}:
        raise ValueError("complete candidate requires work and attributable charges")
    for fact in selected.values():
        if fact["kind"] == "cost" and projection._identity(fact["work_ref"]) not in selected:
            raise ValueError("charge has no selected work")
    dated = []
    for link in relationships:
        if not _shared(link):
            raise ValueError("private relationship cannot enter shared candidate")
        _reference(link["from_ref"], tenant_id, project_id)
        _reference(link["to_ref"], tenant_id, project_id)
        if _instant(link["recorded_at"]) > cutoff:
            continue
        if link.get("mapping_version") not in mapping_versions:
            raise ValueError("relationship outside declared mapping")
        if any(reference["record_type"] in ("work", "cost")
               and projection._identity(reference) not in selected
               for reference in (link["from_ref"], link["to_ref"])):
            continue
        dated.append(link)
    selected_allocations = []
    for allocation in allocations:
        if not _shared(allocation):
            raise ValueError("private allocation cannot enter shared candidate")
        _reference(allocation["source_ref"], tenant_id, project_id)
        _reference(allocation["target_ref"], tenant_id, project_id)
        if projection._identity(allocation["source_ref"]) in selected:
            selected_allocations.append(allocation)
    snapshot = {"status": "complete", "dataset_version": dataset_version, "selection_as_of": as_of,
                "mapping_versions": sorted(set(mapping_versions)), "facts": list(selected.values()),
                "relationships": dated, "allocations": selected_allocations}
    if len(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()) > SNAPSHOT_LIMIT:
        raise ValueError("candidate exceeds 2 MiB reader limit")
    totals = projection.aggregate_work_cost((fact, ()) for fact in selected.values())
    projected = projection.query_authorized_projection(
        snapshot["facts"], dated, selected_allocations, tenant_id=tenant_id,
        project_ids=[project_id], can_read_project=lambda tenant, project: (
            tenant, project) == (tenant_id, project_id))
    if (projected["coverage"] != "complete" or projected["work_count"] != totals["work_count"]
            or projected["cost_by_currency"] != totals["cost_by_currency"]):
        raise ValueError("candidate lacks complete Product attribution")
    products_by_fact = {
        projection._identity(context["subject_ref"]):
        {projection._identity(product) for product in context["product_refs"]}
        for context in projected["contexts"]
    }
    for fact in selected.values():
        if fact["kind"] == "cost" and not (
                products_by_fact[projection._identity(fact["subject_ref"])]
                & products_by_fact[projection._identity(fact["work_ref"])]):
            raise ValueError("charge and bound work have different Product attribution")
    quality = {"dataset_version": dataset_version, "as_of": as_of,
               "source_scope": source_scope, "evidence_scope": evidence_scope,
               "cost_basis": "actual_charge", "revision_policy": "latest_recorded_at_as_of",
               "coverage": "complete",
               "work_count": totals["work_count"],
               "cost_by_currency": {currency: format(amount, "f")
                                    for currency, amount in totals["cost_by_currency"].items()}}
    return snapshot, quality
