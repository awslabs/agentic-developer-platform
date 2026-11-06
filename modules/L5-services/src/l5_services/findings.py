"""Normalize sealed provider findings without changing native evidence."""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from l5_services.models.finding import Finding
from l5_services.models.publication import Publication
from l5_services.models.publication_retry import PublicationRetryReceipt
from l5_services.models.result import ResultDetail
from l5_services.models.wa_usage import WellArchitectedUsage


def _source_identity(source):
    return tuple(source.get(field) for field in (
        "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"
    ))


def _findings_bytes(findings):
    rows = sorted(({"finding_id": finding.finding_id, **finding.payload} for finding in findings),
                  key=lambda item: item["finding_id"])
    return json.dumps({"items": rows}, sort_keys=True, separators=(",", ":")).encode()


def normalize_result(run, result_id, version, collected, now):
    if run.service_id == "devops" and run.input.get("capability") == "investigation":
        from l5_services.devops.results import normalize_investigation_result

        return normalize_investigation_result(run, result_id, version, collected, now)
    sources = (run.manifest.get("sources") or [] if run.service_id == "devops"
               and run.input.get("capability") == "release_readiness" else run.input.get("sources") or [])
    pinned = {_source_identity(source) for source in sources}
    normalized = []
    seen = {}
    native_job_ref = run.native_job_ref or collected.get("native_job_ref")
    if native_job_ref is None:
        raise ValueError("collected result has no native job identity")
    for raw in collected.get("findings") or []:
        native_id = raw.get("native_id")
        kind = raw.get("kind")
        if not isinstance(native_id, str) or not native_id or kind not in {"candidate_threat", "static_finding"}:
            raise ValueError("finding requires a native identity and supported kind")
        finding_sources = raw.get("source_refs", sources)
        if any(_source_identity(source) not in pinned for source in finding_sources):
            raise ValueError("finding cites a source outside the pinned run")
        validation = raw.get("validation", "candidate" if kind == "candidate_threat" else "unverified")
        if validation not in {"candidate", "supported", "contradicted", "unverified"}:
            raise ValueError("unsupported provider validation")
        payload = {
            "native_id": native_id, "native_job_ref": native_job_ref, "kind": kind,
            "claim": raw["claim"], "provider_severity": raw["provider_severity"],
            "validation": validation, "source_refs": finding_sources,
            "evidence": raw.get("evidence", []), "locations": raw.get("locations", []),
            "assumptions": raw.get("assumptions", []), "mitigations": raw.get("mitigations", []),
        }
        key = (kind, native_id)
        if key in seen:
            if seen[key] != payload:
                raise ValueError("conflicting observations for native finding")
            continue
        seen[key] = payload
        normalized.append(Finding(tenant_id=run.tenant_id, result_id=result_id, finding_id=str(uuid4()),
                                  kind=kind, native_id=native_id, payload=payload))
    coverage = collected.get("coverage", "unknown")
    if coverage not in {"complete", "partial", "unknown"}:
        raise ValueError("invalid provider coverage")
    if ("finding_count" in collected and
            (type(collected["finding_count"]) is not int or collected["finding_count"] != len(normalized))):
        raise ValueError("collected finding count does not match readback")
    if "findings" not in collected and collected.get("finding_count") != 0:
        coverage = "unknown"
    costs = collected.get("costs", [{"kind": "unknown", "currency": "USD"}])
    if any(cost.get("kind") == "unknown" and "amount" in cost for cost in costs):
        raise ValueError("unknown cost must not have an amount")
    scope = run.admission.get("scope") or {
        "tenant_id": run.tenant_id, "product_id": run.product_id, "project_id": run.project_id,
        "relationship_revision": "unknown", "owner_id": "unknown", "visibility": "only_me",
    }
    projection = {
        "result_id": result_id, "run_id": run.run_id, "scope": scope,
        "revision": version, "native_job_ref": native_job_ref,
        "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "coverage": coverage, "coverage_mode": collected.get("coverage_mode", "native_observation"),
        "finding_count": len(normalized), "costs": costs,
    }
    exported = _findings_bytes(normalized)
    if len(exported) <= 8 * 1024 * 1024:
        projection["findings_artifact"] = {
            "artifact_id": f"findings-{result_id}", "version": str(version),
            "sha256": sha256(exported).hexdigest(), "media_type": "application/json", "bytes": len(exported),
        }
    if run.service_id == "devops" and run.input.get("capability") == "release_verification":
        projection["service_id"] = "devops"
        projection["admission"] = run.admission
        for key in ("scenarios", "deployment_ref", "source_ref", "independent_observations", "provider_outcome", "cleanup", "deployed_sha", "native_task_id"):
            projection[key] = collected[key]
    for key in ("manifest_ref", "evidence_ref"):
        if key in collected:
            projection[key] = collected[key]
    return ResultDetail(tenant_id=run.tenant_id, result_id=result_id, payload=projection), normalized


class ResultStore:
    def __init__(self, session, runs, *, actor_id=None, clock=None,
                 transform_artifact_store=None, artifact_authority=None, service_id=None,
                 wa_source_authority=None, wa_context_client=None, wa_insights_client=None,
                 finops_answer_reader=None, contribution_authority=None, task_authority=None, wa_reuse_authority=None):
        self.session = session
        self.runs = {run.run_id: run for run in runs}
        self.actor_id = actor_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transform_artifact_store = transform_artifact_store
        self.finops_answer_reader = finops_answer_reader
        self.artifact_authority = artifact_authority
        self.service_id = service_id
        self.wa_source_authority = wa_source_authority
        self.wa_context_client = wa_context_client
        self.wa_insights_client = wa_insights_client
        self.contribution_authority = contribution_authority
        self.task_authority = task_authority
        self.wa_reuse_authority = wa_reuse_authority

    async def _authorize_contribution(self, run, publication):
        # The initial run list is only a selection snapshot. The host must check
        # current authority and the durable publication again before releasing a copy.
        if (not isinstance(self.actor_id, str) or not self.actor_id
                or not callable(self.contribution_authority)
                or await self.contribution_authority(run, publication) is not True):
            raise HTTPException(404, detail={"code": "not_found"})

    @property
    def wa_reader_ready(self):
        return callable(self.wa_source_authority)

    async def _wa_authorize(self, run):
        if run.service_id != "well_architected":
            return
        if (not self.wa_reader_ready
                or run.tenant_id != (run.admission or {}).get("scope", {}).get("tenant_id")
                or run.product_id != (run.admission or {}).get("scope", {}).get("product_id")
                or run.project_id != (run.admission or {}).get("scope", {}).get("project_id")
                or not (run.input or {}).get("sources")
                or await self.wa_source_authority(run) is not True):
            raise HTTPException(404, detail={"code": "not_found"})
        from l5_services.models.source_retention import SourceRetention
        from l5_services.models.evidence import AssessmentResult

        revoked = await self.session.scalar(select(SourceRetention.artifact_id).where(
            SourceRetention.tenant_id == run.tenant_id,
            SourceRetention.run_id == run.run_id,
            SourceRetention.revoked_at.is_not(None)).limit(1))
        watermark = await self.session.scalar(select(Publication.publication_id).join(
            AssessmentResult,
            (Publication.tenant_id == AssessmentResult.tenant_id)
            & (Publication.result_id == AssessmentResult.result_id),
        ).where(AssessmentResult.tenant_id == run.tenant_id,
                AssessmentResult.run_id == run.run_id,
                Publication.invalidation_watermark > 0).limit(1))
        if revoked is not None or watermark is not None:
            raise HTTPException(404, detail={"code": "not_found"})

    @property
    def transform_reader_ready(self):
        return (self.actor_id is not None and self.transform_artifact_store is not None
                and self.artifact_authority is not None)

    async def publication(self, publication_id):
        if not self.runs:
            raise HTTPException(404, detail={"code": "not_found"})
        tenant_id = next(iter(self.runs.values())).tenant_id
        publication = await self.session.get(Publication, (tenant_id, publication_id))
        if publication is None:
            raise HTTPException(404, detail={"code": "not_found"})
        await self.get(publication.result_id)
        receipt = publication.receipt or {}
        result = {"publication_id": publication.publication_id, "result_id": publication.result_id,
                  "state": publication.state, "contribution_hash": publication.contribution_hash,
                  "invalidation_watermark": publication.invalidation_watermark}
        if publication.state == "published":
            result["receipt_id"] = receipt["receipt_id"]
            result["consumer_revision"] = receipt["consumer_revision"]
        return publication, result

    async def _wa_task_authorize(self, run, task_id):
        if not callable(self.task_authority) or await self.task_authority(run, task_id, self.actor_id) is not True:
            raise HTTPException(404, detail={"code": "not_found"})

    async def _wa_reuse_authorize(self, run, result_id):
        if callable(self.wa_reuse_authority) and await self.wa_reuse_authority(run, result_id) is not True:
            raise HTTPException(404, detail={"code": "not_found"})

    async def use_wa_result(self, result_id, task_id, *, read_only=False):
        result = await self.get(result_id)
        run = self.runs[result["run_id"]]
        if (run.service_id != "well_architected" or not isinstance(self.actor_id, str)
                or not self.actor_id
                or task_id == run.run_id or not task_id
                or not all(getattr(client, "well_architected", False)
                           and getattr(client, "reuse_path", None) is not None for client in (
                               self.wa_context_client, self.wa_insights_client))):
            raise HTTPException(404, detail={"code": "not_found"})
        publications = (await self.session.scalars(select(Publication).where(
            Publication.tenant_id == run.tenant_id,
            Publication.result_id == result_id,
            Publication.result_version == result["revision"],
            Publication.destination.in_(("product-context", "engineering-insights")),
        ))).all()
        by_destination = {row.destination: row for row in publications}
        context = by_destination.get("product-context")
        insights = by_destination.get("engineering-insights")
        if any(row is None or row.state != "published" or row.receipt is None
               or row.invalidation_watermark != 0 or row.payload.get("kind") != "well_architected_result"
               or row.payload.get("scope") != run.admission["scope"]
               or row.payload["result_ref"]["record_id"] != result_id
               or row.payload["result_ref"]["record_revision"] != str(result["revision"])
               for row in (context, insights)):
            raise HTTPException(404, detail={"code": "not_found"})
        citation = f"well_architected_result:{result_id}@{result['revision']}"
        key = (run.tenant_id, context.publication_id, task_id)
        existing = await self.session.get(WellArchitectedUsage, key)
        await self._wa_task_authorize(run, task_id)
        await self._wa_reuse_authorize(run, result_id)
        if existing is not None:
            if existing.actor_id != self.actor_id or existing.result_id != result_id:
                raise HTTPException(404, detail={"code": "not_found"})
            await self._wa_authorize(run)
            await self._wa_task_authorize(run, task_id)
            await self._wa_reuse_authorize(run, result_id)
            return {"result": result, "citation": citation,
                    "retrieval_receipt": existing.retrieval_receipt,
                    "usage_receipt": existing.usage_receipt}
        if read_only:
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            retrieval = await self.wa_context_client.reuse_receipt(
                context, task_id=task_id, actor_id=self.actor_id, citation=citation)
            await self._wa_task_authorize(run, task_id)
            await self._wa_reuse_authorize(run, result_id)
            usage = await self.wa_insights_client.reuse_receipt(
                insights, task_id=task_id, actor_id=self.actor_id, citation=citation,
                retrieval_receipt_id=retrieval["receipt_id"])
        except (ValueError, httpx.HTTPError) as error:
            raise HTTPException(503, detail={"code": "consumer_reuse_unavailable"}) from error
        await self._wa_authorize(run)
        await self._wa_task_authorize(run, task_id)
        await self._wa_reuse_authorize(run, result_id)
        self.session.add(WellArchitectedUsage(
            tenant_id=run.tenant_id, publication_id=context.publication_id,
            task_id=task_id, result_id=result_id, actor_id=self.actor_id,
            retrieval_receipt=retrieval, usage_receipt=usage, observed_at=self.clock()))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(409, detail={"code": "usage_conflict"}) from None
        await self._wa_authorize(run)
        await self._wa_task_authorize(run, task_id)
        await self._wa_reuse_authorize(run, result_id)
        return {"result": result, "citation": citation,
                "retrieval_receipt": retrieval, "usage_receipt": usage}

    async def retry_publication(self, publication_id, request, idempotency_key):
        publication, _ = await self.publication(publication_id)
        if self.actor_id is None:
            raise HTTPException(404, detail={"code": "not_found"})
        run = next(iter(self.runs.values()))
        receipt_key = (publication.tenant_id, run.product_id, run.project_id, run.service_id,
                       self.actor_id, idempotency_key)
        request_hash = sha256(json.dumps({"publication_id": publication_id, "body": request},
                                         sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        original = await self.session.get(PublicationRetryReceipt, receipt_key)
        if original is not None:
            if original.request_hash != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            return original.response
        if publication.state not in {"retry_wait", "pending", "published"}:
            raise HTTPException(409, detail={"code": "publication_not_retryable"})
        if publication.state == "retry_wait":
            changed = await self.session.execute(update(Publication).where(
                Publication.tenant_id == publication.tenant_id,
                Publication.publication_id == publication_id,
                Publication.state == "retry_wait",
                Publication.revision == publication.revision,
            ).values(state="pending", next_due_at=self.clock(), revision=Publication.revision + 1),
                execution_options={"synchronize_session": False})
            if changed.rowcount != 1:
                await self.session.rollback()
                publication, _ = await self.publication(publication_id)
                if publication.state not in {"pending", "published"}:
                    raise HTTPException(409, detail={"code": "publication_not_retryable"})
            await self.session.refresh(publication)
        result = (await self.publication(publication_id))[1]
        self.session.add(PublicationRetryReceipt(
            tenant_id=receipt_key[0], product_id=receipt_key[1], project_id=receipt_key[2],
            service_id=receipt_key[3], actor_id=receipt_key[4], idempotency_key=idempotency_key,
            publication_id=publication_id, request_hash=request_hash, response=result,
        ))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            await self.publication(publication_id)
            original = await self.session.get(PublicationRetryReceipt, receipt_key)
            if original is None:
                raise
            if original.request_hash != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
            return original.response
        return result

    async def list(self, cursor=None):
        if not self.runs:
            if self.service_id == "well_architected":
                raise HTTPException(404, detail={"code": "not_found"})
            return []
        if self.service_id == "well_architected" and cursor is not None:
            await self.get(cursor)
        from l5_services.models.evidence import AssessmentResult

        visible = []
        for run in self.runs.values():
            try:
                await self._wa_authorize(run)
            except HTTPException as error:
                if run.service_id != "well_architected" or error.status_code != 404:
                    raise
            else:
                visible.append(run.run_id)
        if not visible and self.service_id == "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})

        records = (await self.session.execute(
            select(ResultDetail, AssessmentResult.run_id).join(
                AssessmentResult,
                (ResultDetail.tenant_id == AssessmentResult.tenant_id)
                & (ResultDetail.result_id == AssessmentResult.result_id),
            ).where(AssessmentResult.run_id.in_(visible),
                    AssessmentResult.tenant_id == next(iter(self.runs.values())).tenant_id)
            .order_by(ResultDetail.result_id)
        )).all()
        return [detail.payload for detail, run_id in records if cursor is None or detail.result_id > cursor]

    async def get(self, result_id):
        from l5_services.models.evidence import AssessmentResult

        if not self.runs:
            raise HTTPException(404, detail={"code": "not_found"})
        record = (await self.session.execute(
            select(ResultDetail, AssessmentResult.run_id).join(
                AssessmentResult,
                (ResultDetail.tenant_id == AssessmentResult.tenant_id)
                & (ResultDetail.result_id == AssessmentResult.result_id),
            ).where(ResultDetail.result_id == result_id, AssessmentResult.run_id.in_(self.runs),
                    AssessmentResult.tenant_id == next(iter(self.runs.values())).tenant_id)
        )).first()
        if record is None:
            raise HTTPException(404, detail={"code": "not_found"})
        await self._wa_authorize(self.runs[record[1]])
        return record[0].payload

    async def finops_publication_status(self, result_id):
        return await self._result_publication_status(result_id, "finops_cost_inquiry", "finops_cost_result")

    async def finops_answer(self, result_id, revision):
        if (self.service_id != "finops" or not isinstance(self.actor_id, str)
                or not self.actor_id or self.finops_answer_reader is None):
            raise HTTPException(404, detail={"code": "not_found"})
        result = await self.get(result_id)
        run = self.runs.get(result.get("run_id"))
        if run is None:
            raise HTTPException(404, detail={"code": "not_found"})
        reference = (result.get("native") or {}).get("answer_artifact")
        if (result.get("kind") != "finops_cost_inquiry" or result.get("revision") != revision
                or (result.get("native") or {}).get("status") not in {"answered", "refused"}
                or reference is None or reference != (run.provider_readback_ref or {}).get("answer_artifact")
                or result.get("scope") != (run.admission or {}).get("scope")):
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            text = await asyncio.wait_for(self.finops_answer_reader.read(run.run_id, reference), timeout=5)
        except (ValueError, UnicodeError, TimeoutError, ClientError, BotoCoreError):
            raise HTTPException(503, detail={"code": "artifact_unavailable"}) from None
        return {"result_id": result_id, "revision": revision, "run_id": run.run_id,
                "artifact": reference, "text": text}

    async def well_architected_publication_status(self, result_id):
        return await self._result_publication_status(result_id, "well_architected_result", "well_architected_result")

    async def _result_publication_status(self, result_id, result_kind, publication_kind):
        detail = await self.get(result_id)
        if detail.get("kind") != result_kind:
            raise HTTPException(404, detail={"code": "not_found"})
        destinations = ("product-context", "engineering-insights")
        publications = (await self.session.scalars(select(Publication).where(
            Publication.tenant_id == next(iter(self.runs.values())).tenant_id,
            Publication.result_id == result_id, Publication.result_version == detail["revision"],
            Publication.destination.in_(destinations),
            Publication.payload["kind"].as_string() == publication_kind,
        ))).all()
        by_destination = {publication.destination: publication for publication in publications}
        consumers = []
        for destination in destinations:
            publication = by_destination.get(destination)
            status = {"destination": destination, "state": publication.state if publication else "missing",
                      "projection_verified_on_ack": False}
            if publication is not None:
                status["publication_id"] = publication.publication_id
                if publication.last_error:
                    status["last_error"] = publication.last_error
                if (publication.state == "published" and publication.receipt is not None
                        and publication.invalidation_watermark == 0):
                    status["receipt_id"] = publication.receipt["receipt_id"]
                    status["consumer_revision"] = publication.receipt["consumer_revision"]
                    status["projection_verified_on_ack"] = True
            consumers.append(status)
        return {"result_id": result_id, "revision": detail["revision"], "consumers": consumers}

    async def devops_contribution(self, result_id, destination):
        from l5_services.models.evidence import AssessmentResult
        from l5_services.publication.contributions import create_devops_publications, validate_receipt

        detail = await self.get(result_id)
        run = self.runs.get(detail.get("run_id"))
        if (run is None or run.service_id != "devops" or not isinstance(run.admission, dict)
                or destination not in {"product-context", "engineering-insights"}):
            raise HTTPException(404, detail={"code": "not_found"})
        publication = await self.session.scalar(select(Publication).where(
            Publication.tenant_id == run.tenant_id, Publication.result_id == result_id,
            Publication.result_version == detail["revision"], Publication.destination == destination,
            Publication.state == "published", Publication.invalidation_watermark == 0,
            Publication.payload["kind"].as_string() == "devops_result"))
        result = await self.session.get(AssessmentResult, (run.tenant_id, result_id))
        stored_detail = await self.session.get(ResultDetail, (run.tenant_id, result_id))
        if publication is None or result is None or stored_detail is None:
            raise HTTPException(404, detail={"code": "not_found"})
        findings = (await self.session.scalars(select(Finding).where(
            Finding.tenant_id == run.tenant_id, Finding.result_id == result_id).order_by(Finding.finding_id))).all()
        try:
            candidates = create_devops_publications(run, result, stored_detail, findings, now=self.clock())
            expected = next(row for row in candidates if row.destination == destination)
            validate_receipt(publication, publication.receipt)
            if (expected.state != "pending" or expected.payload != publication.payload
                    or expected.contribution_hash != publication.contribution_hash
                    or expected.publication_id != publication.publication_id):
                raise ValueError("stored DevOps contribution differs from original result")
        except (KeyError, ValueError, TypeError):
            raise HTTPException(404, detail={"code": "not_found"}) from None
        await self._authorize_contribution(run, publication)
        return {**publication.payload, "contribution_hash": publication.contribution_hash,
                "receipt": publication.receipt,
                "citation": {"result_ref": publication.payload["result_ref"],
                             "source_refs": publication.payload["source_refs"],
                             "evidence_refs": publication.payload["evidence_refs"]}}

    async def cost_contribution(self, result_id, destination):
        from l5_services.models.publication import Publication

        detail = await self.get(result_id)
        run = self.runs.get(detail.get("run_id"))
        if run is None or run.service_id != "finops" or detail.get("kind") != "finops_cost_inquiry" or destination not in {
                "product-context", "engineering-insights"}:
            raise HTTPException(404, detail={"code": "not_found"})
        publication = await self.session.scalar(select(Publication).where(
            Publication.tenant_id == next(iter(self.runs.values())).tenant_id,
            Publication.result_id == result_id, Publication.result_version == detail["revision"],
            Publication.destination == destination, Publication.state == "published",
            Publication.invalidation_watermark == 0,
        ))
        if publication is None or publication.receipt is None:
            raise HTTPException(404, detail={"code": "not_found"})
        payload = publication.payload
        if (payload.get("kind") != "finops_cost_result"
                or payload.get("scope") != detail["scope"]
                or payload.get("result_ref", {}).get("record_id") != result_id
                or payload["result_ref"].get("record_revision") != str(detail["revision"])
                or payload.get("cost") != {field: value for field, value in detail.items()
                                           if field not in {"result_id", "revision", "scope"}}
                or sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                != publication.contribution_hash):
            raise HTTPException(404, detail={"code": "not_found"})
        await self._authorize_contribution(run, publication)
        return {**payload, "contribution_hash": publication.contribution_hash,
                "citation": {"result_ref": payload["result_ref"],
                             "evidence_refs": payload["evidence_refs"]}}

    @property
    def transform_contribution_reader_ready(self):
        return self.actor_id is not None and callable(self.contribution_authority)

    async def transform_contribution(self, result_id, destination):
        if not self.transform_contribution_reader_ready or destination not in {
                "product-context", "engineering-insights"}:
            raise HTTPException(404, detail={"code": "not_found"})
        detail = await self.get(result_id)
        run = self.runs.get(detail.get("run_id"))
        if run is None or run.service_id != "transform" or detail.get("service_id") != "transform":
            raise HTTPException(404, detail={"code": "not_found"})
        publication = await self.session.scalar(select(Publication).where(
            Publication.tenant_id == run.tenant_id,
            Publication.result_id == result_id, Publication.result_version == detail["revision"],
            Publication.destination == destination, Publication.state == "published",
            Publication.invalidation_watermark == 0,
            Publication.payload["kind"].as_string() == "transform_result",
        ))
        if publication is None or publication.receipt is None:
            raise HTTPException(404, detail={"code": "not_found"})
        from l5_services.publication.contributions import validate_receipt

        payload = publication.payload
        retained = detail.get("retained_artifacts") or {}
        evidence_refs = [{"kind": name, "artifact": reference}
                         for name, reference in sorted(retained.items())]
        try:
            expiration = datetime.fromisoformat(detail["retention_expires_at"].replace("Z", "+00:00"))
            valid_receipt = validate_receipt(publication, publication.receipt)
        except (KeyError, AttributeError, ValueError):
            raise HTTPException(404, detail={"code": "not_found"}) from None
        if (expiration.tzinfo is None or expiration <= self.clock()
                or not retained or "original_patch" not in retained
                or any(reference.get("revoked_at") or reference.get("tenant_id") != run.tenant_id
                       or reference.get("project_id") != run.project_id
                       for reference in retained.values())
                or payload.get("scope") != detail["scope"]
                or detail["scope"] != (run.scope or (run.admission or {}).get("scope"))
                or payload.get("result") != detail
                or payload.get("source_refs") != run.input.get("sources")
                or payload.get("goal") != run.input.get("transform_input", {}).get("goal")
                or payload.get("evidence_refs") != evidence_refs
                or payload.get("result_ref") != {
                    "tenant_id": run.tenant_id, "project_id": run.project_id,
                    "source_connection_id": "l5-transform", "record_type": "transform_result",
                    "record_id": result_id, "record_revision": str(detail["revision"])}
                or valid_receipt["contribution_id"] != publication.publication_id
                or sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                != publication.contribution_hash):
            raise HTTPException(404, detail={"code": "not_found"})
        await self._authorize_contribution(run, publication)
        return {**payload, "contribution_hash": publication.contribution_hash,
                "citation": {"result_ref": payload["result_ref"],
                             "evidence_refs": payload["evidence_refs"]}}

    async def findings(self, result_id, cursor=None):
        await self.get(result_id)
        if self.service_id == "well_architected" and cursor is not None:
            finding = await self.session.get(Finding, (next(iter(self.runs.values())).tenant_id, cursor))
            if finding is None or finding.result_id != result_id:
                raise HTTPException(404, detail={"code": "not_found"})
        query = select(Finding).where(Finding.result_id == result_id,
                                      Finding.tenant_id == next(iter(self.runs.values())).tenant_id)
        if cursor is not None:
            query = query.where(Finding.finding_id > cursor)
        findings = (await self.session.scalars(query.order_by(Finding.finding_id))).all()
        return [{"finding_id": finding.finding_id, **finding.payload} for finding in findings]

    async def _transform_artifact(self, artifact_id):
        if not self.transform_reader_ready:
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        matches = []
        for result in await self.list():
            if result.get("service_id") != "transform":
                continue
            run = self.runs[result["run_id"]]
            for reference in result.get("retained_artifacts", {}).values():
                if reference.get("artifact_id") == artifact_id:
                    matches.append((run, reference))
        if not matches:
            raise HTTPException(404, detail={"code": "not_found"})
        if len(matches) != 1:
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        run, reference = matches[0]
        if (reference.get("tenant_id") != run.tenant_id
                or reference.get("project_id") != run.project_id
                or reference.get("media_type") not in {"text/x-diff", "text/plain"}
                or not isinstance(reference.get("version"), str)
                or not reference["version"] or type(reference.get("bytes")) is not int
                or reference["bytes"] < 0 or not isinstance(reference.get("sha256"), str)
                or len(reference["sha256"]) != 64):
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        try:
            expiration = datetime.fromisoformat(reference["expires_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            raise HTTPException(503, detail={"code": "artifact_unavailable"}) from None
        if expiration.tzinfo is None:
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        if reference.get("revoked_at") or expiration <= self.clock():
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            contents = await asyncio.wait_for(self.transform_artifact_store.read(
                reference=reference, authority=self.artifact_authority), timeout=5)
        except PermissionError:
            raise HTTPException(404, detail={"code": "not_found"}) from None
        except (FileNotFoundError, TimeoutError):
            raise HTTPException(503, detail={"code": "artifact_unavailable"}) from None
        if (not isinstance(contents, bytes) or len(contents) != reference["bytes"]
                or sha256(contents).hexdigest() != reference["sha256"]):
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        return reference, contents

    async def artifact(self, artifact_id):
        if self.service_id == "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        if self.service_id == "transform" or (self.runs and all(run.service_id == "transform" for run in self.runs.values())):
            return await self._transform_artifact(artifact_id)
        if artifact_id.startswith("readback-"):
            from l5_services.models.evidence import AssessmentResult

            result_id = artifact_id.removeprefix("readback-")
            result = await self.get(result_id)
            reference = result.get("evidence_ref") if (result.get("service_id") == "devops"
                and result.get("origin") in {"native", "adp_submitted"}) else None
            if reference is None or reference["artifact_id"] != artifact_id:
                raise HTTPException(404, detail={"code": "not_found"})
            raw = await self.session.get(AssessmentResult, (result["scope"]["tenant_id"], result_id))
            if raw is None:
                raise HTTPException(404, detail={"code": "not_found"})
            from l5_services.devops.results import evidence_bytes

            contents = evidence_bytes(raw.payload)
            if len(contents) != reference["bytes"] or sha256(contents).hexdigest() != reference["sha256"]:
                raise HTTPException(503, detail={"code": "artifact_unavailable"})
            return reference, contents
        if not artifact_id.startswith("findings-"):
            raise HTTPException(404, detail={"code": "not_found"})
        result_id = artifact_id.removeprefix("findings-")
        result = await self.get(result_id)
        reference = result.get("findings_artifact")
        if reference is None or reference["artifact_id"] != artifact_id:
            raise HTTPException(404, detail={"code": "not_found"})
        contents = json.dumps({"items": await self.findings(result_id)},
                              sort_keys=True, separators=(",", ":")).encode()
        if len(contents) != reference["bytes"] or sha256(contents).hexdigest() != reference["sha256"]:
            raise HTTPException(503, detail={"code": "artifact_unavailable"})
        return reference, contents

    def _compare_wa(self, baseline, candidate, base_run, next_run):
        outcome = {
            "comparability": "not_comparable", "reasons": [],
            "baseline_result_id": baseline["result_id"],
            "candidate_result_id": candidate["result_id"],
            "baseline_revision": baseline["revision"],
            "candidate_revision": candidate["revision"],
            "added": [], "removed": [], "changed": [], "unchanged_count": 0,
        }
        reasons = outcome["reasons"]
        if (base_run.tenant_id, base_run.product_id, base_run.project_id) != (
                next_run.tenant_id, next_run.product_id, next_run.project_id):
            reasons.append("assessment_scope_changed")
        scope = base_run.admission.get("scope")
        if (not scope or scope != next_run.admission.get("scope")
                or any(detail.get("scope") != scope for detail in (baseline, candidate))):
            reasons.append("review_visibility_scope_changed")

        sources = [run.input.get("sources") or [] for run in (base_run, next_run)]
        if (not sources[0] or sorted(map(_source_identity, sources[0]), key=str)
                != sorted(map(_source_identity, sources[1]), key=str)):
            reasons.append("source_selection_changed")
        for field, reason in (("pillars", "pillar_coverage_changed"), ("goals", "goal_coverage_changed")):
            selected = [detail.get(field) for detail in (baseline, candidate)]
            pinned = [run.admission.get("generation_scope", {}).get(
                "pillars" if field == "pillars" else "goalIds") for run in (base_run, next_run)]
            if (not selected[0] or not selected[1] or not pinned[0] or not pinned[1]
                    or any(sorted(coverage) != sorted(expected) for coverage, expected in zip(selected, pinned))
                    or sorted(selected[0]) != sorted(selected[1])):
                reasons.append(reason)

        coverage = []
        for run, detail, selected_sources in zip((base_run, next_run), (baseline, candidate), sources):
            manifest = run.manifest or {}
            architecture = manifest.get("architecture") or {}
            files = architecture.get("files") or []
            selected = architecture.get("sources") or []
            archive = manifest.get("archive") or {}
            if (manifest.get("provider_coverage") != "verified" or not files or not selected
                    or not archive.get("sha256") or detail.get("manifest_ref") != archive
                    or detail.get("coverage") != files
                    or sorted((_source_identity(entry.get("source_ref", {})) for entry in selected), key=str)
                    != sorted((_source_identity(source) for source in selected_sources), key=str)):
                reasons.append("manifest_coverage_unavailable")
            coverage.append({
                "sources": sorted(json.dumps({key: item.get(key) for key in (
                    "source_ref", "selected_paths", "selected_hashes", "unresolved")}, sort_keys=True)
                    for item in selected),
                "files": sorted(json.dumps({key: item.get(key) for key in (
                    "source_ref", "path", "sha256", "component", "exclusion", "entry_point")}, sort_keys=True)
                    for item in files),
            })
        if coverage[0] != coverage[1]:
            reasons.append("manifest_coverage_changed")

        def recommendation_index(detail, run):
            files = ((run.manifest or {}).get("architecture") or {}).get("files") or []
            if (detail.get("kind") != "well_architected_result" or detail.get("review_mode") != "source_only"
                    or detail.get("revision", 0) < 2 or not detail.get("based_on_result_id")
                    or not isinstance(detail.get("comparison"), list)
                    or not isinstance(detail.get("recommendations"), list)
                    or not detail["recommendations"]
                    or len(detail["comparison"]) != len(detail.get("recommendations") or [])):
                return None
            indexed = {}
            for item, verdict in zip(detail["recommendations"], detail["comparison"]):
                if not isinstance(item, dict) or not isinstance(verdict, dict):
                    return None
                status = verdict.get("status")
                if (not item.get("native_id") or not item.get("title") or item.get("comparison") != verdict
                        or verdict.get("native_id") != item["native_id"]
                        or status not in {"supported", "contradicted", "not_verifiable"}
                        or not isinstance(verdict.get("citations"), list)
                        or any(not isinstance(citation, dict) for citation in verdict["citations"])
                        or (status in {"supported", "contradicted"} and
                            (not verdict.get("citations") or not verdict.get("verifier_ref")))
                        or any(not any(entry.get("source_ref") == citation.get("source_ref")
                                       and entry.get("path") == citation.get("path")
                                       and entry.get("sha256") == citation.get("sha256")
                                       for entry in files)
                               for citation in verdict.get("citations", []))):
                    return None
                fingerprint = sha256(json.dumps({
                    "scope": {field: scope.get(field) for field in (
                        "tenant_id", "product_id", "project_id")} if scope else None,
                    "pillar": item.get("pillar"), "title": item["title"],
                    "description": item.get("description"),
                }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if fingerprint in indexed:
                    return None
                indexed[fingerprint] = item
            return indexed

        base_items = recommendation_index(baseline, base_run)
        next_items = recommendation_index(candidate, next_run)
        if base_items is None or next_items is None:
            reasons.append("independent_comparison_unavailable")
        if reasons:
            outcome["reasons"] = sorted(set(reasons))
            return outcome
        outcome["comparability"] = "comparable"
        for fingerprint in sorted(base_items.keys() | next_items.keys()):
            previous, current = base_items.get(fingerprint), next_items.get(fingerprint)
            if previous is None:
                outcome["added"].append({"fingerprint": fingerprint, "candidate": current})
            elif current is None:
                outcome["removed"].append({"fingerprint": fingerprint, "baseline": previous})
            elif ({key: previous.get(key) for key in ("priority", "effort", "impact", "tradeoffs")}
                  != {key: current.get(key) for key in ("priority", "effort", "impact", "tradeoffs")}
                  or {key: value for key, value in previous["comparison"].items() if key != "native_id"}
                  != {key: value for key, value in current["comparison"].items() if key != "native_id"}):
                outcome["changed"].append({"fingerprint": fingerprint,
                                           "baseline": previous, "candidate": current})
            else:
                outcome["unchanged_count"] += 1
        return outcome

    async def compare(self, baseline_result_id, candidate_result_id):
        from l5_services.models.evidence import AssessmentResult

        baseline = await self.get(baseline_result_id)
        candidate = await self.get(candidate_result_id)
        base_run = self.runs[baseline["run_id"]]
        next_run = self.runs[candidate["run_id"]]
        if base_run.service_id == "well_architected" or next_run.service_id == "well_architected":
            if base_run.service_id != next_run.service_id:
                raise HTTPException(404, detail={"code": "not_found"})
            return self._compare_wa(baseline, candidate, base_run, next_run)
        base_raw = (await self.session.get(AssessmentResult, (base_run.tenant_id, baseline_result_id))).payload
        next_raw = (await self.session.get(AssessmentResult, (next_run.tenant_id, candidate_result_id))).payload
        outcome = {"comparability": "incomparable", "reasons": [],
                   "baseline_revision": baseline["revision"], "candidate_revision": candidate["revision"],
                   "new_findings": [], "unchanged_findings": [], "resolved_findings": []}
        if (base_run.tenant_id, base_run.product_id, base_run.project_id) != (
                next_run.tenant_id, next_run.product_id, next_run.project_id):
            outcome["reasons"].append("assessment_scope_changed")
        if base_run.input.get("capability") != next_run.input.get("capability"):
            outcome["reasons"].append("capability_changed")
        source_fields = ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id")

        def selected_sources(run):
            return sorted(tuple(source.get(field) for field in source_fields)
                          for source in run.input.get("sources", []))

        if not selected_sources(base_run) or selected_sources(base_run) != selected_sources(next_run):
            outcome["reasons"].append("source_selection_changed")
        base_adapter = (base_run.admission or {}).get("adapter_revision")
        next_adapter = (next_run.admission or {}).get("adapter_revision")
        if base_adapter is None or base_adapter != next_adapter:
            outcome["reasons"].append("adapter_version_changed_or_unknown")
        for version in ("provider_version", "rule_version"):
            if not base_raw.get(version) or not next_raw.get(version):
                outcome["reasons"].append(f"{version}_unknown")
            elif base_raw[version] != next_raw[version]:
                outcome["reasons"].append(f"{version}_changed")
        if baseline["coverage_mode"] != candidate["coverage_mode"]:
            outcome["reasons"].append("coverage_mode_changed")
        if outcome["reasons"]:
            return outcome

        def covered_files(run):
            files = (run.manifest or {}).get("files") or []
            return {(tuple(entry["source_ref"].get(field) for field in source_fields), entry["path"]):
                    entry["sha256"] for entry in files}

        base_files, next_files = covered_files(base_run), covered_files(next_run)
        if not base_files or not next_files:
            outcome["reasons"].append("sealed_file_coverage_unavailable")
            return outcome
        comparable_files = {file for file in base_files.keys() & next_files.keys()
                            if base_files[file] == next_files[file]}
        if base_files != next_files:
            outcome["reasons"].append("source_files_changed_or_unscanned")
        if baseline["coverage"] != "complete" or candidate["coverage"] != "complete":
            outcome["reasons"].append("coverage_incomplete")
        outcome["comparability"] = "partial" if outcome["reasons"] else "comparable"
        if "coverage_incomplete" in outcome["reasons"]:
            return outcome

        def covered(finding):
            sources = {tuple(source.get(field) for field in source_fields) for source in finding["source_refs"]}
            if not sources or not sources <= {identity for identity, _ in comparable_files}:
                return False
            if base_run.input["capability"] != "code_review":
                return True
            locations = finding["locations"]
            return bool(locations) and all(
                any(identity in sources and path == location.split(":", 1)[0]
                    for identity, path in comparable_files)
                for location in locations
            )

        base_findings = {((item["kind"], item["native_id"])): item
                         for item in await self.findings(baseline_result_id)}
        next_findings = {((item["kind"], item["native_id"])): item
                         for item in await self.findings(candidate_result_id)}
        for identity, finding in next_findings.items():
            if not covered(finding):
                if "finding_location_not_covered" not in outcome["reasons"]:
                    outcome["reasons"].append("finding_location_not_covered")
                continue
            previous = base_findings.get(identity)
            if previous is None:
                outcome["new_findings"].append(finding["finding_id"])
            elif covered(previous) and previous["claim"] == finding["claim"]:
                outcome["unchanged_findings"].append(finding["finding_id"])
            elif "finding_claim_changed_or_uncovered" not in outcome["reasons"]:
                outcome["reasons"].append("finding_claim_changed_or_uncovered")
        if not outcome["reasons"]:
            outcome["resolved_findings"] = [finding["finding_id"] for identity, finding in base_findings.items()
                                            if identity not in next_findings and covered(finding)]
            if len(outcome["resolved_findings"]) != len(base_findings.keys() - next_findings.keys()):
                outcome["reasons"].append("baseline_location_not_covered")
                outcome["resolved_findings"] = []
        if outcome["reasons"]:
            outcome["comparability"] = "partial"
        for key in ("new_findings", "unchanged_findings", "resolved_findings"):
            outcome[key].sort()
        return outcome
