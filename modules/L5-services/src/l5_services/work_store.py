"""Claim opaque queue work and commit fenced SQL transitions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import AssessmentResult, ProviderObservation
from l5_services.models.generation_collection import GenerationCollection
from l5_services.models.run import Run
from l5_services.models.work import Work


TRANSFORM_SUBMIT_OPERATION = "transform_provider_submit"


@dataclass(frozen=True)
class WorkClaim:
    tenant_id: str
    work_id: str
    run_id: str
    generation: int
    kind: str
    step: int
    fence: int
    owner: str
    run_revision: int
    admission: dict
    run_input: dict
    native_job_ref: dict | None


async def claim_work(sessions, work_id, generation, *, now=None, lease_seconds=60, service_id=None):
    """An early, duplicate or already leased message has no authority to execute."""
    now = now or datetime.now(UTC)
    if not isinstance(work_id, str) or type(generation) is not int or generation < 1:
        return None
    owner = str(uuid4())
    async with sessions() as session:
        candidates = (await session.scalars(select(Work).where(Work.work_id == work_id).limit(2))).all()
        if len(candidates) != 1:
            return None
        work = candidates[0]
        due_at = work.due_at.replace(tzinfo=work.due_at.tzinfo or UTC)
        if due_at > now:
            return None
        eligible = and_(Work.tenant_id == work.tenant_id, Work.work_id == work_id,
                        Work.generation == generation, Work.completed_at.is_(None), Work.blocked_at.is_(None),
                        Work.due_at <= now,
                        or_(Work.lease_until.is_(None), Work.lease_until <= now))
        updated = await session.execute(update(Work).where(eligible).values(
            lease_owner=owner, lease_until=now + timedelta(seconds=lease_seconds),
            fence=Work.fence + 1,
        ), execution_options={"synchronize_session": False})
        if updated.rowcount != 1:
            await session.rollback()
            return None
        run = await session.get(Run, (work.tenant_id, work.run_id))
        if run is None or (service_id is not None and run.service_id != service_id):
            await session.rollback()
            return None
        claim = WorkClaim(work.tenant_id, work_id, work.run_id, generation, work.kind,
                          work.step, work.fence + 1, owner, run.revision, run.admission, run.input,
                          run.native_job_ref)
        await session.commit()
        return claim


async def renew_work_lease(sessions, claim, *, now, lease_seconds):
    async with sessions() as session:
        run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
        if run is None or run.revision != claim.run_revision:
            return False
        renewed = await session.execute(update(Work).where(
            Work.tenant_id == claim.tenant_id, Work.work_id == claim.work_id,
            Work.generation == claim.generation, Work.fence == claim.fence,
            Work.lease_owner == claim.owner, Work.lease_until > now,
            Work.completed_at.is_(None), Work.blocked_at.is_(None),
        ).values(lease_until=now + timedelta(seconds=lease_seconds)),
            execution_options={"synchronize_session": False})
        await session.commit()
        return renewed.rowcount == 1


async def finish_work(sessions, claim, *, state, provider_state=None, next_kind=None,
                      due_at=None, now=None, manifest=None, observation=None, result=None,
                      native_job_ref=None, attempt_id=None, attempt_status=None, poll_error=None,
                      provider_readback_ref=None):
    """A stale worker cannot commit a result or a successor message."""
    now = now or datetime.now(UTC)
    async with sessions() as session:
        run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
        if run is None or run.revision != claim.run_revision:
            return None
        if native_job_ref is not None and run.native_job_ref is not None:
            await session.rollback()
            return None
        if provider_readback_ref is not None and run.provider_readback_ref is not None:
            await session.rollback()
            return None
        finished = await session.execute(update(Work).where(
            Work.tenant_id == claim.tenant_id, Work.work_id == claim.work_id,
            Work.generation == claim.generation, Work.fence == claim.fence,
            Work.lease_owner == claim.owner, Work.lease_until > now,
            Work.completed_at.is_(None),
        ).values(completed_at=now, lease_owner=None, lease_until=None),
            execution_options={"synchronize_session": False})
        if finished.rowcount != 1:
            await session.rollback()
            return None
        values = {"state": state, "revision": Run.revision + 1}
        if observation is not None or poll_error is not None or next_kind == "poll" or (
                run.service_id in {"finops", "transform", "well_architected"} and next_kind in {"reconcile", "collect"}) or (run.monitoring and state in {
                "succeeded", "partial", "failed", "cancelled", "expired", "blocked", "outcome_unknown"}) or (
                run.service_id == "well_architected" and state in {"cancelled", "expired"} and provider_state == "unknown"):
            monitoring = dict(run.monitoring or {})
            monitoring.setdefault("state", "not_started")
            monitoring.setdefault("progress_state", "unknown")
            monitoring.setdefault("poll_attempts", 0)
            monitoring.setdefault("consecutive_poll_errors", 0)
            if poll_error is not None:
                monitoring["state"] = "degraded"
                monitoring["consecutive_poll_errors"] += 1
                monitoring["last_poll_error"] = poll_error
                if run.service_id != "well_architected":
                    monitoring["progress_state"] = "unknown"
                    monitoring.pop("progress_baseline_at", None)
            elif observation is not None:
                previous = monitoring.get("latest_observation") or {}
                fingerprint = observation.get("progress_fingerprint")
                comparable = observation.get("progress_coverage") == "complete" and fingerprint is not None
                monitoring.update(state="current", consecutive_poll_errors=0,
                                  latest_observation=observation,
                                  last_successful_poll_at=observation["observed_at"])
                monitoring.pop("last_poll_error", None)
                if comparable:
                    baseline = monitoring.get("progress_baseline_at")
                    if previous.get("progress_coverage") != "complete" or not baseline:
                        monitoring["progress_baseline_at"] = now.isoformat()
                        monitoring["progress_state"] = "observed"
                    elif previous.get("progress_fingerprint") != fingerprint:
                        monitoring["last_progress_at"] = now.isoformat()
                        monitoring["progress_state"] = "observed"
                    else:
                        stall_after = run.admission.get("monitoring_policy", {}).get("stall_after_seconds")
                        baseline_at = datetime.fromisoformat(baseline.replace("Z", "+00:00"))
                        progress_at = monitoring.get("last_progress_at")
                        since = baseline_at
                        if progress_at:
                            since = max(since, datetime.fromisoformat(progress_at.replace("Z", "+00:00")))
                        if stall_after and (now - since).total_seconds() >= stall_after:
                            monitoring["progress_state"] = "suspected_stalled"
                        else:
                            monitoring["progress_state"] = "observed"
                else:
                    monitoring["progress_state"] = "unknown"
                    monitoring.pop("progress_baseline_at", None)
            if next_kind == "poll" and due_at is not None:
                monitoring["next_poll_at"] = due_at.isoformat().replace("+00:00", "Z")
            else:
                monitoring.pop("next_poll_at", None)
            if (run.service_id == "well_architected" and state == "collecting"
                    and provider_state == "completed" and poll_error is not None and next_kind is None):
                monitoring["state"] = "recovery_blocked"
            elif (run.service_id == "well_architected" and state in {"cancelled", "expired"}
                    and (run.native_job_ref is not None or native_job_ref is not None
                         or provider_state == "unknown" or next_kind == "reconcile")):
                monitoring["state"] = ("recovery_pending" if next_kind is not None else
                                       "finished" if result is not None and provider_state == "completed" else
                                       "recovery_blocked")
            elif (run.service_id in {"finops", "transform", "security"} and state in {"cancelled", "expired"}
                  and next_kind is not None):
                monitoring["state"] = "recovery_pending"
            elif state in {"succeeded", "partial", "failed", "cancelled", "expired", "blocked", "outcome_unknown"} and poll_error is None:
                monitoring["state"] = "finished"
            values["monitoring"] = monitoring
        if provider_state is not None:
            if provider_state not in {"not_started", "running", "stopped", "completed", "failed", "unknown"}:
                await session.rollback()
                raise ValueError("invalid provider state")
            if (run.service_id == "well_architected" and run.native_job_ref is not None
                    and provider_state == "unknown" and run.provider_state in {"running", "completed", "failed"}):
                provider_state = run.provider_state
            values["provider_state"] = provider_state
        if native_job_ref is not None:
            values["native_job_ref"] = native_job_ref
        if provider_readback_ref is not None:
            values["provider_readback_ref"] = provider_readback_ref
        if manifest is not None:
            values["manifest"] = manifest
        changed = await session.execute(update(Run).where(
            Run.tenant_id == claim.tenant_id, Run.run_id == claim.run_id,
            Run.revision == claim.run_revision,
            *([Run.manifest.is_(None)] if manifest is not None else []),
        ).values(**values), execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await session.rollback()
            return None
        if attempt_status is not None:
            changed_attempt = await session.execute(update(WorkerAttempt).where(
                WorkerAttempt.tenant_id == claim.tenant_id,
                WorkerAttempt.run_id == claim.run_id,
                WorkerAttempt.attempt_id == attempt_id,
            ).values(status=attempt_status, native_job_ref=native_job_ref),
                execution_options={"synchronize_session": False})
            if changed_attempt.rowcount != 1:
                await session.rollback()
                return None
        if observation is not None:
            session.add(ProviderObservation(tenant_id=claim.tenant_id, observation_id=str(uuid4()),
                                            run_id=claim.run_id, work_id=claim.work_id,
                                            observed_at=now, payload=observation))
        if result is not None:
            result_id = str(uuid4())
            if run.service_id == "finops":
                from l5_services.finops_reconciliation import normalize_cost_result
                from l5_services.models.result import ResultDetail

                if set(result) != {"native", "billing"}:
                    raise ValueError("finops result requires native and independent billing evidence")
                cost = normalize_cost_result(run, result["native"], result["billing"])
                detail = ResultDetail(tenant_id=claim.tenant_id, result_id=result_id,
                                      payload={"result_id": result_id, "revision": 1,
                                               "scope": run.admission["scope"], **cost})
                evidence = {"native_series": result["native"].get("series"),
                            "billing_series": result["billing"].get("series"),
                            "native_artifact": cost["native"]["answer_artifact"],
                            "billing_pages": cost["oracle"]["pages"]}
                findings = []
            elif run.service_id == "well_architected":
                from l5_services.adapters.well_architected.results import (
                    normalize_wa_result, wa_recommendation_findings,
                )

                if (result.get("native_job_ref") != run.native_job_ref
                        or not isinstance(result.get("recommendations"), list)):
                    raise ValueError("result does not match original native generation")
                if await session.scalar(select(AssessmentResult.result_id).where(
                        AssessmentResult.tenant_id == claim.tenant_id,
                        AssessmentResult.run_id == claim.run_id,
                        AssessmentResult.version == 1)) is not None:
                    raise ValueError("native result version already committed")
                collection = await session.get(GenerationCollection, (claim.tenant_id, claim.run_id),
                                               with_for_update=True)
                if (collection is None or collection.native_job_ref != run.native_job_ref
                        or collection.next_token is not None or collection.pending_recommendations
                        or collection.result_id is not None
                        or collection.recommendations != result["recommendations"]):
                    raise ValueError("result does not match completed generation collection")
                collection.result_id = result_id
                detail = normalize_wa_result(run, result_id, 1, result, now)
                findings = wa_recommendation_findings(detail)
                evidence = result
            elif run.service_id == "transform":
                from l5_services.adapters.transform.retention import normalize_transform_result

                detail = normalize_transform_result(run, result_id, 1, result, now)
                findings = []
                evidence = result
            else:
                from l5_services.findings import normalize_result

                detail, findings = normalize_result(run, result_id, 1, result, now)
                evidence = result
            assessment = AssessmentResult(tenant_id=claim.tenant_id, result_id=result_id,
                                          run_id=claim.run_id, version=1, payload=evidence)
            session.add(assessment)
            session.add(detail)
            session.add_all(findings)
            if run.service_id == "security":
                from l5_services.publication.contributions import create_publication

                session.add(create_publication(run, assessment, detail, findings, now=now))
            elif run.service_id == "devops":
                from l5_services.publication.contributions import create_devops_publications

                session.add_all(create_devops_publications(run, assessment, detail, findings, now=now))
            elif run.service_id == "well_architected":
                from l5_services.publication.contributions import create_wa_publications

                session.add_all(create_wa_publications(run, assessment, detail, now=now))
            elif run.service_id == "transform":
                from l5_services.publication.contributions import create_transform_publications

                session.add_all(create_transform_publications(run, assessment, detail, now=now))
            elif run.service_id == "finops":
                from l5_services.publication.contributions import create_finops_publications

                session.add_all(create_finops_publications(run, assessment, detail, now=now))
        successor = None
        if next_kind is not None:
            successor = Work(tenant_id=claim.tenant_id, work_id=str(uuid4()),
                             run_id=claim.run_id, kind=next_kind,
                             generation=claim.generation, step=claim.step + 1,
                             due_at=due_at or now)
            session.add(successor)
        await session.commit()
        return successor.work_id if successor is not None else "completed"


async def generation_collection(sessions, claim):
    async with sessions() as session:
        collection = await session.get(GenerationCollection, (claim.tenant_id, claim.run_id))
        if collection is None:
            return None
        if collection.native_job_ref != claim.native_job_ref:
            raise ValueError("collection belongs to another generation")
        return {"next_token": collection.next_token, "page_count": collection.page_count,
                "recommendations": collection.recommendations, "result_id": collection.result_id,
                "pending_recommendations": collection.pending_recommendations,
                "excluded": collection.excluded}


async def _fenced_generation_collection(session, claim, now):
    run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
    work = await session.get(Work, (claim.tenant_id, claim.work_id), with_for_update=True)
    if (run is None or work is None or run.service_id != "well_architected"
            or run.native_job_ref is None or run.native_job_ref != claim.native_job_ref
            or run.revision != claim.run_revision or claim.kind != "collect"
            or work.run_id != claim.run_id or work.generation != claim.generation
            or work.fence != claim.fence or work.lease_owner != claim.owner
            or work.completed_at is not None or work.blocked_at is not None
            or work.lease_until is None
            or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= now):
        raise ValueError("stale native collection work")
    return await session.get(GenerationCollection, (claim.tenant_id, claim.run_id), with_for_update=True)


async def checkpoint_generation_page(sessions, claim, requested_token, next_token, recommendations, *,
                                     pending_recommendations=None, excluded=None, now=None):
    """Atomically retain one validated page and cursor while the original work is fenced."""
    now = now or datetime.now(UTC)
    async with sessions() as session:
        collection = await _fenced_generation_collection(session, claim, now)
        pending_recommendations = [] if pending_recommendations is None else pending_recommendations
        excluded = [] if excluded is None else excluded
        if (not isinstance(recommendations, list) or not isinstance(pending_recommendations, list)
                or not isinstance(excluded, list)
                or not isinstance(next_token, (str, type(None)))
                or next_token == "" or next_token is not None and next_token == requested_token
                or not isinstance(requested_token, (str, type(None)))
                or any(not isinstance(item, dict) or item.get("profileArn") != claim.native_job_ref["profile_arn"]
                       or item.get("generationId") != claim.native_job_ref["generation_id"]
                       or not item.get("recommendationArn") for item in [*recommendations, *pending_recommendations])):
            raise ValueError("recommendation page belongs to another generation")
        if collection is None:
            if requested_token is not None:
                raise ValueError("collection cursor changed")
            collection = GenerationCollection(tenant_id=claim.tenant_id, run_id=claim.run_id,
                                               native_job_ref=claim.native_job_ref, next_token=next_token,
                                               page_count=1, recommendations=recommendations,
                                               pending_recommendations=pending_recommendations, excluded=excluded)
            session.add(collection)
        else:
            if (collection.native_job_ref != claim.native_job_ref or collection.result_id is not None
                    or collection.pending_recommendations
                    or collection.next_token is None or collection.next_token != requested_token):
                raise ValueError("collection cursor or generation changed")
            collection.recommendations = [*collection.recommendations, *recommendations]
            collection.next_token = next_token
            collection.pending_recommendations = pending_recommendations
            collection.excluded = [*collection.excluded, *excluded]
            collection.page_count += 1
        arns = [item["recommendationArn"] for item in [*collection.recommendations, *pending_recommendations]]
        if len(arns) != len(set(arns)):
            raise ValueError("duplicate recommendation in generation collection")
        await session.commit()
        return {"next_token": collection.next_token, "page_count": collection.page_count,
                "recommendations": collection.recommendations, "result_id": collection.result_id,
                "pending_recommendations": collection.pending_recommendations,
                "excluded": collection.excluded}


async def checkpoint_generation_detail(sessions, claim, recommendation, *, now=None):
    async with sessions() as session:
        collection = await _fenced_generation_collection(session, claim, now or datetime.now(UTC))
        if (collection is None or collection.native_job_ref != claim.native_job_ref
                or collection.result_id is not None or not collection.pending_recommendations):
            raise ValueError("collection detail cursor or generation changed")
        expected = collection.pending_recommendations[0]
        if (not isinstance(recommendation, dict)
                or recommendation.get("recommendationArn") != expected["recommendationArn"]
                or recommendation.get("profileArn") != claim.native_job_ref["profile_arn"]
                or recommendation.get("generationId") != claim.native_job_ref["generation_id"]):
            raise ValueError("recommendation detail belongs to another generation")
        collection.recommendations = [*collection.recommendations, recommendation]
        collection.pending_recommendations = collection.pending_recommendations[1:]
        await session.commit()
        return {"next_token": collection.next_token, "page_count": collection.page_count,
                "recommendations": collection.recommendations, "result_id": collection.result_id,
                "pending_recommendations": collection.pending_recommendations,
                "excluded": collection.excluded}


async def transform_baseline(sessions, claim):
    async with sessions() as session:
        record = await session.scalar(select(ProviderObservation).where(
            ProviderObservation.tenant_id == claim.tenant_id,
            ProviderObservation.run_id == claim.run_id,
            ProviderObservation.work_id == claim.work_id))
        return record.payload if record is not None else None


async def record_transform_baseline(sessions, claim, receipt, *, now=None):
    now = now or datetime.now(UTC)
    async with sessions() as session:
        run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
        work = await session.get(Work, (claim.tenant_id, claim.work_id), with_for_update=True)
        if (run is None or work is None or run.service_id != "transform" or run.revision != claim.run_revision
                or run.state != "queued" or work.kind != "submit" or work.fence != claim.fence
                or work.lease_owner != claim.owner or work.completed_at is not None
                or work.lease_until is None
                or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= now):
            raise ValueError("stale Transform baseline work")
        existing = await session.scalar(select(ProviderObservation).where(
            ProviderObservation.tenant_id == claim.tenant_id,
            ProviderObservation.run_id == claim.run_id,
            ProviderObservation.work_id == claim.work_id))
        if existing is not None:
            if existing.payload != receipt:
                raise ValueError("Transform baseline changed after capture")
            return existing.payload
        session.add(ProviderObservation(tenant_id=claim.tenant_id, observation_id=str(uuid4()),
                                        run_id=claim.run_id, work_id=claim.work_id,
                                        observed_at=now, payload=receipt))
        await session.commit()
    return receipt


@dataclass(frozen=True)
class AttemptContext:
    attempt_id: str
    run_id: str
    work_id: str
    fence: int
    idempotency_token: str
    input_hash: str
    started_at: datetime
    can_reconcile: bool
    question_hash: str | None = None
    baseline_policy_sha256: str | None = None
    baseline_receipts: list[dict] | None = None
    prepared_request: dict | None = None


async def prepare_submit_attempt(sessions, claim, *, now=None):
    """Persist the original paid-effect identity before contacting the provider."""
    import json
    from hashlib import sha256

    now = now or datetime.now(UTC)
    async with sessions() as session:
        run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
        work = await session.get(Work, (claim.tenant_id, claim.work_id), with_for_update=True)
        if (run is None or work is None or run.revision != claim.run_revision
                or work.lease_owner != claim.owner or work.fence != claim.fence
                or work.completed_at is not None or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC).astimezone(UTC) <= now
                or run.manifest is None or run.native_job_ref is not None):
            raise ValueError("stale provider attempt")
        manifest_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        question_hash = None
        if run.service_id == "finops":
            question_hash = sha256(run.input["question"].encode()).hexdigest()
            if (run.manifest.get("question_hash") != question_hash
                    or run.manifest.get("scope") != run.admission["scope"]
                    or run.manifest.get("query") != run.input["query"]
                    or run.manifest.get("run_id") != run.run_id
                    or run.manifest.get("binding_id") != run.binding_id
                    or run.manifest.get("binding_generation") != run.binding_generation
                    or run.manifest.get("configuration_revision") != run.configuration_revision
                    or run.manifest.get("native_agent_ref") != run.admission["native_agent_ref"]
                    or run.manifest.get("billing_scope") != run.admission["billing_scope"]):
                raise ValueError("frozen FinOps inquiry changed")
        operation = TRANSFORM_SUBMIT_OPERATION if run.service_id == "transform" else "submit"
        existing = await session.scalar(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == claim.tenant_id, WorkerAttempt.run_id == claim.run_id,
            WorkerAttempt.operation == operation,
        ).with_for_update())
        created = existing is None
        baseline = None
        if run.service_id == "transform":
            from l5_services.adapters.transform.baseline import manifest_digest, pinned_test_policy

            if run.admission.get("transform_test_policy") is not None:
                policy, policy_sha256 = pinned_test_policy(run.admission)
                record = await session.scalar(select(ProviderObservation).where(
                    ProviderObservation.tenant_id == claim.tenant_id,
                    ProviderObservation.run_id == claim.run_id,
                    ProviderObservation.work_id == (claim.work_id if created else existing.first_work_id)))
                if record is not None:
                    baseline = record.payload
                    if (baseline.get("kind") != "transform_pre_submit_baseline"
                            or baseline.get("manifest_sha256") != manifest_digest(run.manifest)
                            or baseline.get("policy_sha256") != policy_sha256
                            or len(baseline.get("checks", [])) != len(policy["commands"])):
                        raise ValueError("pinned Transform baseline differs from paid attempt")
            if created and (baseline is None or baseline.get("status") != "passed"
                            or any(check.get("status") != "passed" or not isinstance(check.get("receipt"), dict)
                                   for check in baseline["checks"])):
                raise ValueError("Transform baseline must pass before paid attempt")
        if created:
            if claim.kind != "submit" or run.state != "queued":
                raise ValueError("provider attempt cannot be recreated")
            identity = (f"{claim.tenant_id}:{claim.run_id}:{operation}:{manifest_hash}"
                        if run.service_id == "transform" else f"{claim.tenant_id}:{claim.run_id}:{manifest_hash}")
            token = sha256(identity.encode()).hexdigest()
            existing = WorkerAttempt(tenant_id=claim.tenant_id, attempt_id=str(uuid4()),
                                     run_id=claim.run_id, operation=operation, first_work_id=claim.work_id,
                                     first_fence=claim.fence, idempotency_token=token,
                                     input_hash=manifest_hash, question_hash=question_hash,
                                     started_at=now, status="prepared")
            session.add(existing)
        elif existing.input_hash != manifest_hash or existing.question_hash != question_hash:
            raise ValueError("provider attempt input changed")
        if run.service_id == "well_architected":
            archive = run.manifest.get("archive") or {}
            if (created and (run.manifest.get("provider_coverage") != "provider_coverage_unverified"
                    or run.manifest.get("archive_readback") != {
                        "sha256": archive.get("sha256"), "bytes": archive.get("bytes"),
                        "artifact": archive}
                    or not archive.get("sha256") or not archive.get("version")
                    or not isinstance(archive.get("bytes"), int))):
                raise ValueError("verified immutable source archive required")
            from l5_services.adapters.well_architected.generation import prepare_generation

            prepared = prepare_generation(run, existing.attempt_id, now=existing.started_at.replace(
                tzinfo=existing.started_at.tzinfo or UTC))
            if created:
                existing.prepared_request = prepared.to_record()
            elif existing.prepared_request != prepared.to_record():
                raise ValueError("pinned generation request changed")
        max_checks = run.admission.get("limits", {}).get("max_polls", 2880)
        can_reconcile = created or existing.reconcile_checks < max_checks
        if not created and can_reconcile:
            existing.reconcile_checks += 1
        attempt = AttemptContext(existing.attempt_id, existing.run_id, claim.work_id, claim.fence,
                                 existing.idempotency_token, existing.input_hash,
                                 existing.started_at.replace(tzinfo=existing.started_at.tzinfo or UTC),
                                 can_reconcile, existing.question_hash,
                                 baseline["policy_sha256"] if baseline is not None and baseline.get("status") == "passed" else None,
                                 [check["receipt"] for check in baseline["checks"]] if baseline is not None and baseline.get("status") == "passed" else None,
                                 existing.prepared_request)
        await session.commit()
        return attempt, created


async def record_poll_attempt(sessions, claim, *, now=None, allow_exhausted=False):
    """Count reads before contacting the provider, including failed or crashed reads."""
    now = now or datetime.now(UTC)
    async with sessions() as session:
        run = await session.get(Run, (claim.tenant_id, claim.run_id), with_for_update=True)
        work = await session.get(Work, (claim.tenant_id, claim.work_id), with_for_update=True)
        if (run is None or work is None or run.revision != claim.run_revision
                or work.fence != claim.fence or work.lease_owner != claim.owner
                or work.completed_at is not None or work.lease_until is None
                or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= now):
            return False
        monitoring = dict(run.monitoring or {})
        monitoring.setdefault("state", "not_started")
        monitoring.setdefault("progress_state", "unknown")
        monitoring.setdefault("consecutive_poll_errors", 0)
        grace = run.admission.get("monitoring_policy", {}).get("poll_grace_seconds", 300)
        if now > work.due_at.replace(tzinfo=work.due_at.tzinfo or UTC) + timedelta(seconds=grace):
            monitoring["state"] = "overdue"
        attempts = monitoring.get("poll_attempts", 0)
        if attempts >= run.admission.get("limits", {}).get("max_polls", 2880) and not allow_exhausted:
            return False
        monitoring["poll_attempts"] = attempts + 1
        if allow_exhausted:
            monitoring["manual_recovery_attempts"] = monitoring.get("manual_recovery_attempts", 0) + 1
        monitoring["last_poll_attempt_at"] = now.isoformat().replace("+00:00", "Z")
        await session.execute(update(Run).where(
            Run.tenant_id == claim.tenant_id, Run.run_id == claim.run_id,
            Run.revision == claim.run_revision,
        ).values(monitoring=monitoring), execution_options={"synchronize_session": False})
        await session.commit()
        return True


async def current_work_fence(sessions, claim, *, now=None):
    now = now or datetime.now(UTC)
    async with sessions() as session:
        work = await session.get(Work, (claim.tenant_id, claim.work_id))
        run = await session.get(Run, (claim.tenant_id, claim.run_id))
        return (work is not None and run is not None and run.revision == claim.run_revision
                and work.run_id == claim.run_id and work.generation == claim.generation
                and work.fence == claim.fence and work.lease_owner == claim.owner
                and work.completed_at is None and work.blocked_at is None
                and work.lease_until is not None
                and work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) > now)
