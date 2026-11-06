"""Read selected Product work against independent native Well-Architected state."""

from hashlib import sha256
import json
import re
from urllib.parse import quote

from l5_services.adapters.well_architected.source_evidence import read_source_files, validate_citations


class ProductReader:
    def __init__(self, client, scope):
        self.client = client
        self.path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/well_architected")

    def get(self, path):
        response = self.client.get(f"{self.path}/{path}")
        response.raise_for_status()
        return response.json()


def _pages(client, operation, **request):
    token = None
    seen = set()
    for _ in range(20):
        page = getattr(client, operation)(**request, **({"nextToken": token} if token else {}))
        if (not isinstance(page, dict) or not isinstance(page.get("items"), list)
                or any(not isinstance(item, dict) for item in page["items"])):
            raise ValueError("independent native page incomplete")
        yield from page["items"]
        token = page.get("nextToken")
        if not token:
            return
        if not isinstance(token, str) or token in seen:
            raise ValueError("independent native pagination incomplete")
        seen.add(token)
    raise ValueError("independent native pagination exceeded bound")


def read_selected_review(product, delegated, selection):
    """Observe one already submitted review; never infer a paid Start from a receipt."""
    scope = selection["scope"]
    if (not isinstance(scope, dict) or any(not isinstance(scope.get(key), str) or not scope[key]
            for key in ("tenant_id", "product_id", "project_id"))
            or any(not isinstance(selection.get(key), str) or not selection[key]
                   for key in ("binding_id", "run_id", "result_id"))
            or any(type(selection.get(key)) is not int or selection[key] < 1
                   for key in ("configuration_revision", "binding_revision", "run_revision"))
            or type(selection.get("result_revision")) is not int):
        raise ValueError("one pinned Product review selection required")
    config = product.get("configuration")
    binding = product.get("bindings/" + quote(selection["binding_id"], safe=""))
    run = product.get("runs/" + quote(selection["run_id"], safe=""))
    result = product.get("results/" + quote(selection["result_id"], safe=""))
    if any(not isinstance(record, dict) for record in (config, binding, run, result)):
        raise ValueError("Product review readback incomplete")
    admission = run.get("admission") or {}
    source_refs = (run.get("input") or {}).get("sources")
    profile_ref = admission.get("native_profile_ref") or {}
    native_ref = run.get("native_job_ref") or {}
    manifest_ref = result.get("manifest_ref") or {}
    expected_scope = all(record.get("scope", {}).get(key) == value for record in (config, binding, run, result)
                         for key, value in scope.items() if key in {"tenant_id", "product_id", "project_id"})
    if (not expected_scope or config.get("revision") != selection["configuration_revision"]
            or config.get("state") != "ready" or config.get("handoff_state") != "linked"
            or config.get("settings", {}).get("enabled") is not True
            or config["settings"].get("well_architected", {}).get("review_mode") != "source_only"
            or config["settings"].get("source_refs") != source_refs
            or binding.get("binding_id") != selection["binding_id"] or binding.get("state") != "ready"
            or binding.get("revision") != selection["binding_revision"]
            or binding.get("generation") != admission.get("binding_generation")
            or binding.get("configuration_id") != config.get("configuration_id")
            or config.get("hash") != admission.get("configuration_hash")
            or binding.get("native_resource_ref") != profile_ref
            or run.get("run_id") != selection["run_id"] or run.get("state") != "succeeded"
            or run.get("revision") != selection["run_revision"]
            or run.get("input", {}).get("binding_id") != selection["binding_id"]
            or run["input"].get("configuration_revision") != selection["configuration_revision"]
            or not isinstance(source_refs, list) or not source_refs
            or source_refs != selection.get("sources") or admission.get("scope") != scope
            or result.get("result_id") != selection["result_id"]
            or result.get("run_id") != selection["run_id"]
            or result.get("revision") != selection["result_revision"]
            or result.get("kind") != "well_architected_result"
            or result.get("review_mode") != "source_only"
            or result.get("configuration_revision") != config["revision"]
            or result.get("binding_generation") != binding["generation"]
            or result.get("native_job_ref") != native_ref
            or result.get("coverage") is None or not isinstance(result.get("recommendations"), list)
            or not profile_ref.get("record_id") or profile_ref.get("record_revision") != admission.get("profile_hash")
            or native_ref.get("profile_arn") != profile_ref["record_id"]
            or not native_ref.get("generation_id") or not native_ref.get("name")
            or not manifest_ref.get("artifact_id") or not manifest_ref.get("version")):
        raise ValueError("Product review differs from selected scope or revision")
    coverage = result["coverage"]
    if (not isinstance(coverage, list) or not coverage
            or any(not isinstance(item, dict) or item.get("source_ref") not in source_refs
                   or item.get("revision") != item["source_ref"].get("record_revision")
                   or not isinstance(item.get("archive_path"), str)
                   or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64
                   for item in coverage)
            or any(not any(item["source_ref"] == source for item in coverage)
                   for source in source_refs)):
        raise ValueError("Product result lacks selected source-file coverage")
    identity = binding.get("provider_identity") or {}
    if (not all(identity.get(key) == getattr(delegated, key, None)
                for key in ("account_id", "role_arn", "region"))
            or not identity.get("account_id") or not identity.get("role_arn") or not identity.get("region")
            or not re.fullmatch(
                rf"arn:aws:wellarchitected:{re.escape(identity['region'])}:{re.escape(identity['account_id'])}:agent-profile/[A-Za-z0-9_-]+",
                native_ref["profile_arn"])):
        raise ValueError("selected delegated provider identity changed")
    native = delegated.client("wellarchitected")
    source_manifest = run.get("source_manifest")
    if (not isinstance(source_manifest, dict) or source_manifest.get("archive") != manifest_ref
            or source_manifest.get("architecture", {}).get("files") != coverage):
        raise ValueError("selected durable source manifest differs from Product result")
    source_files = read_source_files(source_manifest, getattr(delegated, "read_archive", None))
    profile = native.get_agent_profile(profileArn=native_ref["profile_arn"])
    contexts = list(_pages(native, "list_agent_contexts", profileArn=native_ref["profile_arn"]))
    review = config["settings"]["well_architected"]
    fingerprint = sha256(json.dumps({
        "arn": native_ref["profile_arn"], "aggregation": profile.get("aggregationConfiguration"),
        "role": profile.get("executionRoleArn"), "pillars": profile.get("pillars"),
        "contexts": contexts, "eligible": profile.get("eligibleForArchitectureGeneration"),
        "scheduled": profile.get("eligibleForScheduledGeneration"), "field_errors": profile.get("fieldErrors"),
    }, sort_keys=True, default=str).encode()).hexdigest()
    if (profile.get("arn") != native_ref["profile_arn"] or contexts
            or profile.get("aggregationConfiguration") != []
            or profile.get("executionRoleArn") != review.get("execution_role_ref", {}).get("record_id")
            or profile.get("eligibleForArchitectureGeneration") is not True
            or profile.get("fieldErrors") or fingerprint != admission["profile_hash"]):
        raise ValueError("native Profile no longer matches selected source-only revision")
    generation = native.get_agent_recommendation_generation(
        profileArn=native_ref["profile_arn"], generationId=native_ref["generation_id"])
    context = generation.get("additionalContext") or {}
    if (generation.get("id") != native_ref["generation_id"]
            or generation.get("profileArn") != native_ref["profile_arn"]
            or generation.get("name") != native_ref["name"] or generation.get("status") != "COMPLETED"
            or generation.get("createdBy") != admission.get("generation_creator")
            or generation.get("scope") != admission.get("generation_scope")
            or context.get("s3VersionId") != manifest_ref["version"]
            or context.get("s3Uri") != source_manifest["provider_source_uri"]
            or context.get("lensArns") != ["arn:aws:wellarchitected::aws:lens/wellarchitected"]):
        raise ValueError("independent generation differs from selected Product result")
    recommendations = {}
    for item in _pages(native, "list_agent_recommendations", profileArn=native_ref["profile_arn"],
                       maxResults=1000):
        if item.get("generationId") != native_ref["generation_id"]:
            continue
        recommendation_arn = item.get("recommendationArn")
        if (item.get("profileArn") != native_ref["profile_arn"] or not isinstance(recommendation_arn, str)
                or not re.fullmatch(
                    rf"arn:aws:wellarchitected:{re.escape(identity['region'])}:{re.escape(identity['account_id'])}:agent-recommendation/[0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}",
                    recommendation_arn)
                or recommendation_arn in recommendations):
            raise ValueError("native recommendation has ambiguous attribution")
        detail = native.get_agent_recommendation(recommendationArn=recommendation_arn)
        if (not isinstance(detail, dict) or detail.get("recommendationArn") != recommendation_arn
                or detail.get("profileArn") != native_ref["profile_arn"]
                or detail.get("generationId") != native_ref["generation_id"]):
            raise ValueError("native recommendation detail differs from generation")
        recommendations[recommendation_arn] = detail
    projected = result["recommendations"]
    if (not projected or len(projected) != len(recommendations)
            or {item.get("native_id") for item in projected if isinstance(item, dict)} != set(recommendations)
            or any(item.get("generation_id") != native_ref["generation_id"]
                   or item.get("title") != recommendations[item["native_id"]].get("title")
                   for item in projected)):
        raise ValueError("Product recommendations differ from independent native readback")
    return {"selection": selection, "product": {"configuration": config, "binding": binding,
            "run": run, "result": result}, "native": {"profile": profile, "generation": generation,
            "recommendations": recommendations}, "source_files": source_files,
            "provider_coverage": "provider_coverage_unverified"}


def emit_native_case(observation):
    """Project checked Product/native reads into the native evaluator's source-bound case."""
    selected = observation["selection"]
    product = observation["product"]
    result = product["result"]
    native = observation["native"]
    sources = selected["sources"]
    goals = product["configuration"]["settings"]["well_architected"].get("goal_refs", [])
    archive = result["manifest_ref"]
    source_manifest = product["run"]["source_manifest"]
    context = native["generation"]["additionalContext"]
    if (selected.get("project_kind") != "non_adp" or not goals
            or any(not isinstance(goal, dict)
                   or goal.get("tenant_id") != selected["scope"]["tenant_id"]
                   or goal.get("project_id") != selected["scope"]["project_id"]
                   or goal.get("record_id") not in result.get("goals", []) for goal in goals)
            or result.get("revision") != selected["result_revision"]
            or result.get("run_id") != selected["run_id"]
            or native["generation"].get("id") != result["native_job_ref"]["generation_id"]
            or set(native["recommendations"]) != {item["native_id"]
                                                     for item in result["recommendations"]}
            or observation.get("provider_coverage") != "provider_coverage_unverified"
            or "provider_source_coverage_unverified" not in result.get("limitations", [])
            or source_manifest.get("archive") != archive
            or source_manifest.get("architecture", {}).get("files") != result["coverage"]
            or context.get("s3Uri") != source_manifest.get("provider_source_uri")
            or context.get("s3VersionId") != archive["version"]):
        raise ValueError("selected sources, goals or native coverage limitation unavailable")
    for recommendation in result["recommendations"]:
        validate_citations(recommendation.get("comparison", {}).get("citations", []),
                           observation["source_files"], selected["scope"])
    return {
        "project_kind": selected["project_kind"], "architecture": sources[0], "goal": goals[0],
        "run_id": selected["run_id"], "result_revision": selected["result_revision"],
        "binding_id": product["binding"]["binding_id"], "provider_identity": product["binding"]["provider_identity"],
        "sources": sources,
        "run_input": product["run"]["input"],
        "source_manifest": source_manifest,
        "settings": product["configuration"]["settings"]["well_architected"],
        "native_job_ref": product["run"]["native_job_ref"], "result": result,
        "source_receipt": {"architecture": sources[0], "native_job_ref": result["native_job_ref"],
                           "sources": sources, "files": result["coverage"], "archive": archive,
                           "provider_source_uri": source_manifest["provider_source_uri"],
                           "provider_coverage": "provider_coverage_unverified"},
    }
