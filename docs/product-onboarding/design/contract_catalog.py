"""Generate the reviewable OpenAPI document, not a server implementation."""

import json
import re
from pathlib import Path


HOME = Path(__file__).resolve().parent
SCOPE = "/products/{product_id}/projects/{project_id}"
SESSION = SCOPE + "/onboarding-sessions/{session_id}"
OPERATIONS = [
    ("GET", SCOPE + "/people", "getProductPeople", None, "ProductPeople", 200, None, False),
    ("GET", "/products", "listProducts", None, "ProductPage", 200, "product list", False),
    ("POST", "/products", "createProduct", "CreateProduct", "Profile", 201, "product create", False),
    ("GET", "/projects", "listProjects", None, "ProjectPage", 200, "product project list", False),
    ("POST", "/products/{product_id}/projects", "bindProject", "ProjectSelection", "Profile", 201, "product project bind", True),
    ("GET", SCOPE, "getProfile", None, "Profile", 200, "product show", False),
    ("PATCH", SCOPE, "updateProduct", "ProductPatch", "Profile", 200, "product update", True),
    ("PATCH", SCOPE + "/configuration", "updateProject", "ProjectPatch", "Profile", 200, "product project update", True),
    ("DELETE", SCOPE, "unbindProject", None, "Profile", 200, "product project unbind", True),
    ("POST", SCOPE + "/lifecycle", "changeLifecycle", "LifecycleRequest", "Profile", 200, "product lifecycle", True),
    ("GET", SCOPE + "/sources", "listSources", None, "SourcePage", 200, "product sources", False),
    ("GET", SCOPE + "/bindings", "listBindings", None, "BindingPage", 200, "product binding list", False),
    ("POST", SCOPE + "/bindings", "createBinding", "BindingInput", "Binding", 201, "product binding add", True),
    ("DELETE", SCOPE + "/bindings/{binding_id}", "unlinkBinding", None, "Binding", 200, "product binding remove", True),
    ("POST", SCOPE + "/bindings/{binding_id}/verify", "verifyBinding", None, "Operation", 202, "product binding verify", True),
    ("POST", SCOPE + "/onboarding-sessions", "startOnboarding", "SessionStart", "Session", 201, "onboard start", True),
    ("GET", SESSION, "getOnboarding", None, "Session", 200, "onboard resume", False),
    ("PUT", SESSION + "/steps/{step_id}", "updateStep", "StepUpdate", "Session", 200, "onboard step", True),
    ("POST", SESSION + "/discoveries", "discoverSources", "DiscoveryRequest", "Operation", 202, "onboard discover", True),
    ("POST", SESSION + "/guidance", "guideOnboarding", "GuidanceRequest", "Guidance", 200, "onboard guide", True),
    ("POST", SESSION + "/handoffs", "startHandoff", "HandoffRequest", "Operation", 202, "onboard handoff", True),
    ("GET", SCOPE + "/operations/{operation_id}", "getOperation", None, "Operation", 200, "onboard operation show", False),
    ("POST", SCOPE + "/operations/{operation_id}/reconcile", "reconcileOperation", None, "Operation", 202, "onboard operation reconcile", True),
    ("POST", SCOPE + "/operations/{operation_id}/cancel", "cancelOperation", None, "Operation", 200, "onboard operation cancel", True),
    ("GET", SCOPE + "/changes", "listChanges", None, "ChangePage", 200, "product history", False),
    ("GET", SCOPE + "/revisions/{revision}", "getRevision", None, "Profile", 200, "product show --revision", False),
    ("POST", "/product-callbacks/{operation_id}", "applyCallback", "Callback", "Ack", 200, "internal authenticated producer only", False),
]


def schema(name):
    return {"$ref": f"schemas.json#/$defs/{name}"}


def generate():
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Proposed Product Profile and Onboarding", "version": "1.0.0", "description": "Design contract for #8153. Not deployed. All URLs and identities in examples are fictional. Semantics: api.md."},
        "servers": [{"url": "https://adp.example.com/api/v1", "description": "Fictional public gateway; backend mount /v1"}],
        "security": [{"BearerAuth": []}],
        "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "Existing ADP authenticated session"}, "ProducerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "Existing service token with product-callback audience and operation scope"}}},
        "paths": {},
    }
    for method, path, operation_id, request, response, status, command, conditional in OPERATIONS:
        parameters = []
        for name in re.findall(r"\{([^}]+)\}", path):
            value = schema("Revision" if name == "revision" else "Id")
            if name == "step_id":
                value = {"type": "string", "enum": ["information", "basic", "advanced", "financials"]}
            parameters.append({"name": name, "in": "path", "required": True, "schema": value})
        if method == "GET" and response.endswith("Page"):
            parameters.extend([
                {"name": "cursor", "in": "query", "schema": {"type": "string", "maxLength": 2048}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
            ])
        if operation_id == "getProductPeople":
            parameters.extend([
                {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
            ])
        if operation_id == "listSources":
            parameters.extend([
                {"name": "connection_id", "in": "query", "schema": schema("Id")},
                {"name": "connection_kind", "in": "query", "schema": {"type": "string", "enum": ["github_installation", "credential"]}},
                {"name": "resource_cursor", "in": "query", "schema": {"type": "string", "maxLength": 2048}},
            ])
        if method != "GET" and operation_id != "applyCallback":
            parameters.append({"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "minLength": 16, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"}})
        if conditional:
            parameters.append({"name": "If-Profile-Revision", "in": "header", "required": True, "schema": schema("Revision"), "description": "Aggregate revision from an authorized response, not an HTTP representation ETag. Replays resolve the operation key before checking a stale revision."})
        responses = {
            str(status): {"description": "Success; see operation semantics in api.md", "content": {"application/json": {"schema": schema(response)}}}
        }
        if response in {"Profile", "Session", "Binding"}:
            responses[str(status)]["headers"] = {"Profile-Revision": {"schema": schema("Revision"), "description": "Committed aggregate revision; live progress/authorization overlays are checked separately"}}
        if status == 202:
            responses[str(status)]["headers"] = {"Location": {"schema": {"type": "string"}, "description": "Same-scope operation polling URL"}, "Retry-After": {"schema": {"type": "integer", "minimum": 1}}}
        if status == 201:
            responses[str(status)].setdefault("headers", {})["Location"] = {"schema": {"type": "string"}, "description": "Canonical resource URL"}
        if operation_id == "startOnboarding":
            responses["200"] = responses["201"]
        for code, description in {400: "Invalid syntax/cursor", 401: "Authentication required", 403: "Known resource; operation denied", 404: "Not found or scope not visible", 409: "Semantic conflict / idempotency mismatch / stale callback generation", 410: "Expired receipt or historical revision", 412: "Stale revision", 422: "Typed or semantic validation failure", 428: "Required precondition missing", 429: "Rate bound exceeded", 503: "Dependency unavailable; saved profile unaffected"}.items():
            responses[str(code)] = {"description": description, "content": {"application/json": {"schema": schema("Error")}}}
        operation = {"operationId": operation_id, "summary": command or operation_id, **({"x-cli-command": command} if command else {}), "parameters": parameters, "responses": responses}
        if request:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema(request)}}}
        if operation_id == "applyCallback":
            operation["security"] = [{"ProducerAuth": []}]
        document["paths"].setdefault(path, {})[method.lower()] = operation
    return json.dumps(document, indent=2) + "\n"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    content = generate()
    if args.write:
        (HOME / "openapi.json").write_text(content)
    elif (HOME / "openapi.json").read_text() != content:
        raise SystemExit("openapi.json differs from the operation catalog")
    else:
        print(f"OpenAPI catalog: {len(OPERATIONS)} operations match")
