"""Prepare private connector prerequisites without switching to the adapter."""

import argparse
import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

import guard_release
from handoff_release import (
    CONNECTORS,
    FOUNDATION_GROUP,
    MAX_DOCUMENT,
    Backend,
    connector_configuration,
    require,
    stage_configuration,
)

CREDENTIALS = [
    {
        "credentialProviderType": "GATEWAY_IAM_ROLE",
        "credentialProvider": {"iamCredentialProvider": {"service": "execute-api"}},
    }
]


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_stream(stream):
    try:
        raw = stream.read(MAX_DOCUMENT + 1)
    finally:
        stream.close()
    require(0 < len(raw) <= MAX_DOCUMENT)
    return json.loads(raw)


def private_write(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(encoded(value))


def adapter_edge_provenance(state):
    """Read the adapter's existing edge secret from its protected foundation state."""
    expected = state["outputs"]["observation_runtime"]["value"]["edge_provenance_sha256"]
    values = [
        instance.get("attributes", {}).get("result")
        for resource in state.get("resources", [])
        if resource.get("mode") == "managed"
        and resource.get("type") == "random_password"
        and resource.get("name") == "observation_edge"
        for instance in resource.get("instances", [])
        if instance.get("index_key") == "runtime"
    ]
    require(len(values) == 1 and isinstance(values[0], str) and re.fullmatch(r"[A-Za-z0-9]{64}", values[0]))
    require(hashlib.sha256(values[0].encode()).hexdigest() == expected)
    return values[0]


def restored_release(snapshot):
    """Read the consistent, disabled release retained by a completed rollback."""
    from adp_tools.adapter_contracts import AdapterRelease

    maps = list(snapshot["maps"].values())
    require(bool(maps))
    if all("ADP_TOOL_ADAPTER_RELEASE" not in values for values in maps):
        return None
    require(all(values.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") == "false" for values in maps))
    releases = [AdapterRelease.model_validate_json(values.get("ADP_TOOL_ADAPTER_RELEASE", "null")) for values in maps]
    require(all(value == releases[0] and value.group == "tool-connectors" for value in releases))
    return releases[0].model_dump(mode="json")


def configuration(value, environment):
    require(
        type(value) is dict
        and set(value)
        == {"api_id", "stage", "gateway_id", "foundation_state", "adapter_namespace", "workloads", "release"}
    )
    for field in ("api_id", "stage", "gateway_id", "adapter_namespace"):
        require(type(value[field]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[field]))
    state = value["foundation_state"]
    require(type(state) is dict and set(state) == {"bucket", "key"})
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", state["bucket"]))
    require(re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", state["key"]) and ".." not in state["key"].split("/"))
    from adp_tools.adapter_contracts import AdapterRelease

    release = AdapterRelease.model_validate(value["release"]).model_dump(mode="json")
    require(release["source_revision"] == environment["ADP_SOURCE_REVISION"])
    require(release["group"] == "tool-connectors")
    return copy.deepcopy(value)


def policy_document(value):
    """API Gateway returns JSON-escaped policy text without enclosing quotes."""
    if not isinstance(value, str):
        return copy.deepcopy(value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(json.loads('"' + value + '"'))


def extended_policy(value, resources, role, endpoint):
    policy = policy_document(value)
    require(type(policy) is dict and policy.get("Version") == "2012-10-17")
    statements = policy.get("Statement")
    require(type(statements) is list and all(type(item) is dict for item in statements))
    addition = {
        "Sid": "ADPAdapterPreparedConnectors",
        "Effect": "Allow",
        "Principal": {"AWS": role},
        "Action": "execute-api:Invoke",
        "Resource": sorted(resources),
        "Condition": {"StringEquals": {"aws:SourceVpce": endpoint}},
    }
    existing = [item for item in statements if item.get("Sid") == addition["Sid"]]
    require(not existing or existing == [addition])
    if not existing:
        statements.append(addition)
    return policy


class Preparer:
    def __init__(self, config, environment, *, clients=None, backend_factory=Backend, sleep=time.sleep):
        self.env, self.sleep = environment, sleep
        self.config = configuration(config, environment)
        factory = clients or (lambda name: boto3.client(name, region_name=environment["ADP_TOOLS_AWS_REGION"]))
        self.api, self.control, self.s3 = (factory(name) for name in ("apigateway", "bedrock-agentcore-control", "s3"))
        self.sts = factory("sts")
        self.ec2, self.elb, self.links, self.iam = (factory(name) for name in ("ec2", "elbv2", "apigatewayv2", "iam"))
        self.backend_factory = backend_factory
        self.account, self.region = environment["ADP_TOOLS_ACCOUNT_ID"], environment["ADP_TOOLS_AWS_REGION"]
        self.key = f"tools/adapters/handoff/{config['api_id']}/{config['release']['source_revision']}.json"
        self.bucket = config["foundation_state"]["bucket"]
        self.owner = {
            key: environment[key] for key in ("ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CORRELATION")
        }
        self.journal = None

    def mutate(self, method, **arguments):
        guard_release.authorize(self.env)
        return method(**arguments)

    def save(self, *, first=False):
        arguments = {
            "Bucket": self.bucket,
            "Key": self.key,
            "Body": encoded(self.journal),
            "ContentType": "application/json",
            "ServerSideEncryption": self.encryption,
        }
        if self.encryption == "aws:kms":
            arguments["SSEKMSKeyId"] = self.kms_key
        arguments["IfNoneMatch" if first else "IfMatch"] = "*" if first else self.etag
        self.etag = self.mutate(self.s3.put_object, **arguments)["ETag"]

    def read(self, stage):
        selected = self.api.get_stage(restApiId=self.config["api_id"], stageName=stage)
        require(not selected.get("canarySettings", {}).get("percentTraffic", 0))
        document = read_stream(
            self.api.get_export(
                restApiId=self.config["api_id"],
                stageName=stage,
                exportType="swagger",
                parameters={"extensions": "integrations,authorizers"},
            )["body"]
        )
        confirmed = self.api.get_stage(restApiId=self.config["api_id"], stageName=stage)
        require(stage_configuration(confirmed) == stage_configuration(selected))
        return selected, document

    def targets(self):
        rows, token = [], None
        for _ in range(5):
            page = self.control.list_gateway_targets(
                gatewayIdentifier=self.config["gateway_id"], maxResults=100, **({"nextToken": token} if token else {})
            )
            rows.extend(page["items"])
            token = page.get("nextToken")
            if not token:
                require(len({row["name"] for row in rows}) == len(rows))
                return {row["name"]: row["targetId"] for row in rows}
        raise ValueError("Unbounded gateway target inventory")

    def load_balancer(self, hostname):
        token = None
        found = []
        for _ in range(10):
            page = self.elb.describe_load_balancers(**({"Marker": token} if token else {}))
            found.extend(item for item in page["LoadBalancers"] if item["DNSName"] == hostname)
            token = page.get("NextMarker")
            if not token:
                break
        require(not token and len(found) == 1)
        value = found[0]
        require(value["Scheme"] == "internal" and value["Type"] == "application" and value["State"]["Code"] == "active")
        require(
            value["LoadBalancerArn"].startswith(
                f"arn:aws:elasticloadbalancing:{self.region}:{self.account}:loadbalancer/app/"
            )
        )
        return {key: value[key] for key in ("LoadBalancerArn", "DNSName", "VpcId", "SecurityGroups")}

    def discover(self):
        api = self.api.get_rest_api(restApiId=self.config["api_id"])
        require(api["endpointConfiguration"]["types"] == ["PRIVATE"])
        endpoints = api["endpointConfiguration"]["vpcEndpointIds"]
        require(len(endpoints) == 1)
        selected, document = self.read(self.config["stage"])
        knowledge_path = "/internal/v1/tools/knowledge/mcp"
        method = document["paths"][knowledge_path]["post"]
        require(method["security"] == [{"sigv4": []}])
        require(document["securityDefinitions"]["sigv4"]["x-amazon-apigateway-authtype"] == "awsSigv4")
        route = method["x-amazon-apigateway-integration"]
        require(
            route["connectionType"] == "VPC_LINK" and route["type"] == "http_proxy" and route["httpMethod"] == "POST"
        )
        legacy = self.load_balancer(urlsplit(route["uri"]).hostname)
        require(route["integrationTarget"] == legacy["LoadBalancerArn"])
        state = self.s3.get_object(Bucket=self.bucket, Key=self.config["foundation_state"]["key"])
        self.encryption = state.get("ServerSideEncryption")
        require(self.encryption in {"aws:kms", "AES256"})
        self.kms_key = state.get("SSEKMSKeyId")
        require(self.encryption != "aws:kms" or self.kms_key)
        foundation = read_stream(state["Body"])
        edge_provenance = adapter_edge_provenance(foundation)
        retained = foundation["outputs"]["private_edge"]["value"]
        require(
            retained["namespace"] == self.config["adapter_namespace"] and retained["ingress"] == "tool-adapters-private"
        )
        require(set(retained["paths"]) == {f"/internal/v1/tools/{name}/mcp" for name in CONNECTORS})
        adapter = self.load_balancer(retained["hostname"])
        require(adapter["VpcId"] == legacy["VpcId"] and adapter["LoadBalancerArn"] != legacy["LoadBalancerArn"])
        link = self.links.get_vpc_link(VpcLinkId=route["connectionId"])
        require(link["VpcLinkStatus"] == "AVAILABLE" and link["SecurityGroupIds"])
        groups = self.ec2.describe_security_groups(
            GroupIds=sorted(set(link["SecurityGroupIds"] + adapter["SecurityGroups"]))
        )
        require(
            {group["GroupId"] for group in groups["SecurityGroups"]}
            == set(link["SecurityGroupIds"] + adapter["SecurityGroups"])
        )
        require(
            all(
                group["VpcId"] == adapter["VpcId"] and group["OwnerId"] == self.account
                for group in groups["SecurityGroups"]
            )
        )
        gateway = self.control.get_gateway(gatewayIdentifier=self.config["gateway_id"])
        require(
            gateway["gatewayArn"]
            == f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{self.config['gateway_id']}"
        )
        require(gateway["status"] == "READY" and gateway["roleArn"].startswith(f"arn:aws:iam::{self.account}:role/"))
        inventory = self.targets()
        knowledge = self.control.get_gateway_target(
            gatewayIdentifier=self.config["gateway_id"], targetId=inventory["knowledge"]
        )
        require(knowledge["status"] == "READY")
        require(knowledge.get("credentialProviderConfigurations") == CREDENTIALS)
        private = knowledge["privateEndpoint"]
        require(private["managedVpcResource"]["vpcIdentifier"] == adapter["VpcId"])
        endpoint = self.ec2.describe_vpc_endpoints(VpcEndpointIds=endpoints)["VpcEndpoints"]
        require(len(endpoint) == 1 and endpoint[0]["VpcId"] == adapter["VpcId"] and endpoint[0]["State"] == "available")
        return {
            "original_stage": {"deploymentId": selected["deploymentId"]},
            "original_document": document,
            "method": method,
            "adapter_edge_provenance": edge_provenance,
            "adapter": adapter,
            "legacy": legacy,
            "link": {key: link[key] for key in ("VpcLinkId", "SecurityGroupIds", "SubnetIds")},
            "endpoint": {"VpcEndpointId": endpoints[0]},
            "gateway": {"roleArn": gateway["roleArn"]},
            "private": private,
            "unrelated_targets": {key: value for key, value in inventory.items() if key not in CONNECTORS},
        }

    def network(self, inputs):
        for destination in inputs["adapter"]["SecurityGroups"]:
            for source in inputs["link"]["SecurityGroupIds"]:
                permission = {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "UserIdGroupPairs": [{"GroupId": source}],
                }
                reverse = {**permission, "UserIdGroupPairs": [{"GroupId": destination}]}
                for group, desired, field, method in (
                    (destination, permission, "IpPermissions", self.ec2.authorize_security_group_ingress),
                    (source, reverse, "IpPermissionsEgress", self.ec2.authorize_security_group_egress),
                ):

                    def present():
                        observed = self.ec2.describe_security_groups(GroupIds=[group])["SecurityGroups"][0]
                        return any(
                            all(item.get(key) == desired[key] for key in ("IpProtocol", "FromPort", "ToPort"))
                            and any(
                                pair.get("GroupId") == desired["UserIdGroupPairs"][0]["GroupId"]
                                for pair in item.get("UserIdGroupPairs", [])
                            )
                            for item in observed[field]
                        )

                    if not present():
                        self.mutate(method, GroupId=group, IpPermissions=[desired])
                    require(present())

    def permissions(self, inputs):
        api_id, stage = self.config["api_id"], self.config["stage"]
        resources = [
            f"arn:aws:execute-api:{self.region}:{self.account}:{api_id}/{stage}/POST/internal/v1/tools/{name}/mcp"
            for name in sorted(CONNECTORS)
        ]
        role, endpoint = inputs["gateway"]["roleArn"], inputs["endpoint"]["VpcEndpointId"]
        api = self.api.get_rest_api(restApiId=api_id)
        desired = extended_policy(api["policy"], resources, role, endpoint)
        if policy_document(api["policy"]) != desired:
            self.mutate(
                self.api.update_rest_api,
                restApiId=api_id,
                patchOperations=[{"op": "replace", "path": "/policy", "value": encoded(desired).decode()}],
            )
        require(policy_document(self.api.get_rest_api(restApiId=api_id)["policy"]) == desired)
        observed = self.ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint])["VpcEndpoints"][0]
        desired = extended_policy(observed["PolicyDocument"], resources, role, endpoint)
        if json.loads(observed["PolicyDocument"]) != desired:
            self.mutate(self.ec2.modify_vpc_endpoint, VpcEndpointId=endpoint, PolicyDocument=encoded(desired).decode())
        require(
            json.loads(self.ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint])["VpcEndpoints"][0]["PolicyDocument"])
            == desired
        )
        name = role.rsplit("/", 1)[-1]
        policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": resources}],
        }
        try:
            previous = self.iam.get_role_policy(RoleName=name, PolicyName="adp-adapter-prepared-invoke")[
                "PolicyDocument"
            ]
        except ClientError as error:
            require(error.response["Error"]["Code"] == "NoSuchEntity")
            previous = None
        require(previous is None or previous == policy)
        if previous is None:
            self.mutate(
                self.iam.put_role_policy,
                RoleName=name,
                PolicyName="adp-adapter-prepared-invoke",
                PolicyDocument=encoded(policy).decode(),
            )
        require(
            self.iam.get_role_policy(RoleName=name, PolicyName="adp-adapter-prepared-invoke")["PolicyDocument"]
            == policy
        )

    def ensure_stage(self, name, document, *, retained_deployment=None):
        marker = "adp-adapter-preparation-" + hashlib.sha256(encoded(document)).hexdigest()
        deployments, token = ([retained_deployment] if retained_deployment else []), None
        for _ in range(10):
            if retained_deployment:
                break
            page = self.api.get_deployments(
                restApiId=self.config["api_id"], limit=500, **({"position": token} if token else {})
            )
            deployments.extend(item["id"] for item in page["items"] if item.get("description") == marker)
            token = page.get("position")
            if not token:
                break
        require(not token and len(deployments) <= 1)
        if not deployments:
            self.mutate(
                self.api.put_rest_api,
                restApiId=self.config["api_id"],
                mode="merge",
                failOnWarnings=True,
                body=encoded(document),
            )
            deployment = self.mutate(self.api.create_deployment, restApiId=self.config["api_id"], description=marker)[
                "id"
            ]
        else:
            deployment = deployments[0]
        try:
            current = self.api.get_stage(restApiId=self.config["api_id"], stageName=name)
        except ClientError as error:
            require(error.response["Error"]["Code"] == "NotFoundException")
            current = self.mutate(
                self.api.create_stage,
                restApiId=self.config["api_id"],
                stageName=name,
                deploymentId=deployment,
                description=marker,
            )
        require(current["deploymentId"] == deployment and current.get("description") == marker)
        return deployment

    def ensure_targets(self, inputs):
        inventory, result = self.targets(), {}
        for name in sorted(CONNECTORS):
            expected = {
                "targetConfiguration": {
                    "mcp": {
                        "mcpServer": {
                            "endpoint": f"https://{self.config['api_id']}.execute-api.{self.region}.amazonaws.com/{self.config['stage']}/internal/v1/tools/{name}/mcp",
                            "listingMode": "DYNAMIC",
                        }
                    }
                },
                "credentialProviderConfigurations": copy.deepcopy(CREDENTIALS),
                "privateEndpoint": inputs["private"],
            }
            identifier = inventory.get(name)
            if identifier is None:
                value = self.mutate(
                    self.control.create_gateway_target,
                    gatewayIdentifier=self.config["gateway_id"],
                    name=name,
                    clientToken=hashlib.sha256(
                        encoded({"gateway": self.config["gateway_id"], "name": name, **expected})
                    ).hexdigest(),
                    description="ADP prepared private connector",
                    **expected,
                )
                identifier = value["targetId"]
            for _ in range(60):
                observed = self.control.get_gateway_target(
                    gatewayIdentifier=self.config["gateway_id"], targetId=identifier
                )
                require(observed["name"] == name and all(observed.get(key) == value for key, value in expected.items()))
                if observed["status"] == "READY":
                    break
                require(observed["status"] in {"CREATING", "UPDATING"})
                self.sleep(5)
            require(observed["status"] == "READY")
            result[name] = identifier
        require(
            {key: value for key, value in self.targets().items() if key not in CONNECTORS}
            == inputs["unrelated_targets"]
        )
        return result

    def verify_identity(self):
        identity = self.sts.get_caller_identity()
        role = self.env["ADP_TOOLS_DEPLOY_ROLE_ARN"]
        require(role.split(":")[4] == self.account == identity["Account"])
        require(
            re.fullmatch(
                rf"arn:aws:sts::{self.account}:assumed-role/{re.escape(role.rsplit('/', 1)[-1])}/[^/]+", identity["Arn"]
            )
        )

    def prepare(self):
        guard_release.authorize(self.env)
        self.verify_identity()
        inputs = self.discover()
        objects = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.key, MaxKeys=2)
        require(not objects.get("IsTruncated") and all(item["Key"] == self.key for item in objects.get("Contents", [])))
        stored = self.s3.get_object(Bucket=self.bucket, Key=self.key) if objects.get("Contents") else None
        if stored:
            self.journal, self.etag = read_stream(stored["Body"]), stored["ETag"]
            # The current job's native authority and identity were checked above.
            # Keep the original owner for stable stage names while a newly
            # authorized attempt resumes the same release and retained inputs.
            require(self.journal["config"] == self.config)
            require(
                all(
                    inputs[key] == self.journal["inputs"][key]
                    for key in ("adapter", "legacy", "link", "private", "unrelated_targets", "adapter_edge_provenance")
                )
            )
            inputs = self.journal["inputs"]
        else:
            self.journal = {"owner": self.owner, "config": self.config, "inputs": inputs}
            self.save(first=True)
        token = hashlib.sha256(encoded(self.journal["owner"])).hexdigest()[:20]
        selectors = {
            "account_id": self.account,
            "region": self.region,
            "gateway_id": self.config["gateway_id"],
            "candidate_stage": "adapter_" + token,
            "baseline_stage": "baseline_" + token,
            "legacy_host": inputs["legacy"]["DNSName"],
            "adapter_host": inputs["adapter"]["DNSName"],
            "adapter_namespace": self.config["adapter_namespace"],
            "workloads": self.config["workloads"],
        }
        config = {"selectors": selectors, "plan": {"api_id": self.config["api_id"], "stage": self.config["stage"]}}
        backend = self.backend_factory(config, self.env)
        ingress = json.loads(
            backend.kubectl(
                "-n", self.config["adapter_namespace"], "get", "ingress", "tool-adapters-private", "-o", "json"
            )
        )
        endpoints = ingress["status"]["loadBalancer"]["ingress"]
        require(
            type(endpoints) is list
            and len(endpoints) == 1
            and type(endpoints[0]) is dict
            and endpoints[0].get("hostname") == selectors["adapter_host"]
            and not endpoints[0].get("ip")
        )
        configured = self.verify_runtime(backend)
        require(
            hashlib.sha256(inputs["adapter_edge_provenance"].encode()).hexdigest()
            == configured.observation.edge_provenance_sha256
        )
        # Retain the first snapshot across retries and interrupted preparation.
        # It is stored with the protected plan before any route/network change.
        if "runtime_before" not in self.journal:
            from catalogue_binding import snapshot

            self.journal["runtime_before"] = snapshot(backend)
            self.save()
        selectors["runtime_before"] = self.journal["runtime_before"]
        # A completed rollback keeps its generations in the runtime maps. Use
        # that exact legacy baseline for the next image, rather than pretending
        # this is the first activation. An active adapter must first be restored
        # through the protected rollback path before replacing its container.
        previous_release = restored_release(selectors["runtime_before"])
        for field in ("group_generation", "backend_generation"):
            require(self.config["release"][field] == (previous_release[field] if previous_release else 0) + 1)
        self.network(inputs)
        self.permissions(inputs)
        targets = self.ensure_targets(inputs)
        baseline = copy.deepcopy(inputs["original_document"])
        baseline.pop("basePath", None)
        baseline["x-amazon-apigateway-policy"] = policy_document(
            self.api.get_rest_api(restApiId=self.config["api_id"])["policy"]
        )
        for name in sorted(CONNECTORS - {"atlassian"}):
            path = f"/internal/v1/tools/{name}/mcp"
            if path not in baseline["paths"]:
                method = copy.deepcopy(inputs["method"])
                method["x-amazon-apigateway-integration"]["uri"] = f"http://{selectors['legacy_host']}{path}"
                baseline["paths"][path] = {"post": method}
        require("/internal/v1/tools/atlassian/mcp" not in baseline["paths"])
        before_id = self.ensure_stage(
            selectors["baseline_stage"],
            baseline,
            retained_deployment=inputs["original_stage"]["deploymentId"] if previous_release else None,
        )
        candidate = copy.deepcopy(baseline)
        for name in sorted(CONNECTORS):
            path = f"/internal/v1/tools/{name}/mcp"
            method = copy.deepcopy(inputs["method"])
            method["x-amazon-apigateway-integration"].update(
                uri=f"http://{selectors['adapter_host']}{path}", integrationTarget=inputs["adapter"]["LoadBalancerArn"]
            )
            # Knowledge and the standalone adapter have distinct private-edge
            # secrets. Retain the IAM caller mapping, but bind these four routes
            # to the secret actually configured in the destination adapter.
            method["x-amazon-apigateway-integration"].setdefault("requestParameters", {})[
                "integration.request.header.X-Adp-Edge-Provenance"
            ] = "'" + inputs["adapter_edge_provenance"] + "'"
            candidate["paths"][path] = {"post": method}
        after_id = self.ensure_stage(selectors["candidate_stage"], candidate)
        routes = {
            name: {
                "backend": "disabled" if name == "atlassian" else "legacy",
                "target_id": identifier,
                "digest": "sha256:" + "0" * 64,
            }
            for name, identifier in targets.items()
        }
        before = {"status": "legacy", "release": previous_release, "deployment_id": before_id, "routes": routes}
        after = {
            "status": "active",
            "release": self.config["release"],
            "deployment_id": after_id,
            "routes": {name: {**value, "backend": "adapter"} for name, value in routes.items()},
        }
        before = backend.observe(selectors["baseline_stage"], before)
        after = backend.observe(selectors["candidate_stage"], after)
        config["plan"].update(before=before, after=after)
        backend.verify_prepared(config["plan"])
        current = self.api.get_stage(restApiId=self.config["api_id"], stageName=self.config["stage"])
        allowed = {inputs["original_stage"]["deploymentId"], before_id, after_id}
        require(current["deploymentId"] in allowed and not current.get("canarySettings", {}).get("percentTraffic", 0))
        if current["deploymentId"] == inputs["original_stage"]["deploymentId"] and current["deploymentId"] != before_id:
            _, actual = self.read(self.config["stage"])
            original = copy.deepcopy(inputs["original_document"])
            for document in (actual, original):
                document.pop("x-amazon-apigateway-policy", None)
            require(actual == original)
            self.mutate(
                self.api.update_stage,
                restApiId=self.config["api_id"],
                stageName=self.config["stage"],
                patchOperations=[{"op": "replace", "path": "/deploymentId", "value": before_id}],
            )
        expected = after if current["deploymentId"] == after_id else before
        require(backend.observe(self.config["stage"], expected) == expected)
        self.journal["handoff"] = config
        self.save()
        return config

    def rollback_plan(self):
        guard_release.authorize(self.env)
        self.verify_identity()
        stored = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        journal = read_stream(stored["Body"])
        require(journal["config"] == self.config and type(journal.get("handoff")) is dict)
        config = journal["handoff"]
        plan, selectors = config["plan"], config["selectors"]
        require(plan["api_id"] == self.config["api_id"] and plan["stage"] == self.config["stage"])
        require(plan["after"]["release"] == self.config["release"])
        require(selectors["account_id"] == self.account and selectors["region"] == self.region)
        require(
            all(selectors[field] == self.config[field] for field in ("gateway_id", "adapter_namespace", "workloads"))
        )
        state = guard_release.authorize(self.env, transition={"action": "read", "plan": plan})
        require(state is not None and state.get("transition", {}).get("plan") == plan)
        require(state["transition"]["phase"] in {"fenced", "active", "rollback", "restored"})
        backend = self.backend_factory(config, self.env)
        backend.verify_prepared(plan)
        current = self.api.get_stage(restApiId=plan["api_id"], stageName=plan["stage"])
        expected = [
            snapshot
            for snapshot in (plan["before"], plan["after"])
            if snapshot["deployment_id"] == current["deploymentId"]
        ]
        require(len(expected) == 1 and backend.observe(plan["stage"], expected[0]) == expected[0])
        return config

    def verify_runtime(self, backend):
        namespace = self.config["adapter_namespace"]
        config = json.loads(
            backend.kubectl("-n", namespace, "get", "configmap", "tool-adapters-contract", "-o", "json")
        )
        configured = connector_configuration(json.loads(config["data"]["config.json"]))
        release = configured.release.model_dump(mode="json")
        # Require the exact configured foundation image and revision. The fenced
        # transition is what changes the release group, so accept either the
        # foundation group it was installed under or the requested connector
        # group, and nothing else.
        require(all(release[key] == self.config["release"][key] for key in ("source_revision", "image_digest")))
        require(release["group"] in {self.config["release"]["group"], FOUNDATION_GROUP})
        deployment = json.loads(backend.kubectl("-n", namespace, "get", "deployment", "tool-adapters", "-o", "json"))
        spec, status = deployment["spec"], deployment["status"]
        require(deployment["metadata"]["generation"] == status["observedGeneration"])
        require(
            type(spec["replicas"]) is int
            and spec["replicas"] > 0
            and all(
                status.get(key) == spec["replicas"]
                for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
        )
        containers = spec["template"]["spec"]["containers"]
        require(
            len(containers) == 1
            and containers[0]["image"].startswith(f"{self.account}.dkr.ecr.{self.region}.amazonaws.com/")
        )
        require(containers[0]["image"].endswith("@" + release["image_digest"]))
        require(
            any(
                volume.get("configMap", {}).get("name") == "tool-adapters-contract"
                for volume in spec["template"]["spec"]["volumes"]
            )
        )
        return configured


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dispatch-context", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        path = Path(args.config)
        require(
            path.is_file()
            and not path.is_symlink()
            and not path.stat().st_mode & 0o077
            and path.stat().st_size <= MAX_DOCUMENT
        )
        preparer = Preparer(json.loads(path.read_bytes()), os.environ)
        if args.dispatch_context:
            from handoff_delivery import decode, identity, verify_checkout

            preparer.verify_identity()
            inputs = decode(os.environ["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"])
            selectors = {
                "account_id": preparer.account,
                "region": preparer.region,
                "gateway_id": preparer.config["gateway_id"],
            }
            common = identity(os.environ, {"selectors": selectors, "plan": {"stage": preparer.config["stage"]}}, inputs)
            verify_checkout(os.environ, inputs)
            config = {
                "schema_version": 1,
                **common,
                "inputs": {
                    key: value
                    for key, value in inputs.items()
                    if not key.startswith("adp_") or key == "adp_contract_digest"
                },
            }
        elif args.rollback:
            config = preparer.rollback_plan()
        else:
            config = preparer.prepare()
        private_write(args.output, config)
    except Exception:
        raise SystemExit(
            "Private connector preparation incomplete; retain its original journal before retrying"
        ) from None


if __name__ == "__main__":
    main()
