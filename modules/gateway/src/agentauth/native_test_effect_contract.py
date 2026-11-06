"""Pinned, protected Dynamo source for complete native lifecycle quotes.

No contract is installed by this module. The deployment registrar must supply
all priced enforced bounds. Gateway and CI roles cannot write the protected
deployment partition. Contracts contain hashes of exact server SDK envelopes;
arbitrary tool arguments, caller costs and policy ceilings are unsupported.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound, NativeEffectQuote, component_total, sha256
from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest


class EffectContractTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component: str
    unit: str
    quantity: str
    usd_per_unit: str
    pricing_evidence_sha256: str
    enforced_limit_sha256: str


class EffectCostContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    state: Literal["active"]
    scope: Literal["model_tool_setup_cleanup"]
    effect_key: str
    request_sha256: str
    source_sha256: str
    pricing_revision: str
    not_before: int = Field(ge=1)
    expires_at: int = Field(ge=1)
    quote_seconds: int = Field(ge=1, le=60)
    session_seconds: int = Field(ge=1, le=300)
    cleanup_seconds: int = Field(ge=1, le=600)
    cleanup_attempts: int = Field(ge=1, le=32)
    components: list[EffectContractTerm] = Field(min_length=1, max_length=128)


class ProtectedEffectQuoteAdapter:
    """Real consistent reads and exact quote-document CAS, no tariff fallback.

    The server pins deployment, contract and release digests. The immutable
    contract's evidence hashes attest enforced quantities and published rates;
    this reader cannot establish missing bounds or issue all-in credentials.
    """

    def __init__(self, *, store, deployment_id, contract_id, contract_sha256, source_sha256):
        if (
            type(store) is not NativeTestStore
            or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) is None for value in (deployment_id, contract_id))
            or not sha256(contract_sha256)
            or not sha256(source_sha256)
        ):
            raise AdmissionRefusedError("native_test_effect_contract_unavailable")
        self.store = store
        self.key = {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + deployment_id}, "sk": {"S": "EFFECT_CONTRACT#" + contract_id}}
        self.contract_sha256, self.source_sha256 = contract_sha256, source_sha256

    def _read(self, now):
        try:
            document = self.store.document(self.key, "native_test_effect_contract_v1")
            if digest(document) != self.contract_sha256:
                raise ValueError
            model = EffectCostContract
            if isinstance(document, dict) and document.get("request_binding") == "facade_call_v1":
                from src.agentauth.native_test_facade_contract import FacadeEffectCostContract, InterpreterStepCostContract

                model = InterpreterStepCostContract if "interpreter_step" in document else FacadeEffectCostContract
            contract = model.model_validate(document)
            terms = tuple(CostBound(**term.model_dump()) for term in contract.components)
            if (
                contract.model_dump() != document
                or contract.source_sha256 != self.source_sha256
                or not contract.not_before <= now.timestamp() < contract.expires_at
                or not sha256(contract.request_sha256)
                or not sha256(contract.pricing_revision)
                or re.fullmatch(r"native-effect:[a-z][a-z0-9_.-]{1,127}", contract.effect_key) is None
                or contract.cleanup_seconds < contract.session_seconds
                or {term.component for term in terms} != COMPONENTS
                or component_total(terms) <= 0
            ):
                raise ValueError
            return document, contract
        except Exception:
            raise AdmissionRefusedError("native_test_effect_contract_unavailable") from None

    def registration_guards(self, *, effect_key, dispatch_expires_at, now):
        document, contract = self._read(now)
        if contract.effect_key != effect_key or contract.expires_at < dispatch_expires_at:
            raise AdmissionRefusedError("native_test_effect_contract_binding_changed")
        return [self.store.guard(self.key, "native_test_effect_contract_v1", document)]

    async def quote(self, body, effect_key, authority, *, now):
        _, contract = await run_in_threadpool(self._read, now)
        if effect_key != contract.effect_key or not self._request_permitted(body, contract):
            raise AdmissionRefusedError("native_test_effect_contract_binding_changed")
        terms = tuple(CostBound(**term.model_dump()) for term in contract.components)
        return NativeEffectQuote(
            effect_key=effect_key,
            request_sha256=hashlib.sha256(body).hexdigest(),
            authority_sha256=digest(authority),
            source_contract_sha256=self.contract_sha256,
            pricing_revision=contract.pricing_revision,
            issued_at=now,
            expires_at=min(now + timedelta(seconds=contract.quote_seconds), datetime.fromtimestamp(contract.expires_at, UTC)),
            session_seconds=contract.session_seconds,
            cleanup_seconds=contract.cleanup_seconds,
            cleanup_attempts=contract.cleanup_attempts,
            components=terms,
            total_usd=component_total(terms),
        )

    def _require_quote(self, quote, contract, now):
        if (
            quote.source_contract_sha256 != self.contract_sha256
            or quote.effect_key != contract.effect_key
            or not self._quote_request_permitted(quote, contract)
            or quote.pricing_revision != contract.pricing_revision
            or quote.session_seconds != contract.session_seconds
            or quote.cleanup_seconds != contract.cleanup_seconds
            or quote.cleanup_attempts != contract.cleanup_attempts
            or quote.components != tuple(CostBound(**term.model_dump()) for term in contract.components)
            or quote.expires_at.timestamp() > contract.expires_at
            or (quote.expires_at - quote.issued_at).total_seconds() > contract.quote_seconds
            or quote.expired(now)
        ):
            raise AdmissionRefusedError("native_test_effect_contract_binding_changed")

    async def revalidate(self, quote, body, authority, *, now):
        _, contract = await run_in_threadpool(self._read, now)
        if not self._request_permitted(body, contract):
            raise AdmissionRefusedError("native_test_effect_contract_binding_changed")
        quote.require_binding(body, contract.effect_key, authority, now)
        self._require_quote(quote, contract, now)

    async def confirm(self, quote, *, now):
        _, contract = await run_in_threadpool(self._read, now)
        self._require_quote(quote, contract, now)

    def guards(self, quote, *, now):
        current = datetime.fromtimestamp(now, UTC)
        document, contract = self._read(current)
        self._require_quote(quote, contract, current)
        return [self.store.guard(self.key, "native_test_effect_contract_v1", document)]

    def _request_permitted(self, body, contract):
        return type(contract) is EffectCostContract and hashlib.sha256(body).hexdigest() == contract.request_sha256

    def _quote_request_permitted(self, quote, contract):
        return type(contract) is EffectCostContract and quote.request_sha256 == contract.request_sha256
