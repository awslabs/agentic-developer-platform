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


def verify_network(installer):
    env = installer.env
    if not native_egress.enabled(env):
        return None
    worker = env["paid_worker"]
    rule_set = native_egress.rules(env)
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
