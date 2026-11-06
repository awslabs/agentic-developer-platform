"""Security tool descriptors from the versioned, installed L5 contract."""

import base64
import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request

from src.agentauth.task_tool_policy import TOOL_PATTERN


@lru_cache(maxsize=1)
def l5_tool_catalogue() -> dict:
    try:
        descriptors = json.loads(files("l5_services").joinpath("contracts/v1/mcp-tools.json").read_text())
        if not isinstance(descriptors, list) or not descriptors:
            raise ValueError("empty L5 MCP contract")
        tools = []
        for descriptor in descriptors:
            name = descriptor["name"]
            permission = name.replace("___", ".", 1)
            if (
                not name.startswith("l5___")
                or not re.fullmatch(TOOL_PATTERN, permission)
                or descriptor["inputSchema"].get("additionalProperties") is not False
                or not isinstance(descriptor["outputSchema"], dict)
                or descriptor["annotations"].get("readOnlyHint") not in (True, False)
                or not isinstance(descriptor["x-http-operation"], str)
            ):
                raise ValueError("invalid L5 MCP contract")
            tools.append(
                {
                    "name": permission,
                    "description": descriptor["description"],
                    "connector": "l5",
                    "credential_kinds": [],
                    "inputSchema": descriptor["inputSchema"],
                    "outputSchema": descriptor["outputSchema"],
                    "annotations": descriptor["annotations"],
                    "http_operation": descriptor["x-http-operation"],
                }
            )
        if len({tool["name"] for tool in tools}) != len(tools) or len({tool["http_operation"] for tool in tools}) != len(tools):
            raise ValueError("duplicate L5 MCP operation")
        return {"tools": tools, "connectors": [{"name": "l5", "credential_kinds": [], "dynamic": False}]}
    except (OSError, TypeError, ValueError, KeyError, AttributeError, ModuleNotFoundError):
        raise HTTPException(503, "L5 tool contract unavailable") from None


@lru_cache(maxsize=1)
def l5_operations() -> dict:
    try:
        spec = json.loads(files("l5_services").joinpath("contracts/v1/openapi.json").read_text())
        if spec["info"]["version"] != "1.0.0":
            raise ValueError("unsupported L5 API version")
        operations = {
            action["operationId"]: (method.upper(), path, action)
            for path, methods in spec["paths"].items()
            for method, action in methods.items()
            if method in {"get", "post", "put", "patch", "delete"}
        }
        names = {tool["http_operation"] for tool in l5_tool_catalogue()["tools"]}
        if (
            len(operations)
            != len({entry["operationId"] for methods in spec["paths"].values() for entry in methods.values() if "operationId" in entry})
            or not names <= operations.keys()
        ):
            raise ValueError("L5 API and MCP contract mismatch")
        return operations
    except (OSError, TypeError, ValueError, KeyError, AttributeError, ModuleNotFoundError):
        raise HTTPException(503, "L5 API contract unavailable") from None


TRANSFORM_UNSUPPORTED_TOOLS = frozenset(
    {
        "l5___get_devops_prevention",
        "l5___configure_devops_prevention",
        "l5___list_binding_events",
        "l5___retry_run",
        "l5___list_findings",
        "l5___set_triage",
        "l5___get_result_publication_status",
        "l5___list_triage",
        "l5___compare_results",
        "l5___get_publication",
        "l5___retry_publication",
    }
)


FINOPS_UNSUPPORTED_TOOLS = frozenset(
    {
        "l5___get_devops_prevention",
        "l5___configure_devops_prevention",
        "l5___list_binding_events",
        "l5___retry_run",
        "l5___list_findings",
        "l5___set_triage",
        "l5___list_triage",
        "l5___compare_results",
        "l5___get_artifact_access",
        "l5___download_artifact",
    }
)


def l5_http_request(tool: str, arguments: dict) -> tuple:
    catalogue = {entry["name"].replace(".", "___", 1): entry for entry in l5_tool_catalogue()["tools"]}
    descriptor = catalogue.get(tool)
    if isinstance(arguments, dict) and (
        arguments.get("service_id") == "finops"
        and tool in FINOPS_UNSUPPORTED_TOOLS
        or arguments.get("service_id") == "transform"
        and tool in TRANSFORM_UNSUPPORTED_TOOLS
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    if descriptor is None or not isinstance(arguments, dict):
        raise HTTPException(422, "invalid L5 tool arguments")
    schema = descriptor["inputSchema"]
    if set(arguments) - schema["properties"].keys() or set(schema.get("required", [])) - arguments.keys():
        raise HTTPException(422, "invalid L5 tool arguments")
    for field, value in arguments.items():
        field_type = schema["properties"][field].get("type")
        if (
            field_type == "string"
            and (not isinstance(value, str) or not value)
            or field_type == "integer"
            and type(value) is not int
            or field_type == "boolean"
            and type(value) is not bool
            or field_type == "object"
            and not isinstance(value, dict)
        ):
            raise HTTPException(422, "invalid L5 tool arguments")
    operation = descriptor["http_operation"]
    if tool == "l5___download_artifact":
        operation = "getArtifactAccess"
    method, path, action = l5_operations()[operation]
    path_arguments = {parameter["name"] for parameter in action.get("parameters", []) if parameter["in"] == "path"}
    if "run_id" in path_arguments:
        path_arguments.remove("run_id")
        path_arguments.add("assessment_ref")
    for name in path_arguments:
        field = "run_id" if name == "assessment_ref" else name
        path = path.replace("{" + field + "}", quote(arguments[name], safe=""))
    query = {
        parameter["name"]: arguments[parameter["name"]]
        for parameter in action.get("parameters", [])
        if parameter["in"] == "query" and parameter["name"] in arguments
    }
    headers = {}
    if "idempotency_key" in arguments:
        headers["Idempotency-Key"] = arguments["idempotency_key"]
    if "if_match" in arguments:
        headers["If-Match"] = arguments["if_match"]
    if arguments.get("create_only") is True:
        if "If-Match" in headers:
            raise HTTPException(422, "conflicting L5 preconditions")
        headers["If-None-Match"] = "*"
    return method, path, query, headers, arguments.get("input")


async def _request_backend(request: Request):
    # Only the in-process transport supplies this object, after caller admission.
    # Never keep it on the shared application or in dependency_overrides.
    if "adp.l5_backend" not in request.scope:
        raise HTTPException(503, "L5 backend unavailable")
    return request.scope["adp.l5_backend"]


@lru_cache(maxsize=1)
def _l5_application(router_factory) -> FastAPI:
    """Compile the canonical routes once, without capturing a caller's backend."""
    application = FastAPI()
    application.include_router(router_factory(_request_backend))
    return application


def available_l5_catalogue() -> dict:
    try:
        from l5_services.api.router import create_router

        operations = l5_operations()
    except HTTPException as error:
        if error.status_code != 503:
            raise
        return {"tools": [], "connectors": []}
    except ImportError:
        return {"tools": [], "connectors": []}
    routes = _l5_application(create_router).routes
    implemented = {route.operation_id for route in routes if getattr(route, "operation_id", None)}
    supported = []
    for tool in l5_tool_catalogue()["tools"]:
        operation = tool["http_operation"]
        if operation in {"getDevopsPrevention", "configureDevopsPrevention"}:
            continue
        resolved = "getArtifactAccess" if operation == "downloadArtifact" else operation
        if resolved in implemented and resolved in operations:
            supported.append(tool)
    return {"tools": supported, "connectors": [{"name": "l5", "credential_kinds": [], "dynamic": False}] if supported else []}


async def dispatch_l5(tool: str, arguments: dict, backend) -> dict:
    from l5_services.api.router import create_router

    if tool not in {entry["name"].replace(".", "___", 1) for entry in available_l5_catalogue()["tools"]}:
        raise HTTPException(503, "L5 tool operation unavailable")
    method, path, query, headers, body = l5_http_request(tool, arguments)
    application = _l5_application(create_router)

    async def scoped_application(scope, receive, send):
        # A new ASGI scope per request keeps concurrent callers, cancellations,
        # and the artifact follow-up request on their own admitted backend.
        await application({**scope, "adp.l5_backend": backend}, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=scoped_application), base_url="http://l5.internal", trust_env=False, follow_redirects=False
    ) as client:
        response = await client.request(method, path, params=query, headers=headers, json=body)
        if tool == "l5___download_artifact" and arguments.get("service_id") == "transform" and response.status_code < 400:
            access = response.json()
            reference = access.get("artifact") if isinstance(access, dict) else None
            if (
                not isinstance(reference, dict)
                or access.get("download_path") != path + "/content"
                or not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
                or type(reference.get("bytes")) is not int
                or not 0 <= reference["bytes"] <= 256 * 1024
                or reference.get("media_type") not in {"text/x-diff", "text/plain"}
            ):
                response = httpx.Response(503, json={"detail": {"code": "artifact_unavailable"}})
            else:
                response = await client.get(path + "/content")
                if response.status_code < 400:
                    contents = response.content
                    if (
                        len(contents) != reference["bytes"]
                        or response.headers.get("X-Adp-Content-Sha256") != reference["sha256"]
                        or response.headers.get("Content-Type", "").split(";", 1)[0] != reference["media_type"]
                        or hashlib.sha256(contents).hexdigest() != reference["sha256"]
                    ):
                        response = httpx.Response(503, json={"detail": {"code": "artifact_unavailable"}})
                    else:
                        artifact_content = {**reference, "content_base64": base64.b64encode(contents).decode("ascii")}
    if response.status_code < 200:
        raise HTTPException(503, "L5 tool transport unavailable")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        detail = detail if isinstance(detail, dict) else {}
        code = detail.get("code")
        if response.status_code == 404:
            code = "not_found"
        elif response.status_code == 403:
            code = "forbidden"
        elif response.status_code == 503 and code == "source_capture_preparing":
            # Safe Product state, needed to follow the original durable receipt.
            # Keep provider errors and all arbitrary detail fields private.
            pass
        elif response.status_code >= 500:
            code = "service_unavailable"
        elif not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = "request_denied"
        payload = {
            "http_status": response.status_code,
            "error": {
                "code": code,
                "message": (
                    "Repository source preparation is in progress; check the original request receipt."
                    if response.status_code == 503 and code == "source_capture_preparing"
                    else "Resource not found"
                    if response.status_code == 404
                    else "Request failed"
                ),
                "retryable": response.status_code not in {403, 404, 410, 412} and detail.get("retryable") is True,
                "request_id": str(uuid4()),
            },
        }
    else:
        if tool == "l5___download_artifact" and arguments.get("service_id") == "transform":
            data = artifact_content
        else:
            data = response.json()
            if tool == "l5___download_artifact":
                data = data["artifact"]
        payload = {"data": data, "http_status": response.status_code}
        if "etag" in response.headers:
            payload["etag"] = response.headers["etag"]
    result = {"structuredContent": payload, "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}]}
    if response.status_code >= 400:
        result["isError"] = True
    return result
