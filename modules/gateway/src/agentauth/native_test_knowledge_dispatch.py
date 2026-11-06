"""A second, durable claim for the original native Knowledge read.

The KMS-verified facade assertion transports a reference, never new authority.
The mediator reopens the original journal, protected quote, current designation
and retained holds before claiming exactly one Door submission. An uncertain
claim or Door response cannot authorize another submission or release funds.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from src.agentauth.native_test_effect_admission import NativeTestEffectAdmission
from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_facade_contract import MAX_CALL_BYTES, MAX_RESPONSE_BYTES, FacadeEffectQuoteAdapter, NativeFacadeCall
from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeRuntime
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.test_run_admission import _uuid
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


@dataclass
class NativeKnowledgeDispatch:
    bridge: NativeKnowledgeRuntime = field(repr=False)
    approval: AllInApproval = field(repr=False)
    original: dict = field(repr=False)
    source: FacadeEffectQuoteAdapter = field(repr=False)
    quote: object = field(repr=False)
    deadline: float
    evidence: dict
    _submitted: bool = field(default=False, init=False)

    @classmethod
    async def prepare(cls, *, runtime, claims, operation, arguments, binding):
        try:
            if type(runtime) is not NativeTestRuntime:
                raise ValueError
            enrollment = runtime.require_enrollment()
            bridge = enrollment._effect_runtime
            if type(bridge) is not NativeKnowledgeRuntime or bridge.runtime is not runtime:
                raise ValueError
            bridge.coverage()
            descriptor = claims["native_test"]
            if type(descriptor) is not dict or set(descriptor) != {"operation_id", "execution_id", "request_id", "request_sha256"}:
                raise ValueError
            for key in ("operation_id", "execution_id", "request_id"):
                _uuid(descriptor[key])
            _uuid(claims["run_id"])
            approval = await run_in_threadpool(enrollment.store.approval, claims["principal"]["tenant_id"], claims["run_id"])
            if (
                type(approval) is not AllInApproval
                or claims["principal"]
                != {
                    "user_id": approval.designation.user_id,
                    "tenant_id": approval.designation.tenant_id,
                    "team_id": approval.designation.team_id,
                }
                or claims["root_human_id"] != approval.designation.user_id
                or claims["workload_id"] != "native-test-" + approval.credential_id
                or claims["run_expires_at"] != approval.dispatch_expires_at
                or digest(binding) != bridge.binding.catalogue.target_binding_sha256
            ):
                raise ValueError
            # This uses current canonical membership/consent and the exact live
            # catalogue; it does not trust membership copied into the assertion.
            await enrollment.current_fixture(approval)
            authority = await run_in_threadpool(enrollment.store.authority, approval.designation.tenant_id, approval.run_id)
            if authority != approval.authority():
                raise ValueError
            original, quote, registered = await runtime.tool_lifecycle._original(approval, descriptor["execution_id"])
            body = canonical(
                {
                    "schema_version": "1.0",
                    "action": "call",
                    "tool": "knowledge___" + operation,
                    "arguments": arguments,
                    "request_id": descriptor["request_id"],
                    "execution_id": None,
                    "ttl_seconds": None,
                }
            ).encode()
            call = NativeFacadeCall.parse(body)
            expected_binding = {
                **asdict(approval.identity),
                "authority_revision": approval.revision,
                "meter_epoch": approval.meter_epoch,
                "route": registered.effect_key,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "operation_id": descriptor["operation_id"],
            }
            service = NativeTestEffectAdmission(
                effect_key=registered.effect_key, store=enrollment.store, budget=bridge.budget, clock=enrollment.clock
            )
            if (
                original["operation_id"] != descriptor["operation_id"]
                or original["binding"] != expected_binding
                or descriptor["request_sha256"] != expected_binding["request_sha256"]
                or original["status"] != "dispatch_started"
                or original.get("native_knowledge_dispatch") is not None
                or original.get("native_cleanup_attempts_used", 0) != 0
                or registered.action != "call"
                or registered.tool != call.tool.replace("___", ".", 1)
                or registered.source_sha256 != bridge.binding.release_sha256
                or original["reservation_id"] != claims["receipt_id"]
                or original["reservation_id"] != service._reservation_id(authority["tenant_id"], authority["run_id"], descriptor["operation_id"])
            ):
                raise ValueError
            source = FacadeEffectQuoteAdapter(
                store=enrollment.store,
                deployment_id=bridge.binding.deployment_id,
                contract_id=registered.contract_id,
                contract_sha256=registered.contract_sha256,
                source_sha256=registered.source_sha256,
            )
            service.quote_adapter = source
            await service._validate_quote(quote, body, registered.effect_key, authority)
            hold_deadline = await runtime.tool_lifecycle._funding(approval, original, quote.total_usd)
            await service._confirm_quote(quote)
            deadline = min(
                hold_deadline,
                quote.expires_at.timestamp(),
                approval.dispatch_expires_at,
                approval.approved_at + 300,
                original["facade_dispatch"]["session_deadline_at"],
                claims["expires_at"],
            )
            evidence = {
                "status": "dispatch_started",
                "target_binding_sha256": digest(binding),
                "rpc_id_sha256": digest(claims["rpc_id"]),
                "request_sha256": expected_binding["request_sha256"],
            }
            transfer = cls(bridge, approval, original, source, quote, deadline, evidence)
            await run_in_threadpool(transfer._claim)
            transfer.require_fresh()
            return transfer
        except AdmissionRefusedError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AdmissionRefusedError("native_test_knowledge_dispatch_refused") from None

    def require_fresh(self):
        if self.bridge.enrollment.clock().timestamp() >= self.deadline:
            raise AdmissionRefusedError("native_test_knowledge_dispatch_expired")

    def _claim(self):
        self.require_fresh()
        store, now = self.bridge.runtime.store, self.bridge.enrollment.clock().timestamp()
        original = self.original
        updated = {**original, "version": original["version"] + 1, "native_knowledge_dispatch": {**self.evidence, "claimed_at": now}}
        guards = store._distinct_guards(
            [
                *store.authority_guards(self.approval.authority(), now),
                *self.source.guards(self.quote, now=now),
            ]
        )
        # The original operation is the replay fence. No TTL, separate run or
        # fresh operation can recover a lost acknowledgement into dispatch.
        store.transaction(
            [
                *guards,
                {
                    "Put": {
                        "TableName": store.table,
                        "Item": store._item(updated),
                        "ConditionExpression": "document = :original",
                        "ExpressionAttributeValues": {":original": {"S": canonical(original)}},
                    },
                },
            ]
        )
        self.original = updated

    def before_send(self):
        self.require_fresh()
        if self._submitted:
            raise AdmissionRefusedError("native_test_knowledge_dispatch_already_used")
        self._submitted = True

    async def forward(self, method, url, headers, body):
        """One bounded stream to the already validated private Door endpoint."""
        if method != "POST" or len(body) > MAX_CALL_BYTES:
            raise AdmissionRefusedError("native_test_knowledge_transport_refused")
        self.before_send()
        timeout = min(15, self.deadline - self.bridge.enrollment.clock().timestamp())
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, url, headers=headers, content=body) as upstream:
                if not 200 <= upstream.status_code < 300:
                    raise AdmissionRefusedError("native_test_knowledge_response_unknown")
                parts, size = [], 0
                async for part in upstream.aiter_bytes():
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        raise AdmissionRefusedError("native_test_knowledge_response_unknown")
                    parts.append(part)
                content_type = upstream.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"application/json", "text/event-stream"}:
                    raise AdmissionRefusedError("native_test_knowledge_response_unknown")
                self.require_fresh()
                return Response(b"".join(parts), status_code=upstream.status_code, media_type=content_type)

    async def received(self, answer):
        """Record only a bounded response digest, never claim billed quantities."""
        if not self._submitted:
            raise AdmissionRefusedError("native_test_knowledge_dispatch_refused")
        self.require_fresh()
        store = self.bridge.runtime.store
        received = {
            **self.original["native_knowledge_dispatch"],
            "status": "response_received",
            "response_sha256": digest(answer),
            "received_at": self.bridge.enrollment.clock().timestamp(),
        }
        updated = {**self.original, "version": self.original["version"] + 1, "native_knowledge_dispatch": received}
        await run_in_threadpool(
            store.transaction,
            [
                {
                    "Put": {
                        "TableName": store.table,
                        "Item": store._item(updated),
                        "ConditionExpression": "document = :original",
                        "ExpressionAttributeValues": {":original": {"S": canonical(self.original)}},
                    },
                }
            ],
        )
        self.original = updated
