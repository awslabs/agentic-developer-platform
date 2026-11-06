"""Exercise preparation against modeled AWS APIs, without cloud mutations."""

import copy
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from test_handoff_release import Transitions, configuration

ROOT = Path(__file__).parents[1]
ADAPTER_EDGE = "A" * 64
KNOWLEDGE_EDGE = "K" * 64


def missing(code):
    return ClientError({"Error": {"Code": code}}, "fixture")


def runtime_configuration(release):
    """Model the deployed contract: the foundation image plus connector configuration.

    The deployed adapter keeps the foundation release group until the fenced
    transition adopts the connector group, and it already carries the
    deployment-owned observation configuration with authenticated read schemas.
    """
    from tool_adapters.atlassian import READ_TOOLS
    from tool_adapters.atlassian_observe import canonical
    from tool_adapters.config import AdapterConfiguration

    schemas = {
        alias: {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]}
        for alias in READ_TOOLS
    }
    document = {
        "schema_version": "1.0",
        "mode": "observation",
        "release": release,
        "observation": {
            "core_authorize_url": "https://core.internal.example/internal/v1/tool-adapters/authorize",
            "core_role_arn": "arn:aws:iam::123456789012:role/adp-dev-core",
            "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
            "edge_provenance_sha256": hashlib.sha256(ADAPTER_EDGE.encode()).hexdigest(),
            "identity_workload": "adp-dev-atlassian-mcp-oauth-user",
            "identity_provider": "adp-dev-atlassian-mcp-user",
            "legacy_identity_workload": "adp-dev-oauth-user",
            "config_revision": "d" * 64,
            "gateway_role_arn": "arn:aws:iam::123456789012:role/adp-dev-gateway",
            "subject_schema_sha256": "a" * 64,
            "sites_schema_sha256": "b" * 64,
            "read_schemas": schemas,
            "read_schemas_sha256": {
                alias: hashlib.sha256(canonical(schema)).hexdigest() for alias, schema in schemas.items()
            },
        },
    }
    # The fixture is only useful while the shipped model still accepts it.
    AdapterConfiguration.model_validate(document)
    return document


class Cloud:
    def __init__(self, module):
        self.module, self.calls, self.objects, self.targets = module, [], {}, {}
        self.env = {
            "ADP_SOURCE_REVISION": "a" * 40,
            "ADP_DEFINITION_REVISION": "b" * 40,
            "ADP_CORRELATION": "orch:11111111-2222-3333-4444-555555555555",
            "ADP_TOOLS_ACCOUNT_ID": "123456789012",
            "ADP_TOOLS_AWS_REGION": "us-east-1",
            "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tools-handoff-trusted-deployment",
        }
        self.config = {
            "api_id": "api123",
            "stage": "dev",
            "gateway_id": "gateway123",
            "foundation_state": {"bucket": "example-state", "key": "tools/adapters/dev.tfstate"},
            "adapter_namespace": "adapters",
            "workloads": configuration()["selectors"]["workloads"],
            "release": configuration()["plan"]["after"]["release"],
        }
        self.role = "arn:aws:iam::123456789012:role/gateway"
        self.balancers = [
            {
                "DNSName": f"internal-{name}.elb.amazonaws.com",
                "Scheme": "internal",
                "Type": "application",
                "State": {"Code": "active"},
                "VpcId": "vpc-123",
                "SecurityGroups": ["sg-" + name],
                "LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/{name}/123",
            }
            for name in ("legacy", "adapter")
        ]
        self.ingress_endpoints = [{"hostname": self.balancers[1]["DNSName"]}]
        self.method = {
            "security": [{"sigv4": []}],
            "x-amazon-apigateway-auth": {"type": "AWS_IAM"},
            "x-amazon-apigateway-integration": {
                "connectionType": "VPC_LINK",
                "type": "http_proxy",
                "httpMethod": "POST",
                "connectionId": "link123",
                "integrationTarget": self.balancers[0]["LoadBalancerArn"],
                "uri": "http://internal-legacy.elb.amazonaws.com/internal/v1/tools/knowledge/mcp",
                "requestParameters": {
                    "integration.request.header.X-Caller-Identity": "context.identity.userArn",
                    "integration.request.header.X-Adp-Edge-Provenance": "'" + KNOWLEDGE_EDGE + "'",
                },
            },
        }
        self.document = {
            "swagger": "2.0",
            "paths": {"/internal/v1/tools/knowledge/mcp": {"post": self.method}},
            "securityDefinitions": {"sigv4": {"x-amazon-apigateway-authtype": "awsSigv4"}},
        }
        self.policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "execute-api:Invoke",
                    "Resource": "*",
                    "Condition": {"StringNotEquals": {"aws:SourceVpce": "vpce-123"}},
                }
            ],
        }
        self.endpoint = {
            "VpcEndpointId": "vpce-123",
            "VpcId": "vpc-123",
            "State": "available",
            "PolicyDocument": json.dumps(self.policy),
        }
        self.api = {
            "id": "api123",
            "name": "fixture-knowledge-private",
            "endpointConfiguration": {"types": ["PRIVATE"], "vpcEndpointIds": ["vpce-123"]},
            "policy": json.dumps(self.policy),
        }
        self.deployments = {"original": {"id": "original", "document": copy.deepcopy(self.document)}}
        self.stages = {"dev": {"stageName": "dev", "deploymentId": "original"}}
        self.targets["knowledge"] = {
            "targetId": "knowledge",
            "name": "knowledge",
            "status": "READY",
            "privateEndpoint": {"managedVpcResource": {"vpcIdentifier": "vpc-123"}},
            "credentialProviderConfigurations": copy.deepcopy(module.CREDENTIALS),
        }
        self.groups = {
            name: {
                "GroupId": name,
                "VpcId": "vpc-123",
                "OwnerId": "123456789012",
                "IpPermissions": [],
                "IpPermissionsEgress": [],
            }
            for name in ("sg-link", "sg-adapter")
        }
        self.inline, self.fail_once = None, None
        # The deployed runtime carries the foundation release group until the
        # fenced transition adopts the connector group, and it already holds the
        # deployment-owned connector configuration.
        self.runtime_release = {**copy.deepcopy(self.config["release"]), "group": "atlassian-foundation"}
        self.runtime_settings = runtime_configuration(self.runtime_release)
        self.catalogue_maps = {name: {} for name in ("gateway", "facade")}
        self.catalogue_deployments = {
            name: {
                "metadata": {"resourceVersion": "1"},
                "spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": name, "env": []}]}}},
            }
            for name in ("gateway", "facade")
        }

    def clients(self, _):
        return self

    def backend(self, config, env):
        backend = self.module.Backend.__new__(self.module.Backend)
        backend.selectors, backend.plan, backend.env = config["selectors"], config["plan"], env
        backend.api = backend.targets = self
        backend.kubectl = self.kubectl
        return backend

    def preparer(self):
        return self.module.Preparer(
            self.config, self.env, clients=self.clients, backend_factory=self.backend, sleep=lambda *_: None
        )

    def record(self, action):
        self.calls.append(action)
        if self.fail_once == action:
            self.fail_once = None
            raise OSError("lost response")

    def get_caller_identity(self):
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-tools-handoff-trusted-deployment/fixture",
        }

    def get_rest_api(self, **_):
        return copy.deepcopy(self.api)

    def get_stage(self, stageName, **_):
        if stageName not in self.stages:
            raise missing("NotFoundException")
        return copy.deepcopy(self.stages[stageName])

    def get_export(self, stageName, **_):
        document = copy.deepcopy(self.deployments[self.stages[stageName]["deploymentId"]]["document"])
        document["x-amazon-apigateway-policy"] = json.loads(self.api["policy"])
        return {"body": io.BytesIO(json.dumps(document).encode())}

    def get_object(self, Key, **_):
        if Key == self.config["foundation_state"]["key"]:
            value = {
                "outputs": {
                    "private_edge": {
                        "value": {
                            "namespace": "adapters",
                            "ingress": "tool-adapters-private",
                            "hostname": self.balancers[1]["DNSName"],
                            "paths": [f"/internal/v1/tools/{name}/mcp" for name in self.module.CONNECTORS],
                        }
                    },
                    "observation_runtime": {
                        "value": {"edge_provenance_sha256": hashlib.sha256(ADAPTER_EDGE.encode()).hexdigest()}
                    },
                },
                "resources": [
                    {
                        "mode": "managed",
                        "type": "random_password",
                        "name": "observation_edge",
                        "instances": [{"index_key": "runtime", "attributes": {"result": ADAPTER_EDGE}}],
                    }
                ],
            }
            return {"Body": io.BytesIO(json.dumps(value).encode()), "ServerSideEncryption": "AES256"}
        value = self.objects[Key]
        return {"Body": io.BytesIO(value["Body"]), "ETag": value["ETag"]}

    def list_objects_v2(self, Prefix, **_):
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}

    def put_object(self, Key, **args):
        if "IfNoneMatch" in args:
            assert Key not in self.objects
        else:
            assert args["IfMatch"] == self.objects[Key]["ETag"]
        assert args["ServerSideEncryption"] == "AES256"
        self.objects[Key] = {**args, "ETag": str(len(self.calls))}
        self.record("journal")
        return {"ETag": self.objects[Key]["ETag"]}

    def describe_load_balancers(self, **_):
        return {"LoadBalancers": self.balancers}

    def get_vpc_link(self, **_):
        return {
            "VpcLinkId": "link123",
            "VpcLinkStatus": "AVAILABLE",
            "SecurityGroupIds": ["sg-link"],
            "SubnetIds": ["subnet-a", "subnet-b"],
        }

    def describe_security_groups(self, GroupIds):
        return {"SecurityGroups": [copy.deepcopy(self.groups[key]) for key in GroupIds]}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        self.groups[GroupId]["IpPermissions"].extend(IpPermissions)
        self.record("ingress")

    def authorize_security_group_egress(self, GroupId, IpPermissions):
        self.groups[GroupId]["IpPermissionsEgress"].extend(IpPermissions)
        self.record("egress")

    def get_gateway(self, **_):
        return {
            "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway123",
            "status": "READY",
            "roleArn": self.role,
        }

    def list_gateway_targets(self, **_):
        return {"items": copy.deepcopy(list(self.targets.values()))}

    def get_gateway_target(self, targetId, **_):
        return copy.deepcopy(self.targets[targetId])

    def create_gateway_target(self, name, **args):
        self.targets[name] = {
            "name": name,
            "targetId": name,
            "status": "READY",
            **{
                key: value
                for key, value in args.items()
                if key in ("targetConfiguration", "credentialProviderConfigurations", "privateEndpoint")
            },
        }
        self.record("target-" + name)
        return {"targetId": name}

    def describe_vpc_endpoints(self, **_):
        return {"VpcEndpoints": [copy.deepcopy(self.endpoint)]}

    def modify_vpc_endpoint(self, PolicyDocument, **_):
        self.endpoint["PolicyDocument"] = PolicyDocument
        self.record("endpoint-policy")

    def update_rest_api(self, patchOperations, **_):
        self.api["policy"] = patchOperations[0]["value"]
        self.record("api-policy")

    def get_role_policy(self, **_):
        if self.inline is None:
            raise missing("NoSuchEntity")
        return {"PolicyDocument": copy.deepcopy(self.inline)}

    def put_role_policy(self, PolicyDocument, **_):
        self.inline = json.loads(PolicyDocument)
        self.record("role-policy")

    def get_deployments(self, **_):
        return {
            "items": [
                {key: value for key, value in item.items() if key != "document"} for item in self.deployments.values()
            ]
        }

    def put_rest_api(self, body, **_):
        self.document = json.loads(body)
        self.record("api-body")

    def create_deployment(self, description, **_):
        identifier = "dep" + str(len(self.deployments))
        self.deployments[identifier] = {
            "id": identifier,
            "description": description,
            "document": copy.deepcopy(self.document),
        }
        self.record("deployment")
        return {"id": identifier}

    def create_stage(self, stageName, deploymentId, description, **_):
        self.stages[stageName] = {"stageName": stageName, "deploymentId": deploymentId, "description": description}
        self.record("stage")
        return copy.deepcopy(self.stages[stageName])

    def update_stage(self, stageName, patchOperations, **_):
        self.stages[stageName]["deploymentId"] = patchOperations[0]["value"]
        self.record("select-baseline")

    def kubectl(self, *args, mutation=False):
        if args[1] in self.catalogue_maps:
            return self.catalogue_kubectl(*args, mutation=mutation)
        kind = args[3]
        if kind == "ingress":
            return json.dumps({"status": {"loadBalancer": {"ingress": self.ingress_endpoints}}})
        if kind.startswith("configmap"):
            return json.dumps(
                {
                    "metadata": {"resourceVersion": "1"},
                    "data": {"config.json": json.dumps(self.runtime_settings)},
                }
            )
        return json.dumps(
            {
                "metadata": {"generation": 1},
                "status": {
                    "observedGeneration": 1,
                    "replicas": 2,
                    "updatedReplicas": 2,
                    "readyReplicas": 2,
                    "availableReplicas": 2,
                },
                "spec": {
                    "replicas": 2,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/adapter@"
                                    + self.runtime_release["image_digest"]
                                }
                            ],
                            "volumes": [{"configMap": {"name": "tool-adapters-contract"}}],
                        }
                    },
                },
            }
        )

    def catalogue_kubectl(self, *args, mutation=False):
        namespace, action, resource = args[1:4]
        is_map = resource == "configmap/adp-tool-adapter-runtime"
        document = (
            {"metadata": {"resourceVersion": "1"}, "data": self.catalogue_maps[namespace]}
            if is_map
            else self.catalogue_deployments[namespace]
        )
        if action == "get":
            return json.dumps(document)
        assert action == "patch" and mutation
        changes = json.loads(next(value.removeprefix("--patch=") for value in args if value.startswith("--patch=")))
        for change in changes:
            keys = change["path"].strip("/").split("/")
            target = document
            for key in keys[:-1]:
                target = target[int(key)] if isinstance(target, list) else target[key]
            if change["op"] == "test":
                assert target[keys[-1]] == change["value"]
            else:
                assert change["op"] == "add"
                target[keys[-1]] = change["value"]
        if is_map:
            self.catalogue_maps[namespace] = document["data"]
        return ""


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("prepared_subject", ROOT / "prepare_handoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = Cloud(module)
    monkeypatch.setattr(module.guard_release, "authorize", lambda *_: None)
    return value


def test_prepares_private_targets_and_stages_without_switching_to_adapter(cloud):
    knowledge = copy.deepcopy(cloud.targets["knowledge"])
    result = cloud.preparer().prepare()
    assert cloud.stages["dev"]["deploymentId"] == result["plan"]["before"]["deployment_id"]
    for name in cloud.module.CONNECTORS:
        target = cloud.targets[name]
        assert target["credentialProviderConfigurations"] == cloud.module.CREDENTIALS
        assert target["targetConfiguration"]["mcp"]["mcpServer"]["listingMode"] == "DYNAMIC"
    assert cloud.targets["knowledge"] == knowledge
    assert json.loads(cloud.api["policy"])["Statement"][0] == cloud.policy["Statement"][0]
    assert all(len(group["IpPermissions"] + group["IpPermissionsEgress"]) == 1 for group in cloud.groups.values())
    before_calls = list(cloud.calls)
    assert cloud.preparer().prepare() == result
    assert cloud.calls[len(before_calls) :] == ["journal"]


def test_prepared_connector_headers_pass_the_actual_adapter_authorization(cloud):
    from fastapi import HTTPException
    from starlette.requests import Request

    from tool_adapters.atlassian_route import require_gateway
    from tool_adapters.config import AdapterConfiguration

    result = cloud.preparer().prepare()
    candidate = cloud.deployments[result["plan"]["after"]["deployment_id"]]["document"]
    configured = AdapterConfiguration.model_validate(cloud.runtime_settings)

    def request(proof):
        return Request(
            {
                "type": "http",
                "headers": [
                    (b"x-adp-edge-provenance", proof.encode()),
                    (b"x-caller-identity", b"arn:aws:sts::123456789012:assumed-role/adp-dev-gateway/agentcore"),
                ],
            }
        )

    # The old knowledge header is a valid, distinct edge secret, but the
    # destination adapter must reject it. The prepared four routes must work.
    with pytest.raises(HTTPException) as error:
        require_gateway(request(KNOWLEDGE_EDGE), configured)
    assert error.value.status_code == 403
    for name in cloud.module.CONNECTORS:
        integration = candidate["paths"][f"/internal/v1/tools/{name}/mcp"]["post"]["x-amazon-apigateway-integration"]
        headers = integration["requestParameters"]
        assert headers["integration.request.header.X-Caller-Identity"] == "context.identity.userArn"
        require_gateway(request(headers["integration.request.header.X-Adp-Edge-Provenance"].strip("'")), configured)
    assert (
        candidate["paths"]["/internal/v1/tools/knowledge/mcp"]
        == cloud.document["paths"]["/internal/v1/tools/knowledge/mcp"]
    )


def test_preparation_refuses_edge_secret_that_differs_from_the_running_adapter(cloud):
    cloud.runtime_settings["observation"]["edge_provenance_sha256"] = hashlib.sha256(
        KNOWLEDGE_EDGE.encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        cloud.preparer().prepare()
    assert not set(cloud.calls) - {"journal"}
    assert set(cloud.targets) == {"knowledge"}


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "changed", "unsafe"])
def test_preparation_refuses_unverified_foundation_edge_secret(cloud, monkeypatch, fault):
    original = cloud.get_object

    def read(Key, **kwargs):
        result = original(Key, **kwargs)
        if Key == cloud.config["foundation_state"]["key"]:
            state = json.loads(result["Body"].read())
            if fault == "missing":
                state["resources"] = []
            elif fault == "ambiguous":
                state["resources"] *= 2
            else:
                state["resources"][0]["instances"][0]["attributes"]["result"] = (
                    KNOWLEDGE_EDGE if fault == "changed" else "'" * 64
                )
            result["Body"] = io.BytesIO(json.dumps(state).encode())
        return result

    monkeypatch.setattr(cloud, "get_object", read)
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        cloud.preparer().prepare()
    assert cloud.calls == []


def test_prepares_against_configured_foundation_image_before_the_fenced_group_change(cloud):
    assert cloud.runtime_release["group"] == "atlassian-foundation"
    assert cloud.config["release"]["group"] == "tool-connectors"
    result = cloud.preparer().prepare()
    assert result["plan"]["after"]["release"] == cloud.config["release"]
    assert result["plan"]["before"]["release"] is None
    # Preparation observes the deployed foundation image and never adopts the
    # connector release group itself; only the fenced transition may do that.
    assert cloud.runtime_release["group"] == "atlassian-foundation"
    assert cloud.stages["dev"]["deploymentId"] == result["plan"]["before"]["deployment_id"]


def test_prepares_with_kubernetes_ingress_port_metadata(cloud):
    cloud.ingress_endpoints[0]["ports"] = [{"port": 80, "protocol": ""}]
    result = cloud.preparer().prepare()
    assert result["selectors"]["adapter_host"] == cloud.balancers[1]["DNSName"]
    assert set(cloud.targets) == cloud.module.CONNECTORS | {"knowledge"}


@pytest.mark.parametrize(
    "endpoints",
    [
        [],
        [{"hostname": "foreign.example"}],
        [{"ip": "10.0.0.1"}],
        [{"hostname": "internal-adapter.elb.amazonaws.com", "ip": "10.0.0.1"}],
        [{"hostname": "internal-adapter.elb.amazonaws.com"}, {"hostname": "foreign.example"}],
    ],
)
def test_refuses_changed_or_ambiguous_ingress_before_preparation_mutations(cloud, endpoints):
    cloud.ingress_endpoints = endpoints
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        cloud.preparer().prepare()
    assert not set(cloud.calls) - {"journal"}
    assert set(cloud.targets) == {"knowledge"}


@pytest.mark.parametrize(
    "fault",
    [
        "foundation-mode",
        "missing-observation",
        "missing-read-schemas",
        "missing-legacy-workload",
        "unauthenticated-schema",
        "foreign-group",
    ],
)
def test_refuses_preparation_without_complete_connector_configuration(cloud, fault):
    settings = cloud.runtime_settings
    if fault == "foundation-mode":
        settings["mode"] = "foundation"
    elif fault == "missing-observation":
        settings["observation"] = None
    elif fault == "missing-read-schemas":
        settings["observation"].pop("read_schemas")
        settings["observation"].pop("read_schemas_sha256")
    elif fault == "missing-legacy-workload":
        settings["observation"].pop("legacy_identity_workload")
    elif fault == "unauthenticated-schema":
        alias = sorted(settings["observation"]["read_schemas"])[0]
        settings["observation"]["read_schemas"][alias]["properties"]["injected"] = {"type": "string"}
    else:
        cloud.runtime_release["group"] = "tool-foreign"
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        cloud.preparer().prepare()
    # The refusal precedes every route, target and permission mutation.
    assert not set(cloud.calls) - {"journal"}
    assert set(cloud.targets) == {"knowledge"}
    assert set(cloud.stages) == {"dev"} and cloud.inline is None


def outage_runtime(cloud, monkeypatch):
    import handoff_release

    prepared = cloud.preparer().prepare()
    cloud.stages["dev"]["deploymentId"] = prepared["plan"]["after"]["deployment_id"]
    core = Transitions(handoff_release)
    core(action="fence", plan=prepared["plan"])
    core(action="activate", plan=prepared["plan"], observed=prepared["plan"]["after"])
    core.calls.clear()
    cloud.calls.clear()
    native_kubectl = cloud.kubectl
    core_settings = {}

    def kubectl(*args, mutation=False):
        if args[1] in cloud.catalogue_maps and (args[3] == "configmap/adp-tool-adapter-runtime" or args[2] == "patch"):
            return cloud.catalogue_kubectl(*args, mutation=mutation)
        if args[1] == "adapters":
            if args[2] == "get" and args[3].startswith("configmap"):
                return json.dumps(
                    {
                        "metadata": {"resourceVersion": "1"},
                        "data": {"config.json": json.dumps(cloud.runtime_settings)},
                    }
                )
            if args[2] == "patch" or args[2:4] == ("rollout", "restart"):
                cloud.calls.append("adapter-mutation")
                return ""
            if args[2:4] == ("set", "env"):
                assert mutation and core.state["status"] == "fenced"
                flags = dict(item.split("=", 1) for item in args if item.startswith("ADP_TOOL_ADAPTER_"))
                cloud.calls.append("adapter-legacy:" + flags["ADP_TOOL_ADAPTER_LEGACY_ENABLED"])
                return ""
            if args[2:4] == ("rollout", "status"):
                raise OSError("adapter rollout unavailable")
            value = json.loads(native_kubectl(*args))
            if "status" in value and "readyReplicas" in value["status"]:
                value["status"].update(readyReplicas=0, availableReplicas=0)
                return json.dumps(value)
            raise OSError("adapter unavailable")
        selected = prepared["selectors"]["workloads"][args[1]]
        if args[2:4] == ("set", "env"):
            assert mutation and core.state["status"] == "fenced"
            core_settings[args[1]] = dict(item.split("=", 1) for item in args if item.startswith("ADP_TOOL_ADAPTER_"))
            current = cloud.catalogue_deployments[args[1]]["spec"]["template"]["spec"]["containers"][0]
            current["env"] = [value for value in current["env"] if value["name"] not in core_settings[args[1]]]
            current["env"].extend({"name": name, "value": value} for name, value in core_settings[args[1]].items())
            cloud.calls.append("configure-" + args[1] + ":" + core_settings[args[1]]["ADP_TOOL_ADAPTER_LEGACY_ENABLED"])
            return ""
        if args[2:4] == ("rollout", "status"):
            return ""
        assert args[2] == "get"
        assert selected["container"] == args[1]
        return cloud.catalogue_kubectl(*args)

    def authorize(environment, **arguments):
        if "transition" in arguments:
            return core(**arguments["transition"])

    monkeypatch.setattr(cloud, "kubectl", kubectl)
    monkeypatch.setattr(cloud.module.guard_release, "authorize", authorize)
    return prepared, core, core_settings


def test_switcher_restores_legacy_routes_without_waiting_for_failed_adapter(cloud, monkeypatch):
    import handoff_release

    prepared, core, settings = outage_runtime(cloud, monkeypatch)
    backend = cloud.backend(prepared, cloud.env)
    result = handoff_release.execute(prepared, backend, core, rollback=True, clock=lambda: 150)
    assert result["transition"]["phase"] == "restored"
    expected = handoff_release.restored(prepared["plan"])
    assert backend.observe("dev", expected) == expected
    assert all(json.loads(value["ADP_TOOL_ADAPTER_RELEASE"]) == expected["release"] for value in settings.values())
    # Rollback restores legacy routing and leaves the connectors disabled.
    assert all(value["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] == "false" for value in settings.values())
    assert core.calls == ["read", "rollback", "restore"]


def test_failed_adapter_activation_restores_legacy_routes_but_reports_failure(cloud, monkeypatch):
    import handoff_release

    prepared, core, settings = outage_runtime(cloud, monkeypatch)
    cloud.stages["dev"]["deploymentId"] = prepared["plan"]["before"]["deployment_id"]
    core.state = None
    backend = cloud.backend(prepared, cloud.env)
    with pytest.raises(OSError, match="adapter rollout unavailable"):
        handoff_release.execute(prepared, backend, core, clock=lambda: 150)
    expected = handoff_release.restored(prepared["plan"])
    assert core.calls == ["read", "fence", "rollback", "restore"]
    assert core.state["status"] == "legacy"
    assert core.state["release"] == expected["release"]
    assert backend.observe("dev", expected) == expected
    assert set(settings) == {"gateway", "facade"}
    assert all(json.loads(value["ADP_TOOL_ADAPTER_RELEASE"]) == expected["release"] for value in settings.values())
    assert all(value["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] == "false" for value in settings.values())
    assert cloud.calls.count("adapter-mutation") == 2


def test_fenced_transition_enables_legacy_connectors_then_disables_them_on_rollback(cloud, monkeypatch):
    import handoff_release

    prepared, core, settings = outage_runtime(cloud, monkeypatch)
    cloud.stages["dev"]["deploymentId"] = prepared["plan"]["before"]["deployment_id"]
    core.state = None
    with pytest.raises(OSError, match="adapter rollout unavailable"):
        handoff_release.execute(prepared, cloud.backend(prepared, cloud.env), core, clock=lambda: 150)
    flags = [call for call in cloud.calls if call.startswith("configure-")]
    # The legacy connectors reach the adapter only inside the fence, and the
    # restore path disables them again, so legacy routing is always retained.
    assert len(flags) == 4
    assert {call.split(":")[0] for call in flags[:2]} == {"configure-gateway", "configure-facade"}
    assert {call.split(":")[1] for call in flags[:2]} == {"true"}
    assert {call.split(":")[0] for call in flags[2:]} == {"configure-gateway", "configure-facade"}
    assert {call.split(":")[1] for call in flags[2:]} == {"false"}
    # The adapter workload accepts legacy connectors only while fenced.
    assert cloud.calls.count("adapter-legacy:true") == 1
    assert "adapter-legacy:false" not in cloud.calls
    assert all(value["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] == "false" for value in settings.values())


def test_fenced_transition_refuses_connectors_without_configured_legacy_authority(cloud, monkeypatch):
    import handoff_release

    prepared, core, settings = outage_runtime(cloud, monkeypatch)
    cloud.stages["dev"]["deploymentId"] = prepared["plan"]["before"]["deployment_id"]
    core.state = None
    cloud.runtime_settings["observation"].pop("legacy_identity_workload")
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        handoff_release.execute(prepared, cloud.backend(prepared, cloud.env), core, clock=lambda: 150)
    # The deployed contract is validated before any workload or adapter change,
    # so an unauthorized runtime never receives the connector release group.
    assert core.calls == ["read", "fence", "rollback", "restore"]
    assert "adapter-mutation" not in cloud.calls and "adapter-legacy:true" not in cloud.calls
    assert [call for call in cloud.calls if call.startswith("configure-")] == [
        "configure-" + name + ":false" for name in prepared["selectors"]["workloads"]
    ]
    assert all(value["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] == "false" for value in settings.values())


@pytest.mark.parametrize("workload", ["gateway", "facade"])
def test_core_rollout_failure_keeps_rollback_fenced_until_verified_retry(cloud, monkeypatch, workload):
    import handoff_release

    prepared, core, _ = outage_runtime(cloud, monkeypatch)
    kubectl = cloud.kubectl

    def unavailable_core(*args, **kwargs):
        if args[1] == workload and args[2:4] == ("rollout", "status"):
            raise OSError("core rollout unavailable")
        return kubectl(*args, **kwargs)

    monkeypatch.setattr(cloud, "kubectl", unavailable_core)
    with pytest.raises(OSError, match="core rollout unavailable"):
        handoff_release.execute(prepared, cloud.backend(prepared, cloud.env), core, rollback=True, clock=lambda: 150)
    assert core.state["status"] == "fenced"
    assert core.state["transition"]["phase"] == "rollback"
    assert core.calls == ["read", "rollback"]
    assert cloud.stages["dev"]["deploymentId"] == prepared["plan"]["after"]["deployment_id"]
    assert "adapter-mutation" not in cloud.calls
    monkeypatch.setattr(cloud, "kubectl", kubectl)
    result = handoff_release.execute(
        prepared, cloud.backend(prepared, cloud.env), core, rollback=True, clock=lambda: 150
    )
    assert result["transition"]["phase"] == "restored"
    assert cloud.stages["dev"]["deploymentId"] == prepared["plan"]["before"]["deployment_id"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_rollback_commands_restore_original_routes_during_adapter_outage(cloud, monkeypatch, tmp_path, interrupted):
    import handoff_release

    prepared, core, settings = outage_runtime(cloud, monkeypatch)
    preparer = cloud.preparer()
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        preparer.verify_runtime(cloud.backend(prepared, cloud.env))
    input_file, plan_file, result_file = (tmp_path / name for name in ("input.json", "plan.json", "result.json"))
    cloud.module.private_write(input_file, cloud.config)
    monkeypatch.setattr(cloud.module, "Preparer", lambda *_: preparer)
    monkeypatch.setattr(
        sys, "argv", ["prepare_handoff.py", "--config", str(input_file), "--output", str(plan_file), "--rollback"]
    )
    cloud.module.main()
    assert json.loads(plan_file.read_bytes()) == prepared
    assert cloud.calls == []
    for key, value in cloud.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_HANDOFF_FILE", str(plan_file))
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_HANDOFF_RESULT", str(result_file))
    monkeypatch.setattr(handoff_release, "Backend", cloud.backend)
    monkeypatch.setattr(sys, "argv", ["handoff_release.py", "rollback"])
    if interrupted:
        cloud.fail_once = "select-baseline"
        with pytest.raises(SystemExit, match="Adapter handoff did not complete"):
            handoff_release.main()
        assert core.state["transition"]["phase"] == "rollback"
        assert not result_file.exists()
        assert preparer.rollback_plan() == prepared
    handoff_release.main()
    report = json.loads(result_file.read_bytes())
    expected = handoff_release.restored(prepared["plan"])
    assert report["observed"] == expected
    assert report["state"]["transition"]["phase"] == "restored"
    assert report["state"]["release"] == expected["release"]
    assert cloud.stages["dev"]["deploymentId"] == prepared["plan"]["before"]["deployment_id"]
    assert set(settings) == {"gateway", "facade"}
    assert all(json.loads(value["ADP_TOOL_ADAPTER_RELEASE"]) == expected["release"] for value in settings.values())
    assert all(value["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] == "false" for value in settings.values())
    assert "adapter-mutation" not in cloud.calls
    mutations = list(cloud.calls)
    assert preparer.rollback_plan() == prepared
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_HANDOFF_RESULT", str(tmp_path / "repeated.json"))
    handoff_release.main()
    assert cloud.calls == mutations
    assert json.loads((tmp_path / "repeated.json").read_bytes())["observed"] == expected


@pytest.mark.parametrize(
    "fault",
    [
        "missing-plan",
        "foreign-config",
        "foreign-transition",
        "missing-transition",
        "baseline-drift",
        "current-route",
        "identity",
        "authority",
    ],
)
def test_rollback_loader_refuses_unbound_or_changed_original_plan_without_mutation(cloud, monkeypatch, fault):
    prepared, core, _ = outage_runtime(cloud, monkeypatch)
    preparer = cloud.preparer()
    if fault in {"missing-plan", "foreign-config"}:
        journal = json.loads(cloud.objects[preparer.key]["Body"])
        if fault == "missing-plan":
            journal.pop("handoff")
        else:
            journal["config"]["api_id"] = "foreign"
        cloud.objects[preparer.key]["Body"] = json.dumps(journal).encode()
    elif fault == "foreign-transition":
        core.state["transition"]["plan"]["stage"] = "foreign"
    elif fault == "missing-transition":
        core.state = None
    elif fault == "baseline-drift":
        cloud.deployments[prepared["plan"]["before"]["deployment_id"]]["document"]["paths"].pop(
            "/internal/v1/tools/jira/mcp"
        )
    elif fault == "current-route":
        cloud.stages["dev"]["deploymentId"] = "foreign"
    elif fault == "identity":
        monkeypatch.setattr(
            cloud,
            "get_caller_identity",
            lambda: {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/other/session"},
        )
    else:
        monkeypatch.setattr(
            cloud.module.guard_release,
            "authorize",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("lease denied")),
        )
    with pytest.raises(ValueError):
        preparer.rollback_plan()
    assert cloud.calls == []


@pytest.mark.parametrize(
    "operation", ["journal", "target-atlassian", "ingress", "endpoint-policy", "deployment", "stage", "select-baseline"]
)
@pytest.mark.parametrize("new_attempt", [False, True])
def test_response_loss_reconciles_original_resources_without_duplicates(cloud, operation, new_attempt):
    cloud.fail_once = operation
    with pytest.raises(OSError):
        cloud.preparer().prepare()
    original_owner = copy.deepcopy(cloud.preparer().owner)
    if new_attempt:
        cloud.env["ADP_CORRELATION"] = "orch:66666666-2222-3333-4444-555555555555"
    result = cloud.preparer().prepare()
    assert json.loads(cloud.objects[cloud.preparer().key]["Body"])["owner"] == original_owner
    assert set(cloud.targets) == cloud.module.CONNECTORS | {"knowledge"}
    assert len(cloud.deployments) == 3 and len(cloud.stages) == 3
    assert cloud.stages["dev"]["deploymentId"] == result["plan"]["before"]["deployment_id"]


@pytest.mark.parametrize("fault", ["authority", "configuration"])
def test_new_attempt_cannot_resume_without_current_authority_and_same_configuration(cloud, monkeypatch, fault):
    cloud.fail_once = "journal"
    with pytest.raises(OSError):
        cloud.preparer().prepare()
    cloud.env["ADP_CORRELATION"] = "orch:66666666-2222-3333-4444-555555555555"
    if fault == "authority":
        monkeypatch.setattr(
            cloud.module.guard_release, "authorize", lambda *_: (_ for _ in ()).throw(ValueError("lease denied"))
        )
    else:
        cloud.config["workloads"]["gateway"]["container"] = "foreign"
    before = copy.deepcopy(cloud.objects)
    cloud.calls.clear()
    with pytest.raises(ValueError):
        cloud.preparer().prepare()
    assert not cloud.calls and cloud.objects == before


@pytest.mark.parametrize(
    "fault", ["identity", "runtime-source", "runtime-image", "credential", "target", "ready", "guard"]
)
def test_refuses_foreign_or_unready_prerequisites(cloud, monkeypatch, fault):
    if fault == "identity":
        monkeypatch.setattr(
            cloud,
            "get_caller_identity",
            lambda: {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/other/session"},
        )
    elif fault.startswith("runtime"):
        cloud.runtime_release["source_revision" if fault.endswith("source") else "image_digest"] = "foreign"
    elif fault == "credential":
        cloud.targets["knowledge"]["credentialProviderConfigurations"][0].pop("credentialProvider")
    elif fault in {"target", "ready"}:
        cloud.preparer().prepare()
        cloud.calls.clear()
        cloud.targets["jira"]["status" if fault == "ready" else "privateEndpoint"] = (
            "FAILED" if fault == "ready" else {}
        )
    else:
        monkeypatch.setattr(
            cloud.module.guard_release, "authorize", lambda *_: (_ for _ in ()).throw(ValueError("lease denied"))
        )
    with pytest.raises(ValueError):
        cloud.preparer().prepare()
    assert not set(cloud.calls) - {"journal"}


def test_repeated_authorized_flow_reuses_completed_preparation_and_never_reselects(cloud):
    prepared = cloud.preparer().prepare()
    cloud.stages["dev"]["deploymentId"] = prepared["plan"]["after"]["deployment_id"]
    cloud.env["ADP_CORRELATION"] = "orch:66666666-2222-3333-4444-555555555555"
    before = len(cloud.calls)
    assert cloud.preparer().prepare() == prepared
    assert cloud.calls[before:] == ["journal"]


@pytest.fixture
def retention():
    path = ROOT.parent / "platform/knowledge-edge/retained_handoff.py"
    spec = importlib.util.spec_from_file_location("retained_subject", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prepared", [False, True])
def test_first_create_can_resume_missing_live_stage_but_cannot_discard_preparation(retention, prepared):
    query = {
        "region": "us-east-1",
        "gateway_role_arn": "arn:aws:iam::123456789012:role/gateway",
        "name": "fixture-api",
        "stage": "dev",
    }

    def call(service, operation, **args):
        if operation == "get-caller-identity":
            return {"Account": "123456789012"}
        if operation == "get-rest-apis":
            return {"items": [{"name": "fixture-api", "id": "api123"}]}
        assert operation == "get-stages"
        return {
            "item": [{"stageName": "candidate", "description": "adp-adapter-preparation-fixture"}] if prepared else []
        }

    if prepared:
        with pytest.raises(ValueError):
            retention.read_selection(query, call)
    else:
        assert retention.read_selection(query, call)["prepared"] == "false"


@pytest.mark.parametrize("active", [False, True])
def test_owning_terraform_readback_retains_prepared_or_active_routes(cloud, retention, active):
    specification = copy.deepcopy(cloud.document)
    prepared = cloud.preparer().prepare()
    if active:
        cloud.stages["dev"]["deploymentId"] = prepared["plan"]["after"]["deployment_id"]
    query = {
        "region": "us-east-1",
        "gateway_role_arn": cloud.role,
        "name": cloud.api["name"],
        "stage": "dev",
        "endpoint_id": "vpce-123",
        "specification": json.dumps(specification),
    }

    def call(service, operation, **args):
        if operation == "get-caller-identity":
            return cloud.get_caller_identity()
        if operation == "get-rest-apis":
            return {"items": [cloud.api]}
        if operation == "get-stages":
            return {"item": list(cloud.stages.values())}
        if operation == "get-deployment":
            return cloud.deployments[args["deployment_id"]]
        if operation == "get-export":
            return copy.deepcopy(cloud.deployments[cloud.stages["dev"]["deploymentId"]]["document"])
        if operation == "describe-vpc-endpoints":
            return cloud.describe_vpc_endpoints()
        return cloud.get_stage(stageName=args["stage_name"])

    result = retention.read_selection(query, call)
    assert result["prepared"] == "true"
    assert result["deployment_id"] == cloud.stages["dev"]["deploymentId"]
    assert json.loads(result["api_policy"]) == json.loads(cloud.api["policy"])
    assert json.loads(result["endpoint_policy"]) == json.loads(cloud.endpoint["PolicyDocument"])
    expected_paths = {
        f"/internal/v1/tools/{name}/mcp"
        for name in cloud.module.CONNECTORS | {"knowledge"}
        if active or name != "atlassian"
    }
    assert set(json.loads(result["document"])["paths"]) == expected_paths
    specification["paths"]["/internal/v1/tools/knowledge/mcp"]["post"]["security"] = []
    query["specification"] = json.dumps(specification)
    with pytest.raises(ValueError):
        retention.read_selection(query, call)


def transport(value, request_id):
    """Model a real boto3 response: the stage plus its per-call transport metadata.

    The modeled AWS CLI responses elsewhere in this module carry no
    ResponseMetadata, so these fixtures reproduce what boto3 actually returns
    to preparation and to the fenced transition.
    """
    return {
        **copy.deepcopy(value),
        "ResponseMetadata": {
            "RequestId": request_id,
            "HTTPStatusCode": 200,
            "HTTPHeaders": {"x-amzn-requestid": request_id, "content-type": "application/json"},
            "RetryAttempts": 0,
        },
    }


def varying_stage_reads(cloud, requests, *, redeploy=None):
    """Return a get_stage that mirrors boto3 by issuing a fresh RequestId per call.

    With redeploy set, the stage is actually redeployed after the first read so
    the readback still has to refuse a changed deployment.
    """
    original = cloud.get_stage

    def get_stage(stageName, **arguments):
        request_id = f"{len(requests):08x}-7b2a-4f1e-9c3d-5e6f70819a2b"
        value = transport(original(stageName=stageName, **arguments), request_id)
        requests.append(request_id)
        if redeploy is not None and len(requests) == 1:
            cloud.stages[stageName]["deploymentId"] = redeploy
        return value

    return get_stage


def stage_backend(cloud):
    plan = {"api_id": cloud.config["api_id"], "stage": cloud.config["stage"]}
    return cloud.backend({"selectors": configuration()["selectors"], "plan": plan}, cloud.env)


@pytest.mark.parametrize("subject", ["preparation", "transition"])
def test_stage_readback_confirms_an_unchanged_stage_across_fresh_request_ids(cloud, subject):
    """Transport metadata alone must not look like a stage that changed under us."""
    requests = []
    cloud.get_stage = varying_stage_reads(cloud, requests)
    if subject == "preparation":
        selected, document = cloud.preparer().read(cloud.config["stage"])
        assert set(document["paths"]) == {"/internal/v1/tools/knowledge/mcp"}
        deployment = selected["deploymentId"]
    else:
        deployment, document = stage_backend(cloud).export(cloud.config["stage"])
        assert type(document["paths"]) is dict
    assert deployment == cloud.stages[cloud.config["stage"]]["deploymentId"] == "original"
    assert len(requests) == 2 and len(set(requests)) == 2
    assert cloud.calls == []


@pytest.mark.parametrize("subject", ["preparation", "transition"])
def test_stage_readback_still_refuses_an_actual_deployment_change_between_reads(cloud, subject):
    """A real redeployment between the two reads must keep refusing the handoff."""
    stage = cloud.config["stage"]
    cloud.deployments["later"] = {"id": "later", "document": copy.deepcopy(cloud.document)}
    requests = []
    cloud.get_stage = varying_stage_reads(cloud, requests, redeploy="later")
    read = cloud.preparer().read if subject == "preparation" else stage_backend(cloud).export
    with pytest.raises(ValueError):
        read(stage)
    assert len(requests) == 2 and cloud.stages[stage]["deploymentId"] == "later"
    assert cloud.calls == []


def test_preparation_accepts_aws_escaped_policy_without_replacing_existing_denials(cloud, monkeypatch):
    read = cloud.get_rest_api

    def aws_encoded_policy(**kwargs):
        result = read(**kwargs)
        result["policy"] = json.dumps(result["policy"])[1:-1]
        return result

    monkeypatch.setattr(cloud, "get_rest_api", aws_encoded_policy)
    original = copy.deepcopy(cloud.policy)
    result = cloud.preparer().prepare()
    after = json.loads(cloud.api["policy"])
    assert after["Statement"][:-1] == original["Statement"]
    assert after["Statement"][-1]["Sid"] == "ADPAdapterPreparedConnectors"
    calls = list(cloud.calls)
    assert cloud.preparer().prepare() == result
    assert cloud.calls[len(calls) :] == ["journal"]


def restored_baseline(cloud):
    original = cloud.preparer().prepare()
    previous = {**original["plan"]["after"]["release"], "group_generation": 2, "backend_generation": 2}
    baseline_id = original["plan"]["before"]["deployment_id"]
    cloud.stages["dev"]["deploymentId"] = baseline_id
    for values in cloud.catalogue_maps.values():
        values.update(ADP_TOOL_ADAPTER_RELEASE=json.dumps(previous), ADP_TOOL_ADAPTER_LEGACY_ENABLED="false")
    cloud.config["release"].update(
        source_revision="e" * 40, image_digest="sha256:" + "f" * 64, group_generation=3, backend_generation=3
    )
    cloud.env["ADP_SOURCE_REVISION"] = "e" * 40
    cloud.env["ADP_CORRELATION"] = "orch:22222222-2222-3333-4444-555555555555"
    cloud.runtime_release.update(cloud.config["release"], group="atlassian-foundation")
    cloud.runtime_settings = runtime_configuration(cloud.runtime_release)
    cloud.calls.clear()
    return previous, baseline_id


def test_new_image_after_rollback_retains_exact_baseline_and_advances_generations(cloud):
    previous, baseline_id = restored_baseline(cloud)
    prepared = cloud.preparer().prepare()
    assert prepared["plan"]["before"]["release"] == previous
    assert prepared["plan"]["before"]["deployment_id"] == baseline_id
    assert cloud.stages["dev"]["deploymentId"] == baseline_id
    assert "select-baseline" not in cloud.calls
    assert prepared["plan"]["after"]["release"] == cloud.config["release"]
    assert prepared["plan"]["after"]["release"]["source_revision"] != previous["source_revision"]
    cloud.backend(prepared, cloud.env).verify_prepared(prepared["plan"])
    assert cloud.preparer().prepare() == prepared


@pytest.mark.parametrize("fault", ["active", "mixed", "missing", "stale-generation"])
def test_replacement_refuses_unrestored_or_inconsistent_baseline_before_route_mutation(cloud, fault):
    restored_baseline(cloud)
    values = next(iter(cloud.catalogue_maps.values()))
    if fault == "active":
        values["ADP_TOOL_ADAPTER_LEGACY_ENABLED"] = "true"
    elif fault == "mixed":
        prior = json.loads(values["ADP_TOOL_ADAPTER_RELEASE"])
        prior["source_revision"] = "d" * 40
        values["ADP_TOOL_ADAPTER_RELEASE"] = json.dumps(prior)
    elif fault == "missing":
        values.pop("ADP_TOOL_ADAPTER_RELEASE")
    else:
        cloud.config["release"]["group_generation"] = 1
    with pytest.raises(ValueError):
        cloud.preparer().prepare()
    assert not set(cloud.calls) - {"journal"}
