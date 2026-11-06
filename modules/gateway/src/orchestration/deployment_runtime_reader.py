"""Read actual pilot runtime through the registered role; no ambient cluster token."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import time
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.signers import RequestSigner
from websockets.sync.client import connect

from .deployment_runtime_contract import RuntimeComponent
from .review_cycle import CycleBlockedError

AWS_BOUNDS = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
MAX_RESPONSE = 8 * 1024 * 1024
# The pod's own cluster CA; equality with the described cluster CA proves co-location.
SERVICE_ACCOUNT_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
SCHEMA_PROBE = """import asyncio,hashlib,json,os
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from src.shared.database import get_engine
async def probe():
    engine=get_engine()
    async with engine.connect() as connection:
        await connection.execute(text('SET TRANSACTION READ ONLY'))
        await connection.execute(text("SET LOCAL statement_timeout = '8s'"))
        await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        rows=(await connection.execute(text('SELECT version_num FROM alembic_version'))).scalars().all()
        manifest=Path('/app/cli/command-manifest.json')
        print(json.dumps({
            'database_heads':sorted(rows),
            'image_heads':sorted(ScriptDirectory.from_config(Config('/app/alembic.ini')).get_heads()),
            'cli_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None,
            'product_profile_enabled':os.getenv('BG_PRODUCT_PROFILE_ENABLED'),
            'product_onboarding_enabled':os.getenv('BG_PRODUCT_ONBOARDING_ENABLED'),
        }))
    await engine.dispose()
asyncio.run(asyncio.wait_for(probe(), timeout=12))
"""


NETWORK_PROBE = """const net = require('node:net');
function open(host, port) {
  return new Promise(resolve => {
    const socket = net.connect({host, port});
    socket.setTimeout(3000);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', () => { socket.destroy(); resolve(false); });
    socket.once('timeout', () => { socket.destroy(); resolve(false); });
  });
}
Promise.all([open(process.argv[1], 8443), open('1.1.1.1', 443)])
  .then(([internal_allowed, external_allowed]) =>
    console.log(JSON.stringify({internal_allowed, external_denied: !external_allowed})));
"""


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason)


def label_name(value, *, allow_empty=False):
    return isinstance(value, str) and (
        (allow_empty and value == "") or re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9_.-]{0,61}[a-zA-Z0-9])?", value) is not None
    )


def label_key(value):
    """A Kubernetes label name, optionally qualified by a bounded DNS prefix."""
    if not isinstance(value, str) or len(value) > 317:
        return False
    parts = value.split("/")
    if len(parts) == 1:
        return label_name(parts[0])
    if len(parts) != 2 or not label_name(parts[1]) or len(parts[0]) > 253:
        return False
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) is not None for part in parts[0].split("."))


def insights_networks(cidrs):
    require(isinstance(cidrs, list) and cidrs and all(isinstance(cidr, str) for cidr in cidrs), "deployment_insights_egress_cidrs_invalid")
    try:
        networks = {ip_network(cidr) for cidr in cidrs}
    except ValueError as exc:
        raise CycleBlockedError("deployment_insights_egress_cidrs_invalid") from exc
    require(all(network.prefixlen > 0 for network in networks), "deployment_insights_open_egress")
    return networks


def read_bytes(client, url, **kwargs):
    with client.stream("GET", url, **kwargs) as response:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # A refusal by the cluster API (or any other non-2xx read-back) is a named
            # hold, not a transport fault. The Actions read route carries a
            # CycleBlockedError reason into the worker's tool result and the operator's
            # log, whereas the raw HTTPStatusError became a bare 503 "provider
            # unavailable" and the Door story ended "exited without verified runtime
            # evidence" with no trace of the 403 (#7870, TLS-X02 door5).
            status = error.response.status_code
            raise CycleBlockedError("deployment_runtime_read_forbidden" if status in (401, 403) else "deployment_runtime_read_failed") from None
        result = bytearray()
        for chunk in response.iter_bytes():
            require(len(result) + len(chunk) <= MAX_RESPONSE, "deployment_runtime_response_limit")
            result.extend(chunk)
        return bytes(result)


def eks_token(scoped, cluster):
    sts = scoped.client("sts", config=AWS_BOUNDS)
    signer = RequestSigner(sts.meta.service_model.service_id, scoped.region_name, "sts", "v4", scoped.get_credentials(), scoped.events)
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": sts.meta.endpoint_url + "/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster},
            "context": {},
        },
        region_name=scoped.region_name,
        expires_in=60,
        operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class KubernetesRuntime:
    def __init__(self, scoped, description, namespace, *, client=None, websocket=connect, proof_invoke_url=None):
        self.scoped, self.description, self.namespace, self.websocket = scoped, description, namespace, websocket
        self.proof_invoke_url = proof_invoke_url
        self.endpoint = description["endpoint"].rstrip("/")
        require(self.endpoint.startswith("https://"), "deployment_cluster_endpoint_invalid")
        self.tls = ssl.create_default_context(cadata=base64.b64decode(description["certificateAuthority"]["data"]).decode())
        self.client = client or httpx.Client(verify=self.tls, timeout=10, trust_env=False, follow_redirects=False)
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def get(self, path, **params):
        return json.loads(
            read_bytes(
                self.client,
                self.endpoint + path,
                params=params,
                headers={"Authorization": "Bearer " + eks_token(self.scoped, self.description["name"])},
            )
        )

    def qualification_api(self, artifact):
        cluster, namespace, workload = artifact.resource_id.split("/")
        require(
            cluster == self.description["name"]
            and namespace == self.namespace
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", workload) is not None,
            "deployment_qualification_target_changed",
        )
        scope = quote(namespace, safe="")
        name = quote(workload, safe="")
        deployment = self.get(f"/apis/apps/v1/namespaces/{scope}/deployments/{name}")
        metadata = deployment.get("metadata", {})
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        selector = spec.get("selector", {}).get("matchLabels", {})
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        require(
            metadata.get("name") == workload
            and metadata.get("namespace") == namespace
            and metadata.get("uid")
            and not metadata.get("deletionTimestamp")
            and spec.get("replicas") == 1
            and status.get("observedGeneration", 0) >= metadata.get("generation", 1)
            and status.get("updatedReplicas") == status.get("availableReplicas") == status.get("readyReplicas") == 1
            and len(containers) == 1
            and isinstance(containers[0].get("name"), str)
            and bool(containers[0]["name"])
            and containers[0].get("image", "").endswith("@" + artifact.image_digest)
            and isinstance(selector, dict)
            and len(selector) == 1
            and not spec.get("selector", {}).get("matchExpressions")
            and all(label_key(key) and label_name(value, allow_empty=True) for key, value in selector.items()),
            "deployment_qualification_rollout_mismatch",
        )
        label, value = next(iter(selector.items()))
        replicas = self.get(f"/apis/apps/v1/namespaces/{scope}/replicasets", labelSelector=f"{label}={value}", limit=10)
        require(not replicas.get("metadata", {}).get("continue"), "deployment_qualification_replicas_unbounded")
        owned = {
            row["metadata"]["uid"]
            for row in replicas.get("items", [])
            if row.get("metadata", {}).get("uid")
            and any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in row.get("metadata", {}).get("ownerReferences", []))
        }
        pods = self.get(f"/api/v1/namespaces/{scope}/pods", labelSelector=f"{label}={value}", limit=2)
        items = pods.get("items", [])
        require(not pods.get("metadata", {}).get("continue") and len(items) == 1, "deployment_qualification_pods_missing")
        pod = items[0]
        pod_meta = pod.get("metadata", {})
        live = pod.get("status", {})
        statuses = live.get("containerStatuses", [])
        pod_containers = pod.get("spec", {}).get("containers", [])
        require(
            pod_meta.get("uid")
            and pod_meta.get("namespace") == namespace
            and not pod_meta.get("deletionTimestamp")
            and any(ref.get("uid") in owned and ref.get("controller") is True for ref in pod_meta.get("ownerReferences", []))
            and len(pod_containers) == 1
            and pod_containers[0].get("name") == containers[0]["name"]
            and pod_containers[0].get("image") == containers[0]["image"]
            and len(statuses) == 1
            and statuses[0].get("name") == containers[0]["name"]
            and statuses[0].get("ready") is True
            and "running" in statuses[0].get("state", {})
            and statuses[0].get("imageID", "").endswith("@" + artifact.image_digest)
            and any(item.get("type") == "Ready" and item.get("status") == "True" for item in live.get("conditions", [])),
            "deployment_qualification_pod_not_serving",
        )
        return [pod_meta["uid"]]

    def qualification_client(self, artifact):
        cluster, namespace = artifact.resource_id.split("/")
        require(cluster == self.description["name"] and namespace == self.namespace, "deployment_qualification_target_changed")
        scope = quote(namespace, safe="")
        job_name = quote(artifact.qualification_job, safe="")
        job = self.get(f"/apis/batch/v1/namespaces/{scope}/jobs/{job_name}")
        metadata = job.get("metadata", {})
        spec = job.get("spec", {})
        status = job.get("status", {})
        container = spec.get("template", {}).get("spec", {}).get("containers", [])
        annotations = metadata.get("annotations", {})
        require(
            metadata.get("name") == artifact.qualification_job
            and metadata.get("namespace") == namespace
            and metadata.get("uid")
            and not metadata.get("deletionTimestamp")
            and annotations.get("adp/source-revision") == artifact.source_revision
            and annotations.get("adp/workflow-revision") == artifact.workflow_revision
            and annotations.get("adp/workflow-run-id") == str(artifact.run_id)
            and annotations.get("adp/workflow-run-attempt") == str(artifact.run_attempt)
            and spec.get("completions", 1) == spec.get("parallelism", 1) == 1
            and spec.get("template", {}).get("spec", {}).get("restartPolicy") == "Never"
            and status.get("succeeded") == 1
            and not status.get("failed")
            and not status.get("active")
            and any(item.get("type") == "Complete" and item.get("status") == "True" for item in status.get("conditions", []))
            and len(container) == 1
            and isinstance(container[0].get("name"), str)
            and bool(container[0]["name"])
            and container[0].get("image", "").endswith("@" + artifact.image_digest)
            and isinstance(container[0].get("command"), list)
            and container[0]["command"]
            and isinstance(container[0].get("args", []), list),
            "deployment_qualification_job_incomplete",
        )
        command = {"image": container[0]["image"], "command": container[0]["command"], "args": container[0].get("args", [])}
        require(
            hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == artifact.cli_manifest_sha256,
            "deployment_qualification_command_changed",
        )
        pods = self.get(f"/api/v1/namespaces/{scope}/pods", labelSelector=f"job-name={artifact.qualification_job}", limit=2)
        items = pods.get("items", [])
        require(not pods.get("metadata", {}).get("continue") and len(items) == 1, "deployment_qualification_job_pod_missing")
        pod = items[0]
        pod_meta = pod.get("metadata", {})
        pod_status = pod.get("status", {})
        statuses = pod_status.get("containerStatuses", [])
        pod_containers = pod.get("spec", {}).get("containers", [])
        require(
            pod_meta.get("uid")
            and pod_meta.get("namespace") == namespace
            and not pod_meta.get("deletionTimestamp")
            and any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in pod_meta.get("ownerReferences", []))
            and pod_status.get("phase") == "Succeeded"
            and len(pod_containers) == 1
            and pod_containers[0].get("name") == container[0]["name"]
            and pod_containers[0].get("image") == command["image"]
            and pod_containers[0].get("command") == command["command"]
            and pod_containers[0].get("args", []) == command["args"]
            and len(statuses) == 1
            and statuses[0].get("name") == container[0]["name"]
            and statuses[0].get("imageID", "").endswith("@" + artifact.image_digest)
            and statuses[0].get("state", {}).get("terminated", {}).get("exitCode") == 0,
            "deployment_qualification_job_pod_changed",
        )
        return [pod_meta["uid"]]

    def facade_probe(self):
        """Prove the tools façade answers through the cluster, as the connection role.

        The façade's only unauthenticated route is ``GET /health`` (``{"status": "ok"}``;
        ``modules/tools/facade/tools_facade/server.py``). Its tool discovery
        (``/agent/tools?live=true``) requires a human-rooted bearer with search
        authority by design, so the verifier must not — and cannot — list tools here;
        the served catalogue is bound by the release smoke and the gateway
        verification (#7729). The Service exposes one port, named ``http`` or unnamed.
        """
        namespace = quote(self.namespace, safe="")
        service = self.get(f"/api/v1/namespaces/{namespace}/services/tools-facade")
        ports = [item.get("port") for item in service.get("spec", {}).get("ports", []) if item.get("name") in (None, "", "http")]
        require(len(ports) == 1 and type(ports[0]) is int and 0 < ports[0] < 65536, "deployment_facade_service_invalid")
        path = f"/api/v1/namespaces/{namespace}/services/http:tools-facade:{ports[0]}/proxy"
        headers = {"Authorization": "Bearer " + eks_token(self.scoped, self.description["name"])}
        with self.client.stream("GET", self.endpoint + path + "/health", headers=headers) as response:
            require(response.status_code == 200, "deployment_facade_health_failed")
            body = bytearray()
            for chunk in response.iter_bytes():
                require(len(body) + len(chunk) <= MAX_RESPONSE, "deployment_runtime_response_limit")
                body.extend(chunk)
        try:
            health = json.loads(body)
        except ValueError:
            health = None
        require(isinstance(health, dict) and health.get("status") == "ok", "deployment_facade_health_failed")

    def runs_inside_target_cluster(self):
        """True when this process is a pod of the described cluster: its own service-account
        CA is byte-identical to the cluster CA the connection role described. Nothing weaker
        (an endpoint hostname, an environment variable) proves co-location."""
        try:
            own = SERVICE_ACCOUNT_CA.read_bytes().strip()
        except OSError:
            return False
        return bool(own) and own == base64.b64decode(self.description["certificateAuthority"]["data"]).strip()

    def door_identity_status(self):
        """Probe the Door with an invalid identity header; returns ``(status, probe_path)``.

        The Door's ingress NetworkPolicy admits only named pod peers (the gateway, the
        agents). A probe through the API server's ``services/proxy`` reaches the pod from a
        control-plane ENI, which no pod selector admits, so an enforced policy drops it and
        the verifier times out (TLS-X02 door5, #7870). When the verifier itself runs inside
        the target cluster it is one of those admitted peers, and the real caller's path —
        the Service over the cluster network — is the probe. Off-cluster the scoped service
        proxy remains the only path, and the receipt names which one was used.
        """
        headers = {"x-adp-door-identity": "invalid"}
        if self.runs_inside_target_cluster():
            url, probe_path = f"http://context-mcp.{self.namespace}.svc.cluster.local:5100/tools", "cluster_network"
        else:
            path = f"/api/v1/namespaces/{quote(self.namespace, safe='')}/services/http:context-mcp:5100/proxy/tools"
            headers["Authorization"] = "Bearer " + eks_token(self.scoped, self.description["name"])
            url, probe_path = self.endpoint + path, "api_server_proxy"
        try:
            with self.client.stream("GET", url, headers=headers) as response:
                return response.status_code, probe_path
        except httpx.HTTPError:
            # A dropped or refused connection is a named hold the operator can act on
            # (a NetworkPolicy, a missing Service), not a provider outage.
            raise CycleBlockedError("deployment_door_identity_probe_unreachable") from None

    def resources(self, path):
        values, cursor = [], ""
        for _ in range(10):
            page = self.get(path, labelSelector="app=bedrockgateway", limit=100, **({"continue": cursor} if cursor else {}))
            require(isinstance(page.get("items"), list) and len(page["items"]) <= 100, "deployment_runtime_list_invalid")
            values.extend(page["items"])
            cursor = page.get("metadata", {}).get("continue", "")
            if not cursor:
                return values
        raise CycleBlockedError("deployment_runtime_list_limit")

    def insights_aws_networks(self, region):
        vpc = self.description.get("resourcesVpcConfig", {}).get("vpcId")
        require(vpc, "deployment_insights_egress_vpc_missing")
        ec2 = self.scoped.client("ec2", config=AWS_BOUNDS)
        prefixes = ec2.describe_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": [f"com.amazonaws.{region}.s3"]}])
        require(not prefixes.get("NextToken") and len(prefixes.get("PrefixLists", [])) == 1, "deployment_insights_egress_s3_missing")
        networks = insights_networks(prefixes["PrefixLists"][0].get("Cidrs"))
        endpoints = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "service-name", "Values": [f"com.amazonaws.{region}.sts"]}]
        )
        require(not endpoints.get("NextToken") and len(endpoints.get("VpcEndpoints", [])) == 1, "deployment_insights_egress_sts_missing")
        endpoint = endpoints["VpcEndpoints"][0]
        require(
            endpoint.get("VpcId") == vpc
            and endpoint.get("VpcEndpointType") == "Interface"
            and endpoint.get("PrivateDnsEnabled") is True
            and endpoint.get("State") == "available"
            and endpoint.get("NetworkInterfaceIds"),
            "deployment_insights_egress_sts_invalid",
        )
        interfaces = ec2.describe_network_interfaces(NetworkInterfaceIds=endpoint["NetworkInterfaceIds"])
        rows = interfaces.get("NetworkInterfaces", [])
        require(
            not interfaces.get("NextToken")
            and {row.get("NetworkInterfaceId") for row in rows} == set(endpoint["NetworkInterfaceIds"])
            and all(row.get("VpcId") == vpc for row in rows),
            "deployment_insights_egress_sts_invalid",
        )
        return networks | insights_networks([f"{row.get('PrivateIpAddress')}/32" for row in rows])

    def insights_egress(self, region, public_client):
        namespace = quote(self.namespace, safe="")
        response = self.get(f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies", limit=10)
        policies = response.get("items", [])
        require(
            not response.get("metadata", {}).get("continue")
            and len(policies) == 2
            and {policy.get("metadata", {}).get("name") for policy in policies} == {"deny-all", "gateway-s3-github-egress"},
            "deployment_insights_egress_policy_set_invalid",
        )
        specs = {policy["metadata"]["name"]: policy.get("spec", {}) for policy in policies}
        deny, allowed = specs["deny-all"], specs["gateway-s3-github-egress"]
        require(
            set(deny.get("policyTypes", [])) == {"Ingress", "Egress"}
            and deny.get("podSelector") == {}
            and not deny.get("egress")
            and not deny.get("ingress"),
            "deployment_insights_default_deny_missing",
        )
        require(
            allowed.get("policyTypes") == ["Egress"] and allowed.get("podSelector") == {} and not allowed.get("ingress"),
            "deployment_insights_egress_policy_missing",
        )
        dns = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}]
        gateway = [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "adp-gateway"}},
                "podSelector": {"matchLabels": {"app": "bedrockgateway"}},
            }
        ]
        seen, cidrs = set(), []
        for rule in allowed.get("egress", []):
            require(set(rule) == {"ports", "to"} and rule["ports"] and rule["to"], "deployment_insights_open_egress")
            require(all(set(port) <= {"port", "protocol"} for port in rule["ports"]), "deployment_insights_egress_ports_invalid")
            ports = {(port.get("port"), port.get("protocol", "TCP")) for port in rule["ports"]}
            if ports == {(53, "UDP"), (53, "TCP")}:
                require(rule["to"] == dns, "deployment_insights_egress_dns_invalid")
                seen.add("dns")
            elif ports == {(8080, "TCP")}:
                require(rule["to"] == gateway, "deployment_insights_egress_gateway_invalid")
                seen.add("gateway")
            else:
                require(ports == {(443, "TCP")}, "deployment_insights_egress_ports_invalid")
                for peer in rule["to"]:
                    require(set(peer) == {"ipBlock"} and set(peer["ipBlock"]) == {"cidr"}, "deployment_insights_egress_peer_invalid")
                    cidrs.append(peer["ipBlock"]["cidr"])
        require(seen == {"dns", "gateway"}, "deployment_insights_egress_policy_missing")
        observed, aws_networks = insights_networks(cidrs), self.insights_aws_networks(region)
        require(aws_networks <= observed, "deployment_insights_egress_aws_missing")
        extra = observed - aws_networks
        if extra:
            client = public_client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
            try:
                metadata = json.loads(read_bytes(client, "https://api.github.com/meta"))
            finally:
                if public_client is None:
                    client.close()
            github_networks = insights_networks(metadata.get("api"))
            require(
                all(any(network.version == approved.version and network.subnet_of(approved) for approved in github_networks) for network in extra),
                "deployment_insights_egress_destination_unapproved",
            )

    def insights(self, *, digest, account, region, environment, public_client=None):
        namespace = quote(self.namespace, safe="")
        details = self.get(f"/api/v1/namespaces/{namespace}")
        require(details.get("metadata", {}).get("name") == "adp-insights", "deployment_insights_namespace_mismatch")
        require(
            details["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") == "restricted",
            "deployment_insights_pod_security_missing",
        )
        quota = self.get(f"/api/v1/namespaces/{namespace}/resourcequotas/insights-budget")
        require(quota.get("spec", {}).get("hard", {}).get("pods") == "4", "deployment_insights_quota_missing")
        self.insights_egress(region, public_client)
        uids, release_found = [], False
        for service in ("emitter", "distiller"):
            name = "insights-" + service
            deployment = self.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
            metadata, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
            desired = spec.get("replicas", 0)
            require(
                not metadata.get("deletionTimestamp")
                and isinstance(desired, int)
                and 0 < desired <= 4
                and status.get("observedGeneration", 0) >= metadata["generation"]
                and status.get("updatedReplicas") == status.get("availableReplicas") == desired,
                "deployment_insights_rollout_incomplete",
            )
            require(spec["template"]["spec"].get("serviceAccountName") == name, "deployment_insights_identity_mismatch")
            service_account = self.get(f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}")
            require(
                service_account.get("metadata", {}).get("annotations", {}).get("eks.amazonaws.com/role-arn")
                == f"arn:aws:iam::{account}:role/adp-{environment}-insights-{service}",
                "deployment_insights_identity_mismatch",
            )
            container = [item for item in spec["template"]["spec"]["containers"] if item.get("name") == service]
            registry = f"{account}.dkr.ecr.{region}.amazonaws.com/adp-{environment}-insights-{service}"
            require(
                len(container) == 1 and re.fullmatch(re.escape(registry) + r"@sha256:[a-f0-9]{64}", container[0]["image"]),
                "deployment_insights_registry_mismatch",
            )
            replicas = self.get(f"/apis/apps/v1/namespaces/{namespace}/replicasets", labelSelector=f"app={name}", limit=10)
            require(not replicas.get("metadata", {}).get("continue"), "deployment_insights_replicas_unbounded")
            owners = {
                row["metadata"]["uid"]
                for row in replicas.get("items", [])
                if any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in row["metadata"].get("ownerReferences", []))
            }
            pods = self.get(f"/api/v1/namespaces/{namespace}/pods", labelSelector=f"app={name}", limit=5)
            require(not pods.get("metadata", {}).get("continue") and len(pods.get("items", [])) == desired, "deployment_insights_pods_missing")
            for pod in pods["items"]:
                conditions = pod.get("status", {}).get("conditions", [])
                current = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == service]
                require(
                    not pod["metadata"].get("deletionTimestamp")
                    and len(current) == 1
                    and any(ref.get("uid") in owners and ref.get("controller") is True for ref in pod["metadata"].get("ownerReferences", []))
                    and current[0].get("ready") is True
                    and "running" in current[0].get("state", {})
                    and current[0].get("imageID", "").rsplit("@", 1)[-1] == container[0]["image"].rsplit("@", 1)[-1]
                    and any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions),
                    "deployment_insights_pod_not_healthy",
                )
                uids.append(pod["metadata"]["uid"])
            release_found |= container[0]["image"].endswith("@" + digest)
        require(release_found, "deployment_insights_release_not_serving")
        return sorted(uids)

    def probe_network(self, image):
        ns = quote(self.namespace, safe="")
        listing = self.get(f"/api/v1/namespaces/{ns}/pods", labelSelector="adp.io/chat-sandbox=true", limit=100)
        pods = [
            pod
            for pod in listing.get("items", [])
            if pod.get("status", {}).get("phase") == "Running"
            and pod.get("metadata", {}).get("labels", {}).get("adp.io/chat-sandbox") == "true"
            and pod.get("spec", {}).get("serviceAccountName") == "adp-chat-sandbox"
            and pod.get("spec", {}).get("containers", [{}])[0].get("image") == image
            and pod.get("status", {}).get("containerStatuses", [{}])[0].get("ready") is True
            and pod.get("status", {}).get("containerStatuses", [{}])[0].get("imageID", "").endswith("@" + image.rsplit("@", 1)[1])
            and not pod.get("metadata", {}).get("deletionTimestamp")
        ]
        require(pods and len(listing.get("items", [])) <= 100 and not listing.get("metadata", {}).get("continue"), "deployment_runtime_mismatch")
        pod = sorted(pods, key=lambda item: item["metadata"]["name"])[0]
        name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)), "deployment_runtime_mismatch")
        aliases = pod["spec"].get("hostAliases", [])
        require(len(aliases) == 1 and aliases[0].get("hostnames") == ["chat-sandbox-gateway.adp-gateway.svc"], "deployment_runtime_mismatch")
        address = aliases[0]["ip"]
        require(address.startswith(("10.", "192.168.", "172.")), "deployment_runtime_mismatch")
        path = f"/api/v1/namespaces/{ns}/pods/{quote(name, safe='')}/exec"
        query = urlencode(
            [
                ("container", "chat-agent"),
                ("stdout", "true"),
                ("stderr", "true"),
                ("stdin", "false"),
                ("tty", "false"),
                *[("command", part) for part in ("node", "-e", NETWORK_PROBE, address)],
            ]
        )
        output, status, size = bytearray(), None, 0
        deadline = time.monotonic() + 12
        with self.websocket(
            self.endpoint.replace("https://", "wss://", 1) + path + "?" + query,
            ssl=self.tls,
            additional_headers={"Authorization": "Bearer " + eks_token(self.scoped, self.description["name"])},
            subprotocols=["v4.channel.k8s.io"],
            proxy=None,
            open_timeout=5,
            close_timeout=1,
            max_size=65536,
            compression=None,
        ) as channel:
            require(channel.subprotocol == "v4.channel.k8s.io", "deployment_runtime_mismatch")
            for _ in range(100):
                remaining = deadline - time.monotonic()
                require(remaining > 0, "deployment_runtime_mismatch")
                message = channel.recv(timeout=remaining)
                require(isinstance(message, bytes) and bool(message), "deployment_runtime_mismatch")
                size += len(message)
                require(size <= 65536, "deployment_runtime_mismatch")
                if message[0] == 1:
                    output.extend(message[1:])
                elif message[0] == 3:
                    status = json.loads(message[1:])
                    break
        require(status is not None and status.get("status") == "Success", "deployment_runtime_mismatch")
        current = self.get(f"/api/v1/namespaces/{ns}/pods/{quote(name, safe='')}")
        require(current.get("metadata", {}).get("uid") == uid and not current["metadata"].get("deletionTimestamp"), "deployment_runtime_mismatch")
        proof = json.loads(output)
        return uid, proof.get("internal_allowed"), proof.get("external_denied")

    def protected_schema(self, pod):
        origin = getattr(self, "proof_invoke_url", None) or os.getenv("ADP_RUNTIME_PROOF_INVOKE_URL", "")
        region = self.scoped.region_name
        require(
            re.fullmatch(rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+", origin),
            "deployment_runtime_proof_route_unconfigured",
        )
        nonce = secrets.token_hex(32)
        uid = pod["metadata"]["uid"]
        body = json.dumps({"pod_uid": uid, "namespace": self.namespace, "nonce": nonce}).encode()
        url = origin + "/internal/v1/runtime-proof/schema"
        deadline = time.monotonic() + 30
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            for _ in range(100):
                require(time.monotonic() < deadline, "deployment_runtime_proof_pod_unreachable")
                signed = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
                SigV4Auth(self.scoped.get_credentials().get_frozen_credentials(), "execute-api", region).add_auth(signed)
                with client.stream("POST", url, content=body, headers=dict(signed.headers)) as response:
                    if response.status_code == 409:
                        continue
                    require(response.status_code == 200, "deployment_runtime_proof_unavailable")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        require(len(raw) + len(chunk) <= 8192, "deployment_runtime_proof_response_limit")
                        raw.extend(chunk)
                try:
                    proof = json.loads(raw)
                    observed = datetime.fromisoformat(proof["observed_at"])
                    require(
                        proof.get("pod_uid") == uid
                        and proof.get("namespace") == self.namespace
                        and proof.get("nonce") == nonce
                        and observed.tzinfo is not None
                        and 0 <= (datetime.now(UTC) - observed).total_seconds() <= 30,
                        "deployment_runtime_proof_identity_mismatch",
                    )
                except (ValueError, KeyError, TypeError):
                    raise CycleBlockedError("deployment_runtime_proof_invalid") from None
                return proof
        raise CycleBlockedError("deployment_runtime_proof_pod_unreachable")

    def schema(self, pod, *, product=False):
        name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)), "deployment_pod_name_invalid")
        if product:
            try:
                proof = self.protected_schema(pod)
            except httpx.HTTPError:
                raise CycleBlockedError("deployment_runtime_proof_unavailable") from None
        else:
            proof = self.exec_schema(name)
        current = self.get(f"/api/v1/namespaces/{quote(self.namespace, safe='')}/pods/{quote(name, safe='')}")
        require(current.get("metadata", {}).get("uid") == uid and not current["metadata"].get("deletionTimestamp"), "deployment_probe_pod_replaced")
        heads = proof.get("database_heads")
        require(isinstance(heads, list) and len(heads) == 1 and heads == proof.get("image_heads"), "deployment_migration_not_at_single_head")
        require(isinstance(heads[0], str) and 1 <= len(heads[0]) <= 128, "deployment_migration_head_invalid")
        if product:
            require(
                isinstance(proof.get("cli_manifest_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", proof["cli_manifest_sha256"])
                and proof.get("product_profile_enabled") in {"true", "false"}
                and proof.get("product_onboarding_enabled") in {"true", "false"},
                "deployment_product_probe_incomplete",
            )
            return proof
        return heads[0]

    def exec_schema(self, name):
        path = f"/api/v1/namespaces/{quote(self.namespace, safe='')}/pods/{quote(name, safe='')}/exec"
        query = urlencode(
            [
                ("container", "bedrockgateway"),
                ("stdout", "true"),
                ("stderr", "true"),
                ("stdin", "false"),
                ("tty", "false"),
                *[("command", part) for part in ("python", "-c", SCHEMA_PROBE)],
            ]
        )
        output, status, size = bytearray(), None, 0
        deadline = time.monotonic() + 15
        with self.websocket(
            self.endpoint.replace("https://", "wss://", 1) + path + "?" + query,
            ssl=self.tls,
            additional_headers={"Authorization": "Bearer " + eks_token(self.scoped, self.description["name"])},
            subprotocols=["v4.channel.k8s.io"],
            proxy=None,
            open_timeout=5,
            close_timeout=1,
            max_size=65536,
            compression=None,
        ) as channel:
            require(channel.subprotocol == "v4.channel.k8s.io", "deployment_schema_protocol_invalid")
            for _ in range(100):
                remaining = deadline - time.monotonic()
                require(remaining > 0, "deployment_schema_probe_timeout")
                message = channel.recv(timeout=remaining)
                require(isinstance(message, bytes) and len(message) > 0, "deployment_schema_frame_invalid")
                size += len(message)
                require(size <= 65536, "deployment_schema_response_limit")
                if message[0] == 1:
                    output.extend(message[1:])
                elif message[0] == 3:
                    status = json.loads(message[1:])
                    break
        require(status is not None and status.get("status") == "Success", "deployment_schema_probe_failed")
        return json.loads(output)

    def gateway(self, *, digest, source_revision, account, region, product=None, wheel_sha256=None):
        ns = quote(self.namespace, safe="")
        deployment = self.get(f"/apis/apps/v1/namespaces/{ns}/deployments/bedrockgateway")
        desired, status, metadata = deployment["spec"].get("replicas", 1), deployment.get("status", {}), deployment["metadata"]
        require(
            not metadata.get("deletionTimestamp")
            and 0 < desired <= 100
            and status.get("observedGeneration", 0) >= metadata["generation"]
            and status.get("updatedReplicas") == status.get("availableReplicas") == desired,
            "deployment_gateway_rollout_incomplete",
        )
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com/adp-gateway"
        expected = {registry + ":" + source_revision, registry + "@" + digest}
        containers = [c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "bedrockgateway"]
        require(len(containers) == 1 and containers[0]["image"] in expected, "deployment_gateway_revision_mismatch")
        if product is not None:
            env = containers[0].get("env", [])
            for name, enabled in (
                ("BG_PRODUCT_PROFILE_ENABLED", product.product_profile_enabled),
                ("BG_PRODUCT_ONBOARDING_ENABLED", product.product_onboarding_enabled),
            ):
                values = [item.get("value") for item in env if item.get("name") == name]
                require(values == [str(enabled).lower()], "deployment_product_admissions_mismatch")
        replicas = self.resources(f"/apis/apps/v1/namespaces/{ns}/replicasets")
        owners = {
            row["metadata"]["uid"]
            for row in replicas
            if any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in row["metadata"].get("ownerReferences", []))
        }
        pods = [pod for pod in self.resources(f"/api/v1/namespaces/{ns}/pods") if not pod["metadata"].get("deletionTimestamp")]
        require(len(pods) == desired, "deployment_gateway_replica_evidence_incomplete")
        heads, uids = set(), []
        for pod in pods:
            require(
                any(ref.get("uid") in owners and ref.get("controller") is True for ref in pod["metadata"].get("ownerReferences", [])),
                "deployment_pod_owner_mismatch",
            )
            statuses = [c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == "bedrockgateway"]
            require(
                len(statuses) == 1
                and statuses[0].get("ready") is True
                and statuses[0].get("imageID", "").rsplit("@", 1)[-1] == digest
                and "running" in statuses[0].get("state", {})
                and any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])),
                "deployment_serving_pod_not_at_release",
            )
            proof = self.schema(pod, product=True) if product is not None or wheel_sha256 is not None else self.schema(pod)
            if wheel_sha256 is not None:
                require(
                    isinstance(proof, dict) and proof.get("l5_wheel_sha256") == wheel_sha256,
                    "deployment_gateway_wheel_mismatch",
                )
                if product is None:
                    proof = proof["database_heads"][0]
            if product is not None:
                require(
                    isinstance(proof, dict)
                    and proof.get("product_profile_enabled") == str(product.product_profile_enabled).lower()
                    and proof.get("product_onboarding_enabled") == str(product.product_onboarding_enabled).lower()
                    and proof.get("cli_manifest_sha256") == product.cli_manifest_sha256
                    and proof.get("database_heads") == [product.migration_head],
                    "deployment_product_runtime_mismatch",
                )
                proof = product.migration_head
            heads.add(proof)
            uids.append(pod["metadata"]["uid"])
        require(len(heads) == 1, "deployment_migration_evidence_inconsistent")
        # No acceptance on an observation which raced another rollout.
        current = self.get(f"/apis/apps/v1/namespaces/{ns}/deployments/bedrockgateway")
        require(
            current["metadata"]["uid"] == metadata["uid"] and current["metadata"]["generation"] == metadata["generation"],
            "deployment_changed_during_verification",
        )
        return sorted(uids), next(iter(heads))


class PilotRuntimeReader:
    def __init__(self, *, kubernetes=KubernetesRuntime, public_client=None, dev_box_controller=None):
        self.kubernetes, self.public_client = kubernetes, public_client
        self.dev_box_controller = dev_box_controller

    def dev_box(self, scoped, description, namespace, artifact, artifact_hash, evidence_ref):
        receipt = artifact.dev_box_receipt
        require(
            namespace is None
            and receipt is not None
            and scoped.region_name == artifact.region
            and description.get("LaunchTemplateId") == artifact.resource_id,
            "deployment_dev_box_target_changed",
        )
        require(
            receipt.controller_ready and receipt.controller_status == "present" and self.dev_box_controller is not None,
            "deployment_dev_box_controller_unready",
        )
        ec2 = scoped.client("ec2", config=AWS_BOUNDS)
        images = ec2.describe_images(ImageIds=[receipt.image_id]).get("Images", [])
        require(
            len(images) == 1
            and images[0].get("ImageId") == receipt.image_id
            and images[0].get("OwnerId") == artifact.account_id
            and {tag["Key"]: tag["Value"] for tag in images[0].get("Tags", [])}.get("adp:source-revision") == artifact.source_revision,
            "deployment_dev_box_image_changed",
        )
        versions = ec2.describe_launch_template_versions(LaunchTemplateId=artifact.resource_id, Versions=[str(receipt.launch_template_version)]).get(
            "LaunchTemplateVersions", []
        )
        require(len(versions) == 1 and versions[0].get("VersionNumber") == receipt.launch_template_version, "deployment_dev_box_template_changed")
        template = versions[0]["LaunchTemplateData"]
        interfaces = template.get("NetworkInterfaces", [])
        require(
            template.get("ImageId") == receipt.image_id
            and template.get("MetadataOptions", {}).get("HttpTokens") == "required"
            and len(interfaces) == 1
            and interfaces[0].get("AssociatePublicIpAddress") is False
            and interfaces[0].get("SubnetId") == receipt.private_subnet_id
            and interfaces[0].get("Groups") == [receipt.guest_security_group_id]
            and template.get("IamInstanceProfile", {}).get("Arn") == receipt.guest_instance_profile_arn
            and not template.get("UserData")
            and len(template.get("BlockDeviceMappings", [])) == 1
            and template["BlockDeviceMappings"][0].get("Ebs", {}).get("Encrypted") is True,
            "deployment_dev_box_template_changed",
        )
        groups = ec2.describe_security_groups(GroupIds=[receipt.guest_security_group_id]).get("SecurityGroups", [])
        expected_ingress = [(8443, receipt.gateway_security_group_id)] if receipt.dashboard_enabled else []
        ingress = groups[0].get("IpPermissions", []) if len(groups) == 1 else []
        require(
            len(groups) == 1
            and groups[0].get("GroupId") == receipt.guest_security_group_id
            and len(ingress) == len(expected_ingress)
            and all(
                permission.get("IpProtocol") == "tcp"
                and permission.get("FromPort") == port
                and permission.get("ToPort") == port
                and not permission.get("IpRanges")
                and not permission.get("Ipv6Ranges")
                and not permission.get("PrefixListIds")
                and [source.get("GroupId") for source in permission.get("UserIdGroupPairs", [])] == [group]
                for permission, (port, group) in zip(ingress, expected_ingress)
            ),
            "deployment_dev_box_ingress_changed",
        )
        iam = scoped.client("iam", config=AWS_BOUNDS)
        require(
            all(
                arn.startswith(f"arn:aws:iam::{artifact.account_id}:")
                for arn in (receipt.guest_instance_profile_arn, receipt.guest_role_arn, receipt.controller_role_arn)
            ),
            "deployment_dev_box_role_changed",
        )
        profile_name = receipt.guest_instance_profile_arn.rsplit("/", 1)[-1]
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
        require(
            profile.get("Arn") == receipt.guest_instance_profile_arn
            and [role.get("Arn") for role in profile.get("Roles", [])] == [receipt.guest_role_arn],
            "deployment_dev_box_role_changed",
        )
        guest_inline = ["approved-model-route-only"] if receipt.gateway_execute_arn else []
        controller_inline = ["owned-host-lifecycle", "selected-ebs-key-only"]
        if receipt.run_table_arn:
            controller_inline.append("owner-bound-host-runs")
        for arn, expected_attached, expected_inline in (
            (receipt.guest_role_arn, ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"], guest_inline),
            (receipt.controller_role_arn, [], controller_inline),
        ):
            name = arn.rsplit("/", 1)[-1]
            role = iam.get_role(RoleName=name)["Role"]
            attached = iam.list_attached_role_policies(RoleName=name)
            inline = iam.list_role_policies(RoleName=name)
            require(
                role.get("Arn") == arn
                and not attached.get("IsTruncated")
                and not inline.get("IsTruncated")
                and sorted(policy["PolicyArn"] for policy in attached.get("AttachedPolicies", [])) == expected_attached
                and sorted(inline.get("PolicyNames", [])) == sorted(expected_inline),
                "deployment_dev_box_role_changed",
            )
        controller_name = receipt.controller_role_arn.rsplit("/", 1)[-1]
        lifecycle = iam.get_role_policy(RoleName=controller_name, PolicyName="owned-host-lifecycle")["PolicyDocument"]
        command_statements = [statement for statement in lifecycle.get("Statement", []) if statement.get("Sid") == "ReadCommandResult"]
        require(
            command_statements
            == [
                {
                    "Sid": "ReadCommandResult",
                    "Effect": "Allow",
                    "Action": ["ssm:GetCommandInvocation"],
                    "Resource": "*",
                    "Condition": {"StringEquals": {"aws:RequestedRegion": receipt.region}},
                }
            ],
            "deployment_dev_box_command_policy_changed",
        )
        if receipt.run_table_arn:
            policy = iam.get_role_policy(RoleName=controller_name, PolicyName="owner-bound-host-runs")["PolicyDocument"]
            run_statements = policy.get("Statement")
            require(isinstance(run_statements, list) and bool(run_statements), "deployment_dev_box_run_policy_changed")
            require(
                run_statements[0]
                == {
                    "Effect": "Allow",
                    "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"],
                    "Resource": receipt.run_table_arn,
                },
                "deployment_dev_box_run_policy_changed",
            )
            # A run registry encrypted with a customer KMS key (#8139) adds exactly one KMS statement,
            # usable through DynamoDB only, and only when the receipt names that key (#8145, mirroring
            # runtime.py's #8144 read-back). A KMS grant the receipt does not name, or a named key without
            # its grant, fails closed; the action order is the one infra/main.tf
            # (aws_iam_role_policy.controller_run_registry) emits.
            expected_key_statements = (
                [
                    {
                        "Effect": "Allow",
                        "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                        "Resource": receipt.run_table_kms_key_arn,
                        "Condition": {"StringEquals": {"kms:ViaService": f"dynamodb.{receipt.region}.amazonaws.com"}},
                    }
                ]
                if receipt.run_table_kms_key_arn
                else []
            )
            require(run_statements[1:] == expected_key_statements, "deployment_dev_box_run_key_policy_changed")
        if receipt.gateway_execute_arn:
            policy = iam.get_role_policy(RoleName=receipt.guest_role_arn.rsplit("/", 1)[-1], PolicyName="approved-model-route-only")["PolicyDocument"]
            require(
                policy.get("Statement")
                == [
                    {
                        "Effect": "Allow",
                        "Action": ["execute-api:Invoke"],
                        "Resource": [
                            f"{receipt.gateway_execute_arn}/POST/agent/model/*/invoke",
                            f"{receipt.gateway_execute_arn}/POST/agent/crew/model-token/refresh",
                            f"{receipt.gateway_execute_arn}/POST/agent/crew/host-restore/grant",
                            f"{receipt.gateway_execute_arn}/POST/agent/crew/host-restore/release",
                        ],
                    }
                ],
                "deployment_dev_box_gateway_policy_changed",
            )
        observed = self.dev_box_controller(scoped, receipt)
        require(
            observed.get("healthy") is True
            and observed.get("image_digest") == receipt.controller_digest
            and observed.get("source_revision") == receipt.source_revision
            and observed.get("release_sha256") == receipt.release_manifest_sha256,
            "deployment_dev_box_controller_changed",
        )
        return RuntimeComponent(
            component="dev-box",
            actual_revision=artifact.source_revision,
            artifact_hash=artifact_hash,
            image_digest=receipt.controller_digest,
            host_image_id=receipt.image_id,
            host_template_id=receipt.launch_template_id,
            healthy=True,
            evidence_ref=evidence_ref,
            observed_at=datetime.now(UTC),
        )

    def inspect(self, scoped, description, namespace, *, artifact, artifact_hash, evidence_ref, environment):
        if artifact.component in {"agent-worker", "chat-worker"}:
            require(environment in {None, "dev"}, "deployment_environment_unsupported")
        else:
            require(environment in {"dev", "staging", "prod"}, "deployment_environment_unsupported")
        if artifact.component == "dev-box":
            return self.dev_box(scoped, description, namespace, artifact, artifact_hash, evidence_ref)
        if artifact.component in {"tools-platform", "tools-interceptor"}:
            from .deployment_runtime_tools import verify_agentcore_gateway, verify_lambda_version

            require(scoped.region_name == artifact.region, "deployment_runtime_target_changed")
            if artifact.component == "tools-platform":
                require(
                    description.get("gatewayArn") == artifact.resource_id and description.get("gatewayId") == artifact.gateway_id,
                    "deployment_runtime_target_changed",
                )
                return verify_agentcore_gateway(scoped, artifact, artifact_hash, evidence_ref)
            require(description.get("Configuration", {}).get("FunctionArn") == artifact.resource_id, "deployment_runtime_target_changed")
            return verify_lambda_version(scoped, artifact, artifact_hash, evidence_ref)
        resource_id = description["name"] + "/" + namespace
        resource_ids = {resource_id}
        if artifact.component == "tools-facade":
            resource_ids = {resource_id + "/tools-facade"}
        elif artifact.component == "tool-adapters":
            resource_ids.add(resource_id + "/tool-adapters")
        elif artifact.component == "qualification-api":
            require(artifact.resource_id.startswith(resource_id + "/"), "deployment_qualification_target_changed")
            resource_ids.add(artifact.resource_id)
        require(
            artifact.resource_id in resource_ids
            and scoped.region_name == artifact.region
            and description.get("arn") == f"arn:aws:eks:{artifact.region}:{artifact.account_id}:cluster/{description['name']}",
            "deployment_runtime_target_changed",
        )
        if artifact.component == "qualification-api":
            kube = self.kubernetes(scoped, description, namespace)
            try:
                uids = kube.qualification_api(artifact)
            finally:
                kube.close()
            return RuntimeComponent(
                component=artifact.component,
                actual_revision=artifact.source_revision,
                artifact_hash=artifact_hash,
                image_digest=artifact.image_digest,
                healthy=True,
                evidence_ref=evidence_ref,
                observed_at=datetime.now(UTC),
                pod_uids=uids,
            )
        if artifact.component == "qualification-client":
            kube = self.kubernetes(scoped, description, namespace)
            try:
                uids = kube.qualification_client(artifact)
            finally:
                kube.close()
            return RuntimeComponent(
                component=artifact.component,
                actual_revision=artifact.source_revision,
                artifact_hash=artifact_hash,
                image_digest=artifact.image_digest,
                cli_manifest_sha256=artifact.cli_manifest_sha256,
                healthy=True,
                evidence_ref=evidence_ref,
                observed_at=datetime.now(UTC),
                pod_uids=uids,
            )
        if artifact.component == "tool-adapters":
            from .deployment_runtime_adapters import verify_tool_adapters

            require(environment == artifact.environment, "deployment_adapter_environment_changed")
            kube = self.kubernetes(scoped, description, namespace)
            try:
                observed = verify_tool_adapters(
                    kube,
                    artifact,
                    artifact_hash,
                    evidence_ref,
                    expected_manifest=artifact.adapter_manifest,
                    expected_contract=artifact.adapter_contract,
                )
                return RuntimeComponent(
                    **observed,
                    manifests_sha256=artifact.assets["adapter-manifest.json"],
                    adapter_contract_sha256=artifact.assets["adapter-contract.json"],
                )
            finally:
                kube.close()
        if artifact.component in {"agent-worker", "chat-worker"}:
            from .deployment_runtime_chat import ChatRuntimeReader

            return ChatRuntimeReader(kubernetes=self.kubernetes).inspect(
                scoped, description, namespace, artifact=artifact, artifact_hash=artifact_hash, evidence_ref=evidence_ref
            )
        if artifact.component == "tools-facade":
            from .deployment_runtime_tools import verify_tools_facade

            kube = self.kubernetes(scoped, description, namespace)
            try:
                return verify_tools_facade(kube, artifact, artifact_hash, evidence_ref)
            finally:
                kube.close()
        if artifact.component == "knowledge-door":
            from .deployment_runtime_tools import verify_door_identity

            kube = self.kubernetes(scoped, description, namespace)
            try:
                return verify_door_identity(kube, artifact, artifact_hash, evidence_ref, environment)
            finally:
                kube.close()
        if artifact.component == "insights":
            require(namespace == "adp-insights" and artifact.image_digest is not None, "deployment_insights_release_image_missing")
            bucket = f"adp-{environment}-insights-evidence"
            s3 = scoped.client("s3", config=AWS_BOUNDS)
            s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=artifact.account_id)
            version = s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=artifact.account_id)
            require(version.get("Status") == "Enabled", "deployment_insights_versioning_missing")
            encryption = s3.get_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=artifact.account_id)
            rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            require(
                len(rules) == 1
                and rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "aws:kms"
                and rules[0]["ApplyServerSideEncryptionByDefault"]
                .get("KMSMasterKeyID", "")
                .startswith(f"arn:aws:kms:{artifact.region}:{artifact.account_id}:key/"),
                "deployment_insights_encryption_missing",
            )
            block = s3.get_public_access_block(Bucket=bucket, ExpectedBucketOwner=artifact.account_id)["PublicAccessBlockConfiguration"]
            require(
                all(block.get(name) is True for name in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")),
                "deployment_insights_bucket_public",
            )
            kube = self.kubernetes(scoped, description, namespace)
            try:
                uids = kube.insights(
                    digest=artifact.image_digest,
                    account=artifact.account_id,
                    region=artifact.region,
                    environment=environment,
                    public_client=self.public_client,
                )
            finally:
                kube.close()
            return RuntimeComponent(
                component="insights",
                actual_revision=artifact.source_revision,
                artifact_hash=artifact_hash,
                image_digest=artifact.image_digest,
                healthy=True,
                evidence_ref=evidence_ref,
                observed_at=datetime.now(UTC),
                pod_uids=uids,
                asset_count=len(artifact.assets),
            )
        if artifact.component == "gateway-frontend":
            count = self.frontend(scoped, artifact, environment)
            return RuntimeComponent(
                component=artifact.component,
                actual_revision=artifact.source_revision,
                artifact_hash=artifact_hash,
                healthy=True,
                evidence_ref=evidence_ref,
                observed_at=datetime.now(UTC),
                asset_count=count,
            )
        product = artifact if artifact.component == "gateway-backend" and artifact.product_profile_schema_version is not None else None
        if product is not None:
            require(
                product.product_profile_enabled is not None
                and product.product_onboarding_enabled is not None
                and product.migration_head is not None
                and product.cli_manifest_sha256 is not None,
                "deployment_product_release_incomplete",
            )
        kube = self.kubernetes(scoped, description, namespace)
        try:
            options = {"product": product} if product is not None else {}
            uids, head = kube.gateway(
                digest=artifact.image_digest,
                source_revision=artifact.source_revision,
                account=artifact.account_id,
                region=artifact.region,
                **options,
            )
        finally:
            kube.close()
        if product is not None:
            require(head == product.migration_head, "deployment_product_schema_mismatch")
        tick_digest = None
        if artifact.component == "gateway-backend":
            function = scoped.client("lambda", config=AWS_BOUNDS).get_function(
                FunctionName=f"arn:aws:lambda:{artifact.region}:{artifact.account_id}:function:adp-{environment}-orchestration-tick"
            )
            require(
                function["Configuration"].get("State") == "Active" and function["Configuration"].get("LastUpdateStatus") == "Successful",
                "deployment_tick_not_ready",
            )
            image = function["Code"].get("ResolvedImageUri", "")
            require(
                image == f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-gateway@{artifact.image_digest}",
                "deployment_tick_revision_mismatch",
            )
            tick_digest = artifact.image_digest
        return RuntimeComponent(
            component=artifact.component,
            actual_revision=artifact.source_revision,
            artifact_hash=artifact_hash,
            image_digest=artifact.image_digest,
            healthy=True,
            evidence_ref=evidence_ref,
            observed_at=datetime.now(UTC),
            migration_head=head,
            cli_manifest_sha256=product.cli_manifest_sha256 if product is not None else None,
            product_profile_enabled=product.product_profile_enabled if product is not None else None,
            product_onboarding_enabled=product.product_onboarding_enabled if product is not None else None,
            tick_digest=tick_digest,
            pod_uids=uids,
        )

    def frontend(self, scoped, artifact, environment):
        ssm = scoped.client("ssm", config=AWS_BOUNDS)
        distribution_id = ssm.get_parameter(Name=f"/adp/{environment}/gateway/cloudfront-id")["Parameter"]["Value"]
        distribution = scoped.client("cloudfront", config=AWS_BOUNDS).get_distribution(Id=distribution_id)["Distribution"]
        require(
            distribution.get("ARN") == f"arn:aws:cloudfront::{artifact.account_id}:distribution/{distribution_id}",
            "deployment_frontend_account_mismatch",
        )
        require(
            distribution.get("Status") == "Deployed" and distribution["DistributionConfig"].get("Enabled") is True, "deployment_frontend_not_ready"
        )
        domain = distribution["DomainName"]
        require(bool(re.fullmatch(r"[a-z0-9]+\.cloudfront\.net", domain)), "deployment_frontend_domain_invalid")
        owned = self.public_client is None
        client = self.public_client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        total = 0
        try:
            for path, expected in artifact.assets.items():
                payload = read_bytes(client, "https://" + domain + "/" + quote(path, safe="/"))
                total += len(payload)
                require(total <= 32 * 1024 * 1024, "deployment_frontend_response_limit")
                require(hashlib.sha256(payload).hexdigest() == expected, "deployment_frontend_artifact_mismatch")
        finally:
            if owned:
                client.close()
        return len(artifact.assets)
