"""Compare independent DevOps source, publication and later decision readbacks."""

import hashlib
from decimal import Decimal, InvalidOperation

from qualification import digest, timestamp, validate_manifest


def verify_context(manifest, driver, reader, *, now):
    """A later operation must cite and use current scoped knowledge, not repeat setup."""
    validate_manifest(manifest)
    context = manifest.get("context", {})
    scope = context.get("scope", {})
    sources = context.get("sources", [])
    limits = context.get("limits", {})
    try:
        spend = Decimal(str(limits["max_spend_usd"]))
    except (KeyError, TypeError, InvalidOperation):
        raise ValueError("later operation needs a current bounded allocation") from None
    if (not spend.is_finite() or spend <= 0
            or type(limits.get("max_requests")) is not int or not 1 <= limits["max_requests"] <= 50
            or type(limits.get("deadline_seconds")) is not int
            or not 30 <= limits["deadline_seconds"] <= 900):
        raise ValueError("later operation needs bounded spend, requests and time")
    if (not all(scope.get(field) for field in ("tenant_id", "product_id", "project_id"))
            or not isinstance(sources, list) or not sources
            or any(not isinstance(source, dict) or not source.get("record_id")
                   or not source.get("record_revision") or not digest(source.get("sha256"), 64)
                   or source.get("record_revision", "").lower() in {"head", "latest", "null"}
                   or any(source.get(field) != scope[field] for field in ("tenant_id", "project_id"))
                   for source in sources)
            or not context.get("result_ref") or not context.get("publication_ref")
            or not context.get("asset_ref") or not context.get("intent")):
        raise ValueError("later Context use needs scoped, pinned source and result selections")
    result = reader.read_result(scope, context["result_ref"])
    if (not isinstance(result, dict) or result.get("scope") != scope or result.get("status") != "completed"
            or result.get("source_refs") != sources or not result.get("native_job_id")
            or not result.get("finding_id") or result.get("result_ref") != context["result_ref"]):
        raise ValueError("selected Context result lacks independently verified native findings")
    native = reader.read_native_job(scope, result["native_job_id"])
    if (not isinstance(native, dict) or native.get("scope") != scope
            or native.get("status") != "completed" or native.get("job_id") != result["native_job_id"]
            or native.get("finding_id") != result["finding_id"]
            or native.get("source_refs") != sources):
        raise ValueError("provider did not independently confirm selected native findings")
    for source in sources:
        observed = reader.read_source(scope, source)
        if (not isinstance(observed, dict) or observed.get("record_revision") != source["record_revision"]
                or not isinstance(observed.get("content"), bytes)
                or hashlib.sha256(observed["content"]).hexdigest() != source["sha256"]
                or observed.get("scope") != scope
                or observed.get("revoked") is not False):
            raise ValueError("selected source is stale, revoked or from another project")
    publication = reader.read_publication(scope, context["publication_ref"])
    asset = reader.read_asset(scope, context["asset_ref"])
    if (not isinstance(publication, dict) or publication.get("scope") != scope
            or publication.get("state") != "published"
            or publication.get("result_ref") != result["result_ref"]
            or publication.get("source_refs") != sources
            or publication.get("asset_ref") != context["asset_ref"]
            or not isinstance(asset, dict) or asset.get("scope") != scope
            or asset.get("status") != "active" or asset.get("source_refs") != sources
            or not asset.get("version") or asset.get("ref") != context["asset_ref"]):
        raise ValueError("Context publication or native knowledge differs from selected source")
    receipt = reader.read_receipt(scope, context["publication_ref"])
    if (not isinstance(receipt, dict) or receipt.get("contribution_ref") != context["publication_ref"]
            or receipt.get("result_ref") != result["result_ref"]
            or receipt.get("consumer_revision") != publication.get("consumer_revision")):
        raise ValueError("independent Context receipt does not match published result")
    visibility = reader.read_visibility(scope, context["publication_ref"])
    if (not isinstance(visibility, dict) or visibility.get("scope") != scope
            or visibility.get("status") != "active" or not visibility.get("revision")):
        raise ValueError("Context access lacks current authorized visibility")
    baseline = reader.read_baseline(scope, context["intent"])
    if (not isinstance(baseline, dict) or baseline.get("scope") != scope
            or baseline.get("intent") != context["intent"]
            or baseline.get("source_refs") != sources or baseline.get("citations") != []
            or not baseline.get("run_id") or not baseline.get("decision")
            or timestamp(baseline.get("observed_at")) >= timestamp(publication.get("published_at"))):
        raise ValueError("independent baseline must precede the published contribution")
    if timestamp(publication["published_at"]) > now:
        raise ValueError("publication is in the future")
    submitted = driver.run_later(scope, context["intent"], context["publication_ref"], limits)
    if not isinstance(submitted, dict) or not submitted.get("run_id"):
        raise ValueError("later operation was not submitted")
    later = reader.read_later(scope, submitted["run_id"])
    citation = {"contribution_ref": context["publication_ref"], "result_ref": result["result_ref"],
                "finding_id": result["finding_id"], "source_refs": sources,
                "asset_ref": context["asset_ref"], "asset_version": asset["version"]}
    if (not isinstance(later, dict) or later.get("scope") != scope or later.get("status") != "completed"
            or later.get("run_id") != submitted["run_id"] or later["run_id"] == baseline["run_id"]
            or later.get("intent") != baseline["intent"] or later.get("source_refs") != sources
            or later.get("decision") == baseline["decision"] or not later.get("decision")
            or later.get("citations") != [citation]
            or later.get("used_contributions") != [context["publication_ref"]]
            or later.get("visibility_revision") != visibility["revision"]
            or not timestamp(publication["published_at"]) < timestamp(later.get("observed_at")) <= now):
        raise ValueError("later decision did not use the current scoped citation")
    usage = reader.read_usage(scope, submitted["run_id"])
    try:
        spent = Decimal(str(usage["spend_usd"]))
    except (KeyError, TypeError, InvalidOperation):
        raise ValueError("later Context work needs measured usage") from None
    if (not spent.is_finite() or not 0 <= spent <= Decimal(str(limits["max_spend_usd"]))
            or type(usage.get("requests")) is not int
            or not 0 <= usage["requests"] <= limits["max_requests"]
            or type(usage.get("elapsed_seconds")) is not int
            or not 0 <= usage["elapsed_seconds"] <= limits["deadline_seconds"]):
        raise ValueError("later Context operation exceeded authorized budget")
    for denial in ("revoked", "irrelevant"):
        readback = reader.read_denial(scope, submitted["run_id"], denial)
        if (not isinstance(readback, dict) or readback.get("status") not in {403, 404}
                or readback.get("disclosed_resources") != []):
            raise ValueError("revoked or irrelevant Context input was not excluded")
    return {"result_ref": result["result_ref"], "later_run_id": later["run_id"],
            "publication_ref": context["publication_ref"], "citation": citation}
