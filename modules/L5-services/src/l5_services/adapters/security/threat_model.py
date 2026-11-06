"""Document-only Security Agent threat assessment on an authorized persistent model."""

from asyncio import to_thread
from datetime import UTC, datetime

import httpx
from botocore.exceptions import ClientError

from l5_services.adapters.protocol import AdapterOutcome
from l5_services.source_manifest import SourceIntegrityError, materialize_inputs
from l5_services.workers.execution import AuthorityRevoked


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("invalid native timestamp")
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat().replace("+00:00", "Z")


class ThreatModelAdapter:
    """The scoped session supplies the provider client and already-authorized native IDs."""

    def __init__(self, source_client, *, clock=None):
        self.source_client = source_client
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _scope(run, session):
        if (run.service_id != "security" or run.input.get("capability") != "threat_model"
                or not run.manifest or run.manifest.get("coverage_mode") != "documents"
                or run.manifest.get("sources") != run.input.get("sources")
                or any(source.get("record_type") != "document" for source in run.input["sources"])):
            raise ValueError("unsupported threat model input")
        if (session["binding_id"] != run.binding_id
                or session["tenant_id"] != run.tenant_id
                or session["project_id"] != run.project_id
                or session["native_space_ref"] != run.admission.get("native_space_ref")
                or session["native_model_ref"] != run.admission.get("native_model_ref")):
            raise PermissionError("provider session does not match run scope")
        space_id = session["native_space_ref"]["record_id"]
        model_id = session["native_model_ref"]["record_id"]
        if not space_id or not model_id:
            raise ValueError("ready space and persistent model required")
        return session["client"], space_id, model_id

    @staticmethod
    def _native(run, space_id, model_id, job_id, artifact_id):
        return {"tenant_id": run.tenant_id, "project_id": run.project_id,
                "agent_space_id": space_id, "threat_model_id": model_id,
                "threat_model_job_id": job_id, "artifact_id": artifact_id}

    def _check_job(self, run, native, session):
        _, space_id, model_id = self._scope(run, session)
        if (native.get("tenant_id") != run.tenant_id or native.get("project_id") != run.project_id
                or native.get("agent_space_id") != space_id or native.get("threat_model_id") != model_id
                or not native.get("threat_model_job_id") or not native.get("artifact_id")):
            raise ValueError("native job does not match selected model")
        return session["client"], space_id, model_id, native["threat_model_job_id"]

    async def _job(self, run, native, session):
        client, space_id, model_id, job_id = self._check_job(run, native, session)
        response = await to_thread(client.batch_get_threat_model_jobs,
                                   agentSpaceId=space_id, threatModelJobIds=[job_id])
        jobs = response.get("threatModelJobs", [])
        if (response.get("notFound") or len(jobs) != 1
                or any(jobs[0].get(key) != value for key, value in (
                    ("agentSpaceId", space_id), ("threatModelId", model_id), ("threatModelJobId", job_id)))
                or {item.get("artifactId") for item in jobs[0].get("scopeDocs", [])} != {native["artifact_id"]}):
            raise ValueError("native job readback mismatch")
        return jobs[0]

    async def submit(self, run, manifest, attempt, session):
        client, space_id, model_id = self._scope(run, session)
        if manifest != run.manifest:
            raise SourceIntegrityError("manifest changed after admission")
        try:
            _, content = await materialize_inputs(run, session.get("claim", attempt),
                                                  self.source_client, now=self.clock())
        except SourceIntegrityError:
            return AdapterOutcome("definitive_failure")
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403, 409, 410}:
                raise AuthorityRevoked("document receipt no longer authorized") from None
            raise
        if len(content) != 1 or len(run.input["sources"]) != 1:
            raise ValueError("one architecture document per native assessment")
        if (run.manifest["archive"]["sha256"] != run.admission["source_snapshots"][0]["content"]["sha256"]):
            return AdapterOutcome("definitive_failure")
        response = await to_thread(client.batch_get_threat_models, agentSpaceId=space_id,
                                   threatModelIds=[model_id])
        models = response.get("threatModels", [])
        if (response.get("notFound") or len(models) != 1
                or models[0].get("agentSpaceId") != space_id or models[0].get("threatModelId") != model_id):
            raise ValueError("persistent model readback mismatch")
        media_type = run.manifest["archive"]["media_type"]
        artifact_type, suffix = {"text/markdown": ("MD", ".md"), "text/plain": ("TXT", ".txt"),
                                 "application/pdf": ("PDF", ".pdf")}.get(media_type, (None, None))
        if artifact_type is None:
            raise ValueError("unsupported architecture document format")
        artifact = await to_thread(client.add_artifact, agentSpaceId=space_id,
                                   artifactContent=content[0], artifactType=artifact_type,
                                   fileName=f"{run.run_id}{suffix}")
        artifact_id = artifact["artifactId"]
        try:
            # Scope documents belong to scopeDocs. The live service rejects documents
            # under assets even though the SDK still exposes that legacy member.
            await to_thread(client.update_threat_model, agentSpaceId=space_id, threatModelId=model_id,
                            scopeDocs=[{"artifactId": artifact_id}], serviceRole=models[0]["serviceRole"])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in {"ValidationException", "AccessDeniedException", "ResourceNotFoundException"}:
                raise
            # No StartThreatModelJob call has occurred. Retain a safe, actionable
            # diagnosis without reflecting provider messages or document contents.
            return AdapterOutcome("definitive_failure", observation={
                "code": "threat_model_scope_rejected", "stage": "update_threat_model",
                "cause": code,
                "next_action": "Check the selected threat model, scope document and connection permissions; "
                               "correct setup and start a new run. No assessment was started.",
            })
        started = await to_thread(client.start_threat_model_job,
                                  agentSpaceId=space_id, threatModelId=model_id)
        job_id = started["threatModelJobId"]
        native = self._native(run, space_id, model_id, job_id, artifact_id)
        job = await self._job(run, native, session)
        if {entry.get("artifactId") for entry in job.get("scopeDocs", [])} != {artifact_id}:
            raise ValueError("native job did not capture submitted document")
        return AdapterOutcome("accepted", native_job_ref=native)

    async def reconcile_submit(self, run, attempt, session):
        client, space_id, model_id = self._scope(run, session)
        token = None
        seen = set()
        artifact_ids = set()
        for _ in range(20):
            options = {"agentSpaceId": space_id, "maxResults": 100}
            if token:
                options["nextToken"] = token
            response = await to_thread(client.list_artifacts, **options)
            artifact_ids.update(item["artifactId"] for item in response.get("artifactSummaries", [])
                                if item.get("fileName", "").rsplit(".", 1)[0] == run.run_id)
            token = response.get("nextToken")
            if not token:
                break
            if token in seen:
                return AdapterOutcome("outcome_unknown")
            seen.add(token)
        else:
            return AdapterOutcome("outcome_unknown")
        if not artifact_ids:
            return AdapterOutcome("outcome_unknown")
        token = None
        seen.clear()
        matches = []
        for _ in range(20):
            options = {"agentSpaceId": space_id, "threatModelId": model_id, "maxResults": 100}
            if token:
                options["nextToken"] = token
            response = await to_thread(client.list_threat_model_jobs, **options)
            listed = [job["threatModelJobId"] for job in response.get("threatModelJobSummaries", [])]
            if listed:
                detail = await to_thread(client.batch_get_threat_model_jobs,
                                         agentSpaceId=space_id, threatModelJobIds=listed)
                jobs = detail.get("threatModelJobs", [])
                if (detail.get("notFound") or len(jobs) != len(listed)
                        or {job.get("threatModelJobId") for job in jobs} != set(listed)):
                    return AdapterOutcome("outcome_unknown")
            else:
                jobs = []
            for job in jobs:
                if job.get("agentSpaceId") != space_id or job.get("threatModelId") != model_id:
                    return AdapterOutcome("outcome_unknown")
                identified = artifact_ids & {item.get("artifactId") for item in job.get("scopeDocs", [])}
                if len(identified) == 1 and len(job.get("scopeDocs", [])) == 1:
                    matches.append((job["threatModelJobId"], identified.pop()))
            token = response.get("nextToken")
            if not token:
                break
            if token in seen:
                return AdapterOutcome("outcome_unknown")
            seen.add(token)
        else:
            return AdapterOutcome("outcome_unknown")
        if len(matches) != 1:
            return AdapterOutcome("outcome_unknown")
        native = self._native(run, space_id, model_id, *matches[0])
        await self._job(run, native, session)
        return AdapterOutcome("accepted", native_job_ref=native)

    async def observe(self, run, native_job_ref, attempt, session):
        job = await self._job(run, native_job_ref, session)
        client, space_id, _, job_id = self._check_job(run, native_job_ref, session)
        status = job.get("status")
        classification = {"IN_PROGRESS": "running", "STOPPING": "running",
                          "STOPPED": "stopped", "FAILED": "failed",
                          "COMPLETED": "completed"}.get(status, "unknown")
        observation = {"native_job_ref": native_job_ref, "outcome": classification,
                       "observed_at": _instant(self.clock()), "native_status": status or "unknown",
                       "progress_coverage": "unavailable"}
        for field in ("createdAt", "updatedAt", "executionStartTime", "executionEndTime"):
            if job.get(field) is not None:
                observation[field] = _instant(job[field])
        if job.get("errorInformation"):
            observation["error"] = job["errorInformation"]
        tasks = []
        token = None
        visited = set()
        for _ in range(20):
            options = {"agentSpaceId": space_id, "threatModelJobId": job_id, "maxResults": 100}
            if token:
                options["nextToken"] = token
            page = await to_thread(client.list_threat_model_job_tasks, **options)
            tasks.extend(page.get("threatModelJobTaskSummaries", []))
            token = page.get("nextToken")
            if not token:
                break
            if token in visited:
                return AdapterOutcome("unknown", native_job_ref=native_job_ref,
                                      observation={**observation, "outcome": "unknown"})
            visited.add(token)
        else:
            return AdapterOutcome("unknown", native_job_ref=native_job_ref,
                                  observation={**observation, "outcome": "unknown"})
        if any(task.get("agentSpaceId") != space_id or task.get("threatModelId") != native_job_ref["threat_model_id"]
               or task.get("threatModelJobId") != job_id for task in tasks):
            raise ValueError("foreign native job task")
        observation["tasks"] = [{"task_id": task["taskId"], "status": task.get("executionStatus", "unknown"),
                                 **{field: _instant(task[field]) for field in ("createdAt", "updatedAt")
                                    if task.get(field) is not None}} for task in tasks]
        return AdapterOutcome(classification, native_job_ref=native_job_ref, observation=observation)

    async def collect(self, run, native_job_ref, attempt, session):
        client, space_id, _, job_id = self._check_job(run, native_job_ref, session)
        job = await self._job(run, native_job_ref, session)
        if job.get("status") != "COMPLETED":
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        token = None
        seen = set()
        summaries = []
        for _ in range(20):
            options = {"agentSpaceId": space_id, "threatJobId": job_id, "maxResults": 100}
            if token:
                options["nextToken"] = token
            response = await to_thread(client.list_threats, **options)
            summaries.extend(response.get("threats", []))
            token = response.get("nextToken")
            if not token:
                break
            if token in seen:
                return AdapterOutcome("unknown", native_job_ref=native_job_ref)
            seen.add(token)
        else:
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        ids = [item["threatId"] for item in summaries]
        if len(set(ids)) != len(ids) or any(item.get("threatJobId") != job_id for item in summaries):
            raise ValueError("foreign or duplicate threat summary")
        details = []
        for offset in range(0, len(ids), 100):
            response = await to_thread(client.batch_get_threats,
                                       agentSpaceId=space_id, threatIds=ids[offset:offset + 100])
            if response.get("notFound"):
                return AdapterOutcome("unknown", native_job_ref=native_job_ref)
            details.extend(response.get("threats", []))
        if {item.get("threatId") for item in details} != set(ids) or len(details) != len(ids):
            raise ValueError("incomplete or foreign threat details")
        if any(item.get("threatJobId") != job_id for item in details):
            raise ValueError("threat detail belongs to another job")
        source = run.input["sources"][0]
        findings = [{"native_id": item["threatId"], "kind": "candidate_threat",
                     "claim": item.get("statement") or item["title"],
                     "provider_severity": item.get("severity", "INFO"),
                     "validation": "candidate", "source_refs": [source],
                     "evidence": item.get("evidence") or [],
                     "mitigations": [item["recommendation"]] if item.get("recommendation") else []}
                    for item in details]
        return AdapterOutcome("completed", native_job_ref=native_job_ref,
                              result={"native_job_ref": native_job_ref, "findings": findings,
                                      "finding_count": len(findings), "coverage": "complete",
                                      "coverage_mode": "documents", "manifest_ref": run.manifest["archive"]})

    async def cancel(self, run, native_job_ref, attempt, session):
        client, space_id, _, job_id = self._check_job(run, native_job_ref, session)
        await to_thread(client.stop_threat_model_job, agentSpaceId=space_id, threatModelJobId=job_id)
        return await self.observe(run, native_job_ref, attempt, session)
