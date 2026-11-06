"""Revalidate and consume the original native reservation at adapter dispatch.

Only hashes of provider arguments are retained. The facade records them while
validating the full quoted request; its signed adapter assertion must match.
A lost claim acknowledgement leaves the operation consumed and funds held.
"""

from dataclasses import asdict, dataclass

from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime
from src.agentauth.native_test_effect_admission import NativeTestEffectAdmission
from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_facade_contract import FacadeEffectCostContract, FacadeEffectQuoteAdapter
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.test_run_admission import _uuid
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


@dataclass
class NativeAtlassianDispatch:
    bridge: object
    approval: object
    original: dict
    source: object
    quote: object
    deadline: float
    binding_sha256: str

    @classmethod
    async def verify(cls, *, runtime, claims, operation, binding):
        """Read current ownership, registered payload, release and every hold."""
        try:
            if type(runtime) is not NativeTestRuntime:
                raise ValueError
            enrollment = runtime.require_enrollment()
            bridge = enrollment._effect_runtime
            if type(bridge) is not NativeAtlassianRuntime or bridge.runtime is not runtime:
                raise ValueError
            bridge.coverage()
            descriptor = claims["native_atlassian"]
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
                or claims.get("host_generation") is not None
                or claims.get("native_test") is not None
            ):
                raise ValueError
            await enrollment.current_fixture(approval)
            authority = await run_in_threadpool(enrollment.store.authority, approval.designation.tenant_id, approval.run_id)
            if authority != approval.authority():
                raise ValueError
            await run_in_threadpool(enrollment.store.authority_guards, authority, enrollment.clock().timestamp())
            original, quote, registered = await runtime.tool_lifecycle._original(approval, descriptor["execution_id"])
            expected = {
                **asdict(approval.identity),
                "authority_revision": approval.revision,
                "meter_epoch": approval.meter_epoch,
                "route": registered.effect_key,
                "request_sha256": descriptor["request_sha256"],
                "operation_id": descriptor["operation_id"],
            }
            recorded = original["native_atlassian_request"]
            service = NativeTestEffectAdmission(
                effect_key=registered.effect_key, store=enrollment.store, budget=bridge.budget, clock=enrollment.clock
            )
            if (
                original["binding"] != expected
                or original["operation_id"] != descriptor["operation_id"]
                or original["status"] != "dispatch_started"
                or original.get("native_atlassian_dispatch") is not None
                or original.get("native_cleanup_attempts_used", 0) != 0
                or set(recorded) != {"tool", "request_id", "arguments_sha256", "approved_sha256"}
                or recorded["tool"] != "atlassian___" + operation
                or recorded["request_id"] != descriptor["request_id"]
                or recorded["arguments_sha256"] != binding["payload_sha256"]
                or registered.action != "call"
                or registered.tool != "atlassian." + operation
                or registered.source_sha256 != bridge.binding.release_sha256
                or original["reservation_id"] != binding["receipt_id"]
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
            _, contract = await run_in_threadpool(source._read, enrollment.clock())
            if type(contract) is not FacadeEffectCostContract or contract.request_sha256 != recorded["approved_sha256"]:
                raise ValueError
            await source.confirm(quote, now=enrollment.clock())
            hold_deadline = await runtime.tool_lifecycle._funding(approval, original, quote.total_usd)
            deadline = min(
                hold_deadline,
                quote.expires_at.timestamp(),
                approval.dispatch_expires_at,
                approval.approved_at + 300,
                original["facade_dispatch"]["session_deadline_at"],
                claims["expires_at"],
            )
            transfer = cls(bridge, approval, original, source, quote, deadline, digest(binding))
            transfer.require_fresh()
            return transfer
        except AdmissionRefusedError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AdmissionRefusedError("native_test_atlassian_dispatch_refused") from None

    def require_fresh(self):
        if self.bridge.enrollment.clock().timestamp() >= self.deadline:
            raise AdmissionRefusedError("native_test_atlassian_dispatch_expired")

    def claim(self, admission_id):
        """Consume once before the adapter can obtain a provider access token."""
        self.require_fresh()
        if not isinstance(admission_id, str) or not admission_id or self.original.get("native_atlassian_dispatch") is not None:
            raise AdmissionRefusedError("native_test_atlassian_dispatch_refused")
        store, now = self.bridge.runtime.store, self.bridge.enrollment.clock().timestamp()
        original = self.original
        updated = {
            **original,
            "version": original["version"] + 1,
            "native_atlassian_dispatch": {
                "status": "dispatch_started",
                "claimed_at": now,
                "binding_sha256": self.binding_sha256,
                "admission_id": admission_id,
            },
        }
        guards = store._distinct_guards(
            [
                *store.authority_guards(self.approval.authority(), now),
                *self.source.guards(self.quote, now=now),
            ]
        )
        store.transaction(
            [
                *guards,
                {
                    "Put": {
                        "TableName": store.table,
                        "Item": store._item(updated),
                        "ConditionExpression": "document = :original",
                        "ExpressionAttributeValues": {":original": {"S": canonical(original)}},
                    }
                },
            ]
        )
        self.original = updated
        self.require_fresh()
