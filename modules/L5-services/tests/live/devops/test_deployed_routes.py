"""Stage exports preserve retained route behavior, not just deployment identities."""

import json
from copy import deepcopy
from io import BytesIO

import pytest

from deployed_routes import route_definitions


@pytest.fixture
def document():
    return {"openapi": "3.0.1", "paths": {"/retained": {
        "parameters": [{"name": "example", "in": "query", "schema": {"type": "string"}}],
        "get": {"responses": {"default": {"description": "response"}}, "security": [{"member": ["read"]}],
                "x-amazon-apigateway-integration": {"$ref": "#/components/x-amazon-apigateway-integrations/example"}}}},
        "components": {"securitySchemes": {"member": {"type": "oauth2", "x-amazon-apigateway-authorizer": {
            "identitySource": "$request.header.Authorization", "type": "jwt", "jwtConfiguration": {
                "issuer": "https://identity.example.org", "audience": ["example"]}}}},
            "x-amazon-apigateway-integrations": {"example": {"type": "http_proxy", "uri": "https://service.example.org"}}}}


@pytest.mark.parametrize("change", [
    lambda value: value["components"]["securitySchemes"]["member"]["x-amazon-apigateway-authorizer"]["jwtConfiguration"].update(audience=["other"]),
    lambda value: value["components"]["x-amazon-apigateway-integrations"]["example"].update(uri="https://other.example.org"),
    lambda value: value["paths"]["/retained"]["get"].update(security=[]),
    lambda value: value["paths"]["/retained"]["parameters"][0].update(required=True),
])
def test_retained_route_dependencies_are_compared(document, change):
    before = route_definitions(json.dumps(document))
    change(document)
    assert route_definitions(json.dumps(document)) != before


def test_unused_components_do_not_change_retained_behavior(document):
    before = route_definitions(json.dumps(document))
    document["components"]["securitySchemes"]["unused"] = {"type": "apiKey"}
    assert route_definitions(json.dumps(document)) == before


@pytest.mark.parametrize("stage", [
    {"StageVariables": {"target": "other"}},
    {"DefaultRouteSettings": {"ThrottlingRateLimit": 1.0}},
    {"RouteSettings": {"GET /retained": {"ThrottlingRateLimit": 1.0}}},
])
def test_effective_retained_stage_settings_are_compared(document, stage):
    assert route_definitions(json.dumps(document), stage=stage) != route_definitions(json.dumps(document))


def test_default_and_any_method_routes_are_distinct():
    document = {"openapi": "3.0.1", "paths": {
        "/$default": {"x-amazon-apigateway-any-method": {"isDefaultRoute": True}},
        "/{proxy+}": {"x-amazon-apigateway-any-method": {"responses": {}}}}}
    assert set(route_definitions(json.dumps(document))) == {"$default", "ANY /{proxy+}"}


@pytest.mark.parametrize("body", [b"{}", b"{", b" " * (1024 * 1024 + 1)])
def test_missing_malformed_and_oversized_exports_close_the_stream(body):
    stream = BytesIO(body)
    with pytest.raises(ValueError):
        route_definitions(stream)
    assert stream.closed


def test_missing_or_external_dependencies_are_rejected(document):
    for reference in ("#/components/missing", "https://other.example.org/schema"):
        changed = deepcopy(document)
        changed["paths"]["/retained"]["get"]["x-amazon-apigateway-integration"]["$ref"] = reference
        with pytest.raises(ValueError, match="reference"):
            route_definitions(json.dumps(changed))
