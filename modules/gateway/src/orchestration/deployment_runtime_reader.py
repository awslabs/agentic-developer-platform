"""Read actual pilot runtime through the registered role; no ambient cluster token."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx
from botocore.config import Config
from botocore.signers import RequestSigner
from websockets.sync.client import connect

from .deployment_runtime_contract import RuntimeComponent
from .review_cycle import CycleBlockedError

AWS_BOUNDS = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
MAX_RESPONSE = 8 * 1024 * 1024
SCHEMA_PROBE = """import asyncio,json
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
        print(json.dumps({'database_heads':sorted(rows),'image_heads':sorted(ScriptDirectory.from_config(Config('/app/alembic.ini')).get_heads())}))
    await engine.dispose()
asyncio.run(asyncio.wait_for(probe(), timeout=12))
"""


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason)


def read_bytes(client, url, **kwargs):
    with client.stream("GET", url, **kwargs) as response:
        response.raise_for_status()
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
    def __init__(self, scoped, description, namespace, *, client=None, websocket=connect):
        self.scoped, self.description, self.namespace, self.websocket = scoped, description, namespace, websocket
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

    def schema(self, pod):
        name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)), "deployment_pod_name_invalid")
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
        current = self.get(f"/api/v1/namespaces/{quote(self.namespace, safe='')}/pods/{quote(name, safe='')}")
        require(current.get("metadata", {}).get("uid") == uid and not current["metadata"].get("deletionTimestamp"), "deployment_probe_pod_replaced")
        proof = json.loads(output)
        heads = proof.get("database_heads")
        require(isinstance(heads, list) and len(heads) == 1 and heads == proof.get("image_heads"), "deployment_migration_not_at_single_head")
        require(isinstance(heads[0], str) and 1 <= len(heads[0]) <= 128, "deployment_migration_head_invalid")
        return heads[0]

    def gateway(self, *, digest, source_revision, account, region):
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
            heads.add(self.schema(pod))
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
    def __init__(self, *, kubernetes=KubernetesRuntime, public_client=None):
        self.kubernetes, self.public_client = kubernetes, public_client

    def inspect(self, scoped, description, namespace, *, artifact, artifact_hash, evidence_ref, environment):
        require(environment in {"dev", "staging", "prod"}, "deployment_environment_unsupported")
        require(
            description["name"] + "/" + namespace == artifact.resource_id
            and scoped.region_name == artifact.region
            and description.get("arn") == f"arn:aws:eks:{artifact.region}:{artifact.account_id}:cluster/{description['name']}",
            "deployment_runtime_target_changed",
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
        kube = self.kubernetes(scoped, description, namespace)
        try:
            uids, head = kube.gateway(
                digest=artifact.image_digest, source_revision=artifact.source_revision, account=artifact.account_id, region=artifact.region
            )
        finally:
            kube.close()
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
