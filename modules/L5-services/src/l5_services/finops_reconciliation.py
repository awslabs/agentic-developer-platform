"""Compare signed cost series without dropping credits or rounding provider values."""

import re
from decimal import Decimal, localcontext


AMOUNT = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?\Z")


def decimal_amount(value):
    if not isinstance(value, str) or AMOUNT.fullmatch(value) is None:
        raise ValueError("cost amount must be a decimal string")
    return Decimal(value)


def _amounts(rows, query):
    amounts = {}
    for row in rows:
        if any(row.get(field) != query[field] for field in ("metric", "currency", "granularity", "group_by")):
            raise ValueError("cost dimensions differ from the pinned query")
        period = row["period"]
        if period["start"] < query["start"] or period["end"] > query["end"] or period["start"] >= period["end"]:
            raise ValueError("cost period differs from the pinned query")
        group = tuple(row["group"])
        if len(group) != len(query["group_by"]) or any(not isinstance(part, str) for part in group):
            raise ValueError("cost group differs from the pinned query")
        key = (period["start"], period["end"], group)
        if key in amounts:
            raise ValueError("duplicate cost group")
        amounts[key] = decimal_amount(row["amount"])
    return amounts


def _sum(values):
    if not values:
        raise ValueError("complete cost series requires groups")
    with localcontext() as context:
        context.prec = sum(len(value.as_tuple().digits) + abs(value.as_tuple().exponent) for value in values) + 2
        return sum(values, Decimal(0))


def covers_period(query, rows):
    periods = sorted({(row["period"]["start"], row["period"]["end"]) for row in rows})
    end = query["start"]
    for start, finish in periods:
        if start != end:
            return False
        end = finish
    return end == query["end"]


def compare_cost_series(query, native, billing, *, display_quantum=None):
    """Compare independently sourced groups and totals under the same pinned query."""
    def outcome(state, reason=None, delta=None):
        return {"state": state, "delta": delta, "display_quantum": display_quantum, "reason": reason}

    if native.get("status") in {"refused", "failed", "outcome_unknown", "unavailable"}:
        return outcome("unavailable", "native_answer_unavailable")
    if "unavailable" in (native.get("coverage"), billing.get("coverage")):
        return outcome("unavailable", "source_unavailable")
    if (native.get("coverage") != "complete" or billing.get("coverage") != "complete"
            or native.get("estimated") is None or billing.get("estimated") is None):
        return outcome("incomparable", "incomplete_or_unknown_source")
    if billing.get("freshness_reason") == "stale":
        return outcome("incomparable", "stale_billing")
    if native["estimated"] != billing["estimated"]:
        return outcome("incomparable", "estimated_mismatch")
    if native.get("currency") != query["currency"] or billing.get("currency") != query["currency"]:
        return outcome("incomparable", "currency_mismatch")
    if (not native.get("scope_digest") or not native.get("query_hash")
            or native.get("scope_digest") != billing.get("scope_digest")
            or native.get("query_hash") != billing.get("query_hash")):
        return outcome("incomparable", "scope_or_query_mismatch")
    native_amounts = _amounts(native.get("series") or [], query)
    billing_amounts = _amounts(billing.get("series") or [], query)
    if (not native_amounts or native_amounts.keys() != billing_amounts.keys()
            or not covers_period(query, native.get("series") or [])):
        return outcome("incomparable", "group_coverage_mismatch")
    native_total = _sum(list(native_amounts.values()))
    billing_total = _sum(list(billing_amounts.values()))
    if decimal_amount(native["amount"]) != native_total or decimal_amount(billing["amount"]) != billing_total:
        raise ValueError("cost total differs from complete groups")
    with localcontext() as context:
        values = list(native_amounts.values()) + list(billing_amounts.values())
        context.prec = (sum(len(value.as_tuple().digits) + abs(value.as_tuple().exponent) for value in values)
                        + (len(display_quantum) if display_quantum is not None else 0) + 4)
        differences = [native_amounts[key] - billing_amounts[key] for key in native_amounts]
        delta = native_total - billing_total
        difference_text = format(delta, "f")
        if all(difference == 0 for difference in differences):
            state = "exact"
        elif (display_quantum is not None and decimal_amount(display_quantum) > 0
              and all(abs(difference) <= decimal_amount(display_quantum) / 2 for difference in differences)
              and abs(delta) <= decimal_amount(display_quantum) / 2):
            state = "rounded"
        else:
            state = "mismatch"
        return outcome(state, delta=difference_text)



def unavailable_native_cost(run, query_hash):
    """Preserve a real answer without inventing native billing numbers."""
    answer = run.provider_readback_ref or {}
    if (answer.get("kind") != "finops_native_answer" or answer.get("native_job_ref") != run.native_job_ref
            or answer.get("status") not in {"answered", "refused"}
            or not isinstance(answer.get("answer_artifact"), dict)
            or not answer.get("observed_at")):
        raise ValueError("original native answer is required for unavailable cost")
    return {"status": answer["status"], "coverage": "unavailable", "estimated": None,
            "amount": None, "currency": None, "series": [], "source_refreshed_at": None,
            "query": run.input["query"], "scope_digest": run.admission["billing_scope"]["scope_digest"],
            "query_hash": query_hash, "answer_artifact": answer["answer_artifact"],
            "observed_at": answer["observed_at"]}


def normalize_cost_result(run, native, billing):
    """Build a scoped result only from an original answer and independent evidence."""
    answer = run.provider_readback_ref or {}
    scope = run.admission["billing_scope"]
    artifact = answer.get("answer_artifact")
    if (answer.get("kind") != "finops_native_answer" or not artifact
            or answer.get("native_job_ref") != run.native_job_ref):
        raise ValueError("original native answer identity is required")
    if (native.get("answer_artifact") != artifact or native.get("status") != answer.get("status")
            or native.get("observed_at") != answer.get("observed_at")):
        raise ValueError("parsed native evidence differs from original answer")
    if (scope.get("scope_digest") is None or native.get("scope_digest") != scope["scope_digest"]
            or billing.get("scope_digest") != scope["scope_digest"]
            or billing.get("query") != run.input["query"]
            or native.get("query") != run.input["query"]):
        raise ValueError("cost evidence differs from pinned scope or query")
    if (not scope.get("account_ids") or not scope.get("view_ref")
            or not scope.get("snapshot_artifact") or not billing.get("collection_id")
            or not isinstance(billing.get("pages"), list) or not billing.get("query_hash")
            or billing.get("coverage") == "complete" and not billing["pages"]):
        raise ValueError("independent billing provenance is required")
    for reference in billing["pages"]:
        if (not isinstance(reference, dict) or set(reference) != {"artifact_id", "version", "sha256"}
                or any(not isinstance(reference[key], str) or not reference[key] for key in reference)
                or re.fullmatch(r"[a-f0-9]{64}", reference["sha256"]) is None):
            raise ValueError("billing page evidence is not a versioned artifact")
    comparison = compare_cost_series(run.input["query"], native, billing,
                                     display_quantum=native.get("display_quantum"))

    def measure(source):
        coverage = source.get("coverage")
        estimated = source.get("estimated")
        if coverage not in {"complete", "partial", "unavailable"}:
            raise ValueError("invalid cost coverage")
        if estimated is not None and type(estimated) is not bool:
            raise ValueError("unknown Estimated must be null")
        if source.get("currency") not in {None, run.input["query"]["currency"]}:
            raise ValueError("measure currency differs from pinned query")
        amount = source.get("amount") if coverage == "complete" else None
        if coverage == "complete":
            if amount is None or source is native and native["status"] != "answered":
                raise ValueError("complete amount requires an answered native or billing source")
            values = _amounts(source.get("series") or [], run.input["query"])
            if (not values or not covers_period(run.input["query"], source["series"])
                    or _sum(list(values.values())) != decimal_amount(amount)):
                raise ValueError("complete amount differs from observed groups")
        refreshed_at = source.get("source_refreshed_at")
        freshness_reason = source.get("freshness_reason") or ("source_refresh_unknown" if refreshed_at is None else None)
        return {"amount": amount, "currency": source.get("currency"), "estimated": estimated,
                "source_refreshed_at": refreshed_at, "freshness_reason": freshness_reason,
                "coverage": coverage}

    native_measure = measure(native)
    billing_measure = measure(billing)
    return {"kind": "finops_cost_inquiry", "run_id": run.run_id, "query": run.input["query"],
            "scope_digest": scope["scope_digest"], "binding_generation": run.binding_generation,
            "attribution": "account" if billing_measure["coverage"] == "complete" else "unknown",
            "attribution_evidence": [],
            "native": {"status": native["status"], "conversation_id": run.native_job_ref.get("conversation_id"),
                       "turn_id": run.native_job_ref.get("turn_id"),
                       **({"turn_locator": run.native_job_ref["turn_locator"]}
                          if run.native_job_ref.get("turn_locator") else {}), "answer_artifact": artifact,
                       "observed_at": answer["observed_at"], "measure": native_measure},
            "oracle": {"collection_id": billing["collection_id"], "query_hash": billing["query_hash"],
                       "observed_at": billing.get("observed_at"), "pages": billing["pages"],
                       "measure": billing_measure},
            "comparison": comparison,
            "billing_scope": {"account_ids": scope["account_ids"], "view_ref": scope["view_ref"],
                              "snapshot_artifact": scope["snapshot_artifact"]}}
