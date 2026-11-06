"""Mandatory model scope for protected native test subjects.

Compose after real authentication, before rate/budget middleware. Never install
an enrollment endpoint without this same registry/enrollment instance. The
application's native-test runtime owns the protected production composition.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OPERATION_HEADER, NativeTestStore
from src.agentauth.test_run_admission import MODEL_ROUTES, AdmissionReceipt, PreparedModelAdmission
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.schemas.auth import TokenContext

MAX_BODY = 16 * 1024 * 1024


@dataclass
class NativeModelHandoff:
    service: object = field(repr=False)
    prepared: PreparedModelAdmission = field(repr=False)
    login_context: TokenContext = field(repr=False)
    credential: str = field(repr=False)
    body: bytes = field(repr=False)
    route: str
    used: bool = False
    submitted: bool = False

    def require_context(self, context, request_id):
        if context is not self.prepared._context or request_id != self.prepared.receipt.reservation_id:
            raise AdmissionRefusedError("native_test_reservation_identity_changed")

    def before_submit(self):
        """Synchronous final guard after the SDK executor queue, before boto3."""
        deadline = self.prepared._context._native_test_dispatch_deadline
        if not self.used or self.submitted or deadline is None or not math.isfinite(deadline) or self.service.clock().timestamp() >= deadline:
            raise AdmissionRefusedError("native_test_submission_expired_or_consumed")
        self.submitted = True

    async def dispatch(self, context, *, body: bytes, model: str, route: str, request_id: str | None, client=None, stream=False):
        """Verify the approved transformation, then consume one dispatch intent."""
        from pricing_policy import canonical_billing_model_id

        self.require_context(context, request_id)
        if self.used or route != self.route or canonical_billing_model_id(model) != self.prepared._quote.billing_model_id:
            raise AdmissionRefusedError("native_test_provider_binding_changed")
        try:
            original = json.loads(self.body)
            upstream = json.loads(body)
            if original.get("stream", False) is not stream:
                raise ValueError
            if route == "/v1/messages":
                from src.proxy.format_translator import FormatTranslator
                from src.proxy.schemas import AnthropicMessagesRequest

                expected = FormatTranslator().anthropic_to_bedrock(AnthropicMessagesRequest.model_validate(original)).model_dump(exclude_none=True)
                if upstream != expected:
                    raise ValueError
                sdk = client._streaming_client if stream else client._invoke_client
                if sdk.meta.config.retries.get("total_max_attempts") != 1:
                    raise ValueError
            elif route == "/openai/v1/responses":
                # The only permitted Responses conversion changes the model's
                # inference-profile prefix, never content, maxima or tools.
                if canonical_billing_model_id(upstream.get("model", "")) != self.prepared._quote.billing_model_id:
                    raise ValueError
                if {**upstream, "model": original["model"]} != original:
                    raise ValueError
                if client.follow_redirects:
                    raise ValueError
            else:
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise AdmissionRefusedError("native_test_provider_binding_changed") from None
        # Mark the in-process handoff consumed before any awaited work, including
        # an ambiguous CAS. An exception never makes the object reusable.
        self.used = True
        await self.service.begin_model_dispatch(
            self.prepared,
            context=self.login_context,
            credential=self.credential,
            body=self.body,
            route=self.route,
        )
        # No awaited preparation belongs between this return and provider I/O.


def native_handoff(context):
    value = context._native_test_handoff
    if value is not None and type(value) is not NativeModelHandoff:
        raise AdmissionRefusedError("native_test_handoff_unavailable")
    return value


async def begin_native_model_dispatch(context, **kwargs):
    if context is not None and (handoff := native_handoff(context)) is not None:
        await handoff.dispatch(context, **kwargs)


class NativeTestModelMiddleware:
    """A negative consistent registry read is required for ordinary fallback.

    Capability omission, a revoked tombstone or cross-tenant context never turns
    a designated fixture into an ordinary request. No headers can designate it.
    No generic finally block releases test holds; owner reconciliation remains
    required for pre-dispatch errors and uncertain provider/usage outcomes.
    """

    def __init__(self, app, *, enrollment, admission, require_designation=False):
        if (
            type(enrollment.store) is not NativeTestStore
            or admission.store is not enrollment.store
            or admission.identity_verifier is not enrollment
            or admission.budget._get_reservations() is not enrollment.reservations
        ):
            raise AdmissionRefusedError("native_test_enforcement_composition_refused")
        self.app, self.enrollment, self.admission = app, enrollment, admission
        self.require_designation = require_designation
        enrollment._enforcer = self

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not any(path.startswith(prefix) for prefix in ENFORCED_PATHS):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        context = state.get("token_context")
        headers = scope.get("headers", [])
        credentials = [v for k, v in headers if k.lower() == CAPABILITY_HEADER.encode()]
        operations = [v for k, v in headers if k.lower() == OPERATION_HEADER.encode()]
        # Remove capability before request logging/downstream header access.
        scope["headers"] = [(k, v) for k, v in headers if k.lower() != CAPABILITY_HEADER.encode()]
        try:
            if context is None:
                raise AdmissionRefusedError("native_test_authenticated_context_required")
            designation = await run_in_threadpool(self.enrollment.store.designation, context.user_id)
            if designation is None:
                if self.require_designation or credentials or operations:
                    raise AdmissionRefusedError("native_test_designation_refused")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, AdmissionRefusedError) else "native_test_authority_unavailable"
            await JSONResponse({"error": reason, "retryable": False}, status_code=403)(scope, receive, send)
            return
        if designation is None:
            await self.app(scope, receive, send)
            return
        try:
            self.enrollment.active(designation)
            if (
                designation.tenant_id != context.org_id
                or context.attributed_org_id != context.org_id
                or context.account_type != "human"
                or context.auth_source != "jwt"
            ):
                raise AdmissionRefusedError("native_test_designation_refused")
            if path not in MODEL_ROUTES or scope.get("method") != "POST":
                raise AdmissionRefusedError("native_test_model_route_unsupported")
            if len(credentials) != 1 or len(operations) != 1 or len(credentials[0]) > 128 or len(operations[0]) != 36:
                raise AdmissionRefusedError("mandatory_native_test_capability_required")
            if any(k.lower() in {b"anthropic-beta", b"x-agent-runid"} for k, _ in headers):
                raise AdmissionRefusedError("native_test_request_headers_unsupported")
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise AdmissionRefusedError("native_test_body_unavailable")
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    raise AdmissionRefusedError("native_test_body_too_large")
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            credential, operation_id = credentials[0].decode("ascii"), operations[0].decode("ascii")
            result = await self.admission.prepare_model(context=context, credential=credential, operation_id=operation_id, body=body, route=path)
            if isinstance(result, AdmissionReceipt):
                await JSONResponse({"error": "native_test_operation_replay", "receipt": asdict(result)}, status_code=409)(scope, receive, send)
                return
            if not isinstance(result, PreparedModelAdmission):
                raise AdmissionRefusedError("native_test_admission_unavailable")
            handoff = NativeModelHandoff(self.admission, result, context, credential, body, path)
            result._context._native_test_handoff = handoff
            result._context._budget_request_timestamp = context._budget_request_timestamp
            # Canonical hierarchy and the exact admitted target list, reservation
            # ID, quote and original context travel through both usage writers.
            state["token_context"], state["request_id"] = result._context, result.receipt.reservation_id
            delivered = False

            async def replay_body():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

        except Exception as exc:
            reason = str(exc) if isinstance(exc, AdmissionRefusedError) else "native_test_authority_unavailable"
            await JSONResponse({"error": reason, "retryable": False}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, replay_body, send)
