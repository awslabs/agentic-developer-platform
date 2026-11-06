"""Foundation probes only; internal routes deny before reading caller payloads.

The next connection-lifecycle story supplies authenticated observation and
admission consumers. There is no implicit enablement, vendor fallback, generic
proxy or credential lookup in this foundation.
"""

import os
import re

from adp_tools.adapter_contracts import ObserveRequest, PolicyObserveRequest, contract_metadata
from adp_tools.jira_system_contract import JiraSystemRequest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from tool_adapters.atlassian_route import ReadbackRequest, atlassian_mcp, reconcile_readback
from tool_adapters.config import AdapterConfiguration, ConfigurationError, load_configuration
from tool_adapters.legacy_route import legacy_mcp
from tool_adapters.observation import observe
from tool_adapters.policy_observation import observe_policy


def create_app(configuration: AdapterConfiguration | None = None, *, source_revision: str | None = None) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Validation responses must never echo assertions or Forge token headers
        # forwarded inside the private signed request.
        return JSONResponse({"error": "invalid_adapter_request"}, status_code=422)

    metadata = {"service": "adp-tool-adapters", **contract_metadata()}
    valid_source = isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None
    ready = configuration is not None and valid_source and configuration.release.source_revision == source_revision

    @app.get("/healthz", include_in_schema=False)
    async def health():
        return JSONResponse({"status": "ok", **metadata}, headers={"Cache-Control": "no-store"})

    @app.get("/contract", include_in_schema=False)
    async def contract():
        # Operational metadata only: no provider catalogue, configuration,
        # owner/site references, assertions, or dynamic capability claim.
        return JSONResponse(metadata, headers={"Cache-Control": "no-store"})

    @app.get("/readyz", include_in_schema=False)
    async def readiness():
        return JSONResponse(
            {
                "status": "ready" if ready else "unavailable",
                "mode": configuration.mode if ready else "foundation",
                "execution_enabled": False,
                **metadata,
            },
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    if ready and configuration.mode == "observation":

        @app.post("/internal/v1/tool-adapters/observe")
        async def observe_route(body: ObserveRequest | PolicyObserveRequest | JiraSystemRequest, request: Request):
            if isinstance(body, JiraSystemRequest):
                from tool_adapters.jira_system import system_read

                if not configuration.observation.system_context_enabled:
                    return JSONResponse({"error": "jira_context_unavailable"}, status_code=503)
                return JSONResponse(
                    await system_read(body, request, configuration), headers={"Cache-Control": "no-store"}
                )
            if isinstance(body, PolicyObserveRequest):
                result = await observe_policy(body, request, configuration)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            result = await observe(body, request, configuration)
            return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

        if (
            configuration.observation.legacy_identity_workload is not None
            and os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") == "true"
        ):
            for connector in ("github", "slack", "jira"):

                def register_legacy(name):
                    @app.post(f"/internal/v1/tools/{name}/mcp")
                    async def legacy_route(request: Request):
                        return await legacy_mcp(request, configuration, name)

                register_legacy(connector)

        if configuration.observation.read_schemas is not None:

            @app.post("/internal/v1/tools/atlassian/mcp")
            async def reads_route(request: Request):
                return await atlassian_mcp(request, configuration)

            if configuration.observation.write_enabled and configuration.observation.reconciler_role_arn:

                @app.post("/internal/v1/tools/atlassian/reconcile")
                async def reconciliation_route(body: ReadbackRequest, request: Request):
                    return await reconcile_readback(request, body, configuration)

    @app.api_route("/internal/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def refused(path: str):
        return JSONResponse(
            {"error": "adapter_authority_required"}, status_code=403, headers={"Cache-Control": "no-store"}
        )

    return app


def application() -> FastAPI:
    try:
        configuration = load_configuration(os.environ.get("ADP_TOOL_ADAPTER_CONFIG"))
    except ConfigurationError:
        # Keep health available for diagnostics, but never report readiness for
        # malformed/stale protected configuration or echo its contents.
        configuration = None
    return create_app(configuration, source_revision=os.environ.get("ADP_ADAPTER_SOURCE_REVISION"))
