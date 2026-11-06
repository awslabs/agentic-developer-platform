"""Scoped inquiry transitions through a separately qualified private executor."""

import asyncio
import json
from hashlib import sha256
from typing import Protocol

from l5_services.adapters.protocol import AdapterOutcome


class PrivateFinOpsExecutor(Protocol):
    async def submit(self, *, intent: dict, authority) -> dict | None: ...

    async def read_submit(self, *, intent: dict, authority) -> dict | None: ...

    async def observe(self, *, native_job_ref: dict, authority) -> dict | None: ...

    async def read_answer(self, *, native_job_ref: dict, authority) -> dict | None: ...

    async def read_native_cost(self, *, native_job_ref: dict, authority) -> dict | None: ...

    async def stop(self, *, native_job_ref: dict, authority) -> dict | None: ...


class FinOpsInquiryAdapter:
    def __init__(self, executor: PrivateFinOpsExecutor, billing, *, timeout_seconds=10):
        if executor is None or billing is None or not 0 < timeout_seconds <= 10:
            raise ValueError("qualified FinOps executor and independent billing are required")
        if not all(callable(getattr(executor, name, None)) for name in (
                "submit", "read_submit", "observe", "read_answer", "read_native_cost", "stop")):
            raise ValueError("private FinOps executor callbacks unavailable")
        if not all(callable(getattr(billing, name, None)) for name in (
                "collect_billing_page", "read_billing_page")):
            raise ValueError("independent FinOps billing callbacks unavailable")
        self.executor = executor
        self.billing = billing
        self.timeout_seconds = timeout_seconds

    def _scope(self, run):
        scope = run.admission["scope"]
        agent = run.admission["native_agent_ref"]
        if (run.service_id != "finops" or run.input.get("capability") != "cost_inquiry"
                or any(scope.get(field) != getattr(run, field) for field in (
                    "tenant_id", "product_id", "project_id"))
                or agent.get("tenant_id") != run.tenant_id
                or agent.get("project_id") != run.project_id
                or agent.get("record_type") != "finops_agent" or not agent.get("record_id")
                or run.admission["billing_scope"].get("scope_generation") != run.binding_generation):
            raise ValueError("FinOps executor scope differs from pinned work")
        return scope, agent

    def _intent(self, run, attempt):
        scope, agent = self._scope(run)
        manifest = run.manifest
        question_hash = sha256(run.input["question"].encode()).hexdigest()
        input_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        token = sha256(f"{run.tenant_id}:{run.run_id}:{input_hash}".encode()).hexdigest()
        if (manifest is None or attempt.run_id != run.run_id
                or attempt.question_hash != question_hash
                or attempt.input_hash != input_hash or attempt.idempotency_token != token
                or manifest.get("scope") != scope
                or manifest.get("native_agent_ref") != agent
                or manifest.get("question_hash") != question_hash
                or manifest.get("query") != run.input["query"]
                or manifest.get("run_id") != run.run_id
                or manifest.get("binding_generation") != run.binding_generation):
            raise ValueError("FinOps inquiry differs from durable attempt")
        display_name = run.admission.get("native_agent_display_name")
        if (not isinstance(display_name, str) or not display_name.strip()
                or manifest.get("native_agent_display_name") != display_name):
            raise ValueError("pinned Product native agent display name unavailable")
        conversation_id = agent.get("conversation_id")
        if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id):
            raise ValueError("selected native conversation is invalid")
        return {"tenant_id": run.tenant_id, "product_id": run.product_id,
                "project_id": run.project_id, "run_id": run.run_id,
                "agent_id": agent["record_id"], "agent_label": display_name,
                "question": run.input["question"],
                "question_hash": question_hash, "attempt_token": attempt.idempotency_token,
                "query": run.input["query"],
                "billing_scope": run.admission["billing_scope"],
                **({"conversation_id": conversation_id} if conversation_id else {})}

    def _native(self, run, intent, receipt):
        if not isinstance(receipt, dict):
            return None
        reference = receipt.get("native_job_ref")
        if (receipt.get("attempt_token") != intent["attempt_token"]
                or not isinstance(reference, dict)
                or set(reference) not in ({"tenant_id", "product_id", "project_id", "agent_id", "conversation_id",
                                           "turn_id", "question_hash"},
                                          {"tenant_id", "product_id", "project_id", "agent_id", "conversation_id",
                                           "turn_locator", "question_hash"})
                or (reference["tenant_id"], reference["product_id"], reference["project_id"],
                    reference["agent_id"], reference["question_hash"]) != (
                        run.tenant_id, run.product_id, run.project_id, intent["agent_id"], intent["question_hash"])
                or intent.get("conversation_id", reference.get("conversation_id"))
                != reference.get("conversation_id")
                or any(not isinstance(reference[field], str) or not reference[field] for field in reference)):
            return None
        return reference

    def _check_native(self, run, reference):
        self._scope(run)
        if (not isinstance(reference, dict)
                or set(reference) not in ({"tenant_id", "product_id", "project_id", "agent_id",
                                          "conversation_id", "turn_id", "question_hash"},
                                         {"tenant_id", "product_id", "project_id", "agent_id",
                                          "conversation_id", "turn_locator", "question_hash"})
                or (reference.get("tenant_id"), reference.get("product_id"), reference.get("project_id"),
                    reference.get("agent_id")) != (
                        run.tenant_id, run.product_id, run.project_id,
                        run.admission["native_agent_ref"]["record_id"])
                or any(not isinstance(value, str) or not value for value in reference.values())
                or run.admission["native_agent_ref"].get("conversation_id", reference.get("conversation_id"))
                != reference.get("conversation_id")
                or reference.get("question_hash") != sha256(run.input["question"].encode()).hexdigest()
                or not reference.get("conversation_id")
                or not (reference.get("turn_id") or reference.get("turn_locator"))):
            raise ValueError("FinOps native turn differs from pinned inquiry")

    async def submit(self, run, manifest, attempt, authority):
        if manifest != run.manifest:
            raise ValueError("FinOps inquiry manifest changed")
        intent = self._intent(run, attempt)
        receipt = await asyncio.wait_for(self.executor.submit(intent=intent, authority=authority), self.timeout_seconds)
        reference = self._native(run, intent, receipt)
        return AdapterOutcome("accepted", native_job_ref=reference) if reference is not None else None

    async def reconcile_submit(self, run, attempt, authority):
        intent = self._intent(run, attempt)
        receipt = await asyncio.wait_for(self.executor.read_submit(
            intent=intent, authority=authority), self.timeout_seconds)
        reference = self._native(run, intent, receipt)
        return AdapterOutcome("accepted", native_job_ref=reference) if reference is not None else None

    async def observe(self, run, native_job_ref, attempt, authority):
        self._check_native(run, native_job_ref)
        receipt = await asyncio.wait_for(self.executor.observe(
            native_job_ref=native_job_ref, authority=authority), self.timeout_seconds)
        if receipt is None:
            return AdapterOutcome("running")
        if (not isinstance(receipt, dict) or receipt.get("native_job_ref") != native_job_ref
                or receipt.get("outcome") not in {"running", "completed", "failed", "stopped"}
                or not isinstance(receipt.get("observed_at"), str)):
            raise ValueError("FinOps status lacks pinned native evidence")
        return AdapterOutcome(receipt["outcome"], observation=receipt)

    async def collect(self, run, native_job_ref, attempt, authority):
        self._check_native(run, native_job_ref)
        answer = await asyncio.wait_for(self.executor.read_answer(
            native_job_ref=native_job_ref, authority=authority), self.timeout_seconds)
        if (not isinstance(answer, dict) or answer.get("native_job_ref") != native_job_ref
                or answer.get("kind") != "finops_native_answer"
                or answer.get("status") not in {"answered", "refused"}
                or not isinstance(answer.get("answer_artifact"), dict)):
            raise ValueError("FinOps answer lacks original native evidence")
        return AdapterOutcome("completed", result=answer)

    async def cancel(self, run, native_job_ref, attempt, authority):
        self._check_native(run, native_job_ref)
        receipt = await asyncio.wait_for(self.executor.stop(
            native_job_ref=native_job_ref, authority=authority), self.timeout_seconds)
        if receipt is None:
            return AdapterOutcome("running")
        if not isinstance(receipt, dict) or receipt.get("native_job_ref") != native_job_ref:
            raise ValueError("FinOps stop lacks pinned native evidence")
        return AdapterOutcome("stopped", observation=receipt)

    async def collect_billing_page(self, *arguments):
        return await self.billing.collect_billing_page(*arguments)

    async def read_billing_page(self, *arguments):
        return await self.billing.read_billing_page(*arguments)

    async def read_native_cost(self, run, claim, authority):
        self._check_native(run, run.native_job_ref)
        return await asyncio.wait_for(self.executor.read_native_cost(
            native_job_ref=run.native_job_ref, authority=authority), self.timeout_seconds)
