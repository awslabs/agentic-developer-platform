"""Application composition for protected native test admission (#8078).

A deployment with no registry bound (no ``ADP_NATIVE_TEST_AUTHORITY_TABLE``) has
never designated a subject, so ordinary traffic keeps its ordinary path there. Once
a registry IS bound, its absence at runtime never becomes a negative designation
read: a configured registry that cannot be built or read fails closed. A configured
registry can still prove ordinary callers are undesignated while enrollment is
unavailable. Tool-only stateless admission additionally requires an explicit
protected runtime binding and complete registered lifecycle prices.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.native_test_configuration import COVERAGE_CONTRACT, NativeTestDeploymentVerifier
from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OPERATION_HEADER, NativeTestEnrollment, NativeTestStore
from src.agentauth.native_test_model import NativeTestModelMiddleware
from src.agentauth.test_run_admission import StrictTestBudgetEnforcementService, TestRunAdmissionService
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from src.shared.enforced_paths import ENFORCED_PATHS


@dataclass
class NativeTestRuntime:
    gateway: object
    store: NativeTestStore | None
    enrollment: NativeTestEnrollment | None = None
    admission: TestRunAdmissionService | None = None
    tool_admission: object | None = None
    tool_lifecycle: object | None = None
    # True once ADP_NATIVE_TEST_AUTHORITY_TABLE names a registry for this
    # deployment. Distinguishes "the feature is not deployed here" (no registry
    # was ever bound, so no subject was ever designated: ordinary traffic passes)
    # from "a registry is bound but cannot be read" (fail closed). Defaults to
    # True so every composition that hands in a store keeps the strict path.
    configured: bool = True

    def paid_routes(self):
        # Include every provider POST route, even an alias omitted from the shared
        # enforced prefix list. New routes cannot silently escape this boundary.
        return [
            route
            for route in self.gateway.routes
            if isinstance(route, APIRoute)
            and "POST" in route.methods
            and ("proxy" in route.tags or getattr(route.endpoint, "__module__", "") == "src.proxy.routes")
        ]

    def applies(self, scope):
        path = scope.get("path", "")
        return path.startswith(ENFORCED_PATHS) or any(route.path_regex.fullmatch(path) for route in self.paid_routes())

    def coverage(self):
        from src.agentauth.model_identity import AgentModelIdentityMiddleware
        from src.auth.approval_middleware import ApprovalEnforcementMiddleware
        from src.auth.middleware import TokenContextMiddleware
        from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
        from src.ratelimit.enforcement_middleware import RateLimitEnforcementMiddleware

        classes = [entry.cls for entry in self.gateway.user_middleware]
        required = [
            TokenContextMiddleware,
            ApprovalEnforcementMiddleware,
            NativeTestRuntimeMiddleware,
            AgentModelIdentityMiddleware,
            BudgetEnforcementMiddleware,
            RateLimitEnforcementMiddleware,
        ]
        if any(classes.count(cls) != 1 for cls in required):
            raise AdmissionRefusedError("native_test_runtime_coverage_unavailable")
        indexes = [classes.index(cls) for cls in required]
        routes = self.paid_routes()
        if indexes != sorted(indexes) or not routes or any(not route.path.startswith(ENFORCED_PATHS) for route in routes):
            raise AdmissionRefusedError("native_test_runtime_coverage_unavailable")
        return digest(
            {
                "contract": COVERAGE_CONTRACT,
                "middleware": [cls.__module__ + "." + cls.__name__ for cls in required],
                "paid_routes": sorted({"POST " + route.path for route in routes}),
                "enforced_prefixes": list(ENFORCED_PATHS),
            }
        )

    def require_enrollment(self):
        if (
            type(self.store) is not NativeTestStore
            or self.enrollment is None
            or self.admission is None
            or self.enrollment.store is not self.store
            or self.admission.store is not self.store
            or self.admission.identity_verifier is not self.enrollment
            or type(self.store._deployment_verifier) is not NativeTestDeploymentVerifier
        ):
            raise AdmissionRefusedError("native_test_runtime_unavailable")
        self.coverage()
        self.enrollment.require_enforcer()
        return self.enrollment


def compose_native_test_runtime(
    gateway, *, store, session_factory, policy_store, budget, deployment_id, clock=None, tool_binding=None, execution_client=None
):
    """One server-owned composition; no caller or HTTP dependency chooses it."""
    if type(store) is not NativeTestStore or type(budget) is not StrictTestBudgetEnforcementService:
        raise AdmissionRefusedError("native_test_runtime_unavailable")
    clock = clock or (lambda: datetime.now(UTC))
    runtime = NativeTestRuntime(gateway, store)
    store._deployment_verifier = NativeTestDeploymentVerifier(store=store, deployment_id=deployment_id, clock=clock, coverage=runtime.coverage)
    runtime.enrollment = NativeTestEnrollment(
        store=store,
        session_factory=session_factory,
        policy_store=policy_store,
        reservations=budget._get_reservations(),
        deployment_id=deployment_id,
        clock=clock,
    )
    runtime.admission = TestRunAdmissionService(store=store, budget=budget, identity_verifier=runtime.enrollment, clock=clock)
    if tool_binding is not None:
        from src.agentauth.native_test_atlassian_runtime import NativeAtlassianBinding, NativeAtlassianRuntime
        from src.agentauth.native_test_interpreter_runtime import NativeInterpreterBinding, NativeInterpreterRuntime
        from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeBinding, NativeKnowledgeRuntime
        from src.agentauth.native_test_stateless_runtime import NativeStatelessRuntime

        if execution_client is None:
            raise AdmissionRefusedError("native_test_stateless_binding_unavailable")
        runtime_type = (
            NativeInterpreterRuntime
            if type(tool_binding) is NativeInterpreterBinding
            else NativeKnowledgeRuntime
            if type(tool_binding) is NativeKnowledgeBinding
            else NativeAtlassianRuntime
            if type(tool_binding) is NativeAtlassianBinding
            else NativeStatelessRuntime
        )
        bridge = runtime_type(runtime=runtime, budget=budget, binding=tool_binding, execution_client=execution_client)
        runtime.enrollment._effect_runtime = bridge
        store._deployment_verifier.effect_coverage = bridge.coverage
    return runtime


def build_native_test_runtime(gateway):
    """Load server deployment bindings, never infer a default authority table."""
    import boto3
    from botocore.config import Config

    from src.budget.config import budget_config
    from src.internal.tools_policy import ToolsPolicyStore
    from src.shared.config import get_settings
    from src.shared.database import get_session_factory

    runtime = NativeTestRuntime(gateway, None)
    table = os.environ.get("ADP_NATIVE_TEST_AUTHORITY_TABLE", "")
    if table == "":
        # No registry is bound to this deployment, so the native test boundary is
        # not adopted here. Shipping the mandatory mount ahead of the table used to
        # refuse every paid model route platform-wide (403
        # native_test_registry_unavailable) on each environment that had not run
        # the registry Terraform yet.
        runtime.configured = False
        return runtime
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table):
        # A value IS bound but is not a table name (an ARN, a stray newline). The
        # operator meant to adopt the boundary; that registry may hold
        # designations, so this stays configured and fails closed.
        return runtime
    try:
        client = boto3.client(
            "dynamodb",
            region_name=get_settings().aws_region,
            config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
        )
        runtime.store = NativeTestStore(table_name=table, client=client)
        deployment_id = os.environ.get("ADP_NATIVE_TEST_DEPLOYMENT_ID", "")
        policy_table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", deployment_id)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", policy_table)
            or budget_config.budget_reservation_backend != "redis"
            or not get_settings().redis_url
        ):
            return runtime
        tool_binding = None
        configured_tools = os.environ.get("ADP_NATIVE_TEST_TOOL_BINDING", "")
        operator_tools = os.environ.get("ADP_OPERATOR_TOOL_BINDING", "")
        if configured_tools and operator_tools:
            return runtime
        configured_tools = operator_tools or configured_tools
        if configured_tools:
            import json

            from src.agentauth.native_test_atlassian_runtime import NativeAtlassianBinding
            from src.agentauth.native_test_interpreter_runtime import NativeInterpreterBinding
            from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeBinding
            from src.agentauth.native_test_stateless_runtime import NativeStatelessBinding

            version = json.loads(configured_tools).get("schema_version")
            if bool(operator_tools) != (version == "4.0"):
                return runtime
            binding_type = {"5.0": NativeAtlassianBinding, "4.0": NativeInterpreterBinding, "3.0": NativeKnowledgeBinding}.get(
                version, NativeStatelessBinding
            )
            tool_binding = binding_type.model_validate_json(configured_tools)
        return compose_native_test_runtime(
            gateway,
            store=runtime.store,
            session_factory=get_session_factory(),
            policy_store=ToolsPolicyStore(table_name=policy_table, client=client),
            budget=StrictTestBudgetEnforcementService(),
            deployment_id=deployment_id,
            tool_binding=tool_binding,
            execution_client=client if tool_binding is not None else None,
        )
    except Exception:
        # Registry failures remain failures. Never manufacture an empty registry.
        return runtime


class NativeTestRuntimeMiddleware:
    def __init__(self, app, *, gateway):
        self.app = app
        self.runtime = build_native_test_runtime(gateway)
        gateway.state.native_test_runtime = self.runtime
        self.model = None
        if self.runtime.enrollment is not None:
            self.model = NativeTestModelMiddleware(
                app, enrollment=self.runtime.enrollment, admission=self.runtime.admission, require_designation=True
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(("/v1/native-tests/", "/v1/operator-tools/")):
            original_send = send

            async def no_store(message):
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [(k, v) for k, v in message.get("headers", []) if k.lower() not in {b"cache-control", b"pragma"}]
                        + [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")],
                    }
                await original_send(message)

            send = no_store
        if scope["type"] != "http" or not self.runtime.applies(scope):
            await self.app(scope, receive, send)
            return
        context = scope.get("state", {}).get("token_context")
        if context is None:
            # No provider route may run before this boundary has authenticated
            # and checked the subject. Preserve the normal authentication status.
            await JSONResponse({"error": "native_test_authenticated_context_required"}, status_code=401)(scope, receive, send)
            return
        try:
            if self.runtime.store is None:
                if self.runtime.configured:
                    # A registry is bound to this deployment but could not be
                    # built: it may hold designations this request cannot see.
                    raise AdmissionRefusedError("native_test_registry_unavailable")
                # No registry was ever bound here, so no subject was ever
                # designated. Ordinary traffic keeps its ordinary path; only a
                # caller presenting native test headers is still refused below.
                designation = None
            else:
                subject = context._authenticated_subject_id or context.user_id
                designation = await run_in_threadpool(self.runtime.store.designation, subject)
                if subject != context.user_id:
                    alias_designation = await run_in_threadpool(self.runtime.store.designation, context.user_id)
                    # A service-account alias cannot erase a designated JWT subject.
                    if designation is not None or alias_designation is not None:
                        raise AdmissionRefusedError("native_test_designation_refused")
            if designation is None:
                if any(name.lower() in {CAPABILITY_HEADER.encode(), OPERATION_HEADER.encode()} for name, _ in scope.get("headers", [])):
                    raise AdmissionRefusedError("native_test_designation_refused")
            else:
                self.runtime.require_enrollment()
                if self.model is None or not scope.get("path", "").startswith(ENFORCED_PATHS):
                    raise AdmissionRefusedError("native_test_model_route_unsupported")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, AdmissionRefusedError) else "native_test_authority_unavailable"
            await JSONResponse({"error": reason, "retryable": False}, status_code=403)(scope, receive, send)
            return
        if designation is None:
            await self.app(scope, receive, send)
            return
        await self.model(scope, receive, send)


def install_native_test_runtime(gateway):
    """Register between model identity and approval/auth middleware."""
    from src.agentauth.native_operator_routes import router as operator_router
    from src.agentauth.native_test_routes import router

    gateway.include_router(router)
    gateway.include_router(operator_router)
    gateway.add_middleware(NativeTestRuntimeMiddleware, gateway=gateway)
