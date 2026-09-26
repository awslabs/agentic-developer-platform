import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from importlib import import_module

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.admin.middleware import create_request_logging_middleware
from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.auth.approval_middleware import ApprovalEnforcementMiddleware  # Issue #4144: gate spend on approval
from src.auth.dependencies import require_admin  # Issue #1424: for agent-context indexing admin router guard
from src.auth.middleware import TokenContextMiddleware
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.ratelimit.enforcement_middleware import RateLimitEnforcementMiddleware
from src.shared.config import get_settings
from src.shared.database_agent_context import get_agent_context_db  # Issue #2182: KL registry in agent_context DB
from src.shared.exceptions import BedrockGatewayError
from src.shared.logging import configure_logging
from src.shared.middleware.logging_middleware import LoggingMiddleware
from src.shared.middleware.request_identity import RequestIdentityMiddleware
from src.shared.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger("bedrockgateway")

UNIT_MODULES = [
    "src.domain_proxy.superplane",
    "src.auth.routes",
    "src.auth.session_admin",
    "src.auth.cli_login",  # Web CLI login: device-authorization flow (no copy-paste)
    "src.auth.cli_native_login",  # Native Cognito bootstrap and MFA for CLI administrators
    "src.auth.vault_routes",  # Issue #135: vault credential + identity CRUD
    "src.auth.vault_authority_routes",  # Workspace delegation and independent validation
    "src.auth.aws_connect_routes",  # Issue #562: self-serve AWS account connect
    "src.internal.routes",  # Issue #446: internal service-to-service endpoints
    "src.internal.credential_routes",  # Issue #136: credential delivery paths
    # #5528: vault evidence + operation-bound delivery for Superplane. A separate
    # module from credential_routes even though it shares the /internal/v1 prefix,
    # because delivery here additionally verifies the individual run credential and
    # pod (the shared worker IRSA identity cannot name one executor), and that
    # authorization deserves review on its own terms rather than by inheritance.
    "src.internal.vault_evidence_routes",
    "src.internal.controller_execution_routes",
    "src.internal.assume_role_routes",  # Issue #481: aws_role STS assume delivery path
    "src.internal.task_credentials",  # Existing customer trust principal, restricted task session
    "src.internal.provenance_routes",  # Issue #785: action provenance write endpoint
    "src.internal.status_callback_routes",  # Issue #2049: ingestion worker status callback
    "src.internal.admin_routes",  # Issue #3462: admin read endpoints for adversarial E2E
    "src.internal.persona_model_probe_routes",  # PMM-03: bounded harness probe worker API
    "src.internal.persona_model_selection",
    "src.agentauth.routes",  # #5028: IAM transport and verified pod-bound agent identity
    "src.agentauth.arc_model",
    "src.agentauth.model_policy_keys",
    "src.agentauth.external_roots",  # Registered ingress creates protected roots before publication.
    "src.agentauth.chat_model",  # Verified chat pod, fresh signed SDK decision.
    "src.agentauth.work_routes",  # Producer signature and protected invocation; no worker-selected ownership.
    "src.agentauth.task_admission_routes",  # Task ingress proof, caller identity and durable admission.
    # #5028 (AC4): the worker's own status/registration writes, moved off the
    # unconditioned DynamoDBWebhookEventsUpdate permission and onto a service that
    # derives the row key from the protected execution record.
    "src.agentauth.registration_routes",
    "src.agentauth.run_services",
    "src.agentauth.knowledge_service",
    "src.agentauth.task_routes",
    # #5796: the publication protocol's claim/settle/sweep adapters. Separate
    # from task_routes because these serve the platform's own publisher and
    # scheduled reconciler (STS producer proof), not a TokenReview-bound pod, and
    # recovery holds a distinct role allowlist from dispatch.
    "src.agentauth.task_dispatch_routes",
    "src.agentauth.task_runtime_routes",
    "src.agentauth.task_tool_routes",  # Generic Task tool authorization, no domain execution.
    # #5799: Task API v1 read, streaming, artifact and host-reporting surface.
    # Mounted always and gated inside by ADP_TASK_API_READ_ENABLED /
    # ADP_TASK_API_WORKER_ENABLED (both default false, design section 11), which is
    # the repo's mount-always/503-when-disabled pattern: conditional registration
    # would make a disabled surface return 404, indistinguishable from a routing
    # mistake, and would leave the routes unimported and so unexercised by the
    # app's own startup.
    "src.tasks.routes",
    "src.tasks.artifacts",
    "src.tasks.internal_artifacts",
    "src.tasks.report_routes",
    "src.tasks.command_routes",
    "src.agentauth.artifact_service",
    "src.agentauth.cyber_jobs",
    "src.orchestration.shared_review",
    # #5223: mediated GitHub operations. A separate module from
    # registration_routes even though it shares the /self prefix, because this is
    # the only route on that prefix that reaches an external provider and holds an
    # installation credential while doing it. Keeping it its own module means the
    # authorization it performs is reviewed on its own terms rather than inherited
    # from a router whose other routes only touch our own records.
    "src.agentauth.github_operation_routes",
    # #5301: the delivering run binds its own implementation PR to its story,
    # authenticated by the run credential rather than a self-declared run header.
    "src.agentauth.pr_binding_routes",
    "src.agentauth.run_report_routes",
    "src.agentauth.service_authority",  # Human-only standing service delegation; never on the internal plane.
    "src.proxy.routes",
    "src.admin.routes",
    "src.admin.identity.router",
    "src.admin.identity.recovery_routes",  # Native Cognito recovery: no legacy /api prefix.
    "src.admin.connections.routes",  # Issue #465: GitHub App install + connections
    # Issue #4842: platform-admin attach/detach of an ORG's GitHub connection.
    # Separate from src.admin.connections.routes on purpose — that router is the
    # self-serve path (a user installs the App and it binds to their own tenant);
    # this one lets a platform admin bind a named installation to any named org,
    # which is a different actor with different authorization.
    "src.admin.org_connections.routes",
    "src.admin.tenants.routes",  # Issue #2954: Multi-org-to-tenant linking (rule 3)
    "src.admin.onboarding.handler",  # Issue #538: Self-serve onboarding flow
    "src.pool.routes",
    "src.budget.routes",
    "src.budget.enforcement_routes",
    # Issue #4397: own-scope budget read API (GET /me/budget). A SEPARATE module
    # from src.budget.routes on purpose — that router takes entity_type/entity_id
    # unscoped from the request (open IDOR #4384), so this one takes identity from
    # the token only and accepts no scope parameter at all.
    "src.budget.me_routes",
    # Issue #4401: managed-scope (operator) budget read API
    # (GET /budget/scope/{entity_type}/{entity_id}). A THIRD budget router, and
    # again separate on purpose: this is the only one that accepts a target other
    # than the caller, so it is the only one carrying cross-tenant risk. Every
    # route on it is explicitly permission-gated and scope-verified server-side
    # from tenant_memberships. Deliberately NOT added to src.budget.routes, whose
    # unscoped entity_type/entity_id pattern is open IDOR #4384 (NFR-1).
    "src.budget.managed_scope_routes",
    # Issue #4627: mis-partitioned person-cap report
    # (GET /budget/reports/mis-partitioned-caps). A FOURTH budget router, and
    # separate for a mechanical reason on top of the same IDOR-hygiene one: it
    # cannot be a route on managed_scope_routes because that router's
    # /{entity_type}/{entity_id} pattern would shadow a literal sibling path and
    # answer a valid report request with a 422. Read-only and detection-only —
    # design note 4620-cross-org-person-budgets.md §8.2 rules out mutation.
    "src.budget.report_routes",
    # Issue #4629 (#4620 · C3): person-level cap authoring
    # (PUT /me/budget/person-cap, PUT /budget/person-cap/{anchor}). A FOURTH
    # budget router, separate again for a different reason from the other three:
    # it is the only one that WRITES, and what it writes is partition-free, so its
    # authoring rule is unlike theirs — the person themselves or a platform admin
    # may author, an org admin may NOT (§4.2; an org admin authoring a cap that
    # spans tenants they cannot see is the authority inversion the #4620 ruling
    # forbids). Not in me_routes, which documents itself read-only; not in
    # src.budget.routes, whose unscoped entity_type/entity_id pattern is open
    # IDOR #4384.
    "src.budget.person_cap_routes",
    "src.budget.overview_routes",
    # Issue #4745 (#4692 · R4): the Bedrock account-routing authoring API — the
    # platform-admin surface over R2's mapping/destination tables. Every route is
    # `require_platform_admin`, org admins included (design ruling 4, §6.5): a mapping
    # decides whose AWS account is BILLED for a principal's model calls, and a
    # user-rung mapping names a person who may work in several tenants. Registered
    # here, not under src.admin.routes, because that module's authoring rules are
    # partition-scoped and this one's deliberately are not.
    "src.admin.bedrock_routing.routes",
    # Issue #4746 (#4692 · R5): the SELF-service half of the same tables — a person
    # pointing their own Bedrock traffic at one of their own connected AWS accounts
    # (`/me/bedrock-routing/selection`, §6.4). A separate module from the router above
    # precisely because that one is platform-admin-only on every route, asserted against
    # its own source; these routes are deliberately callable by an ordinary member, and
    # the authz is the SHAPE of the path — no target parameter at any position, anchor
    # derived from the token — not a check inside the handler. Writes the same user-rung
    # row, and refuses to overwrite one a platform admin authored (§1.4 "admin wins",
    # which with one row per scope can only be enforced at write time).
    "src.admin.bedrock_routing.self_routes",
    # Issue #5419 (PMM-02): persona-model preference self-service and administration.
    # Two separate routers for the same reason bedrock_routing splits them: the self
    # surface takes no target parameter at any position (the authz IS the shape),
    # while the administration surface takes a canonical service principal ID and
    # checks ORG_UPDATE as its first statement.
    "src.admin.persona_models.self_routes",
    "src.admin.persona_models.routes",
    "src.admin.persona_models.human_task_routes",
    # Issue #5425 (PMM-07): the versioned runtime-posture mutation and its audited
    # operational rollback. A THIRD module because its gate is strictly stronger
    # than either router above: the policy-settings row carries no TenantMixin, so
    # the posture applies across every tenant and a tenant admin holding
    # ORG_UPDATE must not be able to flip enforcement platform-wide. Every route
    # here is platform-admin-only, asserted against its own source by
    # tests/admin/persona_models/test_posture_authz.py.
    "src.admin.persona_models.posture_routes",
    "src.admin.persona_models.default_routes",
    # Issue #5420 (PMM-03): read-only persona/model catalogue on the same
    # /me/persona-models namespace. Kept in a separate module so catalogue
    # policy/evidence logic does not broaden either PMM-02 write surface.
    "src.admin.persona_models.catalogue_routes",
    "src.ratelimit.routes",
    "src.usage.routes",
    "src.activity.routes",  # Issue #1456: Agent Activity read API (/me + /admin)
    "src.knowledge.routes",  # Issue #2045: Knowledge-assets registry CRUD
    "src.knowledge.github_repos",  # Issue #2045: GitHub repo picker
    "src.features.routes",  # Issue #3566: Feature-flag endpoint
    "src.gitlab.routes",
    "src.auth.gitlab_sso",  # Issue #3775: GitLab SSO JWT minting + JWKS
    "src.cli_download.routes",  # Issue #4146: /setup page CLI helper-script download
    # Issue #5621 (CLI-08): own-scope CLI capability discovery. Read-only, and
    # deliberately separate from cli_download.routes — that router is public and
    # unauthenticated by design, whereas this one is authenticated and
    # tenant-scoped. Sharing a module would put a public route and a per-caller
    # route behind one review.
    "src.cli_capabilities.routes",
    # Issue #4200: orchestration plan amendment. OPERATOR plane (Cognito + the
    # PLAN_APPROVE permission), deliberately NOT src.internal.* — agent pods can
    # call any internal route with any method, so promotion state must never be
    # registered there. Guarded by tests/orchestration/test_internal_plane_guard.py.
    "src.orchestration.routes",
    # Issue #4213: gate approval / rejection and loop-resume controls. Same
    # operator plane and same reasoning as the router above — these WRITE
    # promotion state, so registering them under src.internal.* would hand agent
    # pods the ability to approve their own gates. Guarded by
    # tests/orchestration/test_internal_plane_guard.py.
    "src.orchestration.controls",
    # Issue #4528: draft plan registration. Same operator plane as the two routers
    # above (Cognito / the SigV4 agent path via get_current_user), but gated on
    # PLAN_DRAFT rather than PLAN_APPROVE — its caller is an authoring agent, which
    # must never hold approval authority. A SEPARATE module on purpose: routes.py's
    # guarantee is "nothing here is reachable below approval authority", and a
    # weaker-permission route cannot live behind that guarantee. What it writes is
    # inert by construction (src/orchestration/registration.py). Guarded by
    # tests/orchestration/test_internal_plane_guard.py.
    "src.orchestration.draft_routes",
    # Issue #5331: the intake-conversation surface a terminal client plans through.
    # Operator plane like the three routers above; PLAN_DRAFT to speak in a
    # conversation and USAGE_READ to read one back. NOT PLAN_APPROVE, and nothing
    # here can accept a plan, stamp an execution policy or move a gate — a planning
    # conversation's output is a draft that a human must still act on, which is the
    # separation this EPIC protects. A separate module for the same reason
    # draft_routes.py is: routes.py's guarantee is "nothing here is reachable below
    # approval authority". Guarded by tests/orchestration/test_internal_plane_guard.py.
    "src.orchestration.intake_routes",
    "src.orchestration.chat_history",
    "src.orchestration.chat_tasks",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("BedrockGateway starting up", extra={"event": "startup"})

    # Issue #2918: Only run create_all when BG_DB_AUTO_CREATE=true (local dev).
    # In deployed environments, alembic migrations are the single source of truth.
    # create_all shadows alembic (no alembic_version row, missing constraints/indexes)
    # and causes DuplicateTableError on fresh-account deploys.
    settings = get_settings()
    await app.state.ratelimit_service.initialize()
    if settings.db_auto_create:
        try:
            # Import all models so Base.metadata knows about them
            import src.admin.models  # noqa: F401
            import src.shared.models.audit  # noqa: F401  # Issue #446
            import src.shared.models.bedrock_routing  # noqa: F401  # Issue #4743
            import src.shared.models.budget  # noqa: F401
            import src.shared.models.organization  # noqa: F401
            import src.shared.models.persona_model_catalogue  # noqa: F401  # Issue #5420
            import src.shared.models.persona_models  # noqa: F401  # Issue #5419
            import src.shared.models.usage  # noqa: F401
            import src.shared.models.vault  # noqa: F401  # Issue #135
            from src.shared.database import get_engine
            from src.shared.models.base import Base

            async with get_engine().begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables verified/created (BG_DB_AUTO_CREATE=true)")
        except Exception as e:
            logger.warning(f"Could not auto-create tables (may already exist): {e}")
    else:
        logger.info("Skipping create_all (BG_DB_AUTO_CREATE=false); alembic manages schema")

    # Initialize proxy service with single-account Bedrock pool
    try:
        from src.pool.simple_pool import SimplePoolService
        from src.proxy.model_resolver import production_model_resolver
        from src.proxy.routes import set_model_resolver, set_proxy_service
        from src.proxy.service import ProxyService

        pool = SimplePoolService()
        model_resolver = production_model_resolver(settings)
        proxy = ProxyService(pool_service=pool, model_resolver=model_resolver)
        set_model_resolver(model_resolver)
        set_proxy_service(proxy)
        logger.info("Proxy service initialized with single-account pool and deployed model policy")
    except Exception as e:
        logger.error(f"Failed to initialize proxy service: {e}")

    # Issue #2709: Initialize the bedrock-mantle passthrough service for OpenAI
    # Responses-API traffic (Codex metering/governance). Only wired when enabled;
    # otherwise the route returns 503 (get_mantle_service raises).
    try:
        settings = get_settings()
        if settings.mantle_enabled:
            from src.proxy.mantle_auth import make_mantle_auth
            from src.proxy.mantle_service import MantlePassthroughService
            from src.proxy.routes import set_mantle_service

            base_url = settings.mantle_base_url.replace("{region}", settings.mantle_region)
            auth = make_mantle_auth(settings.mantle_region)
            set_mantle_service(
                MantlePassthroughService(
                    auth,
                    base_url,
                    inference_profile_prefix=settings.mantle_inference_profile_prefix,
                    on_demand_models=settings.mantle_on_demand_models,
                    stream_read_timeout=settings.mantle_stream_read_timeout_seconds,
                )
            )
            logger.info("Mantle passthrough service initialized", extra={"auth_mode": "sigv4"})
        else:
            logger.info("Mantle passthrough disabled (BG_MANTLE_ENABLED not set)")
    except Exception as e:
        logger.error(f"Failed to initialize mantle passthrough service: {e}")

    from src.budget.pricing_decisions import maintain_pricing_cache, refresh_pricing_cache

    # The shared refresh boundary caps reads at five seconds and records failure.
    await refresh_pricing_cache()
    pricing_task = asyncio.create_task(maintain_pricing_cache(), name="pricing_cache_refresh")
    from src.orchestration.work_admission import maintain_work_claims

    claims_task = asyncio.create_task(maintain_work_claims(), name="work_claim_cleanup")
    try:
        yield
    finally:
        pricing_task.cancel()
        with suppress(asyncio.CancelledError):
            await pricing_task
        claims_task.cancel()
        with suppress(asyncio.CancelledError):
            await claims_task

    await app.state.ratelimit_service.close()

    # Issue #144: Shutdown tracing on app shutdown
    shutdown_tracing()

    logger.info("BedrockGateway shutting down", extra={"event": "shutdown"})


def create_app() -> FastAPI:
    # Configure structured logging inside create_app (not at module level)
    # to avoid interfering with pytest-asyncio event loops
    json_output = os.environ.get("BG_LOG_FORMAT", "json").lower() == "json"
    log_level = os.environ.get("BG_LOG_LEVEL", "INFO")
    configure_logging(level=log_level, json_output=json_output)

    get_settings()

    app = FastAPI(
        title="BedrockGateway",
        description="Multi-tenant SaaS proxy for Amazon Bedrock",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Issue #144: Initialize OpenTelemetry/X-Ray tracing (Phase 2)
    # Must be called before middleware registration so FastAPI is instrumented first
    settings = get_settings()
    if settings.otel_enabled:
        # Set env vars for the tracing module
        os.environ.setdefault("OTEL_ENABLED", "true")
        os.environ.setdefault("OTEL_SERVICE_NAME", settings.otel_service_name)
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_exporter_endpoint)
        tracing_ok = setup_tracing(app)
        if tracing_ok:
            logger.info("OpenTelemetry/X-Ray tracing enabled")
        else:
            logger.warning("OpenTelemetry/X-Ray tracing failed to initialize")

    # Configure CORS middleware
    # Read allowed origins from environment variable (set via ConfigMap from SSM in production)
    cors_origins_str = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173",
    )
    cors_origins = [origin.strip() for origin in cors_origins_str.split(",") if origin.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add logging middleware (should be first to capture all requests)
    app.add_middleware(LoggingMiddleware)

    # Issue #992: Add request logging middleware to record requests in request_logs table
    # for the admin dashboard. Runs after LoggingMiddleware (i.e., sees the response status).
    from src.ratelimit.service import RateLimitService

    app.state.ratelimit_service = RateLimitService()
    app.add_middleware(create_request_logging_middleware())

    # Issue #131: Add enforcement middleware
    # Middleware order is important - they execute in reverse order of addition:
    # Request → Identity → Auth → Approval → ModelIdentity → Budget → RateLimit → Logging → Handler
    # Registration is reversed: identity is added last to wrap admission.
    #
    # Note: Auth middleware sets request.state.token_context which enforcement middleware depends on
    # The enforcement middleware checks for token_context and skips if not present (auth handles 401)
    if os.environ.get("RATELIMIT_ENFORCEMENT_ENABLED", "true").lower() == "true":
        app.add_middleware(RateLimitEnforcementMiddleware, ratelimit_service=app.state.ratelimit_service)
        logger.info("Rate limit enforcement middleware enabled")

    app.add_middleware(BudgetEnforcementMiddleware)
    logger.info("Budget middleware enabled with live enforcement controls")

    # Execute after token-context authentication and before budget resolution.
    # Protected workers cannot fall back to a caller-selected run capability.
    app.add_middleware(AgentModelIdentityMiddleware)

    # Issue #4144: approval (org-assignment) enforcement. Added AFTER budget/rate-limit
    # and BEFORE TokenContextMiddleware, so at runtime it executes after token_context is
    # populated and before the budget/ledger read — rejecting an un-approved caller
    # without spending a DB round-trip on a request that is going to be denied anyway.
    # Registered unconditionally: the BG_ENFORCE_ORG_ASSIGNMENT flag (default True as
    # of #5666 / A11) short-circuits inside the middleware, so it stays flippable by
    # env change + pod recycle with no code-path difference.
    app.add_middleware(ApprovalEnforcementMiddleware)
    logger.info("Approval enforcement middleware enabled")

    # TokenContextMiddleware must be added LAST so it runs FIRST in the request chain.
    # It extracts the Cognito JWT and sets request.state.token_context before
    # budget and rate-limit middleware access it.
    app.add_middleware(TokenContextMiddleware)
    logger.info("Token context middleware enabled")
    # Outermost: every admission/settlement path sees one server-owned identity.
    app.add_middleware(RequestIdentityMiddleware)

    # Error handler for BedrockGatewayError
    @app.exception_handler(BedrockGatewayError)  # nosemgrep: useless-inner-function
    # nosemgrep: useless-inner-function — registered via @app.exception_handler decorator
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        logger.warning(
            "BedrockGatewayError occurred",
            extra={
                "error": exc.error,
                "error_message": exc.message,
                "status_code": exc.status_code,
                "path": request.url.path,
            },
        )
        content = {
            "error": exc.error,
            "message": exc.message,
        }
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    # Health endpoints
    @app.get("/health")  # nosemgrep: useless-inner-function
    async def health():  # nosemgrep: useless-inner-function — registered via @app.get decorator
        return {"status": "healthy"}

    @app.get("/ready")  # nosemgrep: useless-inner-function
    async def ready():  # nosemgrep: useless-inner-function — registered via @app.get decorator
        return {"status": "ready"}

    # Auto-discover and register routers from unit modules.
    # Issue #4145: a module may also expose `well_known_router` for prefix-free
    # /.well-known/* discovery documents that must not inherit the module prefix.
    for module_path in UNIT_MODULES:
        try:
            module = import_module(module_path)
            if hasattr(module, "router"):
                app.include_router(module.router)
                logger.info("Registered router", extra={"module_path": module_path})
            if hasattr(module, "well_known_router"):
                app.include_router(module.well_known_router)
                logger.info("Registered well-known router", extra={"module_path": module_path})
        except ImportError as e:
            logger.debug(
                "Module not available, skipping",
                extra={"module_path": module_path, "error": str(e)},
            )

    # Issue #2047: Startup warning when knowledge dispatch is enabled but queue is absent.
    if os.environ.get("AGENT_CONTEXT_ENABLED", "").lower() == "true":
        if not os.environ.get("INGESTION_QUEUE_URL"):
            logger.warning(
                "AGENT_CONTEXT_ENABLED=true but INGESTION_QUEUE_URL is not set. "
                "Knowledge routes will register but dispatch will return 503 until "
                "the queue URL is configured."
            )

    # Issue #1424: Conditionally mount agent-context indexing admin router.
    # Routes are DEFINED in agent-context, MOUNTED here behind AGENT_CONTEXT_ENABLED.
    # Inherits Cognito JWT auth + admin-role guard from the gateway middleware stack.
    if os.environ.get("AGENT_CONTEXT_ENABLED", "").lower() == "true":
        try:
            from agent_context.api.indexing_router import (
                get_indexing_db,
            )
            from agent_context.api.indexing_router import (
                router as indexing_router,
            )

            # Override the router's DB dependency with the agent_context session factory (Issue #2182)
            app.dependency_overrides[get_indexing_db] = get_agent_context_db
            app.include_router(
                indexing_router,
                dependencies=[Depends(require_admin)],
            )
            logger.info("Agent-context indexing admin router mounted")
        except ImportError as e:
            logger.debug(
                "Agent-context package not available, indexing admin routes skipped",
                extra={"error": str(e)},
            )

        # Issue #2045: knowledge-assets and github-repos routers are now native
        # to the gateway (src.knowledge.routes, src.knowledge.github_repos) and
        # registered via UNIT_MODULES above. No cross-module import needed.

    return app
