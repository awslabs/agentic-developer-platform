"""Match installed WA results and comparisons to read-only native responses."""

import base64
import re

from l5_services.installed_release_probe import ReleaseProofError, require
from l5_services.wa_portability import verify_portable_configurations
from l5_services.adapters.well_architected.source_evidence import read_source_files, validate_citations


def verify_portable_native_reviews(cases, fixture, read, *, read_archive=None):
    try:
        release = verify_portable_configurations(cases)
    except ValueError as error:
        raise ReleaseProofError("wa_native_configuration_invalid") from error
    pinned = {
        "wheel": fixture.wheel_sha256,
        "worker": base64.b64decode(fixture.worker_code_sha256, validate=True).hex(),
        "schema": fixture.schema_sha256,
        "gateway": fixture.gateway_image_digest,
        "frontend": fixture.frontend_asset_sha256,
        "cli": fixture.cli_manifest_sha256,
    }
    require(release == pinned, "wa_native_release_changed")
    for case in cases:
        result = case.get("result")
        scope = result.get("scope") if isinstance(result, dict) else None
        architecture = case["architecture"]
        require(isinstance(scope, dict) and scope.get("tenant_id") == architecture["tenant_id"]
                and scope.get("project_id") == architecture["project_id"],
                "wa_native_configuration_scope_changed")
    return verify_native_reviews(cases, fixture, read, read_archive=read_archive)


def verify_native_reviews(cases, fixture, read, *, read_archive=None):
    require(isinstance(cases, list) and len(cases) == 2, "wa_native_two_projects_required")
    region = fixture.queue_arn.split(":")[3]
    profile_prefix = f"arn:aws:wellarchitected:{region}:{fixture.account_id}:agent-profile/"
    recommendation_prefix = f"arn:aws:wellarchitected:{region}:{fixture.account_id}:agent-recommendation/"
    identities = set()
    for case in cases:
        require(isinstance(case, dict) and case.get("project_kind") == "non_adp", "wa_native_non_adp_required")
        native, result, source = (case.get(name) for name in ("native_job_ref", "result", "source_receipt"))
        require(all(isinstance(part, dict) for part in (native, result, source)), "wa_native_receipts_missing")
        scope = result.get("scope")
        require(isinstance(scope, dict) and all(isinstance(scope.get(name), str) and scope[name]
                                               for name in ("tenant_id", "project_id")), "wa_native_scope_missing")
        profile, generation_id = native.get("profile_arn"), native.get("generation_id")
        require(isinstance(profile, str) and re.fullmatch(re.escape(profile_prefix) + r"[A-Za-z0-9_-]+", profile)
                and isinstance(generation_id, str) and generation_id
                and native.get("tenant_id") == scope["tenant_id"]
                and native.get("project_id") == scope["project_id"]
                and result.get("native_job_ref") == native
                and type(case.get("result_revision")) is int and case["result_revision"] > 0
                and result.get("revision") == case["result_revision"]
                and isinstance(case.get("run_id"), str) and case["run_id"]
                and result.get("run_id") == case.get("run_id")
                and result.get("kind") == "well_architected_result"
                and result.get("review_mode") == "source_only", "wa_native_identity_changed")
        identity = (scope["tenant_id"], scope["project_id"], profile, generation_id)
        require(identity not in identities, "wa_native_generation_reused")
        identities.add(identity)
        architecture, goal = case.get("architecture"), case.get("goal")
        require(isinstance(architecture, dict) and isinstance(goal, dict)
                and architecture.get("tenant_id") == goal.get("tenant_id") == scope["tenant_id"]
                and architecture.get("project_id") == goal.get("project_id") == scope["project_id"]
                and goal.get("record_id") in result.get("goals", []), "wa_native_goals_changed")
        archive = source.get("archive")
        sources, files = source.get("sources"), source.get("files")
        require(source.get("architecture") == architecture and source.get("native_job_ref") == native
                and source.get("provider_coverage") == "provider_coverage_unverified"
                and "provider_source_coverage_unverified" in result.get("limitations", [])
                and isinstance(archive, dict) and archive == result.get("manifest_ref")
                and isinstance(archive.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
                and isinstance(archive.get("version"), str) and archive["version"]
                and isinstance(archive.get("artifact_id"), str) and archive["artifact_id"]
                and type(archive.get("bytes")) is int and 0 < archive["bytes"] <= 25 * 1024 * 1024
                and isinstance(sources, list) and sources and architecture in sources
                and sources == case.get("run_input", {}).get("sources")
                and all(isinstance(selected, dict)
                        and selected.get("tenant_id") == scope["tenant_id"]
                        and selected.get("project_id") == scope["project_id"]
                        and isinstance(selected.get("record_revision"), str)
                        and selected["record_revision"] not in {"", "latest", "HEAD"}
                        for selected in sources)
                and isinstance(files, list) and files and files == result.get("coverage")
                and all(isinstance(item, dict) and item.get("source_ref") in sources
                        and item.get("revision") == item["source_ref"].get("record_revision")
                        and isinstance(item.get("archive_path"), str) and item["archive_path"]
                        and isinstance(item.get("sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in files)
                and all(any(item["source_ref"] == selected for item in files) for selected in sources),
                "wa_native_source_unverified")
        manifest = case.get("source_manifest")
        require(isinstance(manifest, dict) and manifest.get("archive") == archive
                and manifest.get("architecture", {}).get("files") == files
                and manifest.get("provider_source_uri") == source.get("provider_source_uri"),
                "wa_native_source_unverified")
        try:
            source_files = read_source_files(manifest, read_archive)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ReleaseProofError("wa_native_source_unverified") from error
        generation = read("wellarchitected", "get-agent-recommendation-generation",
                          "--profile-arn", profile, "--generation-id", generation_id)
        context = generation.get("additionalContext")
        require(generation.get("id") == generation_id and generation.get("profileArn") == profile
                and generation.get("status") == "COMPLETED"
                and generation.get("name") == native.get("name")
                and isinstance(context, dict)
                and isinstance(context.get("s3Uri"), str)
                and context["s3Uri"] == source.get("provider_source_uri")
                and context["s3Uri"].startswith("s3://")
                and context["s3Uri"].endswith("/" + archive["artifact_id"])
                and context.get("s3VersionId") == archive["version"]
                and context.get("lensArns") == ["arn:aws:wellarchitected::aws:lens/wellarchitected"],
                "wa_native_generation_unverified")
        recommendations = result.get("recommendations")
        require(isinstance(recommendations, list) and 1 <= len(recommendations) <= 20,
                "wa_native_recommendations_missing")
        seen = set()
        for recommendation in recommendations:
            require(isinstance(recommendation, dict), "wa_native_recommendation_unverified")
            arn = recommendation.get("native_id")
            comparison = recommendation.get("comparison")
            require(isinstance(arn, str) and re.fullmatch(re.escape(recommendation_prefix)
                    + r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", arn)
                    and arn not in seen and recommendation.get("generation_id") == generation_id
                    and isinstance(comparison, dict)
                    and comparison.get("status") in {"supported", "contradicted"}
                    and isinstance(comparison.get("citations"), list) and comparison["citations"],
                    "wa_native_comparison_unverified")
            seen.add(arn)
            require(all(isinstance(citation, dict) and citation.get("source_ref") in sources
                        and isinstance(citation.get("quote"), str) and citation["quote"]
                        and isinstance(citation.get("sha256"), str)
                        and any(file["source_ref"] == citation["source_ref"]
                                and file["sha256"] == citation["sha256"] for file in files)
                        for citation in comparison["citations"]), "wa_native_citation_unverified")
            try:
                validate_citations(comparison["citations"], source_files, scope)
            except ValueError as error:
                raise ReleaseProofError("wa_native_citation_unverified") from error
            detail = read("wellarchitected", "get-agent-recommendation", "--recommendation-arn", arn)
            require(detail.get("recommendationArn") == arn
                    and detail.get("profileArn") == profile
                    and detail.get("generationId") == generation_id
                    and detail.get("title") == recommendation.get("title")
                    and isinstance(detail.get("sources"), list) and detail["sources"],
                    "wa_native_recommendation_unverified")
    return identities
