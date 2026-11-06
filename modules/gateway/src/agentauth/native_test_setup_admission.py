"""Reserve all-in issuer/setup work before capability activation.

This path consumes the original staged authority and initialized meter. It does
not create either one, issue a login, call a provider, settle or release a hold.
Uncertain issuance stays reserved and cannot be retried into a new allowance.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_admission import NativeTestEffectAdmission
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_enrollment import AllInApproval, NativeTestEnrollment, is_effect_approval
from src.agentauth.test_run_admission import PreparedModelAdmission, _target_evidence
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical


@dataclass(frozen=True)
class _IssuingIdentity:
    enrollment: NativeTestEnrollment = field(repr=False)
    approval: AllInApproval = field(repr=False)
    capability: dict = field(repr=False)
    effect_key: str
    nonce: str = field(repr=False)

    async def verify_effect(self, context, credential, effect_key):
        if credential != self.nonce or effect_key != self.effect_key:
            raise AdmissionRefusedError("native_test_setup_identity_refused")
        self.enrollment.require_login(context, self.approval, self.enrollment.clock())
        await self.enrollment.current_fixture(self.approval)
        current = await run_in_threadpool(self.enrollment.store.capability, context.org_id, self.approval.credential_id)
        if current != self.capability or current["status"] != "issuing":
            raise AdmissionRefusedError("native_test_setup_identity_refused")
        return self.approval.identity


class _SetupAdmission(NativeTestEffectAdmission):
    _authority_state = "issuing_setup"

    async def begin_effect_dispatch(self, *args, **kwargs):
        raise AdmissionRefusedError("native_test_setup_is_reserved_at_issuance")


@dataclass(frozen=True)
class NativeSetupHold:
    """Server-only original prepared hold; never serialize into an HTTP receipt."""

    service: _SetupAdmission = field(repr=False)
    prepared: PreparedModelAdmission = field(repr=False)
    identity: _IssuingIdentity = field(repr=False)
    body: bytes = field(repr=False)

    async def confirm(self, *, context, enrollment, approval, capability):
        if (
            self.identity.enrollment is not enrollment
            or self.identity.approval != approval
            or self.identity.capability != capability
            or self.service.store is not enrollment.store
            or self.service.budget._get_reservations() is not enrollment.reservations
        ):
            raise AdmissionRefusedError("native_test_setup_hold_refused")
        _, authority = await self.service._authority(context, self.identity.nonce)
        operation = await run_in_threadpool(
            self.service.store.read_operation, authority["tenant_id"], authority["run_id"], self.prepared.receipt.operation_id
        )
        if authority != self.prepared._authority or operation != self.prepared._operation or operation["status"] != "admitted":
            raise AdmissionRefusedError("native_test_setup_hold_refused")
        await self.service._validate_quote(self.prepared._quote, self.body, self.service.effect_key, authority)
        targets = self.prepared._context._budget_admission_targets
        if (
            not targets
            or self.prepared._context._policy_flow_target not in targets
            or self.prepared._context._policy_flow_target.key() != self.service._target(authority).key()
            or self.prepared._context._policy_quote != self.prepared._quote
            or self.prepared._context._policy_request_id != self.prepared.receipt.reservation_id
            or self.prepared._context._policy_estimated_cost != self.prepared._quote.total_usd
            or operation["targets"] != [_target_evidence(target) for target in targets]
            or operation["target_keys"] != [target.key() for target in targets]
        ):
            raise AdmissionRefusedError("native_test_setup_hold_refused")
        for target in targets:
            await self.service._verify_hold(self.prepared.receipt.reservation_id, self.prepared._quote.total_usd, target)
        await self.service._confirm_quote(self.prepared._quote)


async def reserve_native_setup(*, context, enrollment, approval, capability, budget):
    if type(enrollment) is not NativeTestEnrollment or not is_effect_approval(approval) or budget._get_reservations() is not enrollment.reservations:
        raise AdmissionRefusedError("native_test_setup_binding_refused")
    setup = [effect for effect in approval.effects if effect.action == "setup"]
    if len(setup) != 1:
        raise AdmissionRefusedError("native_test_setup_contract_unavailable")
    registration = setup[0]
    source = ProtectedEffectQuoteAdapter(
        store=enrollment.store,
        deployment_id=enrollment.deployment_id,
        contract_id=registration.contract_id,
        contract_sha256=registration.contract_sha256,
        source_sha256=registration.source_sha256,
    )
    identity = _IssuingIdentity(enrollment, approval, capability, registration.effect_key, secrets.token_urlsafe(32))
    service = _SetupAdmission(
        effect_key=registration.effect_key,
        store=enrollment.store,
        budget=budget,
        identity_verifier=identity,
        quote_adapter=source,
        clock=enrollment.clock,
    )
    body = canonical({"effect_key": registration.effect_key, "phase": "test_setup"}).encode()
    prepared = await service.prepare_effect(
        context=context, credential=identity.nonce, operation_id=str(uuid5(NAMESPACE_URL, "adp-native-test-setup:" + approval.run_id)), body=body
    )
    if type(prepared) is not PreparedModelAdmission:
        raise AdmissionRefusedError("native_test_setup_reconciliation_required")
    return NativeSetupHold(service, prepared, identity, body)
