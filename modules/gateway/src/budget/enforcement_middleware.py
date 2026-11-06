"""
Budget Enforcement Middleware (pure ASGI).

This middleware intercepts proxy requests to check budget constraints
before forwarding to Bedrock. It supports cascading enforcement across
the entity hierarchy (user → team → department → organization).

IMPORTANT: This is a raw ASGI middleware — NOT BaseHTTPMiddleware.
BaseHTTPMiddleware has a known Starlette bug where returning a response
from dispatch() without calling call_next() causes the response to hang
indefinitely. By using raw ASGI, we write the 402 response directly via
the ASGI `send` callable, which is guaranteed to reach the client.

Issue #234: Removed inline usage recording — now handled by S3-triggered
            Lambda for accurate cost tracking from chat logs.

Issue #249: Added agent-level budget checking via X-Agent-BudgetConfigId header.
            Agent budgets are checked BEFORE team/org hierarchy (most specific wins).

Issue #4287: the pre-request estimate is model- and size-aware instead of a flat
            $0.05. Still no body read — see _estimate_cost.
"""

import asyncio
import json
import os
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason, EnforcementResult
from src.shared.timing import get_timings

from .enforcement_service import BudgetEnforcementService, budget_enforcement_service
from .metering_activation import mark_legacy_cohort_not_dispatched, record_legacy_cohort_admission, require_non_draining_admission

logger = get_logger(__name__)

# Fallback pre-request estimate (USD), used only when neither the model nor the
# request size can be determined from the ASGI scope — e.g. a chunked upload with
# no content-length on a route that does not carry the model in its path.
#
# Issue #4287: this used to be the estimate for EVERY request, flat, regardless of
# model or size. A single large call against an expensive model could therefore
# overshoot a cap the check had just passed, because the check priced it at 5
# cents. It is now the last resort, not the rule.
_DEFAULT_ESTIMATE_USD = Decimal("0.05")

# Retry-After for check-failure denials (Issue #4075). Short on purpose: the
# ledger is expected back in seconds, and the whole point of using 503 over 402
# is that the client can recover unaided.
_CHECK_UNAVAILABLE_RETRY_AFTER = b"5"


class BudgetEnforcementMiddleware:
    """
    Pure ASGI middleware for enforcing budget limits on proxy requests.

    Pre-request: checks budget using model ID from URL path and a fixed
    cost estimate. Does NOT read the request body.

    When budget is exceeded, writes a 402 response directly via ASGI send()
    — no BaseHTTPMiddleware, no Starlette Response objects, no hanging.

    Measured usage settles transactionally in UsageService. Optional transcript
    events share its receipt through the budget-usage-tracker Lambda.
    """

    def __init__(
        self,
        app: ASGIApp,
        enforcement_service: BudgetEnforcementService | None = None,
    ):
        self.app = app
        self.enforcement_service = enforcement_service or budget_enforcement_service
        self._production_gate_fence = enforcement_service is None or enforcement_service is budget_enforcement_service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if not self._should_enforce(path):
            await self.app(scope, receive, send)
            return

        # Get token_context from scope state (set by TokenContextMiddleware)
        state = scope.get("state", {})
        token_context: TokenContext | None = state.get("token_context")

        if not token_context:
            await self.app(scope, receive, send)
            return

        gateway = scope.get("app")
        metered_responses = (
            path == "/openai/v1/responses"
            and scope.get("method") == "POST"
            and getattr(getattr(gateway, "state", None), "metered_responses_ready", False)
        )
        if metered_responses:
            if token_context._native_test_handoff is not None or any(
                name.lower() in {b"x-adp-native-test-capability", b"x-adp-native-test-operation"} for name, _ in scope.get("headers", [])
            ):
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "unsupported_metered_test_scope", "retryable": False}, status_code=403)(scope, receive, send)
                return

        if (
            not metered_responses
            and scope.get("method") == "POST"
            and (self._production_gate_fence or os.environ.get("BG_METERING_GATE_DRAIN_ENABLED") == "true")
        ):
            try:
                record_cohort = os.environ.get("BG_METERING_GATE_DRAIN_ENABLED") == "true"
                async with asyncio.timeout(2):
                    async with self.enforcement_service._get_session() as session:
                        for tenant_id in sorted({token_context.org_id, token_context.attributed_org_id}):
                            await require_non_draining_admission(session, tenant_id=tenant_id)
                        if record_cohort:
                            request_id = state.get("request_id") or str(uuid4())
                            state["request_id"] = request_id
                            await record_legacy_cohort_admission(
                                session,
                                tenant_id=token_context.attributed_org_id,
                                authenticated_tenant_id=token_context.org_id,
                                request_id=request_id,
                                owner_id=token_context.user_id,
                            )
                        await session.commit()
                        if record_cohort:
                            state["metering_legacy_cohort_admitted"] = True
            except Exception:
                from starlette.responses import JSONResponse

                logger.warning("Provider admission refused while metering gate is draining or unavailable")
                try:
                    message = await asyncio.wait_for(receive(), timeout=0.02)
                    if message.get("type") == "http.disconnect":
                        return
                except TimeoutError:
                    pass
                headers = {"Connection": "close"} if scope.get("http_version", "1.1").startswith("1.") else None
                await JSONResponse({"error": "metering_admission_unavailable", "retryable": False}, status_code=503, headers=headers)(
                    scope, receive, send
                )
                return

        if token_context._native_test_handoff is not None:
            from src.agentauth.native_test_model import native_handoff

            handoff = native_handoff(token_context)
            handoff.require_context(token_context, state.get("request_id"))
            # The native primitive already reserved all hierarchy + test scopes
            # atomically. Generic admission/zero cleanup would lose that scope.
            await self.app(scope, receive, send)
            if not token_context._budget_provider_started:
                await self._disposition_if_unsent(state, token_context, "pre_provider_return")
            return

        if any(name.lower() in {b"x-adp-native-test-capability", b"x-adp-native-test-operation"} for name, _ in scope.get("headers", [])):
            from starlette.responses import JSONResponse

            # An asserted test scope without the adopted native middleware may
            # never silently enter ordinary paid admission.
            await self._disposition_if_unsent(state, token_context, "native_header_denied")
            await JSONResponse({"error": "mandatory_native_test_enforcement_unavailable", "retryable": False}, status_code=403)(scope, receive, send)
            return

        if state.get("request_started_at") is not None:
            token_context._budget_request_timestamp = state["request_started_at"]

        # Build a Request object for timing access (read-only, no body access)
        request = Request(scope, receive, send)

        timings = get_timings(request)
        with timings.time_segment("budget_check"):
            # Issue #249 read the agent-level budget config id from the
            # X-Agent-BudgetConfigId header, which the (now deprecated and
            # unattached) Lambda authorizer was meant to set. Issue #3985
            # removed that: the header was trusted on presence alone under
            # BG_TRUST_APIGW_HEADERS, so a client could name any budget config
            # — including a fresh/unspent one — and dodge its own agent budget.
            # Only the org/team hierarchy check remains, and it derives entirely
            # from the authenticated token_context.
            #
            # Re-adding per-agent budget enforcement requires resolving the
            # config id from the agent registry entry (server-side, keyed off
            # the authenticated identity), not from a request header.
            result = await self.enforcement_service.prepare_enforcement_context(token_context, self._asserted_run_id(scope))
            if result is None and not token_context._budget_enforcement_enabled:
                # An uncapped call still needs a server-owned accounting identity.
                # Caller trace IDs must never replace another request's charge.
                token_context._policy_request_id = state.get("request_id") or token_context._policy_request_id or str(uuid4())
                state["request_id"] = token_context._policy_request_id
            result = result or await self.enforcement_service.check_budget_hierarchy(
                token_context,
                self._estimate_cost(scope, path) if token_context._budget_enforcement_enabled and not metered_responses else Decimal(0),
                # Issue #4287: idempotency key for the live-denominator
                # reservation, so the proxy can adjust THIS request's reservation
                # to its real cost once the response lands. Set by
                # the outer RequestIdentityMiddleware.
                request_id=state.get("request_id"),
                # Issue #4187: the caller's ASSERTED run id. Passed on as an
                # assertion to be verified server-side against the webhook-events
                # registry, never used as an identity — see run_binding.py. The
                # header is read here rather than reusing the route-level
                # `set_agent_run_id_from_header` dependency because this
                # middleware runs before any route is resolved.
                run_id=self._asserted_run_id(scope),
                reserve=not metered_responses,
            )

        if not result.allowed:
            await self._disposition_if_unsent(state, token_context, "budget_denied")
            if metered_responses:
                deadline = asyncio.get_running_loop().time() + 0.02
                body_complete = False
                while True:
                    receive_task = asyncio.create_task(receive())
                    try:
                        done, _ = await asyncio.wait({receive_task}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
                    except BaseException:
                        receive_task.cancel()
                        receive_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                        raise
                    if not done:
                        receive_task.cancel()
                        receive_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                        break
                    message = receive_task.result()
                    if message.get("type") == "http.disconnect":
                        return
                    if message.get("type") != "http.request":
                        break
                    if not message.get("more_body", False):
                        body_complete = True
                        break
            else:
                body_complete = True
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        return
                    if not message.get("more_body", False):
                        break

            denial_send = send
            if not body_complete and scope.get("http_version", "1.1").startswith("1."):

                async def close_connection(message):
                    if message["type"] == "http.response.start":
                        message = {**message, "headers": [*message.get("headers", []), (b"connection", b"close")]}
                    await send(message)

                denial_send = close_connection

            if result.deny_reason == DenyReason.RESERVATIONS_PENDING:
                await self._send_reservations_pending(denial_send, result)
            elif result.deny_reason == DenyReason.CHECK_UNAVAILABLE:
                await self._send_check_unavailable(denial_send, result)
                logger.info("Budget check unavailable response sent successfully")
            else:
                await self._send_budget_exceeded(denial_send, result)
                logger.info("Budget exceeded response sent successfully")
            return
        if metered_responses:
            await self.app(scope, receive, send)
            return

        # Release only a request rejected before provider dispatch. Unknown
        # provider outcomes retain their estimates, and measured charges must
        # never be overwritten by a generic error-response cleanup.
        returned = False
        try:
            await self.app(scope, receive, send)
            returned = True
        finally:
            if not token_context._budget_provider_started:
                if returned:
                    await self._disposition_if_unsent(state, token_context, "pre_provider_return")
                await self.enforcement_service.reconcile_reservation(
                    context=token_context,
                    request_id=state.get("request_id"),
                    model_id="pre-provider-rejection",
                    input_tokens=0,
                    output_tokens=0,
                    actual_cost_usd=Decimal("0"),
                )

    async def _disposition_if_unsent(self, state: dict, token_context: TokenContext, reason: str) -> None:
        if not state.get("metering_legacy_cohort_admitted"):
            return
        async with asyncio.timeout(2):
            async with self.enforcement_service._get_session() as session:
                await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                await mark_legacy_cohort_not_dispatched(
                    session,
                    tenant_id=token_context.attributed_org_id,
                    request_id=state["request_id"],
                    owner_id=token_context.user_id,
                    disposition=reason,
                )
                await session.commit()

    def _should_enforce(self, path: str) -> bool:
        return path != "/v1/messages/count_tokens" and any(path.startswith(p) for p in ENFORCED_PATHS)

    def _estimate_cost(self, scope: Scope, path: str) -> Decimal:
        """Estimate this request's cost from the ASGI scope alone (Issue #4287).

        Two inputs, both available without touching the body:

        * the model, from the URL path (``/model/{model_id}/invoke``)
        * the request size, from the ``content-length`` header

        The body is deliberately NOT read. This is pure ASGI: ``receive()`` is a
        one-shot stream, so consuming it here would starve the downstream handler
        unless it were buffered and replayed — and the mantle route
        (``/openai/v1/responses``) forwards the body byte-for-byte, which
        ``src/shared/enforced_paths.py`` documents as a hard constraint. The cost
        of that constraint is that ``max_tokens`` is invisible, so the output
        estimate falls back to the pricing module's default.

        The remaining gap is closed on the way out, not here: the reservation
        taken against this estimate is adjusted to the request's real token cost
        once the response lands (see
        ``BudgetEnforcementService.reconcile_reservation``). So this only has to
        be a reasonable pre-charge, not an accurate price.

        Falls back to the flat ``_DEFAULT_ESTIMATE_USD`` only when the size is
        unknown, which is strictly better than the pre-#4287 behavior of using it
        for everything.
        """
        content_length = self._content_length(scope)
        if content_length is None:
            return _DEFAULT_ESTIMATE_USD

        # No model in the path (e.g. /v1/chat/completions, where it lives in the
        # unreadable body) — price it with the pricing table's conservative
        # default rather than giving up on size-awareness too.
        model_id = self._extract_model_id_from_path(path) or "default"

        return self.enforcement_service.estimate_cost_from_payload_size(model_id, content_length)

    @staticmethod
    def _asserted_run_id(scope: Scope) -> str | None:
        """Read the ``X-Agent-RunId`` header out of the raw ASGI scope (#4187).

        Named "asserted" on purpose: this value is client-controlled and is not
        trusted for anything here. It is a lookup key that must survive
        verification against the run's registry row before any cap is keyed on it.
        """
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-agent-runid":
                return value.decode("utf-8", errors="replace").strip() or None
        return None

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        """Read ``content-length`` out of the raw ASGI headers.

        Same convention as ``src/admin/middleware.py``. Returns ``None`` when the
        header is absent (chunked upload) or unparseable, so the caller can fall
        back rather than pre-charge a request $0.
        """
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_model_id_from_path(self, path: str) -> str | None:
        """Extract model ID from /model/{model_id}/invoke style paths.

        Issue #4287: revived. This was dead code after Issue #234 removed inline
        usage recording, kept "for potential future per-model budget
        enforcement" — which is exactly what the model-aware pre-request estimate
        needs, and it is the only model signal available without reading the body.
        """
        if not path.startswith("/model/"):
            return None
        parts = path.split("/")
        if len(parts) < 3 or parts[1] != "model":
            return None
        suffix_parts = []
        for i in range(2, len(parts)):
            if parts[i] in ("invoke", "invoke-with-response-stream"):
                break
            suffix_parts.append(parts[i])
        return "/".join(suffix_parts) if suffix_parts else None

    async def _send_budget_exceeded(self, send: Send, result: EnforcementResult) -> None:
        """Write a 402 JSON response directly via ASGI send().

        This bypasses all Starlette response machinery and writes raw
        HTTP response start + body messages to the ASGI send callable.
        It is impossible for this to hang.
        """
        retry_after = "3600"
        error_body = {
            "error": "budget_exceeded",
            "message": result.blocked_reason or "Budget limit exceeded",
            "details": {
                "entity_type": (result.exceeded_entity_type.value if result.exceeded_entity_type else None),
                "entity_id": result.exceeded_entity_id,
                "budget_usd": (float(result.budget_amount_usd) if result.budget_amount_usd is not None else None),
                "spent_usd": (float(result.current_spend_usd) if result.current_spend_usd is not None else None),
                "enforcement_mode": (result.enforcement_mode.value if result.enforcement_mode else None),
            },
        }

        # Issue #4187: tell the worker WHICH cap stopped it. Still a 402 — a run
        # cap is a real spend limit, not an unavailable check, so it must not be
        # retried and must not be a 503. The worker keys `budget_stopped` off this
        # discriminator; without it a run stop is indistinguishable from an org
        # cap and the transcript records the wrong reason.
        if result.scope:
            error_body["details"]["scope"] = result.scope
            error_body["details"]["scope_cap_usd"] = float(result.scope_cap_usd) if result.scope_cap_usd is not None else None
        if result.current_total_usd is not None:
            error_body["details"]["current_total_usd"] = float(result.current_total_usd)
        if result.request_estimate_usd is not None:
            error_body["details"]["request_estimate_usd"] = float(result.request_estimate_usd)

        body_bytes = json.dumps(error_body).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
            (b"retry-after", retry_after.encode()),
            (b"x-budget-remaining", b"0"),
        ]
        if result.budget_amount_usd:
            headers.append((b"x-budget-limit", f"{float(result.budget_amount_usd):.2f}".encode()))

        logger.warning(f"Budget exceeded - blocking request: {error_body['details']}")

        # Use 402 Payment Required instead of 429 Too Many Requests.
        # The AWS SDK auto-retries 429 (throttling), which causes the client
        # to appear "hung" in a retry loop. 402 is not retried by the SDK
        # and semantically correct for "you ran out of budget".
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )

    async def _send_reservations_pending(self, send: Send, result: EnforcementResult) -> None:
        """Throttle unadmitted policy calls while existing provider holds settle.

        This is never proof that another call is affordable. SDK retries must
        re-enter the full authorization and atomic reservation path. Actual cap
        exhaustion remains 402; absent or unknown usage remains fail-closed.
        """
        body = json.dumps(
            {
                "error": "budget_reservations_pending",
                "message": result.blocked_reason,
                "details": {"scope": result.scope},
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"2"),
                    (b"x-amzn-errortype", b"ThrottlingException"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_check_unavailable(self, send: Send, result: EnforcementResult) -> None:
        """Write a 503 JSON response when the budget CHECK failed (Issue #4075).

        Deliberately not a 402. Under fail-closed, a DB/IAM fault would
        otherwise present platform-wide as "budget exceeded" with a null budget
        and null spend — which sends operators chasing a billing problem during
        a database incident, and corrupts any dashboard built on 402 rates.

        503 is also retryable where 402 is deliberately not (see the comment in
        _send_budget_exceeded), so clients recover on their own once the ledger
        comes back instead of needing an operator. Nothing here is known to be
        over budget, so no budget headers are emitted.
        """
        body_bytes = json.dumps(
            {
                "error": "budget_check_unavailable",
                "message": ("Budget enforcement is temporarily unable to verify spend for this request. Retry shortly."),
                "details": {"reason": result.blocked_reason},
            }
        ).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
            (b"retry-after", _CHECK_UNAVAILABLE_RETRY_AFTER),
        ]

        logger.error(f"Budget check unavailable - denying request: {result.blocked_reason}")

        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )


def create_budget_enforcement_middleware(
    enforcement_service: BudgetEnforcementService | None = None,
):
    """Factory function to create the budget enforcement middleware."""

    def middleware(app: ASGIApp) -> BudgetEnforcementMiddleware:
        return BudgetEnforcementMiddleware(app, enforcement_service=enforcement_service)

    return middleware
