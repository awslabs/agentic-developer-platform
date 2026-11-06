"""Source-only native recommendation projection and independent source verification."""

import io
import json
import logging
import zipfile
from datetime import UTC
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from l5_services.models.evidence import AssessmentResult
from l5_services.models.finding import Finding
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from l5_services.models.generation_collection import GenerationCollection


LOGGER = logging.getLogger(__name__)


def normalize_wa_result(run, result_id, version, collected, now):
    native = collected["native_job_ref"]
    if native != run.native_job_ref or not isinstance(collected.get("recommendations"), list):
        raise ValueError("result does not match original generation")
    recommendations = []
    for item in collected["recommendations"]:
        if (item.get("profileArn") != native["profile_arn"]
                or item.get("generationId") != native["generation_id"]
                or not item.get("recommendationArn")):
            raise ValueError("recommendation does not match original generation")
        recommendations.append({
            "native_id": item["recommendationArn"], "generation_id": item["generationId"],
            "pillar": item.get("pillar"), "title": item.get("title"),
            "description": item.get("description"), "priority": item.get("priority"),
            "effort": item.get("effort"), "impact": item.get("impact"),
            "tradeoffs": item.get("tradeOffs", []),
            "comparison": {"status": "not_checked", "explanation": "independent source verification pending",
                           "citations": []},
        })
    manifest = run.manifest or {}
    goals = ([reference["record_id"] for reference in run.admission["goal_refs"]]
             if run.admission.get("goal_refs") else run.admission.get("generation_scope", {}).get("goalIds", []))
    limitations = ["source_only: deployed state was not inspected"]
    if not goals:
        limitations.append("goals_not_pinned")
    if not recommendations:
        limitations.append("no_attributable_recommendations")
    if manifest.get("provider_coverage") != "verified":
        limitations.append("provider_source_coverage_unverified")
    projection = {
        "schema_version": "1.0", "kind": "well_architected_result",
        "result_id": result_id, "run_id": run.run_id, "revision": version,
        "scope": run.admission["scope"], "review_mode": "source_only",
        "configuration_revision": run.configuration_revision,
        "binding_generation": run.binding_generation,
        "native_job_ref": native, "model_hash": run.admission.get("generation_model_hash"),
        "manifest_ref": manifest.get("archive"),
        "pillars": run.admission.get("generation_scope", {}).get("pillars", []),
        "goals": goals, "coverage": manifest.get("architecture", {}).get("files", []),
        "limitations": limitations, "recommendations": recommendations,
        "recommendation_count": len(recommendations),
        "excluded_count": len(collected.get("excluded", [])),
        "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    return ResultDetail(tenant_id=run.tenant_id, result_id=result_id, payload=projection)


def wa_recommendation_findings(detail):
    """Give normalized recommendations typed, result-scoped triage identities."""
    scope = detail.payload["scope"]
    result_id = detail.result_id
    return [Finding(
        tenant_id=scope["tenant_id"], result_id=result_id,
        finding_id=str(uuid5(NAMESPACE_URL, f"{scope['tenant_id']}/{result_id}/{item['native_id']}")),
        kind="wa_recommendation", native_id=item["native_id"],
        payload={"kind": "wa_recommendation", "claim": item["title"], **item},
    ) for item in detail.payload["recommendations"]]


def _source_files(manifest, archived):
    archive_ref = manifest.get("archive") or {}
    architecture = manifest.get("architecture") or {}
    if (not isinstance(archived, bytes) or len(archived) != archive_ref.get("bytes")
            or sha256(archived).hexdigest() != archive_ref.get("sha256")
            or manifest.get("provider_coverage") not in {"verified", "provider_coverage_unverified"}
            or not architecture.get("files")):
        raise ValueError("pinned source archive or coverage unavailable")
    files = {}
    with zipfile.ZipFile(io.BytesIO(archived)) as archive:
        if json.loads(archive.read("manifest.json")) != architecture:
            raise ValueError("source archive manifest changed")
        for entry in architecture["files"]:
            path = entry.get("archive_path")
            if path is None:
                continue
            if (not path.startswith("sources/") or path in files
                    or path not in archive.namelist() or entry.get("bytes", 0) > 8 * 1024 * 1024):
                raise ValueError("pinned source file unavailable")
            with archive.open(path) as source:
                content = source.read(entry["bytes"] + 1)
            if len(content) != entry["bytes"] or sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("pinned source file changed")
            files[path] = {"entry": entry, "lines": content.decode("utf-8").splitlines()}
    return files


def compare_wa_recommendations(run, collected, archived, verifier):
    """Accept verdicts from a separate verifier only after verifying pinned citations."""
    if not callable(verifier) or collected.get("native_job_ref") != run.native_job_ref:
        raise ValueError("independent verifier and original generation required")
    files = _source_files(run.manifest, archived)
    scope = run.admission["scope"]
    goals = ([reference["record_id"] for reference in run.admission["goal_refs"]]
             if run.admission.get("goal_refs") else run.admission.get("generation_scope", {}).get("goalIds", []))
    comparisons = []
    for recommendation in collected["recommendations"]:
        if (recommendation.get("generationId") != run.native_job_ref["generation_id"]
                or recommendation.get("profileArn") != run.native_job_ref["profile_arn"]):
            raise ValueError("recommendation belongs to another generation")
        verdict = verifier(recommendation, files, goals)
        if not isinstance(verdict, dict) or verdict.get("status") not in {
                "supported", "contradicted", "not_verifiable", "not_checked"}:
            raise ValueError("invalid independent verdict")
        citations = verdict.get("citations", [])
        if not isinstance(citations, list):
            raise ValueError("invalid source citations")
        validated = []
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("archive_path") not in files:
                raise ValueError("citation is not in pinned sources")
            entry = files[citation["archive_path"]]["entry"]
            lines = files[citation["archive_path"]]["lines"]
            line = citation.get("line")
            if (entry["source_ref"].get("tenant_id") != scope["tenant_id"]
                    or entry["source_ref"].get("project_id") != scope["project_id"]
                    or entry["revision"] != entry["source_ref"].get("record_revision")
                    or not isinstance(line, int) or isinstance(line, bool)
                    or line < 1 or line > len(lines) or citation.get("quote") != lines[line - 1]):
                raise ValueError("citation does not match pinned source line")
            validated.append({"source_ref": entry["source_ref"], "revision": entry["revision"],
                              "path": entry["path"], "sha256": entry["sha256"],
                              "line": line, "quote": citation["quote"]})
        status = verdict["status"]
        if status in {"supported", "contradicted"} and (
                not validated or not verdict.get("verifier_ref")):
            status = "not_verifiable"
        comparisons.append({"native_id": recommendation["recommendationArn"],
                            "status": status,
                            "explanation": verdict.get("explanation") or "no cited source evidence",
                            "citations": validated, "verifier_ref": verdict.get("verifier_ref")})
    return comparisons


async def persist_wa_comparison(sessions, tenant_id, run_id, archive_store, verifier, *, now, clients=None):
    """Commit a separate compared revision after the native result is durable."""
    async with sessions() as session:
        run = await session.get(Run, (tenant_id, run_id), with_for_update=True)
        if (run is None or run.service_id != "well_architected"
                or run.provider_state != "completed" or run.native_job_ref is None):
            raise ValueError("completed original native generation required")
        existing = await session.scalar(select(AssessmentResult).where(
            AssessmentResult.tenant_id == tenant_id, AssessmentResult.run_id == run_id,
            AssessmentResult.version == 2))
        if existing is not None:
            return existing.result_id
        if archive_store is None or not callable(getattr(archive_store, "get_archive", None)):
            raise ValueError("qualified pinned archive reader required")
        native = await session.scalar(select(AssessmentResult).where(
            AssessmentResult.tenant_id == tenant_id, AssessmentResult.run_id == run_id,
            AssessmentResult.version == 1))
        collection = await session.get(GenerationCollection, (tenant_id, run_id))
        if (native is None or collection is None or collection.result_id != native.result_id
                or native.payload.get("native_job_ref") != run.native_job_ref
                or not isinstance(native.payload.get("recommendations"), list)):
            raise ValueError("durable original collection required before comparison")
        archived = await archive_store.get_archive(run_id, run.manifest["archive"])
        comparisons = compare_wa_recommendations(run, native.payload, archived, verifier)
        result_id = str(uuid4())
        detail = normalize_wa_result(run, result_id, 2, native.payload, now)
        projection = detail.payload
        projection["based_on_result_id"] = native.result_id
        projection["comparison"] = comparisons
        projection["recommendations"] = [
            {**recommendation, "comparison": comparison}
            for recommendation, comparison in zip(projection["recommendations"], comparisons, strict=True)]
        detail.payload = projection
        assessment = AssessmentResult(tenant_id=tenant_id, result_id=result_id, run_id=run_id,
                                      version=2, payload={**native.payload, "comparison": comparisons,
                                                          "based_on_result_id": native.result_id})
        session.add(assessment)
        session.add(detail)
        session.add_all(wa_recommendation_findings(detail))
        from l5_services.publication.contributions import create_wa_publications

        session.add_all(create_wa_publications(run, assessment, detail, now=now, clients=clients))
        await session.commit()
        return result_id


async def repair_wa_comparisons(service, *, now, limit=50):
    """Recover post-native, pre-cleanup comparison work without another provider call."""
    verifier = getattr(service, "well_architected_verifier", None)
    archive_store = getattr(service, "archive_store", None)
    if verifier is None or archive_store is None:
        return 0
    if not 1 <= limit <= 50:
        raise ValueError("comparison repair limit must be between 1 and 50")
    native = aliased(AssessmentResult)
    compared = aliased(AssessmentResult)
    async with service.sessions() as session:
        candidates = (await session.execute(select(Run.tenant_id, Run.run_id).join(
            native, (native.tenant_id == Run.tenant_id) & (native.run_id == Run.run_id)
        ).where(Run.service_id == "well_architected", Run.provider_state == "completed",
                native.version == 1, ~exists(select(compared.result_id).where(
                    compared.tenant_id == native.tenant_id, compared.run_id == native.run_id,
                    compared.version == 2))).order_by(Run.tenant_id, Run.run_id).limit(limit))).all()
    clients = {"product-context": getattr(service, "well_architected_context_client", None),
               "engineering-insights": getattr(service, "well_architected_insights_client", None)}
    completed = 0
    for tenant_id, run_id in candidates:
        try:
            await persist_wa_comparison(service.sessions, tenant_id, run_id, archive_store, verifier,
                                        now=now, clients=clients)
            completed += 1
        except (ValueError, TimeoutError, ConnectionError):
            LOGGER.warning("WA comparison remains owed; original native result stays durable")
    return completed
