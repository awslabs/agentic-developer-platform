"""Native paid-effect admission against the original model + lifecycle meter.

This is a composable server boundary, not an issuer or a provider tariff. Its
default identity and quote adapters refuse. Model-only enrollment cannot be used
as all-in authority. Facade/provider adoption must carry the original handoff;
cleanup and settlement remain unavailable without their complete source adapter.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_quotes import NativeEffectQuote, UnavailableEffectQuoteAdapter
from src.agentauth.test_run_admission import AdmissionReceipt, PreparedModelAdmission, TestRunAdmissionService
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


class UnavailableAllInIdentityVerifier:
    async def verify_effect(self, context, credential, effect_key):
        raise AdmissionRefusedError("native_test_all_in_credential_unavailable")


@dataclass(frozen=True)
class EffectIdentityVerifier:
    verifier: object
    effect_key: str

    async def verify(self, context, credential):
        # The model-only verifier deliberately has no verify_effect method.
        verifier = getattr(self.verifier, "verify_effect", None)
        if verifier is None:
            raise AdmissionRefusedError("native_test_all_in_credential_unavailable")
        return await verifier(context, credential, self.effect_key)


@dataclass(frozen=True)
class NativeEffectQuoteGuard:
    service: object = field(repr=False)
    context: object = field(repr=False)
    quote: NativeEffectQuote = field(repr=False)
    body: bytes = field(repr=False)
    authority: dict = field(repr=False)
    target: object = field(repr=False)

    async def confirm(self, context, budget):
        if (
            context is not self.context
            or budget is not self.service.budget
            or context._policy_quote != self.quote
            or context._policy_estimated_cost != self.quote.total_usd
            or context._policy_flow_target != self.target
        ):
            raise AdmissionRefusedError("native_test_effect_quote_changed")
        await self.service._validate_quote(self.quote, self.body, self.service.effect_key, self.authority)
        return await self.service._confirm_quote(self.quote)


class NativeTestEffectAdmission(TestRunAdmissionService):
    """One registered effect adapter; callers supply bytes and an operation ID.

    The inherited journal, exact authority guards, atomic hierarchy/test reserve,
    original target verification, dispatch CAS and unknown-hold behavior are the
    same ones used by model admission. No caller cost or cleanup release exists.
    """

    def __init__(self, *, effect_key, store, budget=None, identity_verifier=None, quote_adapter=None, clock=None):
        self.effect_key = effect_key
        self.quote_adapter = quote_adapter or UnavailableEffectQuoteAdapter()
        super().__init__(
            store=store,
            budget=budget,
            identity_verifier=EffectIdentityVerifier(identity_verifier or UnavailableAllInIdentityVerifier(), effect_key),
            clock=clock,
        )

    def _supports_request(self, route, authority):
        return route == self.effect_key

    def _reservation_id(self, tenant_id, run_id, operation_id):
        return "tools-test-effect:" + digest({"tenant_id": tenant_id, "run_id": run_id, "operation_id": operation_id})

    async def _fetch_quote(self, body, route, authority):
        return await self.quote_adapter.quote(body, route, authority, now=self.clock())

    def _quote_permitted(self, quote, authority):
        if not (type(quote) is NativeEffectQuote and quote.effect_key == self.effect_key and quote.authority_sha256 == digest(authority)):
            return False
        if authority.get("scope") == "all_in":
            from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter

            registrations = [effect for effect in authority["effects"] if effect["effect_key"] == self.effect_key]
            if len(registrations) != 1 or not isinstance(self.quote_adapter, ProtectedEffectQuoteAdapter):
                return False
            registered = registrations[0]
            return (
                self.quote_adapter.store is self.store
                and quote.source_contract_sha256 == registered["contract_sha256"] == self.quote_adapter.contract_sha256
                and self.quote_adapter.source_sha256 == registered["source_sha256"]
                and self.quote_adapter.key
                == {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + authority["deployment_id"]}, "sk": {"S": "EFFECT_CONTRACT#" + registered["contract_id"]}}
            )
        return True

    async def _validate_quote(self, quote, body, route, authority):
        if not self._quote_permitted(quote, authority):
            raise AdmissionRefusedError("native_test_all_in_quote_unavailable")
        quote.require_binding(body, route, authority, self.clock())
        if await self.quote_adapter.revalidate(quote, body, authority, now=self.clock()) is not None:
            raise AdmissionRefusedError("native_test_effect_quote_changed")
        quote.require_binding(body, route, authority, self.clock())

    async def _confirm_quote(self, quote):
        if quote.expired(self.clock()):
            raise AdmissionRefusedError("native_test_effect_quote_changed")
        if await self.quote_adapter.confirm(quote, now=self.clock()) is not None:
            raise AdmissionRefusedError("native_test_effect_quote_changed")
        if quote.expired(self.clock()):
            raise AdmissionRefusedError("native_test_effect_quote_changed")
        return None

    def _restore_quote(self, evidence):
        return NativeEffectQuote.from_evidence(evidence)

    def _quote_evidence(self, quote):
        evidence = super()._quote_evidence(quote)
        # Dynamo canonical JSON represents tuples as arrays; keep the original
        # journal identical to its readback before final dispatch comparison.
        evidence["components"] = list(evidence["components"])
        return evidence

    def _bind_quote(self, context, quote, body, route, authority):
        context._policy_quote = quote
        context._native_test_effect_quote_guard = NativeEffectQuoteGuard(self, context, quote, body, authority, context._policy_flow_target)

    def _transition(self, authority, operation, **kwargs):
        # Registered protected quote sources add exact document guards to the
        # same journal CAS. The unavailable adapter cannot reach this path.
        guards = getattr(self.quote_adapter, "guards", None)
        if guards is not None and kwargs["status"] != "unknown":
            quote = self._restore_quote(kwargs.get("quote", operation.get("quote")))
            # Preparation's journal timestamp historically uses whole seconds;
            # the quote's issued_at may have microseconds. Never move the source
            # freshness check backwards before the quote's actual issuance.
            kwargs["now"] = max(kwargs["now"], self.clock().timestamp())
            kwargs["quote_guards"] = guards(quote, now=kwargs["now"])
        return super()._transition(authority, operation, **kwargs)

    async def prepare_effect(self, *, context, credential, operation_id, body):
        return await super().prepare_model(context=context, credential=credential, operation_id=operation_id, body=body, route=self.effect_key)

    async def begin_effect_dispatch(self, prepared, *, context, credential, body):
        return await super().begin_model_dispatch(prepared, context=context, credential=credential, body=body, route=self.effect_key)

    async def prepare_model(self, **kwargs):
        raise AdmissionRefusedError("native_test_effect_route_required")

    async def begin_model_dispatch(self, *args, **kwargs):
        raise AdmissionRefusedError("native_test_effect_route_required")


@dataclass
class NativeEffectHandoff:
    """One in-process provider submission after durable admission and intent CAS."""

    service: NativeTestEffectAdmission = field(repr=False)
    prepared: PreparedModelAdmission = field(repr=False)
    context: object = field(repr=False)
    credential: str = field(repr=False)
    body: bytes = field(repr=False)
    _used: bool = field(default=False, init=False)
    _authorized: bool = field(default=False, init=False)
    _submitted: bool = field(default=False, init=False)

    async def dispatch(self, *, body, request_id, client):
        try:
            if (
                self._used
                or type(body) is not bytes
                or body != self.body
                or request_id != self.prepared.receipt.reservation_id
                or type(client.meta.config.retries.get("total_max_attempts")) is not int
                or client.meta.config.retries.get("total_max_attempts") != 1
            ):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise AdmissionRefusedError("native_test_effect_provider_binding_changed") from None
        self._used = True
        receipt = await self.service.begin_effect_dispatch(self.prepared, context=self.context, credential=self.credential, body=self.body)
        self._authorized = True
        return receipt

    def before_submit(self):
        """Call inside the provider executor immediately before its one SDK send."""
        deadline = self.prepared._context._native_test_dispatch_deadline
        if (
            not self._authorized
            or self._submitted
            or not self.prepared._context._budget_provider_started
            or not isinstance(deadline, int | float)
            or not math.isfinite(deadline)
            or self.service.clock().timestamp() >= deadline
        ):
            raise AdmissionRefusedError("native_test_effect_submission_refused")
        self._submitted = True


@dataclass(frozen=True)
class NativeEffectSDKExecutor:
    """Concrete single-send SDK consumer of the original admission.

    Compose with a fixed SDK method/client on the server. Only JSON SDK inputs
    are supported. The quote binds the actual service, endpoint, region, method
    and parameters sent here. There is no detached allow-receipt consumer or
    transport retry. SDK failures preserve the full original lifecycle hold.
    """

    service: NativeTestEffectAdmission = field(repr=False)
    client: object = field(repr=False)
    sdk_method: str

    def _body(self, parameters):
        try:
            if (
                re.fullmatch(r"[a-z][a-z0-9_]{1,127}", self.sdk_method) is None
                or not callable(getattr(self.client, self.sdk_method))
                or type(parameters) is not dict
                or type(self.client.meta.config.retries.get("total_max_attempts")) is not int
                or self.client.meta.config.retries.get("total_max_attempts") != 1
            ):
                raise ValueError
            return canonical(
                {
                    "effect_key": self.service.effect_key,
                    "service": self.client.meta.service_model.service_name,
                    "region": self.client.meta.region_name,
                    "endpoint": self.client.meta.endpoint_url,
                    "sdk_method": self.sdk_method,
                    "parameters": parameters,
                }
            ).encode()
        except (ValueError, TypeError, AttributeError):
            raise AdmissionRefusedError("native_test_effect_provider_binding_changed") from None

    async def invoke(self, *, context, credential, operation_id, parameters):
        body = self._body(parameters)
        # Decode once from the quoted bytes; mutations of caller-owned inputs
        # during admission cannot change the submitted SDK parameters.
        frozen = json.loads(body)["parameters"]
        prepared = await self.service.prepare_effect(context=context, credential=credential, operation_id=operation_id, body=body)
        if isinstance(prepared, AdmissionReceipt):
            return prepared, None
        transfer = NativeEffectHandoff(self.service, prepared, context, credential, body)
        receipt = await transfer.dispatch(body=body, request_id=prepared.receipt.reservation_id, client=self.client)

        def submit():
            if self._body(frozen) != body:
                raise AdmissionRefusedError("native_test_effect_provider_binding_changed")
            transfer.before_submit()
            return getattr(self.client, self.sdk_method)(**frozen)

        return receipt, await run_in_threadpool(submit)
