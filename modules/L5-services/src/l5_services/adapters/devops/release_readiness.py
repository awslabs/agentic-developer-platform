"""Native release review of one pinned, independently checked repository change."""

import asyncio
from datetime import UTC, datetime

import json
from hashlib import sha256

from l5_services.devops.release_prerequisites import release_eligible
from l5_services.adapters.protocol import AdapterOutcome


QUALIFICATION_CODE = "release_readiness_provider_contract_unqualified"
QUALIFICATION_MESSAGE = (
    "Qualify the provider's original base/head comparison, consumed repository revisions, "
    "static-only execution, and final release_analysis_report schema/finality before enabling "
    "new release-readiness tasks. Generic recommendations and matching changed-line citations "
    "do not establish complete release coverage. Existing tasks remain recoverable."
)


def _selection(run, manifest, provider_session):
    admission = run.admission
    options = run.input["devops_input"]
    scope = admission["scope"]
    space = admission["native_space_ref"]
    identity = admission["provider_identity"]
    eligibility = admission["release_eligibility"]
    if (run.service_id != "devops" or run.input.get("capability") != "release_readiness"
            or (run.tenant_id, run.product_id, run.project_id) != (
                scope.get("tenant_id"), scope.get("product_id"), scope.get("project_id"))
            or (space.get("tenant_id"), space.get("project_id")) != (run.tenant_id, run.project_id)
            or space.get("source_connection_id") != admission["connection_ref"]["source_connection_id"]
            or space.get("record_type") != "agent_space" or not space.get("record_id")
            or manifest != run.manifest or manifest.get("scope") != scope
            or manifest.get("admission_hash") != sha256(json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            or manifest.get("source_snapshot_ids") != [snapshot["snapshot_id"] for snapshot in admission.get("source_snapshots", [])]
            or manifest.get("sources") != [snapshot["source_ref"] for snapshot in admission.get("source_snapshots", [])]
            or manifest.get("run_id") != run.run_id
            or manifest.get("configuration_revision") != run.configuration_revision
            or manifest.get("binding_generation") != run.binding_generation
            or manifest.get("repository_ref") != options["repository_ref"]
            or manifest.get("base_revision") != options["base_revision"]
            or manifest.get("head_revision") != options["head_revision"]
            or manifest.get("release_eligibility") != eligibility
            or not manifest.get("changed_paths")
            or provider_session.account_id != identity.get("account_id")
            or provider_session.region != identity.get("region")
            or provider_session.role_arn != identity.get("role_arn")):
        raise ValueError("release review does not match pinned provider scope")
    if not release_eligible(options, eligibility, scope, admission["connection_ref"], space):
        raise ValueError("native release prerequisites unavailable")
    return space["record_id"], provider_session.client("devops-agent"), eligibility


def _description(run, manifest, attempt):
    manifest_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected_token = sha256(f"{run.tenant_id}:{run.run_id}:{manifest_hash}".encode()).hexdigest()
    if (attempt.run_id != run.run_id or attempt.input_hash != manifest_hash
            or attempt.idempotency_token != expected_token):
        raise ValueError("release attempt does not match sealed source")
    description = json.dumps({
        "run_id": run.run_id, "scope": run.admission["scope"],
        "repository_ref": manifest["repository_ref"],
        "base_revision": manifest["base_revision"], "head_revision": manifest["head_revision"],
        "changed_paths": manifest["changed_paths"], "manifest_hash": manifest_hash,
        "binding_id": run.binding_id, "binding_generation": run.binding_generation,
        "attempt_id": attempt.attempt_id, "idempotency_token": attempt.idempotency_token,
        "deployment_context": run.admission["release_eligibility"].get("deployment_context"),
    }, sort_keys=True, separators=(",", ":"))
    if len(description) > 10000:
        raise ValueError("release description exceeds provider limit")
    return description


def _reference(eligibility):
    return {"system": "github", "referenceId": eligibility["reference_id"],
            "referenceUrl": eligibility["reference_url"], "associationId": eligibility["association_id"]}


def _outcome(run, space_id, task, description, reference):
    if (task.get("agentSpaceId") != space_id or task.get("taskType") != "RELEASE_READINESS_REVIEW"
            or task.get("description") != description or task.get("reference") != reference
            or not task.get("taskId") or not task.get("executionId")):
        return AdapterOutcome("unknown")
    execution_id = task["executionId"]
    task_id = task["taskId"]
    if not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (task_id, execution_id)):
        return AdapterOutcome("unknown")
    return AdapterOutcome("accepted", native_job_ref={
        "tenant_id": run.tenant_id, "project_id": run.project_id,
        "source_connection_id": run.admission["connection_ref"]["source_connection_id"],
        "record_type": "native_job", "record_id": execution_id, "record_revision": task_id})


class ReleaseReadinessAdapter:
    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))

    async def submit(self, run, manifest, attempt, provider_session):
        _selection(run, manifest, provider_session)
        _description(run, manifest, attempt)
        # Historical raw descriptions remain recoverable, but must never start
        # another task while original-base/static-report semantics are unknown.
        return AdapterOutcome("definitive_failure", observation={
            "code": QUALIFICATION_CODE, "message": QUALIFICATION_MESSAGE})

    async def reconcile_submit(self, run, attempt, provider_session):
        space_id, client, eligibility = _selection(run, run.manifest, provider_session)
        description = _description(run, run.manifest, attempt)
        seen = set()
        token = None
        matches = []
        while True:
            page = await asyncio.to_thread(client.list_backlog_tasks, agentSpaceId=space_id, limit=100,
                                           **({"nextToken": token} if token else {}))
            tasks = page.get("tasks")
            if not isinstance(tasks, list):
                return AdapterOutcome("unknown")
            matches.extend(task for task in tasks if task.get("description") == description)
            token = page.get("nextToken")
            if not token:
                break
            if token in seen or len(seen) >= 100:
                return AdapterOutcome("unknown")
            seen.add(token)
        if len(matches) != 1 or not matches[0].get("taskId"):
            return AdapterOutcome("unknown")
        task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                        taskId=matches[0]["taskId"])).get("task") or {}
        if task.get("taskId") != matches[0]["taskId"]:
            return AdapterOutcome("unknown")
        return _outcome(run, space_id, task, description, _reference(eligibility))

    async def _task(self, run, native_job_ref, attempt, provider_session):
        space_id, client, eligibility = _selection(run, run.manifest, provider_session)
        if (native_job_ref.get("tenant_id") != run.tenant_id
                or native_job_ref.get("project_id") != run.project_id
                or native_job_ref.get("source_connection_id") != run.admission["connection_ref"]["source_connection_id"]
                or native_job_ref.get("record_type") != "native_job"):
            raise ValueError("foreign native release task")
        task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                        taskId=native_job_ref["record_revision"])).get("task") or {}
        if (_outcome(run, space_id, task, _description(run, run.manifest, attempt),
                     _reference(eligibility)).native_job_ref != native_job_ref):
            raise ValueError("native task readback differs from pinned run")
        return client, task


    async def observe(self, run, native_job_ref, attempt, provider_session):
        _, task = await self._task(run, native_job_ref, attempt, provider_session)
        status = task.get("status")
        classification = ({"COMPLETED": "completed", "FAILED": "failed", "TIMED_OUT": "failed",
                           "CANCELED": "stopped"}.get(status, "running" if status in {
                               "PENDING_TRIAGE", "LINKED", "PENDING_START", "IN_PROGRESS",
                               "PENDING_CUSTOMER_APPROVAL", "WAITING"} else "unknown"))
        return AdapterOutcome(classification, native_job_ref=native_job_ref,
                              observation={"observed_at": self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
                                           "native_job_ref": native_job_ref, "outcome": classification,
                                           "native_task_id": task["taskId"], "status": status})

    async def collect(self, run, native_job_ref, attempt, provider_session):
        client, task = await self._task(run, native_job_ref, attempt, provider_session)
        if task.get("status") != "COMPLETED":
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        token = None
        seen = set()
        summaries = []
        for _ in range(20):
            response = await asyncio.to_thread(client.list_recommendations, agentSpaceId=task["agentSpaceId"],
                                               taskId=task["taskId"], limit=100,
                                               **({"nextToken": token} if token else {}))
            page = response.get("recommendations")
            if not isinstance(page, list):
                return AdapterOutcome("unknown", native_job_ref=native_job_ref)
            summaries.extend(page)
            token = response.get("nextToken")
            if not token:
                break
            if token in seen:
                return AdapterOutcome("unknown", native_job_ref=native_job_ref)
            seen.add(token)
        else:
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        findings = []
        identifiers = set()
        for summary in summaries:
            native_id, version = summary.get("recommendationId"), summary.get("version")
            if (not native_id or not isinstance(version, int)
                    or native_id in identifiers or summary.get("taskId") != task["taskId"]):
                raise ValueError("native release recommendation identity mismatch")
            identifiers.add(native_id)
            response = await asyncio.to_thread(client.get_recommendation,
                                               agentSpaceId=task["agentSpaceId"], recommendationId=native_id,
                                               recommendationVersion=version)
            detail = response.get("recommendation") or {}
            if (detail.get("recommendationId") != native_id or detail.get("version") != version
                    or detail.get("taskId") != task["taskId"]):
                raise ValueError("native release recommendation readback mismatch")
            try:
                citations = json.loads(detail.get("additionalContext") or "{}").get("citations") or []
            except (ValueError, TypeError, AttributeError):
                citations = []
            if not isinstance(citations, list):
                citations = []
            verified = []
            changed = run.manifest["changed_lines"]
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                path, line, digest = (citation.get(key) for key in ("path", "line", "sha256"))
                if (isinstance(path, str) and type(line) is int and line > 0
                        and isinstance(digest, str) and changed.get(path, {}).get(str(line)) == digest):
                    verified.append({"path": path, "line": line, "sha256": digest,
                                     "revision": run.manifest["head_revision"],
                                     "rubric_version": "release-diff-v1"})
            claim = (detail.get("content") or {}).get("summary") or detail.get("title")
            if not isinstance(claim, str) or not claim.strip():
                raise ValueError("native release recommendation has no claim")
            findings.append({"native_id": native_id, "kind": "static_finding", "claim": claim,
                             "provider_severity": detail.get("priority") or "UNKNOWN",
                             "validation": "supported" if verified else "unverified",
                             "source_refs": [run.manifest["repository_ref"]] if verified else [],
                             "evidence": verified, "locations": verified})
        return AdapterOutcome("completed", native_job_ref=native_job_ref,
                              result={"native_job_ref": native_job_ref, "native_task_id": task["taskId"],
                                      "rubric_version": "release-diff-v1", "findings": findings,
                                      "cleanup": {"action": "retain_native_task", "outcome": "not_required"},
                                      "finding_count": len(findings),
                                      "coverage": "unknown", "coverage_mode": "native_observation",
                                      "coverage_reason": QUALIFICATION_CODE, "manifest_ref": run.manifest["archive"]})

    async def cancel(self, run, native_job_ref, attempt, provider_session):
        client, task = await self._task(run, native_job_ref, attempt, provider_session)
        terminal = task.get("status") in {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELED"}
        if not terminal:
            await asyncio.to_thread(client.update_backlog_task, agentSpaceId=task["agentSpaceId"],
                                    taskId=task["taskId"], taskStatus="CANCELED")
        observed = await self.observe(run, native_job_ref, attempt, provider_session)
        cleanup = {"action": "retain_terminal_task" if terminal else "cancel_native_task",
                   "outcome": "already_terminal" if terminal else (
                       "confirmed" if observed.classification == "stopped" else "unconfirmed")}
        return AdapterOutcome(observed.classification, native_job_ref=native_job_ref,
                              observation={**observed.observation, "cleanup": cleanup})
