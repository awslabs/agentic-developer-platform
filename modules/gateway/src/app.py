import logging
import os
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Issue #143: TokenContextMiddleware sets request.state.token_context for enforcement middleware
from src.auth.middleware import TokenContextMiddleware

# Issue #131: Import enforcement middleware
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.ratelimit.enforcement_middleware import RateLimitEnforcementMiddleware
from src.shared.config import get_settings
from src.shared.exceptions import BedrockGatewayError
from src.shared.logging import configure_logging
from src.shared.middleware.logging_middleware import LoggingMiddleware

# Issue #144: Import tracing setup
from src.shared.tracing import setup_tracing, shutdown_tracing

logger = logging.getLogger("bedrockgateway")

UNIT_MODULES = [
    "src.auth.routes",
    "src.proxy.routes",
    "src.admin.routes",
    "src.admin.identity.router",
    "src.pool.routes",
    "src.budget.routes",
    "src.ratelimit.routes",
    "src.usage.routes",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("BedrockGateway starting up", extra={"event": "startup"})

    # Ensure database tables exist
    try:
        # Import all models so Base.metadata knows about them
        import src.admin.models  # noqa: F401
        import src.shared.models.budget  # noqa: F401
        import src.shared.models.organization  # noqa: F401
        import src.shared.models.usage  # noqa: F401
        from src.shared.database import get_engine
        from src.shared.models.base import Base

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified/created")
    except Exception as e:
        logger.warning(f"Could not auto-create tables (may already exist): {e}")

    # Initialize proxy service with single-account Bedrock pool
    try:
        from src.pool.simple_pool import SimplePoolService
        from src.proxy.routes import set_proxy_service
        from src.proxy.service import ProxyService

        pool = SimplePoolService()
        proxy = ProxyService(pool_service=pool)
        set_proxy_service(proxy)
        logger.info("Proxy service initialized with single-account pool")
    except Exception as e:
        logger.error(f"Failed to initialize proxy service: {e}")

    yield

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
    # Read allowed origins from environment variable, fallback to CloudFront and localhost
    cors_origins_str = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://dp7n42m5j4pl6.cloudfront.net,http://localhost:5173",
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

    # Issue #131: Add enforcement middleware
    # Middleware order is important - they execute in reverse order of addition:
    # Request → LoggingMiddleware → BudgetEnforcementMiddleware → RateLimitEnforcementMiddleware → Route Handler
    # So we add rate limit first (executed second) then budget (executed first after logging)
    #
    # Note: Auth middleware sets request.state.token_context which enforcement middleware depends on
    # The enforcement middleware checks for token_context and skips if not present (auth handles 401)
    if os.environ.get("RATELIMIT_ENFORCEMENT_ENABLED", "true").lower() == "true":
        app.add_middleware(RateLimitEnforcementMiddleware)
        logger.info("Rate limit enforcement middleware enabled")

    if os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() == "true":
        app.add_middleware(BudgetEnforcementMiddleware)
        logger.info("Budget enforcement middleware enabled")

    # TokenContextMiddleware must be added LAST so it runs FIRST in the request chain.
    # It extracts the Cognito JWT and sets request.state.token_context before
    # budget and rate-limit middleware access it.
    app.add_middleware(TokenContextMiddleware)
    logger.info("Token context middleware enabled")

    # Error handler for BedrockGatewayError
    @app.exception_handler(BedrockGatewayError)
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
    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    # Auto-discover and register routers from unit modules
    for module_path in UNIT_MODULES:
        try:
            module = import_module(module_path)
            if hasattr(module, "router"):
                app.include_router(module.router)
                logger.info("Registered router", extra={"module_path": module_path})
        except ImportError as e:
            logger.debug(
                "Module not available, skipping",
                extra={"module_path": module_path, "error": str(e)},
            )

    return app
