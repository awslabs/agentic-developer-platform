"""Authority-free live transport proof before enabling native operation delivery."""

import json
from urllib.parse import urlsplit
from uuid import uuid4

from . import native_egress
from .config import LABEL, digest, image, require


PROGRAM = """
import ipaddress,json,os,socket,ssl
v=json.loads(os.environ['ADP_NATIVE_NETWORK_PROBE'])
for host in v['https']:
    addresses=socket.getaddrinfo(host,443,family=socket.AF_INET,type=socket.SOCK_STREAM)
    expected=v.get('private_https',{}).get(host)
    if expected is not None:
        assert addresses and {item[4][0] for item in addresses}==set(expected), 'private service DNS differs from reviewed endpoint interfaces'
    else:
        assert addresses and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses), 'public service DNS resolved outside allowed public IPv4'
    with socket.create_connection(addresses[0][4],timeout=10) as transport:
        with ssl.create_default_context().wrap_socket(transport,server_hostname=host):
            pass
addresses=socket.getaddrinfo(v['database_host'],5432,family=socket.AF_INET,type=socket.SOCK_STREAM)
assert addresses and {item[4][0] for item in addresses}=={v['database_ip']}, 'database DNS differs from reviewed private peer'
for host,port in [v['gateway'],[v['database_host'],5432]]:
    with socket.create_connection((host,port),timeout=10):
        pass
print(json.dumps({'version':1,'reachable':True,'recipe_sha256':v['recipe_sha256']}))
"""


def verified_private_hosts(installer):
    env = installer.env
    endpoints = env["paid_worker"]["egress"].get("interface_endpoints", {})
    if "interface_endpoints" not in env["paid_worker"]["egress"]:
        return {}
    native_egress.validate_interface_endpoints(endpoints)
    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", env["cluster"])
    )["cluster"]
    require(
        cluster.get("arn")
        == f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['cluster']}"
        and cluster.get("status") == "ACTIVE",
        "Endpoint verification cluster differs",
    )
    vpc_id = cluster["resourcesVpcConfig"]["vpcId"]
    hosts = {}
    for service, selected in endpoints.items():
        require(selected["vpc_id"] == vpc_id, "Endpoint is outside the management VPC")
        result = installer.json(
            installer.aws(
                "ec2", "describe-vpc-endpoints", "--vpc-endpoint-ids", selected["id"]
            )
        )
        observed = result.get("VpcEndpoints", [])
        require(len(observed) == 1, "Endpoint identity is ambiguous")
        endpoint = observed[0]
        require(
            endpoint.get("VpcEndpointId") == selected["id"]
            and endpoint.get("VpcId") == vpc_id
            and endpoint.get("OwnerId") == env["account_id"]
            and endpoint.get("State") == "available"
            and endpoint.get("VpcEndpointType") == "Interface"
            and endpoint.get("PrivateDnsEnabled") is True
            and endpoint.get("ServiceName")
            == f"com.amazonaws.{env['region']}.{service}"
            and sorted(endpoint.get("NetworkInterfaceIds", []))
            == sorted(selected["network_interface_ids"]),
            "Live interface endpoint differs from reviewed identity",
        )
        result = installer.json(
            installer.aws(
                "ec2",
                "describe-network-interfaces",
                "--network-interface-ids",
                *selected["network_interface_ids"],
            )
        )
        interfaces = result.get("NetworkInterfaces", [])
        require(
            len(interfaces) == len(selected["network_interface_ids"])
            and {item.get("NetworkInterfaceId") for item in interfaces}
            == set(selected["network_interface_ids"]),
            "Endpoint interface set changed",
        )
        require(
            all(
                item.get("VpcId") == vpc_id
                and item.get("OwnerId") == env["account_id"]
                and item.get("Status") == "in-use"
                and item.get("InterfaceType") == "vpc_endpoint"
                for item in interfaces
            )
            and {item.get("PrivateIpAddress") for item in interfaces}
            == set(selected["ipv4_addresses"]),
            "Endpoint interface ownership or addresses changed",
        )
        hosts[f"{service}.{env['region']}.amazonaws.com"] = sorted(
            selected["ipv4_addresses"]
        )
    return hosts


def verify_network(installer):
    env = installer.env
    if not native_egress.enabled(env):
        return None
    worker = env["paid_worker"]
    rule_set = native_egress.rules(env)
    private_hosts = verified_private_hosts(installer)
    response = installer.json(
        installer.aws(
            "rds",
            "describe-db-instances",
            "--db-instance-identifier",
            env["database"]["identifier"],
        )
    )
    databases = response.get("DBInstances", [])
    require(
        len(databases) == 1
        and databases[0].get("DBInstanceIdentifier") == env["database"]["identifier"],
        "native network probe database identity differs",
    )
    endpoint = databases[0].get("Endpoint", {})
    require(
        isinstance(endpoint.get("Address"), str) and endpoint.get("Port") == 5432,
        "native network probe needs the selected database endpoint",
    )
    transport = env["api_adapters"]["vault"]["transport"]
    region = env["region"]
    targets = {
        "https": sorted(
            {
                *(
                    f"{service}.{region}.amazonaws.com"
                    for service in (
                        "sts",
                        "ec2",
                        "eks",
                        "logs",
                        "kms",
                        "s3",
                        "dynamodb",
                        "sqs",
                    )
                ),
                "iam.amazonaws.com",
                "registry.terraform.io",
                "releases.hashicorp.com",
                "checkip.amazonaws.com",
                urlsplit(env["api_adapters"]["dispatcher"]["endpoint"]).hostname,
                urlsplit(env["origin"]).hostname,
            }
        ),
        "gateway": [
            f"{transport['service']}.{transport['namespace']}.svc.cluster.local",
            transport["port"],
        ],
        "database_host": endpoint["Address"],
        "database_ip": worker["egress"]["database"]["cidr"].split("/")[0],
        "recipe_sha256": digest(rule_set),
    }
    if private_hosts:
        targets["private_https"] = private_hosts
    name = "superplane-network-probe-" + uuid4().hex[:12]
    labels = {
        LABEL: installer.owner,
        "app.kubernetes.io/name": "superplane-network-probe",
    }
    metadata = {"namespace": env["namespace"], "labels": labels}
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {**metadata, "name": "superplane-network-probe"},
        "automountServiceAccountToken": False,
    }
    existing = installer.existing(account)
    if existing is not None:
        require(
            existing.get("metadata", {}).get("labels", {}).get(LABEL) == installer.owner
            and not existing.get("metadata", {}).get("annotations")
            and existing.get("automountServiceAccountToken") is False,
            "network probe service account has foreign or additional authority",
        )
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {**metadata, "name": "superplane-network-probe"},
        "spec": {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": "superplane-network-probe"}
            },
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": rule_set,
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {**metadata, "name": name},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": min(env["timeout_seconds"], 300),
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "superplane-network-probe",
                    "automountServiceAccountToken": False,
                    "nodeSelector": worker["node_selector"],
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65531,
                        "runAsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image(installer.lock, "superplane-paid-worker"),
                            "command": ["/opt/executor/bin/python", "-c", PROGRAM],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                            "env": [
                                {
                                    "name": "ADP_NATIVE_NETWORK_PROBE",
                                    "value": json.dumps(targets, sort_keys=True),
                                }
                            ],
                        }
                    ],
                },
            },
        },
    }
    installer.apply([account, policy, job])
    installer.wait_job(job)
    observed = installer.json(
        installer.kube("logs", "job/" + name, "-c", "probe", "-n", env["namespace"])
    )
    require(
        observed
        == {"version": 1, "reachable": True, "recipe_sha256": digest(rule_set)},
        "native worker network probe did not prove the reviewed recipe",
    )
    return {
        "job": name,
        "recipe_sha256": digest(rule_set),
        "database_resource_id": databases[0].get("DbiResourceId"),
        "reachable": True,
    }
