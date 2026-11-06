"""Read the selected private-edge deployment before its owning Terraform plan."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def require(condition):
    if not condition:
        raise ValueError("Retained connector handoff requires reconciliation")


def contains(expected, observed):
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(key in observed and contains(value, observed[key]) for key, value in expected.items())
    return expected == observed


def read_selection(query, call):
    region, role = query["region"], query["gateway_role_arn"]
    require(re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+", role))
    require(call("sts", "get-caller-identity")["Account"] == role.split(":")[4])
    apis = call("apigateway", "get-rest-apis")["items"]
    matches = [item for item in apis if item["name"] == query["name"]]
    require(len(matches) <= 1)
    inactive = {"prepared": "false", "document": "{}", "api_policy": "{}", "endpoint_policy": "{}", "deployment_id": "", "api_id": ""}
    if not matches:
        return inactive
    api = matches[0]
    stages = call("apigateway", "get-stages", rest_api_id=api["id"])["item"]
    selected = [item for item in stages if item["stageName"] == query["stage"]]
    marker = "adp-adapter-preparation-"
    require(len(selected) <= 1)
    if not selected:
        require(not any(item.get("description", "").startswith(marker) for item in stages))
        return inactive
    deployment = call("apigateway", "get-deployment", rest_api_id=api["id"], deployment_id=selected[0]["deploymentId"])
    if not deployment.get("description", "").startswith(marker):
        require(not any(item.get("description", "").startswith(marker) for item in stages))
        return inactive
    require(not selected[0].get("canarySettings", {}).get("percentTraffic", 0))
    require(api["endpointConfiguration"].get("types") == ["PRIVATE"]
            and api["endpointConfiguration"].get("vpcEndpointIds") == [query["endpoint_id"]])
    document = call("apigateway", "get-export", rest_api_id=api["id"], stage_name=query["stage"],
                    export_type="swagger", parameters="extensions=integrations,authorizers")
    document.pop("basePath", None)
    expected = json.loads(query["specification"])
    wanted = expected["paths"]["/internal/v1/tools/knowledge/mcp"]["post"]
    current = document["paths"]["/internal/v1/tools/knowledge/mcp"]["post"]
    require(current["security"] == wanted["security"])
    require(contains(wanted["x-amazon-apigateway-integration"], current["x-amazon-apigateway-integration"]))
    require(contains(expected["securityDefinitions"], document["securityDefinitions"]))
    paths = {f"/internal/v1/tools/{name}/mcp" for name in ("github", "jira", "slack", "atlassian")}
    require(paths - {"/internal/v1/tools/atlassian/mcp"} <= set(document["paths"]))
    for path in paths & set(document["paths"]):
        route = document["paths"][path]
        require(set(route) == {"post"} and route["post"]["security"] == [{"sigv4": []}])
        integration = route["post"]["x-amazon-apigateway-integration"]
        require(integration["connectionType"] == "VPC_LINK" and integration["httpMethod"] == "POST"
                and integration["type"] == "http_proxy"
                and re.fullmatch(r"http://internal-[A-Za-z0-9.-]+\.elb\.amazonaws\.com" + re.escape(path), integration["uri"]))
    endpoint = call("ec2", "describe-vpc-endpoints", vpc_endpoint_ids=query["endpoint_id"])["VpcEndpoints"]
    require(len(endpoint) == 1 and endpoint[0]["State"] == "available")
    prefix = f"arn:aws:execute-api:{region}:{role.split(':')[4]}:{api['id']}/{query['stage']}/POST"
    addition = {"Sid": "ADPAdapterPreparedConnectors", "Effect": "Allow", "Principal": {"AWS": role},
                "Action": "execute-api:Invoke", "Resource": sorted(prefix + path for path in paths),
                "Condition": {"StringEquals": {"aws:SourceVpce": query["endpoint_id"]}}}
    policies = [json.loads(api["policy"]), json.loads(endpoint[0]["PolicyDocument"])]
    for policy in policies:
        require([item for item in policy["Statement"] if item.get("Sid") == addition["Sid"]] == [addition])
    confirmed = call("apigateway", "get-stage", rest_api_id=api["id"], stage_name=query["stage"])
    require(confirmed["deploymentId"] == deployment["id"] and not confirmed.get("canarySettings", {}).get("percentTraffic", 0))
    document["x-amazon-apigateway-policy"] = policies[0]
    return {"prepared": "true", "api_id": api["id"], "deployment_id": deployment["id"],
            "document": json.dumps(document), "api_policy": json.dumps(policies[0]), "endpoint_policy": json.dumps(policies[1])}


def main():
    query = json.load(sys.stdin)

    def call(service, operation, **arguments):
        command = ["aws", service, operation, "--region", query["region"], "--output", "json"]
        for key, value in arguments.items():
            command.extend(["--" + key.replace("_", "-"), value])
        with tempfile.TemporaryDirectory(prefix="adp-edge-read-") as directory:
            if operation == "get-export":
                path = Path(directory) / "export.json"
                command.append(str(path))
            result = subprocess.run(command, capture_output=True, check=True, timeout=120)
            return json.loads(path.read_bytes() if operation == "get-export" else result.stdout)

    try:
        print(json.dumps(read_selection(query, call)))
    except Exception:
        raise SystemExit("Private edge readback failed; do not overwrite the selected connector deployment") from None


if __name__ == "__main__":
    main()
