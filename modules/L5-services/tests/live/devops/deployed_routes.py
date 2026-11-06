"""Compare route behavior from a stage-scoped API Gateway OpenAPI export."""

import json


METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace",
                     "x-amazon-apigateway-any-method"})


def route_definitions(body, *, stage=None):
    try:
        content = body.read(1024 * 1024 + 1) if callable(getattr(body, "read", None)) else body
    finally:
        if callable(getattr(body, "close", None)):
            body.close()
    if not isinstance(content, (bytes, str)) or len(content) > 1024 * 1024:
        raise ValueError("stage export is missing or oversized")
    document = json.loads(content)
    if (not isinstance(document, dict) or not isinstance(document.get("openapi"), str)
            or not document["openapi"].startswith("3.") or not isinstance(document.get("paths"), dict)):
        raise ValueError("stage export is not a complete OpenAPI document")

    def resolve(reference):
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError("stage export contains an external or invalid reference")
        result = document
        try:
            for part in reference[2:].split("/"):
                result = result[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as error:
            raise ValueError("stage export contains an unresolved reference") from error
        return result

    routes = {}
    stage = stage or {}
    for path, item in document["paths"].items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(item, dict) or "$ref" in item:
            raise ValueError("stage export contains an invalid route path")
        for method, operation in item.items():
            if method not in METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError("stage export contains an invalid operation")
            verb = "ANY" if method == "x-amazon-apigateway-any-method" else method.upper()
            key = "$default" if operation.get("isDefaultRoute") is True else f"{verb} {path}"
            if key in routes:
                raise ValueError("stage export contains duplicate route keys")
            security = operation.get("security", document.get("security", []))
            if not isinstance(security, list) or any(not isinstance(requirement, dict) for requirement in security):
                raise ValueError("stage export contains invalid route security")
            behavior = {"operation": operation, "path": {field: value for field, value in item.items() if field not in METHODS},
                        "security": security, "servers": operation.get("servers", item.get("servers", document.get("servers", []))),
                        "stage_variables": stage.get("StageVariables", {}),
                        "stage_defaults": stage.get("DefaultRouteSettings", {}),
                        "stage_route_settings": stage.get("RouteSettings", {}).get(key, {}),
                        "extensions": {field: value for field, value in document.items() if field.startswith("x-amazon-apigateway-")}}
            references = {}
            for requirement in security:
                for name in requirement:
                    reference = "#/components/securitySchemes/" + name.replace("~", "~0").replace("/", "~1")
                    references[reference] = resolve(reference)
            pending = [behavior, *references.values()]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if "$ref" in value:
                        reference = value["$ref"]
                        resolved = resolve(reference)
                        if reference not in references:
                            references[reference] = resolved
                            pending.append(resolved)
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            routes[key] = {**behavior, "references": references}
    return routes
