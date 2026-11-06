"""Submit a pinned investigation through the provider's idempotent task boundary."""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from l5_services.adapters.protocol import AdapterOutcome
from l5_services.devops.results import TERMINAL_STATUS, evidence_bytes


def _selection(run, manifest, provider_session):
    admission = run.admission
    identity = admission["provider_identity"]
    space = admission["native_space_ref"]
    scope = admission["scope"]
    if (run.service_id != "devops" or run.input.get("capability") != "investigation"
            or (run.tenant_id, run.product_id, run.project_id) != (
                scope.get("tenant_id"), scope.get("product_id"), scope.get("project_id"))
            or (space.get("tenant_id"), space.get("project_id")) != (run.tenant_id, run.project_id)
            or space.get("source_connection_id") != admission["connection_ref"]["source_connection_id"]
            or space.get("record_type") != "agent_space" or not space.get("record_id")
            or manifest.get("scope") != scope or manifest.get("run_id") != run.run_id
            or manifest.get("configuration_revision") != run.configuration_revision
            or manifest.get("binding_generation") != run.binding_generation
            or manifest.get("resource_refs") != run.input["devops_input"]["resource_refs"]
            or manifest.get("telemetry_refs") != run.input["devops_input"]["telemetry_refs"]
            or provider_session.account_id != identity.get("account_id")
            or provider_session.region != identity.get("region")
            or provider_session.role_arn != identity.get("role_arn")):
        raise ValueError("investigation does not match pinned provider scope")
    return space["record_id"], provider_session.client("devops-agent")


def _description(run, manifest, attempt):
    manifest_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected_token = sha256(f"{run.tenant_id}:{run.run_id}:{manifest_hash}".encode()).hexdigest()
    if (attempt.run_id != run.run_id or attempt.input_hash != manifest_hash
            or attempt.idempotency_token != expected_token):
        raise ValueError("investigation attempt does not match pinned provider scope")
    description = json.dumps({
        "question": run.input["devops_input"]["question"],
        "scope": run.admission["scope"], "run_id": run.run_id,
        "event_id": run.admission.get("event_id"),
        "configuration_revision": run.configuration_revision,
        "binding_id": run.binding_id, "binding_generation": run.binding_generation,
        "resource_refs": manifest["resource_refs"],
        "telemetry_refs": manifest["telemetry_refs"],
        "attempt_id": attempt.attempt_id, "idempotency_token": attempt.idempotency_token,
    }, sort_keys=True, separators=(",", ":"))
    if len(description) > 10000:
        raise ValueError("investigation description exceeds provider limit")
    return description


def _outcome(run, space_id, task, description):
    if (task.get("agentSpaceId") != space_id or task.get("taskType") != "INVESTIGATION"
            or task.get("description") != description or not task.get("taskId")):
        return AdapterOutcome("unknown")
    execution_id = task.get("executionId")
    if not isinstance(execution_id, str) or not execution_id or len(execution_id) > 128:
        return AdapterOutcome("unknown")
    native_ref = {"tenant_id": run.tenant_id, "project_id": run.project_id,
                  "source_connection_id": run.admission["connection_ref"]["source_connection_id"],
                  "record_type": "native_job", "record_id": execution_id, "record_revision": task["taskId"]}
    return AdapterOutcome("accepted", native_job_ref=native_ref)


def _readback(client, run, space_id, description):
    token = None
    seen = set()
    matches = []
    while True:
        page = client.list_backlog_tasks(agentSpaceId=space_id, limit=100,
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
    task = client.get_backlog_task(agentSpaceId=space_id, taskId=matches[0]["taskId"]).get("task") or {}
    return _outcome(run, space_id, task, description)


class InvestigationAdapter:
    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))

    async def submit(self, run, manifest, attempt, provider_session):
        space_id, client = _selection(run, manifest, provider_session)
        description = _description(run, manifest, attempt)
        observed = await asyncio.to_thread(client.get_agent_space, agentSpaceId=space_id)
        if (observed.get("agentSpace") or {}).get("agentSpaceId") != space_id:
            raise ValueError("provider space readback differs from pinned binding")
        created = await asyncio.to_thread(
            client.create_backlog_task, agentSpaceId=space_id, taskType="INVESTIGATION",
            title=run.input["devops_input"]["question"][:400], description=description,
            priority="MEDIUM", clientToken=attempt.idempotency_token,
        )
        task = created.get("task") or {}
        if task.get("taskId"):
            task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                            taskId=task["taskId"])).get("task") or {}
            return _outcome(run, space_id, task, description)
        return AdapterOutcome("unknown")

    async def reconcile_submit(self, run, attempt, provider_session):
        space_id, client = _selection(run, run.manifest, provider_session)
        return await asyncio.to_thread(_readback, client, run, space_id,
                                       _description(run, run.manifest, attempt))

    async def _task(self, run, native_job_ref, provider_session):
        space_id, client = _selection(run, run.manifest, provider_session)
        if (native_job_ref != run.native_job_ref
                or native_job_ref.get("tenant_id") != run.tenant_id
                or native_job_ref.get("project_id") != run.project_id
                or native_job_ref.get("source_connection_id") != run.admission["connection_ref"]["source_connection_id"]
                or native_job_ref.get("record_type") != "native_job"
                or not native_job_ref.get("record_revision") or not native_job_ref.get("record_id")):
            raise ValueError("foreign native investigation")
        task = (await asyncio.to_thread(client.get_backlog_task, agentSpaceId=space_id,
                                        taskId=native_job_ref["record_revision"])).get("task") or {}
        try:
            description = json.loads(task["description"])
            manifest_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            attempt = SimpleNamespace(run_id=run.run_id, input_hash=manifest_hash,
                                      attempt_id=description["attempt_id"],
                                      idempotency_token=description["idempotency_token"])
            expected = _description(run, run.manifest, attempt)
        except (KeyError, TypeError, ValueError):
            raise ValueError("native investigation readback differs from pinned run") from None
        if _outcome(run, space_id, task, expected).native_job_ref != native_job_ref:
            raise ValueError("native investigation readback differs from pinned run")
        return client, task

    def _observation(self, native_job_ref, task):
        status = task.get("status")
        classification = TERMINAL_STATUS.get(status, "running" if status in {
            "PENDING_TRIAGE", "LINKED", "PENDING_START", "IN_PROGRESS",
            "PENDING_CUSTOMER_APPROVAL", "WAITING"} else "unknown")
        return AdapterOutcome(classification, native_job_ref=native_job_ref, observation={
            "observed_at": self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "native_job_ref": native_job_ref, "outcome": classification,
            "native_task_id": task["taskId"], "status": status})

    async def observe(self, run, native_job_ref, attempt, provider_session):
        _, task = await self._task(run, native_job_ref, provider_session)
        return self._observation(native_job_ref, task)

    async def collect(self, run, native_job_ref, attempt, provider_session):
        client, task = await self._task(run, native_job_ref, provider_session)
        if task.get("status") != "COMPLETED":
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        selection = {"agentSpaceId": task["agentSpaceId"], "taskId": task["taskId"]}
        executions = await _pages(client.list_executions, "executions", **selection)
        if any(item.get("agentSpaceId") != task["agentSpaceId"] for item in executions):
            raise ValueError("foreign native investigation execution")
        selected = [item for item in executions if item.get("executionId") == native_job_ref["record_id"]]
        if len(selected) != 1:
            raise ValueError("accepted native investigation execution unavailable")
        records = await _pages(client.list_journal_records, "records", agentSpaceId=task["agentSpaceId"],
                               executionId=native_job_ref["record_id"])
        record_ids = set()
        for record in records:
            identifier = record.get("recordId")
            if (record.get("agentSpaceId") != task["agentSpaceId"]
                    or record.get("executionId") != native_job_ref["record_id"]
                    or not isinstance(identifier, str) or not identifier or identifier in record_ids):
                raise ValueError("foreign or duplicate native investigation journal record")
            record_ids.add(identifier)
        summaries = await _pages(client.list_recommendations, "recommendations", **selection)
        recommendations = []
        identifiers = set()
        for summary in summaries:
            identifier, version = summary.get("recommendationId"), summary.get("version")
            if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                    or type(version) is not int or version < 1 or summary.get("taskId") != task["taskId"]):
                raise ValueError("native investigation recommendation identity mismatch")
            identifiers.add(identifier)
            detail = (await asyncio.to_thread(client.get_recommendation, agentSpaceId=task["agentSpaceId"],
                                              recommendationId=identifier,
                                              recommendationVersion=version)).get("recommendation") or {}
            identity = run.admission["provider_identity"]
            space_arn = f"arn:aws:aidevops:{identity['region']}:{identity['account_id']}:agentspace/{task['agentSpaceId']}"
            if (detail.get("recommendationId") != identifier or detail.get("version") != version
                    or detail.get("taskId") != task["taskId"]
                    or detail.get("agentSpaceArn") != space_arn):
                raise ValueError("native investigation recommendation readback mismatch")
            recommendations.append(detail)
        observation = self._observation(native_job_ref, task)
        result = {"kind": "devops_investigation", "native_job_ref": native_job_ref,
                  "task": task, "execution": selected[0], "journal_records": records,
                  "operational_recommendations": recommendations,
                  "observed_at": observation.observation["observed_at"],
                  "costs": [{"kind": "unknown", "currency": "USD"}],
                  "usage_coverage": {"status": "unavailable", "source": "GetBacklogTask",
                                     "scope": {"agentSpaceId": task["agentSpaceId"], "taskId": task["taskId"],
                                               "executionId": native_job_ref["record_id"]},
                                     "observed_at": observation.observation["observed_at"],
                                     "reason": "native task API does not expose measured task cost"}}
        encoded = evidence_bytes(result)
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError("native investigation evidence exceeds collection bound")
        return AdapterOutcome("completed", native_job_ref=native_job_ref,
                              observation=observation.observation, result=json.loads(encoded))

    async def cancel(self, run, native_job_ref, attempt, provider_session):
        client, task = await self._task(run, native_job_ref, provider_session)
        if task.get("status") in TERMINAL_STATUS:
            return self._observation(native_job_ref, task)
        if self._observation(native_job_ref, task).classification != "running":
            return AdapterOutcome("unknown", native_job_ref=native_job_ref)
        token = sha256(f"{run.tenant_id}:{run.run_id}:{task['taskId']}:cancel".encode()).hexdigest()
        await asyncio.to_thread(client.update_backlog_task, agentSpaceId=task["agentSpaceId"],
                                taskId=task["taskId"], taskStatus="CANCELED", clientToken=token)
        return await self.observe(run, native_job_ref, attempt, provider_session)


async def _pages(method, field, **selection):
    records, seen, token = [], set(), None
    for _ in range(10):
        response = await asyncio.to_thread(method, **selection, limit=100,
                                           **({"nextToken": token} if token else {}))
        page = response.get(field)
        if not isinstance(page, list) or len(page) > 100 or any(not isinstance(item, dict) for item in page):
            raise ValueError("native investigation collection requires a bounded page")
        records.extend(page)
        token = response.get("nextToken")
        if not token:
            return records
        if not isinstance(token, str) or token in seen:
            raise ValueError("native investigation collection repeated a page cursor")
        seen.add(token)
    raise ValueError("native investigation collection exceeded page bound")
