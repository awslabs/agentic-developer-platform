"""Real stale-image and failed-rollout fixtures inside one owned namespace."""

from copy import deepcopy
import json
import re
import time

from tests.e2e.orchestration.fixtures import FixtureRequest, provision, resource_units
from tests.e2e.orchestration.report import Intervention
from .http import Unsupported
from .kubernetes import ScopedKubernetes, KubernetesUnavailable
from .native_probe import execute_source

LABEL = "adp-qualification-id"
SOURCE = """import json,sys
from src.orchestration.deployment_runtime_reader import KubernetesRuntime
from src.orchestration.review_cycle import CycleBlockedError
request=json.loads(sys.argv[1])
class CapturedRuntime(KubernetesRuntime):
    def get(self, path, **params):
        if path != request["deployment_path"] or params:
            raise ValueError("negative probe progressed beyond authenticated deployment capture")
        return request["deployment"]
reader=CapturedRuntime.__new__(CapturedRuntime)
reader.namespace=request["namespace"]
try:
    reader.gateway(digest=request["digest"], source_revision=request["source_revision"], account=request["account"], region=request["region"])
    value={"status":"UNEXPECTED_ACCEPTANCE"}
except CycleBlockedError as exc:
    value={"status":"BLOCKED", "reason":exc.reason}
except Exception as exc:
    value={"status":"NOT_RUN", "reason":type(exc).__name__}
print("ADP_Q2_RESULT:"+json.dumps(value),flush=True)
"""


class RuntimeFixtureProvider:
    def __init__(self, client, manifest, kind):
        self.client, self.manifest, self.kind = client, manifest, kind
        self.image = None

    def check_deadline(self):
        if time.monotonic() >= self.client.deadline:
            raise Unsupported(
                "qualification duration exhausted before runtime mutation"
            )

    def kube(self):
        return ScopedKubernetes(self.client, self.manifest.runtime["engine"].cluster)

    def path(self, namespace):
        if not re.fullmatch(r"q-[a-z0-9-]{8,50}", namespace):
            raise Unsupported("runtime fault requires a qualification namespace")
        return {
            "qualification-namespace": f"/api/v1/namespaces/{namespace}",
            "qualification-network-policy": f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/deny-all",
            "qualification-runtime": f"/apis/apps/v1/namespaces/{namespace}/deployments/bedrockgateway",
        }[self.kind]

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        self.check_deadline()
        namespace = ownership_tags["adp:qualification-id"]
        if (
            intended_identity
            != namespace
            + "/"
            + {
                "qualification-namespace": "runtime-namespace",
                "qualification-network-policy": "runtime-network",
                "qualification-runtime": "runtime-deployment",
            }[self.kind]
        ):
            raise Unsupported("runtime fixture identity differs from inventory")
        metadata = {"labels": {LABEL: namespace}, "annotations": ownership_tags}
        if self.kind == "qualification-namespace":
            body = {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {**metadata, "name": namespace},
            }
        elif self.kind == "qualification-network-policy":
            body = {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {**metadata, "name": "deny-all"},
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            }
        else:
            if not self.image:
                raise Unsupported("verified prerequisite image unavailable")
            body = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {**metadata, "name": "bedrockgateway"},
                "spec": {
                    "replicas": 1,
                    "revisionHistoryLimit": 0,
                    "strategy": {"type": "Recreate"},
                    "progressDeadlineSeconds": 60,
                    "selector": {
                        "matchLabels": {"app": "bedrockgateway", LABEL: namespace}
                    },
                    "template": {
                        "metadata": {
                            "labels": {"app": "bedrockgateway", LABEL: namespace}
                        },
                        "spec": {
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 1,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 65534,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "bedrockgateway",
                                    "image": self.image,
                                    "command": [
                                        "python",
                                        "-c",
                                        "import time; time.sleep(600)",
                                    ],
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "10m", "memory": "32Mi"},
                                        "limits": {"cpu": "100m", "memory": "96Mi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            }
        kube = self.kube()
        if self.kind != "qualification-namespace":
            actual = kube.get(f"/api/v1/namespaces/{namespace}")
            if not all(
                actual["metadata"].get("annotations", {}).get(k) == v
                for k, v in ownership_tags.items()
            ):
                raise Unsupported(
                    "namespace is not exclusively owned by this qualification"
                )
        path = self.path(namespace)
        result = kube.request("POST", path.rsplit("/", 1)[0], body)
        return json.dumps(
            {"namespace": namespace, "uid": result["metadata"]["uid"]}, sort_keys=True
        )

    def find(self, *, intended_identity, idempotency_token):
        namespace = intended_identity.split("/", 1)[0]
        try:
            result = self.kube().get(self.path(namespace))
        except KubernetesUnavailable as exc:
            if exc.status_code == 404:
                return None
            raise
        if result["metadata"].get("labels", {}).get(LABEL) != namespace:
            raise Unsupported("existing runtime fixture has a different owner")
        return json.dumps(
            {"namespace": namespace, "uid": result["metadata"]["uid"]}, sort_keys=True
        )

    def read_tags(self, resource_id):
        resource = json.loads(resource_id)
        try:
            result = self.kube().get(self.path(resource["namespace"]))
        except KubernetesUnavailable as exc:
            if exc.status_code == 404:
                return self.client.config.ownership_tags(resource["namespace"])
            raise
        if result["metadata"]["uid"] != resource["uid"]:
            return None
        return {
            k: result["metadata"].get("annotations", {}).get(k)
            for k in self.client.config.ownership_tags(resource["namespace"])
        }

    def inject(self, name, resource_id):
        self.check_deadline()
        resource = json.loads(resource_id)
        kube, path = self.kube(), self.path(resource["namespace"])
        current = kube.get(path)
        if current["metadata"]["uid"] != resource["uid"] or self.read_tags(
            resource_id
        ) != self.client.config.ownership_tags(resource["namespace"]):
            raise Unsupported("runtime fixture ownership changed")
        if name == "stale-image":
            return {
                "operation": "retain-verified-prerequisite-image",
                "deployment": current,
            }
        if name != "failed-deploy":
            raise Unsupported("runtime fixture does not support this operation")
        changed = deepcopy(current)
        changed.pop("status", None)
        container = changed["spec"]["template"]["spec"]["containers"][0]
        container["readinessProbe"] = {
            "exec": {"command": ["python", "-c", "raise SystemExit(1)"]},
            "periodSeconds": 1,
            "failureThreshold": 1,
        }
        # PUT includes current resourceVersion and UID. Only this owned
        # namespace/deployment can lose readiness; no shared resource is touched.
        return {
            "operation": "fail-fixture-readiness",
            "deployment": kube.request("PUT", path, changed),
        }

    def delete(self, resource_id):
        resource = json.loads(resource_id)
        kube, path = self.kube(), self.path(resource["namespace"])
        try:
            kube.request(
                "DELETE",
                path,
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": resource["uid"]},
                    "propagationPolicy": "Foreground",
                },
            )
        except KubernetesUnavailable as exc:
            if exc.status_code == 404:
                return
            raise
        for _ in range(20):
            try:
                kube = self.kube()
                kube.get(path)
            except KubernetesUnavailable as exc:
                if exc.status_code == 404:
                    return
                raise
            time.sleep(1)
        raise Unsupported(
            "runtime fixture deletion has not completed; inventory retained"
        )


def exercise(name, session):
    from .faults import CASES, inject

    if not session.manifest.native_faults:
        raise Unsupported(
            "runtime fault injection is not enabled in the accepted manifest"
        )
    if time.monotonic() >= session.client.deadline:
        raise Unsupported("qualification duration exhausted")
    if session.manifest.runtime["engine"].ecr_repository != "adp-gateway":
        raise Unsupported(
            "native gateway verification requires the adp-gateway repository"
        )
    prerequisite = session.prerequisite_runtime["engine"]
    desired = session.runtime_observations.get("second")
    if not desired or prerequisite["digest"] == desired["digest"]:
        raise Unsupported(
            "stale-image probe needs two observed distinct delivery digests"
        )
    namespace = session.inventory.qualification_id
    if not any(
        r.fixture_id == "runtime-deployment" for r in session.inventory.fixtures
    ):
        # Three accounted objects, all created after durable inventory intents.
        if (
            sum(resource_units(r.kind) for r in session.inventory.unresolved) + 7
            > session.config.max_resources
        ):
            raise Unsupported("runtime fixtures exceed the accepted resource bound")
        for suffix, kind in (
            ("namespace", "qualification-namespace"),
            ("network", "qualification-network-policy"),
            ("deployment", "qualification-runtime"),
        ):
            provider = session.providers[kind]
            provider.client = session.client
            provider.image = f"{prerequisite['account_id']}.dkr.ecr.{prerequisite['cluster'].split(':')[3]}.amazonaws.com/{session.manifest.runtime['engine'].ecr_repository}@{prerequisite['digest']}"
            provision(
                session.inventory,
                session.config,
                provider,
                FixtureRequest(
                    "runtime-" + suffix, kind, namespace + "/runtime-" + suffix
                ),
            )
    provider = session.providers["qualification-runtime"]
    provider.client = session.client
    path = provider.path(namespace)
    # Establish healthy old-image readiness before either fault. Otherwise a
    # pre-existing pull/scheduling failure could masquerade as our injected fault.
    while time.monotonic() < session.client.deadline:
        baseline = provider.kube().get(path)
        status = baseline.get("status", {})
        if (
            status.get("observedGeneration", 0) >= baseline["metadata"]["generation"]
            and status.get("availableReplicas") == status.get("updatedReplicas") == 1
        ):
            break
        time.sleep(session.manifest.poll_seconds)
    else:
        raise Unsupported("runtime fixture never reached healthy baseline")
    intent = session.evidence.save(
        "runtime-fault-intent",
        {"name": name, "namespace": namespace},
        "Q2:fixed-runtime-fault",
    )
    session.interventions.append(
        Intervention(
            at=intent.observed_at,
            actor="qualification-harness",
            kind="fault",
            target=name,
            evidence=intent,
        )
    )
    injected = inject(
        CASES["A6-3." + name],
        fixture_id="runtime-deployment",
        inventory=session.inventory,
        config=session.config,
        providers=session.providers,
    )
    while time.monotonic() < session.started + session.config.max_duration_seconds:
        deployment = provider.kube().get(path)
        status = deployment.get("status", {})
        observed = (
            status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        )
        ready = (
            status.get("availableReplicas", 0) == 1
            and status.get("updatedReplicas", 0) == 1
        )
        if observed and (
            (name == "stale-image" and ready)
            or (
                name == "failed-deploy"
                and status.get("updatedReplicas") == 1
                and status.get("availableReplicas", 0) == 0
                and deployment["metadata"]["generation"]
                > baseline["metadata"]["generation"]
            )
        ):
            if name == "failed-deploy":
                try:
                    pods = readiness_failure(provider, deployment, namespace)
                except Unsupported:
                    time.sleep(session.manifest.poll_seconds)
                    continue
            else:
                pods = []
            break
        time.sleep(session.manifest.poll_seconds)
    else:
        raise Unsupported(
            "fixture rollout did not reach the fault boundary within the accepted duration"
        )
    proof = execute_source(
        session.client,
        session.manifest.runtime["engine"],
        SOURCE,
        {
            "deployment": deployment,
            "deployment_path": path,
            "namespace": namespace,
            "digest": desired["digest"],
            "source_revision": desired["actual_revision"],
            "account": desired["account_id"],
            "region": desired["cluster"].split(":")[3],
        },
        runtime=desired,
    )
    return {
        "baseline": baseline,
        "pods": pods,
        "namespace": namespace,
        "injection": injected,
        "runtime": {
            "actual_revision": prerequisite["actual_revision"],
            "required_revision": desired["actual_revision"],
            "actual_digest": prerequisite["digest"],
            "required_digest": desired["digest"],
            "deployment": deployment,
        },
        "deployment": deployment,
        "after": proof,
        "boundary": "deployed KubernetesRuntime.gateway using fresh registered-role Kubernetes capture",
    }


def assert_runtime_outcome(name, observed):
    """Require native refusal and the authenticated fault generation."""
    namespace = observed["namespace"]
    assert re.fullmatch(r"q-[a-z0-9-]{8,50}", namespace)
    deployment, baseline = observed["deployment"], observed["baseline"]
    metadata, status = deployment["metadata"], deployment["status"]
    assert metadata["namespace"] == namespace
    assert metadata["name"] == "bedrockgateway"
    assert metadata["uid"] == baseline["metadata"]["uid"]
    assert metadata["labels"][LABEL] == namespace
    assert not metadata.get("deletionTimestamp")
    assert status["observedGeneration"] >= metadata["generation"]
    assert (
        baseline["status"]["availableReplicas"]
        == baseline["status"]["updatedReplicas"]
        == 1
    )
    assert baseline["spec"]["replicas"] == deployment["spec"]["replicas"] == 1
    native = observed["after"]
    assert native["status"] == "BLOCKED"
    runtime = observed["runtime"]
    assert runtime["actual_digest"] != runtime["required_digest"]
    assert runtime["actual_revision"] != runtime["required_revision"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith("/adp-gateway@" + runtime["actual_digest"])
    assert (
        container["image"]
        == baseline["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    if name == "stale-image":
        assert native["reason"] == "deployment_gateway_revision_mismatch"
        assert status["availableReplicas"] == status["updatedReplicas"] == 1
        assert (
            observed["injection"]["operation"] == "retain-verified-prerequisite-image"
        )
    else:
        assert native["reason"] == "deployment_gateway_rollout_incomplete"
        assert len(observed["pods"]) == 1
        pod = observed["pods"][0]
        assert pod["status"]["phase"] == "Running"
        assert pod["status"]["containerStatuses"][0]["ready"] is False
        assert pod["status"]["containerStatuses"][0]["state"]["running"]
        assert metadata["generation"] > baseline["metadata"]["generation"]
        assert (
            status["updatedReplicas"] == 1 and status.get("availableReplicas", 0) == 0
        )
        assert observed["injection"]["operation"] == "fail-fixture-readiness"
        assert container["readinessProbe"]["exec"]["command"] == [
            "python",
            "-c",
            "raise SystemExit(1)",
        ]


def readiness_failure(provider, deployment, namespace):
    """A new running-but-unready Pod, not an image pull or scheduling failure."""
    kube = provider.kube()
    selector = "?labelSelector=adp-qualification-id%3D" + namespace
    replicas = kube.get(f"/apis/apps/v1/namespaces/{namespace}/replicasets" + selector)[
        "items"
    ]
    uids = {
        r["metadata"]["uid"]
        for r in replicas
        if any(
            o.get("uid") == deployment["metadata"]["uid"]
            and o.get("controller") is True
            for o in r["metadata"].get("ownerReferences", [])
        )
        and r["spec"]["template"]["spec"]["containers"]
        == deployment["spec"]["template"]["spec"]["containers"]
    }
    pods = [
        p
        for p in kube.get(f"/api/v1/namespaces/{namespace}/pods" + selector)["items"]
        if not p["metadata"].get("deletionTimestamp")
        and any(
            o.get("uid") in uids and o.get("controller") is True
            for o in p["metadata"].get("ownerReferences", [])
        )
    ]
    if len(pods) != 1:
        raise Unsupported("failed readiness has no exact current Pod lineage")
    pod = pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if (
        pod.get("status", {}).get("phase") != "Running"
        or len(statuses) != 1
        or statuses[0].get("ready") is not False
        or not statuses[0].get("state", {}).get("running")
    ):
        raise Unsupported("failure is not a running fixture's readiness rejection")
    return pods
