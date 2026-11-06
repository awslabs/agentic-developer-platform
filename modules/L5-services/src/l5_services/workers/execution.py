"""One invocation processes at most one durable work transition."""

import asyncio
import difflib
import io
import logging
import zipfile
from hashlib import sha256
from datetime import UTC, datetime

from l5_services.work_store import claim_work, finish_work
from l5_services.workers.diagnostics import log_record_failure


LOGGER = logging.getLogger(__name__)


class AuthorityRevoked(PermissionError):
    """Current delegated authority no longer permits this effect."""


async def execute_once(sessions, envelope, transition, publish=None, *, now=None, clock=None, service_id=None):
    # An explicit instant remains a frozen clock for callers testing one step.
    # The service supplies its clock so provider latency cannot extend a lease.
    if clock is None:
        clock = (lambda: now) if now is not None else (lambda: datetime.now(UTC))
    claimed_at = now if now is not None else clock()
    if not isinstance(envelope, dict) or envelope.get("schema_version") != "1.0":
        return False
    claim = await claim_work(sessions, envelope.get("work_id"), envelope.get("generation"), now=claimed_at, service_id=service_id)
    if claim is None:
        return False
    outcome = await transition(claim)
    next_id = await finish_work(sessions, claim, now=clock(), **outcome)
    if next_id is None:
        return False
    if next_id != "completed" and publish is not None:
        from l5_services.dispatcher import dispatch_work

        try:
            await dispatch_work(sessions, claim.tenant_id, next_id, publish, now=clock(), service_id=service_id)
        except Exception:
            LOGGER.exception("Post-commit publication failed; SQL work remains owed")
    return True


class ExecutionService:
    """One provider action per claimed message, with SQL-owned follow-up work."""

    def __init__(self, sessions, source_client, adapter, publish, authorize,
                 *, archive_store=None, clock=None, binding_authorize=None, knowledge_execute=None, verify_event_target=None,
                 context_client=None, verify_source_revocation=None, finops_adapter=None,
                 devops_context_client=None, devops_insights_client=None,
                 finops_context_client=None, finops_insights_client=None, budget_guard=None,
                 transform_adapter=None, transform_context_client=None, transform_insights_client=None,
                 well_architected_adapter=None, well_architected_verifier=None,
                 well_architected_context_client=None, well_architected_insights_client=None,
                 security_threat_adapter=None, well_architected_preflight=None, service_id=None, admit_event=None):
        self.service_id = service_id
        self.sessions = sessions
        self.source_client = source_client
        self.adapter = adapter
        self.security_threat_adapter = security_threat_adapter
        self.publish = publish
        self.authorize = authorize
        self.archive_store = archive_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.binding_authorize = binding_authorize
        self.knowledge_execute = knowledge_execute
        self.verify_event_target = verify_event_target
        self.admit_event = admit_event
        self.context_client = context_client
        self.devops_context_client = devops_context_client
        self.devops_insights_client = devops_insights_client
        self.finops_context_client = finops_context_client
        self.finops_insights_client = finops_insights_client
        self.transform_context_client = transform_context_client
        self.transform_insights_client = transform_insights_client
        self.budget_guard = budget_guard
        self.verify_source_revocation = verify_source_revocation
        self.finops_adapter = finops_adapter
        self.transform_adapter = transform_adapter
        if service_id == "well_architected" and not callable(well_architected_preflight):
            raise ValueError("installed Well-Architected worker requires scoped preflight")
        self.well_architected_preflight = well_architected_preflight
        self.well_architected_adapter = well_architected_adapter
        self.well_architected_verifier = well_architected_verifier
        self.well_architected_context_client = well_architected_context_client
        self.well_architected_insights_client = well_architected_insights_client

    def _adapter_for(self, run):
        if run.service_id == "security" and run.input.get("capability") == "threat_model":
            if self.security_threat_adapter is None:
                raise AuthorityRevoked("installed threat model adapter unavailable")
            return self.security_threat_adapter
        return self.adapter

    async def _submit_security_threat(self, run, claim, attempt, provider_session, adapter):
        from sqlalchemy import text

        from l5_services.work_store import current_work_fence

        space_id = run.admission["native_space_ref"]["record_id"]
        model_id = run.admission["native_model_ref"]["record_id"]
        if not all(isinstance(value, str) and value for value in (space_id, model_id)):
            raise AuthorityRevoked("selected threat model unavailable")
        async with self.sessions() as session:
            connection = await session.connection()
            if connection.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space), hashtext(:model))"),
                                      {"space": space_id, "model": model_id})
            elif connection.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                raise RuntimeError("database cannot serialize threat-model submissions")
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise AuthorityRevoked("Security provider lease changed")
            return await adapter.submit(run, run.manifest, attempt, provider_session)

    async def _collect_billing_page(self, run, claim, now):
        from l5_services.adapters.finops.billing import normalize_pages
        from l5_services.billing_collection import (BillingCursorExpired, BillingUnavailable, active_collection,
            checkpoint_page, close_collection, query_identity, request_limit, reserve_request, supersede)
        from l5_services.finops_reconciliation import compare_cost_series, unavailable_native_cost

        adapter = self.finops_adapter
        limit = request_limit(run)
        if (limit is None or not all(hasattr(adapter, method) for method in
                                     ("collect_billing_page", "read_billing_page", "read_native_cost"))):
            return {"state": "partial", "provider_state": "completed"}
        provider_session = await self._authorize(run, claim, "collect")
        request, digest = query_identity(run)
        async with self.sessions() as session:
            collection = await active_collection(session, run, now)
            if collection.status not in {"collecting", "complete", "partial"}:
                raise ValueError("billing collection cannot be resumed")
            references = (collection.checkpoint_ref or {"pages": []})["pages"]
            pages = []
            for reference in references:
                page = await adapter.read_billing_page(run, claim, provider_session,
                                                       collection.collection_id, reference)
                if page.get("collection_id") != collection.collection_id or page.get("artifact") != reference:
                    raise ValueError("billing page belongs to another collection")
                pages.append(page)
            billing = normalize_pages(run.input["query"], collection.scope_digest, digest,
                                      collection.collection_id, pages, now.isoformat())
            if collection.status == "collecting":
                cursor = pages[-1]["response"].get("NextPageToken") if pages else None
                if pages and cursor is None:
                    raise ValueError("open collection has a terminal page")
                page = None
                reserved = await reserve_request(session, collection, limit)
                if reserved:
                    try:
                        page = await adapter.collect_billing_page(run, claim, provider_session,
                                                                  request, cursor, collection.collection_id)
                    except BillingUnavailable:
                        page = None
                    except BillingCursorExpired:
                        if cursor is None:
                            raise ValueError("initial billing request has no cursor") from None
                        await supersede(session, collection, now)
                        return {"state": "collecting", "provider_state": "completed",
                                "next_kind": "billing", "due_at": now}
                    if page is not None:
                        if page.get("collection_id") != collection.collection_id or page.get("request_token") != cursor:
                            raise ValueError("billing page differs from reserved collection request")
                        billing = normalize_pages(run.input["query"], collection.scope_digest, digest,
                                                  collection.collection_id, [*pages, page], now.isoformat())
                        await checkpoint_page(session, collection, page, now, billing["coverage"])
                        if collection.status == "collecting" and collection.request_count < limit:
                            return {"state": "collecting", "provider_state": "completed",
                                    "next_kind": "billing", "due_at": now}
                elif not pages:
                    billing = normalize_pages(run.input["query"], collection.scope_digest, digest,
                                              collection.collection_id, [], now.isoformat())
                if collection.status == "collecting":
                    await close_collection(session, collection, now,
                                           "request_limit" if not reserved or page is not None else "billing_unavailable")
            native = await adapter.read_native_cost(run, claim, provider_session)
            if native is None:
                native = unavailable_native_cost(run, digest)
            if hasattr(adapter, "read_billing_metadata"):
                billing.update(await adapter.read_billing_metadata(run, claim, provider_session,
                                                                  collection.collection_id))
            comparison = compare_cost_series(run.input["query"], native, billing,
                                             display_quantum=native.get("display_quantum"))
            return {"state": "succeeded" if comparison["state"] in {"exact", "rounded"} else "partial",
                    "provider_state": "completed", "result": {"native": native, "billing": billing}}

    async def revoke_source(self, event):
        from l5_services.publication.invalidation import receive_verified_revocation

        return await receive_verified_revocation(self, event, now=self.clock())

    async def _authorize(self, run, claim, operation):
        import httpx
        from fastapi import HTTPException

        try:
            session = await self.authorize(run, claim, operation)
            if run.service_id in {"finops", "transform", "well_architected"} and session is None:
                raise AuthorityRevoked("delegated authority unavailable")
            return session
        except HTTPException as error:
            if run.service_id in {"finops", "transform", "well_architected"} and error.status_code in {401, 403, 409, 410}:
                raise AuthorityRevoked("delegated authority unavailable") from error
            raise
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403, 404, 409, 410}:
                raise AuthorityRevoked("delegated authority unavailable") from error
            raise

    async def execute(self, envelope):
        from sqlalchemy import select

        from l5_services.models.setup import SetupWork
        from l5_services.dispatcher import service_work_condition
        from l5_services.workers.bindings import execute_setup_work

        if isinstance(envelope, dict) and isinstance(envelope.get("work_id"), str):
            async with self.sessions() as session:
                setup = await session.scalar(select(SetupWork.work_id).where(
                    SetupWork.work_id == envelope["work_id"],
                    service_work_condition(SetupWork, self.service_id)))
            if setup is not None:
                if self.binding_authorize is None:
                    raise RuntimeError("setup work requires a scoped provider authority")
                return await execute_setup_work(self.sessions, envelope, self.binding_authorize,
                                                now=self.clock(), clock=self.clock, knowledge_execute=self.knowledge_execute)
        now = self.clock()

        active_claim = None

        async def transition(claim):
            nonlocal active_claim, now
            active_claim = claim
            from datetime import timedelta

            import httpx
            from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
            from fastapi import HTTPException

            from l5_services.models.run import Run
            from l5_services.source_manifest import SourceIntegrityError, materialize_inputs
            from l5_services.models.attempt import WorkerAttempt
            from l5_services.work_store import current_work_fence, prepare_submit_attempt, record_poll_attempt

            async with self.sessions() as session:
                initial = await session.get(Run, (claim.tenant_id, claim.run_id))
                if initial is None or initial.revision != claim.run_revision:
                    raise ValueError("stale run identity")
                if initial.service_id == "transform":
                    from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION

                    deadline = getattr(initial, "deadline_at", None)
                    expiring = deadline is not None and deadline.replace(tzinfo=deadline.tzinfo or UTC) <= now
                    recovering = claim.kind in {"poll", "collect"} or (
                        claim.kind == "reconcile" and initial.state in {"cancelled", "expired"})
                    if claim.kind not in {"execute", "cancel"} and not expiring and not recovering:
                        if claim.kind not in {"submit", "reconcile"}:
                            raise RuntimeError("Transform observation stage is not qualified")
                        has_attempt = await session.scalar(select(WorkerAttempt.attempt_id).where(
                            WorkerAttempt.tenant_id == claim.tenant_id,
                            WorkerAttempt.run_id == claim.run_id,
                            WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
                        if has_attempt is None and claim.kind == "submit":
                            if (getattr(initial, "state", None) != "queued"
                                    or getattr(initial, "native_job_ref", None) is not None):
                                return {"state": "blocked", "provider_state": initial.provider_state}
                            from l5_services.adapters.transform.baseline import (
                                pinned_test_policy, verify_pre_submit_baseline,
                            )
                            from l5_services.work_store import record_transform_baseline, transform_baseline

                            try:
                                pinned_test_policy(initial.admission)
                            except (ValueError, TypeError):
                                return {"state": "blocked", "provider_state": "not_started"}
                            if (self.transform_adapter is None or self.transform_adapter.detached_runner is None
                                    or not callable(getattr(self.transform_adapter.bridge, "read_sealed_source", None))):
                                return {"state": "blocked", "provider_state": "not_started"}
                            baseline = await transform_baseline(self.sessions, claim)
                            if baseline is None:
                                authority = await self._authorize(initial, claim, "verify_baseline")
                                if not await current_work_fence(self.sessions, claim, now=self.clock()):
                                    raise AuthorityRevoked("Transform baseline lease changed")
                                try:
                                    baseline = await verify_pre_submit_baseline(
                                        initial, self.transform_adapter.bridge,
                                        self.transform_adapter.detached_runner, authority, clock=self.clock)
                                except (ValueError, TimeoutError, FileNotFoundError):
                                    return {"state": "blocked", "provider_state": "not_started"}
                                await record_transform_baseline(self.sessions, claim, baseline, now=self.clock())
                            if baseline.get("status") != "passed":
                                return {"state": "blocked", "provider_state": "not_started"}
                            now = self.clock()
                        if has_attempt is None and claim.kind == "reconcile":
                            return {"state": "outcome_unknown", "provider_state": "unknown"}
                        if self.transform_adapter is None:
                            raise RuntimeError("Transform recovery bridge is unavailable")
                        self.transform_adapter._qualification(initial)
                if initial.service_id == "well_architected" and self.well_architected_adapter is None:
                    return {"state": "blocked", "provider_state": initial.provider_state}
                recoverable_native = (initial.service_id == "security" or initial.service_id == "devops"
                                      and initial.input.get("capability") == "investigation")
                if (recoverable_native and initial.native_job_ref is not None
                        and initial.state in {"running", "collecting", "outcome_unknown", "expired"}
                        and (initial.state == "expired" or (initial.monitoring or {}).get(initial.service_id + "_recovery"))
                        and claim.kind in {"poll", "collect"}
                        and initial.deadline_at.replace(tzinfo=initial.deadline_at.tzinfo or UTC) <= now):
                    return await self._recover_native_job(initial, claim, now)
                if initial.service_id == "well_architected" and initial.state == "cancel_requested" and claim.kind == "cancel":
                    return await self._stop_remote(initial, claim, now, "cancelled")
                if (initial.service_id == "well_architected" and (
                        initial.state in {"cancelled", "expired"} and claim.kind in {"poll", "reconcile", "collect"}
                        or claim.kind in {"collect", "reconcile"} and initial.provider_state == "completed")):
                    return await self._recover_well_architected(initial, claim, now)
                if initial.service_id == "finops" and self.finops_adapter is None:
                    return {"state": "blocked", "provider_state": "not_started"}
                if initial.service_id == "finops" and initial.provider_readback_ref is not None and claim.kind != "billing":
                    return {"state": initial.state, "provider_state": initial.provider_state}
                has_finops_attempt = (await session.scalar(select(WorkerAttempt.attempt_id).where(
                    WorkerAttempt.tenant_id == claim.tenant_id,
                    WorkerAttempt.run_id == claim.run_id,
                    WorkerAttempt.operation == "submit")) is not None
                    if initial.service_id == "finops" and initial.native_job_ref is None else False)
                finops_late = (initial.service_id == "finops" and initial.state in {"cancelled", "expired"}
                               and claim.kind in {"poll", "reconcile", "collect"}
                               and (initial.native_job_ref is not None or has_finops_attempt))
                recovery_until = (initial.deadline_at.replace(tzinfo=initial.deadline_at.tzinfo or UTC)
                                  + timedelta(seconds=initial.admission["monitoring_policy"]["poll_grace_seconds"])
                                  if initial.service_id == "finops" else None)
                if finops_late and now >= recovery_until:
                    return {"state": initial.state, "provider_state": initial.provider_state,
                            "poll_error": {"code": "recovery_window_exhausted"}}
                finops_native_reconcile = (initial.service_id == "finops" and claim.kind == "reconcile"
                                           and initial.native_job_ref is not None)
                finops_unknown_reconcile = (initial.service_id == "finops" and claim.kind == "reconcile"
                                            and initial.native_job_ref is None
                                            and initial.state in {"blocked", "outcome_unknown", "cancelled", "expired"}
                                            and has_finops_attempt)
                guarded = (initial.state in {"cancelled", "expired", "blocked", "failed", "succeeded"}
                           or initial.deadline_at.replace(tzinfo=initial.deadline_at.tzinfo or UTC) <= now
                           or initial.state == "cancel_requested" and claim.kind != "poll")
                if ((finops_unknown_reconcile or finops_late) and now < recovery_until
                        and (finops_late or initial.deadline_at.replace(
                            tzinfo=initial.deadline_at.tzinfo or UTC) > now)):
                    guarded = False
            attempt = None
            created = False
            already_accepted = (claim.kind == "reconcile" and initial.native_job_ref is not None
                                and (initial.service_id == "security" or initial.service_id == "devops"
                                     and initial.input.get("capability") in {"release_readiness", "investigation"}))
            if not guarded and not already_accepted and not finops_native_reconcile and claim.kind in {"submit", "reconcile"}:
                attempt, created = await prepare_submit_attempt(
                    self.sessions, claim, now=self.clock() if initial.service_id == "transform" else now)
            devops_release_read = (initial.service_id == "devops"
                                   and initial.input.get("capability") in {"release_verification", "release_readiness"}
                                   and claim.kind in {"poll", "collect"})
            if not guarded and devops_release_read:
                attempt = await self._devops_release_attempt(initial, claim)
            if not guarded and initial.service_id != "transform" and (claim.kind == "poll" or finops_native_reconcile
                                or finops_late and claim.kind == "collect"):
                can_poll = await record_poll_attempt(self.sessions, claim, now=now)
            else:
                can_poll = True
            async with self.sessions() as session:
                run = await session.get(Run, (claim.tenant_id, claim.run_id))
                if run is None or run.revision != claim.run_revision:
                    raise ValueError("stale run identity")
                if run.service_id == "transform" and (claim.kind in {"poll", "collect"} or (
                        claim.kind == "reconcile" and run.state in {"cancelled", "expired"})):
                    return await self._recover_transform(run, claim, now)
                if run.state in {"cancelled", "expired", "blocked", "failed", "succeeded"} and not (
                        finops_unknown_reconcile or finops_late):
                    return {"state": run.state}
                if run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) <= now and not finops_late:
                    reason = "cancelled" if run.service_id == "well_architected" and run.state == "cancel_requested" else "expired"
                    return await self._stop_remote(run, claim, now, reason)
                if run.state == "cancel_requested":
                    if claim.kind == "cancel":
                        return await self._stop_remote(run, claim, now, "cancelled")
                    if claim.kind != "poll":
                        return {"state": "cancel_requested"}
                if claim.kind == "poll" and not can_poll:
                    if run.service_id == "well_architected":
                        return {"state": "cancelled" if run.state == "cancel_requested" else "expired",
                                "provider_state": run.provider_state,
                                "poll_error": {"code": "poll_limit_reached"}}
                    if run.service_id == "finops":
                        return {"state": run.state if finops_late else "expired",
                                "provider_state": run.provider_state,
                                "poll_error": {"code": "poll_limit_reached"}}
                    return await self._stop_remote(run, claim, now, "expired")
                if finops_native_reconcile and not can_poll:
                    return {"state": run.state if finops_late else "outcome_unknown",
                            "provider_state": run.provider_state,
                            "poll_error": {"code": "poll_limit_reached"}}
                if finops_late and claim.kind == "collect" and not can_poll:
                    return {"state": run.state, "provider_state": run.provider_state,
                            "poll_error": {"code": "poll_limit_reached"}}
                if already_accepted:
                    if run.service_id == "devops" and run.input.get("capability") == "investigation":
                        return {"state": run.state, "provider_state": run.provider_state,
                                "next_kind": "collect" if run.provider_state == "completed" else "poll", "due_at": now}
                    return {"state": run.state, "provider_state": run.provider_state}
                if claim.kind == "submit" and run.state != "queued":
                    return {"state": "outcome_unknown", "provider_state": run.provider_state}
                if claim.kind == "execute":
                    await self._authorize(run, claim, "materialize")
                    if run.service_id == "finops":
                        manifest = {"schema_version": "1.0", "scope": run.admission["scope"],
                                    "run_id": run.run_id, "configuration_revision": run.configuration_revision,
                                    "binding_id": run.binding_id, "binding_generation": run.binding_generation,
                                    "question_hash": sha256(run.input["question"].encode()).hexdigest(),
                                    "query": run.input["query"], "sources": run.input.get("sources", []),
                                    "native_agent_ref": run.admission["native_agent_ref"],
                                    "billing_scope": run.admission["billing_scope"],
                                    **({"native_agent_display_name": run.admission["native_agent_display_name"]}
                                       if "native_agent_display_name" in run.admission else {})}
                    elif run.service_id == "devops" and run.input["capability"] in {"investigation", "release_verification"}:
                        options = run.input["devops_input"]
                        manifest = {"schema_version": "1.0", "scope": run.admission["scope"],
                                    "run_id": run.run_id, "configuration_revision": run.configuration_revision,
                                    "binding_generation": run.binding_generation,
                                    "sources": [], "coverage_mode": "aws_only"}
                        if run.input["capability"] == "release_verification":
                            manifest.update(verification=run.admission["verification"],
                                            scenarios=options["scenarios"],
                                            coverage_mode="deployment_verification")
                            if "test_profile_ref" in options:
                                manifest.update(adapter_revision=run.admission["adapter_revision"],
                                                test_profile_ref=options["test_profile_ref"])
                                if "test_profile_preparation" in run.admission:
                                    manifest["test_profile_preparation"] = run.admission["test_profile_preparation"]
                        else:
                            manifest.update(resource_refs=options["resource_refs"],
                                            telemetry_refs=options["telemetry_refs"])
                    else:
                        try:
                            if run.input.get("capability") == "code_review":
                                from l5_services.source_archive import verify_review_selection

                                verify_review_selection(run)
                            manifest, source_bytes = await materialize_inputs(run, claim, self.source_client,
                                                                             archive_store=self.archive_store, now=now)
                        except SourceIntegrityError:
                            if run.input.get("capability") != "code_review":
                                raise
                            return {"state": "blocked", "provider_state": "not_started", "poll_error": {
                                "code": "repository_snapshot_integrity_failed",
                                "message": "The full repository snapshot could not be verified. Check source access and capture limits; no code scan was submitted.",
                            }}
                        if run.service_id == "devops":
                            files = manifest["files"]
                            base, head = manifest["sources"]
                            base_files = {item["path"]: item["sha256"] for item in files if item["source_ref"] == base}
                            head_files = {item["path"]: item["sha256"] for item in files if item["source_ref"] == head}
                            changed = sorted(path for path in base_files.keys() | head_files.keys()
                                             if base_files.get(path) != head_files.get(path))
                            if not changed:
                                raise ValueError("release has no changed files")
                            def read_text(content):
                                try:
                                    return content.decode("utf-8").splitlines()
                                except UnicodeDecodeError:
                                    return None

                            def extract(content):
                                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                                    return {name: lines for name in archive.namelist()
                                            if not name.endswith("/")
                                            and (lines := read_text(archive.read(name))) is not None}

                            snapshots = [extract(content) for content in source_bytes]
                            changed_lines = {}
                            for path in changed:
                                if path not in snapshots[1]:
                                    continue
                                previous = snapshots[0].get(path, [])
                                current = snapshots[1][path]
                                matcher = difflib.SequenceMatcher(None, previous, current, autojunk=False)
                                changed_lines[path] = {
                                    str(index + 1): sha256(current[index].encode()).hexdigest()
                                    for operation, _, _, start, end in matcher.get_opcodes()
                                    if operation != "equal" for index in range(start, end)
                                }
                            manifest.update(changed_lines=changed_lines,
                                            repository_ref=head, base_revision=base["record_revision"],
                                            head_revision=head["record_revision"], changed_paths=changed,
                                            release_eligibility=run.admission["release_eligibility"])
                        if run.service_id == "well_architected":
                            from l5_services.source_retention import pin_source_hold

                            retention = run.admission["source_retention"]
                            await pin_source_hold(
                                session, run, manifest["archive"],
                                required_until=datetime.fromisoformat(retention["required_until"].replace("Z", "+00:00")),
                                policy_expires_at=datetime.fromisoformat(
                                    retention["policy_expires_at"].replace("Z", "+00:00")), now=now,
                            )
                            await session.commit()
                            return {"state": "queued", "manifest": manifest,
                                    "next_kind": "submit", "due_at": now}
                    return {"state": "queued", "manifest": manifest,
                            "next_kind": "submit", "due_at": now}
                if claim.kind in {"submit", "reconcile"} and not finops_native_reconcile:
                    adapter = (self.transform_adapter if run.service_id == "transform" else
                               self.finops_adapter if run.service_id == "finops" else
                               self.well_architected_adapter if run.service_id == "well_architected" else self._adapter_for(run))
                    if not created and not attempt.can_reconcile:
                        return {"state": run.state if finops_late else "outcome_unknown",
                                "provider_state": "unknown",
                                "attempt_id": attempt.attempt_id, "attempt_status": "outcome_unknown"}
                    if created and run.service_id == "well_architected" and self.well_architected_preflight is not None:
                        try:
                            provider_session = await self._authorize(run, claim, "submit")
                            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                                raise AuthorityRevoked("native provider lease changed")
                            await self.well_architected_preflight(run, claim, provider_session)
                            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                                raise AuthorityRevoked("native provider lease changed")
                        except AuthorityRevoked:
                            return {"state": "blocked", "provider_state": "not_started",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure"}
                    else:
                        provider_session = await self._authorize(run, claim, "submit" if created else "reconcile")
                        if (run.service_id in {"transform", "well_architected"}
                                or run.service_id == "security" and run.input.get("capability") == "threat_model") and not await current_work_fence(
                                self.sessions, claim, now=self.clock()):
                            raise AuthorityRevoked("Transform provider lease changed")
                    try:
                        if created:
                            if run.service_id == "security" and run.input.get("capability") == "threat_model":
                                outcome = await self._submit_security_threat(
                                    run, claim, attempt, provider_session, adapter)
                            else:
                                outcome = await adapter.submit(run, run.manifest, attempt, provider_session)
                        else:
                            outcome = await adapter.reconcile_submit(run, attempt, provider_session)
                    except SourceIntegrityError:
                        if run.input.get("capability") != "code_review" or not created:
                            raise
                        return {"state": "blocked", "provider_state": "not_started",
                                "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure"}
                    except (TimeoutError, ConnectionError, httpx.TransportError, HTTPClientError):
                        outcome = None
                    except (BotoCoreError, ClientError):
                        if run.service_id != "well_architected":
                            raise
                        outcome = None
                    except (HTTPException, httpx.HTTPStatusError) as error:
                        status = error.status_code if isinstance(error, HTTPException) else error.response.status_code
                        if run.service_id == "transform" and status in {401, 403, 409, 410}:
                            raise AuthorityRevoked("native recovery access revoked") from None
                        raise
                    if outcome is not None and outcome.classification == "accepted":
                        native = outcome.native_job_ref
                        if run.service_id == "finops" and (not isinstance(native, dict)
                                or not all(isinstance(native.get(field), str) and native[field] for field in (
                                    "conversation_id", "agent_id", "tenant_id", "product_id", "project_id", "question_hash"))
                                or not (isinstance(native.get("turn_id") or native.get("turn_locator"), str)
                                        and (native.get("turn_id") or native.get("turn_locator")))
                                or native.get("agent_id") != run.admission["native_agent_ref"]["record_id"]
                                or native.get("product_id") != run.product_id
                                or native.get("question_hash") != attempt.question_hash
                                or set(native) not in ({"tenant_id", "product_id", "project_id", "agent_id", "conversation_id",
                                                       "turn_id", "question_hash"},
                                                      {"tenant_id", "product_id", "project_id", "agent_id", "conversation_id",
                                                       "turn_locator", "question_hash"})):
                            return {"state": run.state if finops_late else "outcome_unknown",
                                    "provider_state": "unknown",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "outcome_unknown",
                                    "next_kind": "reconcile" if not finops_late or now + timedelta(seconds=600) < recovery_until else None,
                                    "due_at": now + timedelta(seconds=600)}
                        if run.service_id == "well_architected" and (
                                not isinstance(native, dict)
                                or set(native) != {"tenant_id", "project_id", "profile_arn", "generation_id", "name"}
                                or native.get("profile_arn") != attempt.prepared_request["profile_arn"]
                                or native.get("name") != attempt.prepared_request["name"]
                                or not isinstance(native.get("generation_id"), str)
                                or not native["generation_id"]):
                            return {"state": "outcome_unknown", "provider_state": "unknown",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "outcome_unknown",
                                    "next_kind": "reconcile", "due_at": now + timedelta(seconds=600)}
                        if (native is None or native.get("tenant_id") != run.tenant_id
                                or native.get("project_id") != run.project_id):
                            raise ValueError("reconciled job does not match pinned scope")
                        return {"state": run.state if finops_late else "running", "provider_state": "running",
                                "native_job_ref": native, "attempt_id": attempt.attempt_id,
                                "attempt_status": "accepted", "next_kind": "poll",
                                "due_at": now if finops_late else min(now + timedelta(
                                    seconds=run.admission["monitoring_policy"]["poll_interval_seconds"]),
                                    recovery_until if finops_late else run.deadline_at.replace(
                                        tzinfo=run.deadline_at.tzinfo or UTC))}
                    if outcome is not None and outcome.classification == "definitive_failure":
                        if run.service_id == "security" and outcome.observation is not None:
                            return {"state": "failed", "provider_state": "not_started",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure",
                                    "poll_error": outcome.observation}
                        if finops_late:
                            return {"state": run.state, "provider_state": "not_started",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure"}
                        if run.service_id == "devops" and run.input.get("capability") == "release_verification":
                            return {"state": "failed", "provider_state": "not_started",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure",
                                    "poll_error": {"code": (outcome.observation or {}).get(
                                        "code", "verification_target_unverified")}}
                        if run.service_id == "devops" and run.input.get("capability") == "release_readiness":
                            return {"state": "blocked", "provider_state": "not_started",
                                    "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure",
                                    "poll_error": outcome.observation}
                        return {"state": "failed", "provider_state": "failed",
                                "attempt_id": attempt.attempt_id, "attempt_status": "definitive_failure"}
                    retry_seconds = (run.admission["monitoring_policy"]["poll_interval_seconds"]
                                     if run.service_id == "transform" else 600)
                    due_at = now + timedelta(seconds=retry_seconds)
                    return {"state": run.state if finops_late else "outcome_unknown",
                            "provider_state": "unknown",
                            "attempt_id": attempt.attempt_id, "attempt_status": "outcome_unknown",
                            "next_kind": "reconcile" if not finops_late or due_at < recovery_until else None,
                            "due_at": due_at}
                if claim.native_job_ref is None:
                    raise ValueError("missing persisted native job identity")
                if claim.kind == "poll" or finops_native_reconcile:
                    due_at = min(now + timedelta(
                        seconds=run.admission["monitoring_policy"]["poll_interval_seconds"]),
                        recovery_until if finops_late else run.deadline_at.replace(
                            tzinfo=run.deadline_at.tzinfo or UTC))

                    def retry_poll(code, observation=None):
                        return {"state": run.state, "provider_state": "unknown",
                                "poll_error": {"code": code}, "observation": observation,
                                "next_kind": "poll" if not finops_late or due_at > now else None,
                                "due_at": due_at}

                    try:
                        provider_session = await self._authorize(run, claim, "observe")
                        adapter = (self.finops_adapter if run.service_id == "finops" else
                                   self.well_architected_adapter if run.service_id == "well_architected" else self._adapter_for(run))
                        outcome = await adapter.observe(run, claim.native_job_ref,
                                                        attempt if devops_release_read else claim, provider_session)
                    except AuthorityRevoked:
                        if run.service_id == "well_architected":
                            return retry_poll("status_read_denied")
                        raise
                    except (BotoCoreError, ClientError):
                        if run.service_id == "well_architected":
                            return retry_poll("status_read_failed")
                        raise
                    except httpx.HTTPStatusError as error:
                        if run.service_id == "well_architected":
                            return retry_poll("status_read_failed")
                        if error.response.status_code in {401, 403}:
                            raise AuthorityRevoked("native access revoked") from None
                        return retry_poll("status_read_failed")
                    except HTTPException as error:
                        if run.service_id == "well_architected":
                            return retry_poll("status_read_failed")
                        if run.service_id == "finops" and error.status_code in {401, 403, 409, 410}:
                            raise AuthorityRevoked("native access revoked") from None
                        raise
                    except (TimeoutError, ConnectionError, httpx.TransportError):
                        return retry_poll("status_read_failed")
                    if outcome.observation is None:
                        return retry_poll("status_unknown")
                    if run.service_id == "finops" and set(outcome.observation) != {
                            "native_job_ref", "outcome", "observed_at"}:
                        return retry_poll("status_unknown")
                    if run.service_id == "finops":
                        try:
                            observed_at = datetime.fromisoformat(outcome.observation["observed_at"].replace(
                                "Z", "+00:00"))
                        except (AttributeError, TypeError, ValueError):
                            return retry_poll("status_unknown")
                        if observed_at.tzinfo is None:
                            return retry_poll("status_unknown")
                    if outcome.observation.get("native_job_ref") != claim.native_job_ref:
                        raise ValueError("provider status does not match pinned job")
                    if outcome.observation.get("outcome") != outcome.classification:
                        raise ValueError("provider status classification does not match observation")
                    if finops_late:
                        if outcome.classification == "completed":
                            return {"state": run.state, "provider_state": "completed",
                                    "observation": outcome.observation, "next_kind": "collect", "due_at": now}
                        if outcome.classification in {"running", "stopped", "failed"}:
                            return {"state": run.state, "provider_state": {
                                "running": "running", "stopped": "stopped", "failed": "failed",
                            }[outcome.classification], "observation": outcome.observation,
                                "next_kind": "poll" if outcome.classification == "running" and due_at > now else None,
                                "due_at": due_at}
                        return retry_poll("status_unknown", outcome.observation)
                    if outcome.classification == "failed":
                        if run.service_id == "devops" and run.input.get("capability") == "release_verification":
                            return {"state": "collecting", "provider_state": "failed",
                                    "observation": outcome.observation, "next_kind": "collect", "due_at": now}
                        return {"state": "failed", "provider_state": "failed",
                                "observation": outcome.observation}
                    if outcome.classification == "stopped":
                        return {"state": "cancelled" if run.state == "cancel_requested" else "failed",
                                "provider_state": "stopped", "observation": outcome.observation}
                    if outcome.classification == "unknown":
                        return retry_poll("status_unknown", None if run.service_id == "well_architected" else outcome.observation)
                    if run.service_id == "well_architected" and run.state == "cancel_requested" and outcome.classification == "completed":
                        return {"state": "cancelled", "provider_state": "completed",
                                "observation": outcome.observation, "next_kind": "collect", "due_at": now}
                    if run.state == "cancel_requested":
                        if outcome.classification != "running":
                            return retry_poll("status_unknown", outcome.observation)
                        return {"state": "cancel_requested", "provider_state": "running",
                                "observation": outcome.observation, "next_kind": "poll", "due_at": due_at}
                    if outcome.classification == "completed":
                        return {"state": "collecting", "provider_state": "completed",
                                "observation": outcome.observation, "next_kind": "collect", "due_at": now}
                    if outcome.classification != "running":
                        return retry_poll("status_unknown", outcome.observation)
                    return {"state": "running", "provider_state": "running",
                            "observation": outcome.observation, "next_kind": "poll", "due_at": due_at}
                if run.service_id == "finops" and claim.kind == "billing":
                    if run.provider_readback_ref is None or not run.admission["billing_scope"].get("snapshot_artifact"):
                        raise ValueError("billing collection requires retained native and scope evidence")
                    return await self._collect_billing_page(run, claim, now)
                if claim.kind == "collect":
                    provider_session = await self._authorize(run, claim, "collect")
                    adapter = (self.transform_adapter if run.service_id == "transform" else
                               self.finops_adapter if run.service_id == "finops" else
                               self.well_architected_adapter if run.service_id == "well_architected" else self._adapter_for(run))
                    outcome = await adapter.collect(run, claim.native_job_ref,
                                                    attempt if devops_release_read else claim, provider_session)
                    if run.service_id == "finops":
                        answer = outcome.result or {}
                        artifact = answer.get("answer_artifact") or {}
                        if (outcome.classification != "completed"
                                or set(answer) != {"kind", "native_job_ref", "status", "answer_artifact", "observed_at"}
                                or answer["kind"] != "finops_native_answer"
                                or answer["native_job_ref"] != claim.native_job_ref
                                or answer["status"] not in {"answered", "refused"}
                                or set(artifact) != {"artifact_id", "version", "sha256"}
                                or any(not isinstance(artifact[field], str) or not artifact[field]
                                       for field in artifact)
                                or len(artifact["sha256"]) != 64
                                or any(character not in "0123456789abcdef" for character in artifact["sha256"])
                                or not isinstance(answer["observed_at"], str)):
                            raise ValueError("native answer lacks a scoped, redacted readback")
                        try:
                            observed_at = datetime.fromisoformat(answer["observed_at"].replace("Z", "+00:00"))
                        except ValueError:
                            raise ValueError("native answer lacks an observed time") from None
                        if observed_at.tzinfo is None:
                            raise ValueError("native answer lacks an observed time")
                        from l5_services.billing_collection import request_limit

                        billing_ready = (not finops_late and hasattr(adapter, "collect_billing_page")
                                         and request_limit(run) is not None
                                         and bool(run.admission["billing_scope"].get("snapshot_artifact")))
                        return {"state": run.state if finops_late else "collecting" if billing_ready else "partial",
                                "provider_state": "completed", "provider_readback_ref": answer,
                                **({"next_kind": "billing", "due_at": now} if billing_ready else {})}
                    if outcome.classification != "completed" or outcome.result is None:
                        raise ValueError("incomplete result collection")
                    if (outcome.result.get("native_job_ref") not in (None, claim.native_job_ref)
                            or outcome.observation is not None
                            and outcome.observation.get("native_job_ref") != claim.native_job_ref):
                        raise ValueError("result does not match pinned job")
                    if run.service_id == "transform":
                        if "original_patch" not in outcome.result:
                            return {"state": "partial", "provider_state": "completed",
                                    "observation": {"native_job_ref": claim.native_job_ref,
                                                    "artifact_refs": outcome.result,
                                                    "observed_at": now.isoformat().replace("+00:00", "Z")}}
                        return await self._retain_transform_result(run, outcome.result, provider_session, now)
                    if run.service_id == "devops" and run.input.get("capability") == "release_readiness":
                        from l5_services.adapters.devops.release_readiness import (
                            QUALIFICATION_CODE, QUALIFICATION_MESSAGE,
                        )

                        return {"state": "partial", "provider_state": "completed", "result": outcome.result,
                                "poll_error": {"code": QUALIFICATION_CODE, "message": QUALIFICATION_MESSAGE}}
                    verification = (run.service_id == "devops"
                                    and run.input.get("capability") == "release_verification")
                    native_failed = verification and run.provider_state == "failed"
                    failed_scenarios = verification and (native_failed or outcome.result.get("provider_outcome") == "failed")
                    return {"state": "failed" if failed_scenarios else "succeeded",
                            "provider_state": "failed" if native_failed else "completed", "observation": outcome.observation,
                            "result": outcome.result}
                raise ValueError("unsupported work kind")

        async def leased_transition(claim):
            nonlocal active_claim
            from datetime import timedelta
            from l5_services.adapters.transform.baseline import pinned_test_policy
            from l5_services.adapters.transform.verification import DetachedCheckPending
            from l5_services.models.run import Run
            from l5_services.work_store import renew_work_lease

            if (self.service_id == "security" and claim.run_input.get("capability") == "code_review"
                    and claim.kind in {"execute", "submit"}):
                active_claim = claim
                if not await renew_work_lease(self.sessions, claim, now=self.clock(), lease_seconds=960):
                    raise AuthorityRevoked("Security repository work lease changed")
                try:
                    return await asyncio.wait_for(transition(claim), timeout=840)
                except TimeoutError:
                    if claim.kind != "execute":
                        raise
                    return {"state": "blocked", "provider_state": "not_started", "poll_error": {
                        "code": "repository_transfer_timed_out",
                        "message": "Preparing the complete repository exceeded the transfer window. No code scan was submitted. Check Product source connectivity before retrying this run.",
                    }}
            if self.transform_adapter is None or claim.kind not in {"submit", "collect"}:
                return await transition(claim)
            async with self.sessions() as session:
                service_id = await session.scalar(select(Run.service_id).where(
                    Run.tenant_id == claim.tenant_id, Run.run_id == claim.run_id))
            if service_id != "transform":
                return await transition(claim)
            if getattr(self.transform_adapter.detached_runner, "nonblocking", False):
                try:
                    return await transition(claim)
                except DetachedCheckPending:
                    async with self.sessions() as session:
                        current = await session.get(Run, (claim.tenant_id, claim.run_id))
                        return {"state": current.state, "provider_state": current.provider_state,
                                "next_kind": claim.kind, "due_at": self.clock() + timedelta(seconds=15)}
            try:
                policy, _ = pinned_test_policy(claim.admission)
            except (ValueError, TypeError):
                return await transition(claim)
            command_seconds = sum(command["timeout_seconds"] + 25 for command in policy["commands"])
            retention_seconds = 15 * (1 + 4 * len(policy["commands"])) if claim.kind == "collect" else 0
            timeout_seconds = 60 + 4 * self.transform_adapter.call_seconds + command_seconds + retention_seconds
            active_claim = claim
            if not await renew_work_lease(self.sessions, claim, now=self.clock(),
                                          lease_seconds=timeout_seconds + 60):
                raise AuthorityRevoked("Transform work lease changed")
            return await asyncio.wait_for(transition(claim), timeout=timeout_seconds)

        try:
            completed = await execute_once(self.sessions, envelope, leased_transition,
                                           self.publish, now=now, clock=self.clock, service_id=self.service_id)
            if (completed and active_claim is not None and active_claim.kind == "collect"
                    and isinstance(active_claim.admission, dict)
                    and active_claim.admission.get("native_profile_ref") is not None
                    and self.well_architected_verifier is not None):
                from l5_services.adapters.well_architected.results import persist_wa_comparison
                from l5_services.models.evidence import AssessmentResult

                async with self.sessions() as session:
                    original = await session.scalar(select(AssessmentResult.result_id).where(
                        AssessmentResult.tenant_id == active_claim.tenant_id,
                        AssessmentResult.run_id == active_claim.run_id,
                        AssessmentResult.version == 1))
                if original is None:
                    return completed

                clients = {"product-context": self.well_architected_context_client,
                           "engineering-insights": self.well_architected_insights_client}
                try:
                    await persist_wa_comparison(
                        self.sessions, active_claim.tenant_id, active_claim.run_id, self.archive_store,
                        self.well_architected_verifier, now=self.clock(), clients=clients)
                except (ValueError, TimeoutError, ConnectionError):
                    LOGGER.exception("Post-commit WA comparison failed; original result remains durable")
            return completed
        except AuthorityRevoked as error:
            from l5_services.models.run import Run
            from l5_services.workers.diagnostics import authority_failure

            if active_claim is None:
                raise
            now = self.clock()
            async with self.sessions() as session:
                run = await session.get(Run, (active_claim.tenant_id, active_claim.run_id))
            expired = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) <= now
            if run.service_id == "transform":
                return await finish_work(self.sessions, active_claim,
                                         state=run.state if run.state in {"cancelled", "expired"} else (
                                             "expired" if expired else "outcome_unknown"),
                                         provider_state=run.provider_state if run.native_job_ref else "unknown",
                                         poll_error={"code": "recovery_authority_unavailable"}, now=now) is not None
            if run.service_id == "finops" and run.state in {"cancelled", "expired"}:
                return await finish_work(self.sessions, active_claim, state=run.state,
                                         provider_state=run.provider_state if active_claim.native_job_ref else "unknown",
                                         poll_error={"code": "recovery_authority_unavailable"}, now=now) is not None
            state = "expired" if expired else "outcome_unknown" if active_claim.native_job_ref else "blocked"
            provider = run.provider_state if active_claim.native_job_ref else "unknown"
            error_detail = authority_failure(error, active_claim.kind)
            if expired and run.service_id in {"security", "devops"} and active_claim.native_job_ref:
                if run.service_id == "devops":
                    from l5_services.devops.recovery import recovery_error
                else:
                    from l5_services.security_recovery import recovery_error

                if (run.monitoring or {}).get(run.service_id + "_recovery"):
                    error_detail = {**error_detail, "code": run.service_id + "_recovery_authority_unavailable",
                                    "message": "ADP could not authorize reading the existing AWS job.",
                                    "action": "Check current service consent, connection verification and source access, then reconcile this run again."}
                else:
                    error_detail = {**error_detail, **recovery_error()}
            return await finish_work(self.sessions, active_claim, state=state,
                                     provider_state=provider, poll_error=error_detail, now=now) is not None

    async def _recover_native_job(self, run, claim, now):
        from datetime import timedelta

        import httpx
        from botocore.exceptions import BotoCoreError, ClientError

        if run.service_id == "devops":
            from l5_services.devops.recovery import recovery_deadline, recovery_error
        else:
            from l5_services.security_recovery import recovery_deadline, recovery_error
        from l5_services.work_store import current_work_fence, record_poll_attempt

        until = recovery_deadline(run, claim.kind, now)
        if until is None:
            return {"state": "expired", "provider_state": run.provider_state,
                    "poll_error": recovery_error()}
        if claim.native_job_ref != run.native_job_ref:
            raise ValueError("Native recovery identity changed")
        if not await record_poll_attempt(self.sessions, claim, now=now,
                                         allow_exhausted=bool((run.monitoring or {}).get(run.service_id + "_recovery"))):
            return {"state": "expired", "provider_state": run.provider_state,
                    "poll_error": recovery_error("poll_limit_reached")}
        authority = await self._authorize(run, claim, "collect" if claim.kind == "collect" else "observe")
        if not await current_work_fence(self.sessions, claim, now=self.clock()):
            raise AuthorityRevoked("Native recovery lease changed")
        adapter = self._adapter_for(run)
        due = min(now + timedelta(seconds=max(15, run.admission["monitoring_policy"]["poll_interval_seconds"])), until)
        try:
            outcome = await (adapter.collect if claim.kind == "collect" else adapter.observe)(
                run, claim.native_job_ref, claim, authority)
        except (TimeoutError, ConnectionError, httpx.TransportError, BotoCoreError, ClientError):
            return {"state": "expired", "provider_state": run.provider_state,
                    "poll_error": {"code": run.service_id + "_recovery_read_failed",
                                   "action": "ADP could not read the existing AWS job. It will retry within the recovery window; reconcile again if the read remains unavailable."},
                    "next_kind": claim.kind if due < until else None, "due_at": due}
        observation = outcome.observation
        if (observation is not None and (not isinstance(observation, dict) or observation.get("native_job_ref") != claim.native_job_ref
                or observation.get("outcome") != outcome.classification)):
            raise ValueError("Native recovery observation differs from the pinned job")
        if claim.kind == "collect":
            if (outcome.classification != "completed" or not isinstance(outcome.result, dict)
                    or outcome.result.get("native_job_ref") != claim.native_job_ref):
                raise ValueError("Native recovery result differs from the completed job")
            return {"state": "succeeded", "provider_state": "completed",
                    "observation": observation, "result": outcome.result}
        if outcome.classification == "completed":
            if observation is None:
                raise ValueError("Native recovery requires a completed job observation")
            return {"state": "collecting", "provider_state": "completed",
                    "observation": observation, "next_kind": "collect", "due_at": now}
        if outcome.classification in {"failed", "stopped"}:
            return {"state": "failed", "provider_state": outcome.classification, "observation": observation}
        return {"state": "expired", "provider_state": "running" if outcome.classification == "running" else "unknown",
                "observation": observation, "next_kind": "poll" if due < until else None, "due_at": due,
                **({"poll_error": recovery_error()} if due >= until else {})}

    async def _retain_transform_result(self, run, result, authority, now):
        from l5_services.adapters.transform.retention import normalize_transform_result, retain_transform_artifacts

        adapter = self.transform_adapter
        if adapter.retention_store is None:
            raise ValueError("durable Transform artifact store is unavailable")
        retained = await retain_transform_artifacts(run, result, adapter.bridge, adapter.detached_runner,
                                                    adapter.retention_store, authority, now=now)
        collected = {**result, "retained_artifacts": retained}
        verdict = normalize_transform_result(run, "precommit", 1, collected, now)
        return {"state": verdict.payload["verification_status"],
                "provider_state": "completed", "result": collected}

    async def _recover_transform(self, run, claim, now):
        from datetime import timedelta

        import httpx
        from fastapi import HTTPException
        from sqlalchemy import select

        from l5_services.models.attempt import WorkerAttempt
        from l5_services.work_store import (
            AttemptContext, TRANSFORM_SUBMIT_OPERATION, current_work_fence,
            prepare_submit_attempt, record_poll_attempt,
        )

        terminal = run.state in {"cancelled", "expired"}
        deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
        if not terminal and now >= deadline:
            return await self._stop_remote(run, claim, now, "expired")
        policy = run.admission["monitoring_policy"]
        recovery_until = deadline + timedelta(seconds=policy["poll_grace_seconds"])
        if terminal and now >= recovery_until:
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": "recovery_window_exhausted"}}
        if self.transform_adapter is None:
            raise RuntimeError("Transform recovery bridge is unavailable")
        self.transform_adapter._qualification(run)
        if claim.kind == "reconcile" and claim.native_job_ref is None:
            attempt, created = await prepare_submit_attempt(self.sessions, claim, now=now)
            if created:
                raise ValueError("recovery cannot create a provider submission")
            if not attempt.can_reconcile:
                return {"state": run.state, "provider_state": run.provider_state,
                        "poll_error": {"code": "reconcile_limit_reached"}}
            session = await self._authorize(run, claim, "recover_reconcile" if terminal else "reconcile")
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise AuthorityRevoked("Transform recovery lease changed")
            try:
                outcome = await self.transform_adapter.reconcile_submit(run, attempt, session)
            except (TimeoutError, ConnectionError, httpx.TransportError):
                outcome = None
            except (HTTPException, httpx.HTTPStatusError) as error:
                status = error.status_code if isinstance(error, HTTPException) else error.response.status_code
                if status in {401, 403, 409, 410}:
                    raise AuthorityRevoked("native recovery access revoked") from None
                raise
            if outcome is not None and outcome.classification == "accepted":
                native = outcome.native_job_ref
                if native is None or native.get("tenant_id") != run.tenant_id or native.get("project_id") != run.project_id:
                    raise ValueError("recovered provider identity differs from run")
                return {"state": run.state, "provider_state": run.provider_state,
                        "native_job_ref": native, "attempt_id": attempt.attempt_id,
                        "attempt_status": "accepted", "next_kind": "poll", "due_at": now}
            due_at = now + timedelta(seconds=policy["poll_interval_seconds"])
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": "native_readback_unavailable"},
                    "next_kind": "reconcile" if due_at < (recovery_until if terminal else deadline) else None,
                    "due_at": due_at}
        if claim.native_job_ref is None or claim.kind not in {"poll", "collect"}:
            raise ValueError("recovery needs the original native identity")
        if not await record_poll_attempt(self.sessions, claim, now=now):
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": "poll_limit_reached"},
                    "next_kind": claim.kind if not terminal else None, "due_at": deadline}
        async with self.sessions() as session:
            saved = await session.scalar(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == claim.tenant_id, WorkerAttempt.run_id == claim.run_id,
                WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
        if saved is None or saved.native_job_ref != claim.native_job_ref:
            raise ValueError("recovery does not match committed native attempt")
        from l5_services.work_store import transform_baseline

        from types import SimpleNamespace

        original_claim = SimpleNamespace(tenant_id=claim.tenant_id, run_id=claim.run_id,
                                         work_id=saved.first_work_id)
        baseline = await transform_baseline(self.sessions, original_claim)
        attempt = AttemptContext(saved.attempt_id, saved.run_id, claim.work_id, claim.fence,
                                 saved.idempotency_token, saved.input_hash,
                                 saved.started_at.replace(tzinfo=saved.started_at.tzinfo or UTC), True,
                                 baseline_policy_sha256=baseline.get("policy_sha256") if baseline else None,
                                 baseline_receipts=[check["receipt"] for check in baseline["checks"]]
                                 if baseline and baseline.get("status") == "passed" else None)
        operation = ("recover_collect" if claim.kind == "collect" else "recover_observe") if terminal else claim.kind
        authority = await self._authorize(run, claim, operation)
        if not await current_work_fence(self.sessions, claim, now=self.clock()):
            raise AuthorityRevoked("Transform recovery lease changed")
        try:
            if claim.kind == "collect":
                outcome = await self.transform_adapter.collect(run, claim.native_job_ref, attempt, authority)
            else:
                outcome = await self.transform_adapter.observe(run, claim.native_job_ref, attempt, authority)
        except (TimeoutError, ConnectionError, httpx.TransportError):
            outcome = None
        except (HTTPException, httpx.HTTPStatusError) as error:
            status = error.status_code if isinstance(error, HTTPException) else error.response.status_code
            if status in {401, 403, 409, 410}:
                raise AuthorityRevoked("native recovery access revoked") from None
            raise
        if outcome is not None and outcome.classification == "completed":
            observed = {"native_job_ref": claim.native_job_ref, "outcome": "completed",
                        "observed_at": now.isoformat().replace("+00:00", "Z")}
            if claim.kind == "collect":
                if outcome.result is None:
                    raise ValueError("collection has no native artifacts")
                if "original_patch" in outcome.result:
                    collected = await self._retain_transform_result(run, outcome.result, authority, now)
                    if terminal:
                        collected["state"] = run.state
                    return {**collected, "observation": observed}
                observed["artifact_refs"] = outcome.result
                return {"state": run.state, "provider_state": "completed", "observation": observed}
            return {"state": run.state if terminal else "collecting", "provider_state": "completed",
                    "observation": observed, "next_kind": "collect", "due_at": now}
        if outcome is not None and outcome.classification in {"stopped", "failed"}:
            observed = {"native_job_ref": claim.native_job_ref, "outcome": outcome.classification,
                        "observed_at": now.isoformat().replace("+00:00", "Z")}
            return {"state": run.state if terminal else "failed", "provider_state": outcome.classification,
                    "observation": observed}
        due_at = now + timedelta(seconds=policy["poll_interval_seconds"])
        if not terminal:
            due_at = min(due_at, deadline)
        observed = ({"native_job_ref": claim.native_job_ref, "outcome": "running",
                     "observed_at": now.isoformat().replace("+00:00", "Z")}
                    if outcome is not None and outcome.classification == "running" else None)
        return {"state": run.state, "provider_state": "running" if observed else run.provider_state,
                "observation": observed, "poll_error": None if observed else {"code": "native_readback_unavailable"},
                "next_kind": claim.kind if not terminal or due_at < recovery_until else None,
                "due_at": due_at}

    async def _recover_well_architected(self, run, claim, now):
        from datetime import timedelta

        import httpx
        from botocore.exceptions import BotoCoreError, ClientError
        from fastapi import HTTPException

        from l5_services.work_store import (checkpoint_generation_detail, checkpoint_generation_page, current_work_fence,
                                             generation_collection, prepare_submit_attempt, record_poll_attempt,
                                             renew_work_lease)

        policy = run.admission["monitoring_policy"]
        deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
        recovery_until = deadline + timedelta(seconds=policy["poll_grace_seconds"])
        manual_read = (claim.kind == "reconcile"
                       and (run.monitoring or {}).get("state") == "recovery_blocked")
        terminal_readback = (claim.kind == "collect" and run.provider_state == "completed"
                             and ((run.monitoring or {}).get("latest_observation") or {}).get("native_job_ref")
                             == claim.native_job_ref)
        if now >= recovery_until and not (manual_read or terminal_readback):
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": "recovery_window_exhausted"}}
        if manual_read and run.provider_state == "completed":
            return {"state": run.state, "provider_state": "completed", "next_kind": "collect", "due_at": now}
        due_at = min(now + timedelta(seconds=policy["poll_interval_seconds"]), recovery_until)

        def retry(code):
            next_kind = "reconcile" if claim.native_job_ref is None else (
                "collect" if claim.kind == "collect" else "poll")
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": code}, "next_kind": next_kind if due_at > now and not manual_read else None,
                    "due_at": due_at}

        if claim.native_job_ref is None:
            if claim.kind != "reconcile":
                raise ValueError("missing original generation identity")
            attempt, created = await prepare_submit_attempt(self.sessions, claim, now=now)
            if created:
                raise ValueError("native recovery cannot create a Start attempt")
            if not attempt.can_reconcile:
                return {"state": run.state, "provider_state": run.provider_state,
                        "poll_error": {"code": "reconcile_limit_reached"}}
            try:
                session = await self._authorize(run, claim, "reconcile")
                if not await current_work_fence(self.sessions, claim, now=self.clock()):
                    raise AuthorityRevoked("native recovery lease changed")
                outcome = await self.well_architected_adapter.reconcile_submit(run, attempt, session)
            except (AuthorityRevoked, BotoCoreError, ClientError, HTTPException,
                    httpx.HTTPStatusError, httpx.TransportError, TimeoutError, ConnectionError):
                return retry("reconcile_read_failed")
            if outcome.classification != "accepted":
                return retry("reconcile_unknown")
            native = outcome.native_job_ref
            if (not isinstance(native, dict)
                    or set(native) != {"tenant_id", "project_id", "profile_arn", "generation_id", "name"}
                    or native.get("tenant_id") != run.tenant_id or native.get("project_id") != run.project_id
                    or native.get("profile_arn") != attempt.prepared_request["profile_arn"]
                    or native.get("name") != attempt.prepared_request["name"]
                    or not isinstance(native.get("generation_id"), str) or not native["generation_id"]):
                return retry("reconcile_unknown")
            return {"state": run.state, "provider_state": "running", "native_job_ref": native,
                    "attempt_id": attempt.attempt_id, "attempt_status": "accepted",
                    "next_kind": "poll", "due_at": now}
        if claim.kind == "collect":
            checkpoint = await generation_collection(self.sessions, claim)
            if checkpoint is not None and checkpoint["result_id"] is not None:
                raise ValueError("native collection result already committed")
            pending = checkpoint["pending_recommendations"] if checkpoint is not None else []
            if pending or checkpoint is None or checkpoint["next_token"] is not None:
                request_token = checkpoint["next_token"] if checkpoint is not None else None
                try:
                    session = await self._authorize(run, claim, "collect")
                    if not await renew_work_lease(self.sessions, claim, now=self.clock(), lease_seconds=60):
                        raise AuthorityRevoked("native collection lease changed")
                    if pending:
                        recommendation = await self.well_architected_adapter.collect_detail(
                            run, claim.native_job_ref, claim, session, recommendation=pending[0])
                    else:
                        outcome = await self.well_architected_adapter.collect(
                            run, claim.native_job_ref, claim, session, next_token=request_token)
                except AuthorityRevoked:
                    return retry("collection_read_denied")
                except (BotoCoreError, ClientError, HTTPException, httpx.HTTPStatusError,
                        httpx.TransportError, TimeoutError, ConnectionError):
                    return retry("collection_read_failed")
                if pending:
                    checkpoint = await checkpoint_generation_detail(
                        self.sessions, claim, recommendation, now=self.clock())
                else:
                    if (outcome.classification != "completed" or outcome.result is None
                            or outcome.result.get("native_job_ref") != claim.native_job_ref
                            or outcome.result.get("request_token") != request_token
                            or outcome.observation is not None
                            and outcome.observation.get("native_job_ref") != claim.native_job_ref):
                        raise ValueError("collection does not match original generation")
                    page = outcome.result
                    checkpoint = await checkpoint_generation_page(
                        self.sessions, claim, request_token, page.get("next_token"), page.get("recommendations"),
                        pending_recommendations=page.get("pending_recommendations"),
                        excluded=page.get("excluded"), now=self.clock())
                if checkpoint["pending_recommendations"] or checkpoint["next_token"] is not None:
                    return {"state": run.state, "provider_state": "completed",
                            "next_kind": "collect", "due_at": self.clock()}
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise AuthorityRevoked("native collection lease changed")
            return {"state": run.state if run.state in {"cancelled", "expired"} else "succeeded",
                    "provider_state": "completed", "result": {
                        "native_job_ref": claim.native_job_ref,
                        "recommendations": checkpoint["recommendations"],
                        "excluded": checkpoint["excluded"],
                        "page_count": checkpoint["page_count"],
                        "observed_at": self.clock().isoformat()}}
        if claim.kind not in {"poll", "reconcile"}:
            raise ValueError("unsupported native recovery kind")
        if not await record_poll_attempt(self.sessions, claim, now=now, allow_exhausted=manual_read):
            return {"state": run.state, "provider_state": run.provider_state,
                    "poll_error": {"code": "poll_limit_reached"}}
        try:
            session = await self._authorize(run, claim, "observe")
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise AuthorityRevoked("native observation lease changed")
            outcome = await self.well_architected_adapter.observe(run, claim.native_job_ref, claim, session)
        except AuthorityRevoked:
            return retry("status_read_denied")
        except (BotoCoreError, ClientError, HTTPException, httpx.HTTPStatusError,
                httpx.TransportError, TimeoutError, ConnectionError):
            return retry("status_read_failed")
        if outcome.observation is None or outcome.classification == "unknown":
            return retry("status_unknown")
        if (outcome.observation.get("native_job_ref") != claim.native_job_ref
                or outcome.observation.get("outcome") != outcome.classification):
            raise ValueError("native status does not match original generation")
        if outcome.classification == "completed":
            return {"state": run.state, "provider_state": "completed", "observation": outcome.observation,
                    "next_kind": "collect", "due_at": now}
        if outcome.classification == "failed":
            return {"state": run.state, "provider_state": "failed", "observation": outcome.observation}
        if outcome.classification != "running":
            return retry("status_unknown")
        return {"state": run.state, "provider_state": "running", "observation": outcome.observation,
                "next_kind": "poll" if due_at > now and not manual_read else None, "due_at": due_at}

    async def _devops_release_attempt(self, run, claim):
        from sqlalchemy import select
        from l5_services.models.attempt import WorkerAttempt

        async with self.sessions() as session:
            attempt = await session.scalar(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == claim.tenant_id,
                WorkerAttempt.run_id == claim.run_id, WorkerAttempt.operation == "submit"))
        if (attempt is None or attempt.native_job_ref != claim.native_job_ref
                or run.native_job_ref != claim.native_job_ref):
            raise ValueError("release read requires the original accepted attempt")
        return attempt

    async def _stop_remote(self, run, claim, now, reason):
        from datetime import timedelta

        if run.service_id == "well_architected":
            deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
            recovery_until = deadline + timedelta(seconds=run.admission["monitoring_policy"]["poll_grace_seconds"])
            if claim.native_job_ref is not None:
                return {"state": reason, "provider_state": run.provider_state,
                        "next_kind": "poll" if now < recovery_until else None, "due_at": now}
            from sqlalchemy import select
            from l5_services.models.attempt import WorkerAttempt

            async with self.sessions() as session:
                existing = await session.scalar(select(WorkerAttempt.attempt_id).where(
                    WorkerAttempt.tenant_id == claim.tenant_id, WorkerAttempt.run_id == claim.run_id,
                    WorkerAttempt.operation == "submit"))
            return {"state": reason, "provider_state": "unknown" if existing else "not_started",
                    "next_kind": "reconcile" if existing and now < recovery_until else None, "due_at": now}

        if run.service_id == "transform":
            from sqlalchemy import select

            from l5_services.models.attempt import WorkerAttempt
            from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION

            async with self.sessions() as session:
                has_attempt = await session.scalar(select(WorkerAttempt.attempt_id).where(
                    WorkerAttempt.tenant_id == claim.tenant_id,
                    WorkerAttempt.run_id == claim.run_id,
                    WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
            recovery_until = (run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
                              + timedelta(seconds=run.admission["monitoring_policy"]["poll_grace_seconds"]))
            native_known = claim.native_job_ref is not None
            recoverable = now < recovery_until and (native_known or has_attempt is not None)
            native_kind = "collect" if claim.kind == "collect" else "poll"
            return {"state": reason, "provider_state": run.provider_state if native_known else (
                "unknown" if has_attempt is not None else "not_started"),
                "next_kind": native_kind if recoverable and native_known else (
                    "reconcile" if recoverable else None), "due_at": now}
        if run.service_id == "finops":
            from sqlalchemy import select

            from l5_services.models.attempt import WorkerAttempt

            async with self.sessions() as session:
                existing = await session.scalar(select(WorkerAttempt.attempt_id).where(
                    WorkerAttempt.tenant_id == claim.tenant_id,
                    WorkerAttempt.run_id == claim.run_id, WorkerAttempt.operation == "submit"))
            recovery_until = (run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
                              + timedelta(seconds=run.admission["monitoring_policy"]["poll_grace_seconds"]))
            recoverable = now < recovery_until and (claim.native_job_ref is not None or existing is not None)
            return {"state": reason, "provider_state": run.provider_state if claim.native_job_ref else (
                "unknown" if existing is not None else "not_started"),
                "next_kind": "poll" if recoverable and claim.native_job_ref else (
                    "reconcile" if recoverable else None),
                "due_at": now}
        if claim.native_job_ref is None:
            from sqlalchemy import select

            from l5_services.models.attempt import WorkerAttempt
            from l5_services.work_store import prepare_submit_attempt

            async with self.sessions() as session:
                existing = await session.scalar(select(WorkerAttempt).where(
                    WorkerAttempt.tenant_id == claim.tenant_id,
                    WorkerAttempt.run_id == claim.run_id, WorkerAttempt.operation == "submit"))
            if existing is None:
                return {"state": reason, "provider_state": "not_started"}
            attempt, _ = await prepare_submit_attempt(self.sessions, claim, now=now)
            if not attempt.can_reconcile:
                return {"state": "expired" if reason == "expired" else "outcome_unknown",
                        "provider_state": "unknown"}
            provider_session = await self._authorize(run, claim, "reconcile")
            outcome = await self._adapter_for(run).reconcile_submit(run, attempt, provider_session)
            if outcome.classification == "definitive_failure":
                return {"state": reason, "provider_state": "not_started"}
            native = outcome.native_job_ref
            if (outcome.classification != "accepted" or native is None
                    or native.get("tenant_id") != run.tenant_id
                    or native.get("project_id") != run.project_id):
                return {"state": "expired" if reason == "expired" else "outcome_unknown",
                        "provider_state": "unknown"}
            return {"state": "cancel_requested" if reason == "cancelled" else "expired",
                    "provider_state": "running", "native_job_ref": native,
                    "attempt_id": attempt.attempt_id, "attempt_status": "accepted",
                    "next_kind": "cancel", "due_at": now}
        if not callable(getattr(self._adapter_for(run), "cancel", None)):
            return {"state": "expired" if reason == "expired" else "outcome_unknown",
                    "provider_state": run.provider_state}
        adapter_attempt = (await self._devops_release_attempt(run, claim)
                           if run.service_id == "devops" and run.input.get("capability") in {
                               "release_readiness", "release_verification"} else claim)
        if claim.kind == "cancel" and claim.fence > 1:
            provider_session = await self._authorize(run, claim, "observe")
            outcome = await self._adapter_for(run).observe(run, claim.native_job_ref, adapter_attempt, provider_session)
            if outcome.observation is None:
                return {"state": "expired" if reason == "expired" else "outcome_unknown",
                        "provider_state": "unknown"}
        else:
            provider_session = await self._authorize(run, claim, "cancel")
            outcome = await self._adapter_for(run).cancel(run, claim.native_job_ref, adapter_attempt, provider_session)
        if (outcome.observation is not None
                and outcome.observation.get("native_job_ref") != claim.native_job_ref):
            raise ValueError("cancellation does not match pinned job")
        if outcome.classification == "stopped":
            return {"state": reason, "provider_state": "stopped",
                    "observation": outcome.observation}
        if outcome.classification == "running":
            if reason == "cancelled":
                return {"state": "cancel_requested", "provider_state": "running",
                        "observation": outcome.observation, "next_kind": "poll",
                        "due_at": min(now + timedelta(seconds=run.admission["monitoring_policy"]["poll_interval_seconds"]),
                                      run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC))}
            return {"state": "expired", "provider_state": "running",
                    "observation": outcome.observation}
        return {"state": "expired" if reason == "expired" else "outcome_unknown",
                "provider_state": "unknown", "observation": outcome.observation}


async def process_batch(event, service, *, request_id=None):
    """Report only failed SQS records; never retry a committed batch neighbor."""
    import json

    failures = []
    for record in event["Records"]:
        envelope = None
        try:
            envelope = json.loads(record["body"])
            await service.execute(envelope)
        except Exception as error:
            failures.append({"itemIdentifier": record["messageId"]})
            log_record_failure(LOGGER, error, record=record, envelope=envelope,
                               service=service, request_id=request_id)
    return {"batchItemFailures": failures}


def load_service():
    import importlib
    import os

    module_name, separator, factory_name = os.environ["L5_WORKER_FACTORY"].partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("L5_WORKER_FACTORY must be module:factory")
    service = getattr(importlib.import_module(module_name), factory_name)()
    selected = os.environ.get("L5_SERVICE_ID")
    if selected and getattr(service, "service_id", None) != selected:
        raise ValueError("Installed factory does not own the selected L5 service")
    return service


def lambda_handler(event, context, *, service=None):
    """Packaged SQS Lambda entry point; deployment supplies its runtime factory."""
    import asyncio

    from l5_services.workers.database_probe import REQUEST_KEY, run_probe

    if isinstance(event, dict) and REQUEST_KEY in event:
        return asyncio.run(run_probe(event))
    selected = service or load_service()
    if event == {"l5_runtime_probe": 1}:
        return {"service_id": selected.service_id}
    if event == {"l5_runtime_probe": 1, "connectivity": "transform"}:
        probe = getattr(selected, "runtime_connectivity_probe", None)
        if selected.service_id != "transform" or not callable(probe):
            raise ValueError("Transform connectivity probe is unavailable in the selected worker")
        return {"service_id": selected.service_id, "connectivity": probe()}
    return asyncio.run(process_batch(event, selected, request_id=getattr(context, "aws_request_id", None)))
