"""Observe and retire explicitly owned routes in an isolated HTTP API."""

import json
import re
import sys
import time

from deployed_routes import route_definitions


def recovery_routes(account_id, role_arn, region, selection, *, client_factory=None):
    import boto3
    from botocore.config import Config

    maximum = selection.get("max_requests")
    scope = selection.get("scope")
    api_id = selection.get("api_id")
    routes = selection.get("routes")
    operation = selection.get("operation")
    if (type(maximum) is not int or not 4 <= maximum <= 100
            or not isinstance(scope, dict) or set(scope) != {"tenant_id", "product_id", "project_id"}
            or any(not isinstance(value, str) or not value for value in scope.values())
            or not isinstance(api_id, str) or not re.fullmatch(r"[a-z0-9]{1,20}", api_id)
            or not isinstance(routes, list) or not routes or len(set(routes)) != len(routes)
            or any(not isinstance(route, str) or not re.fullmatch(r"[a-z0-9]{1,20}", route) for route in routes)
            or operation not in {"read", "cleanup"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", selection.get("qualification_run_id", ""))):
        raise ValueError("invalid installed route selection")
    client_factory = client_factory or boto3.client
    config = Config(connect_timeout=4, read_timeout=5, retries={"total_max_attempts": 1})
    calls = 0

    def request(method, **arguments):
        nonlocal calls
        calls += 1
        if calls > maximum:
            raise ValueError("installed routes exceeded their request allocation")
        return method(**arguments)

    identity = request(client_factory("sts", region_name=region, config=config).get_caller_identity)
    expected = rf"arn:aws:sts::{re.escape(account_id)}:assumed-role/{re.escape(role_arn.split('/')[-1])}/[^/]+"
    if identity.get("Account") != account_id or not re.fullmatch(expected, identity.get("Arn", "")):
        raise ValueError("selected AWS connection does not match route identity")
    client = client_factory("apigatewayv2", region_name=region, config=config)
    tags = {**{f"adp:{field}": value for field, value in scope.items()},
            "adp:qualification-run": selection["qualification_run_id"]}

    def ownership():
        api = request(client.get_api, ApiId=api_id)
        if (api.get("ApiId") != api_id or api.get("ProtocolType") != "HTTP"
                or api.get("ApiGatewayManaged") is True
                or any((api.get("Tags") or {}).get(key) != value for key, value in tags.items())):
            raise ValueError("HTTP API is not owned by the selected isolated qualification")

    def inventory():
        observed, visited = {}, set()
        token = None
        while True:
            page = request(client.get_routes, ApiId=api_id, MaxResults="100",
                           **({"NextToken": token} if token else {}))
            items = page.get("Items")
            if not isinstance(items, list):
                raise ValueError("installed route inventory unavailable")
            for item in items:
                route_id = item.get("RouteId") if isinstance(item, dict) else None
                if (not isinstance(route_id, str) or not re.fullmatch(r"[a-z0-9]{1,20}", route_id)
                        or route_id in observed or not isinstance(item.get("RouteKey"), str)):
                    raise ValueError("installed route inventory ambiguous")
                observed[route_id] = item
            token = page.get("NextToken")
            if not token:
                return observed
            if not isinstance(token, str) or token in visited:
                raise ValueError("installed route inventory repeated a cursor")
            visited.add(token)

    def deployments():
        observed, visited = {}, set()
        exported = {}
        token = None
        while True:
            page = request(client.get_stages, ApiId=api_id, MaxResults="100",
                           **({"NextToken": token} if token else {}))
            items = page.get("Items")
            if not isinstance(items, list):
                raise ValueError("installed route stages unavailable")
            for stage in items:
                name = stage.get("StageName") if isinstance(stage, dict) else None
                deployment = stage.get("DeploymentId") if isinstance(stage, dict) else None
                if (not isinstance(name, str) or not name or name in observed
                        or stage.get("AutoDeploy") is not True or not isinstance(deployment, str) or not deployment):
                    raise ValueError("route cleanup requires automatically deployed stages")
                native = request(client.get_deployment, ApiId=api_id, DeploymentId=deployment)
                if native.get("DeploymentId") != deployment or native.get("DeploymentStatus") != "DEPLOYED":
                    raise ValueError("installed route deployment is not ready")
                response = request(client.export_api, ApiId=api_id, StageName=name, Specification="OAS30",
                                   OutputType="JSON", IncludeExtensions=True)
                exported[name] = route_definitions(response.get("body"), stage=stage)
                current = request(client.get_stage, ApiId=api_id, StageName=name)
                if (current.get("StageName") != name or current.get("AutoDeploy") is not True
                        or current.get("DeploymentId") != deployment
                        or any(current.get(field, {}) != stage.get(field, {})
                               for field in ("StageVariables", "DefaultRouteSettings", "RouteSettings"))):
                    raise ValueError("installed stage changed during export")
                observed[name] = deployment
            token = page.get("NextToken")
            if not token:
                return observed, exported
            if not isinstance(token, str) or token in visited:
                raise ValueError("installed stage inventory repeated a cursor")
            visited.add(token)

    ownership()
    before = inventory()
    deployed, stage_routes = deployments()
    if operation == "cleanup":
        snapshots = selection.get("expected_routes")
        previous = selection.get("previous_deployments")
        expected = selection.get("expected_stage_routes")
        if (not isinstance(snapshots, dict) or set(snapshots) != set(routes)
                or not isinstance(previous, dict) or set(previous) != set(deployed)
                or not isinstance(expected, dict) or set(expected) != set(deployed)
                or any(not isinstance(definitions, dict) for definitions in expected.values())):
            raise ValueError("cleanup requires independent snapshots of every owned route")
        retained_keys = {value["RouteKey"] for route, value in before.items() if route not in routes}
        if any(set(definitions) != retained_keys for definitions in expected.values()):
            raise ValueError("expected stage exports must retain exactly the unowned routes")
        if any(route in before and (before[route] != snapshots[route]
                                   or before[route].get("ApiGatewayManaged") is True) for route in routes):
            raise ValueError("owned route changed since independent readback")
        for route in routes:
            if route not in before:
                continue
            ownership()
            current = request(client.get_route, ApiId=api_id, RouteId=route)
            current.pop("ResponseMetadata", None)
            if current != snapshots[route]:
                raise ValueError("owned route changed before cleanup")
            request(client.delete_route, ApiId=api_id, RouteId=route)
        after = inventory()
        if any(route in after for route in routes) or after != {
                route: value for route, value in before.items() if route not in routes}:
            raise ValueError("installed cleanup did not preserve the retained route inventory")
        deployed, stage_routes = deployments()
        while stage_routes != expected:
            if set(deployed) != set(previous):
                raise ValueError("installed stage inventory changed during cleanup")
            time.sleep(0.2)
            deployed, stage_routes = deployments()
        if set(deployed) != set(previous):
            raise ValueError("installed stage inventory changed during cleanup")
    else:
        after = before
    return {"api_id": api_id, "scope": scope, "qualification_run_id": selection["qualification_run_id"],
            "routes": after, "deployments": deployed, "stage_routes": stage_routes, "requests": calls}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4:
            raise ValueError("selected AWS identity required")
        result = recovery_routes(*sys.argv[1:], json.loads(sys.stdin.buffer.read(1024 * 1024)))
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("selected installed route operation unavailable", file=sys.stderr)
        sys.exit(1)
