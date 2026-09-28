"""Stateful grant contracts beneath the real kubectl integration adapters."""

import base64
from copy import deepcopy
import json

from superplane_bootstrap.access import ProviderIdentity
from superplane_bootstrap.authority_backend import BootstrapClients
from superplane_bootstrap.authority_runtime import BootstrapAuthorityFactory
from superplane_bootstrap.grant_plan import BootstrapRelease
from superplane_bootstrap.target import verify_target

from .test_authority_runtime_postgres import Cloud


def compose_authority(cluster, path, arguments):
    target = verify_target(
        **{
            key: arguments[key]
            for key in (
                "binding",
                "provider",
                "expected_account_id",
                "expected_region",
                "expected_cluster_name",
                "expected_cluster_arn",
                "expected_certificate_authority_data",
                "cluster_ownership",
            )
        },
        observed=arguments["observed_cluster"],
    )
    if not hasattr(cluster, "authority_cloud"):
        cluster.authority_cloud = Cloud(target, cluster)
        for name, metadata in cluster.namespaces.items():
            cluster.authority_cloud.objects[("Namespace", None, name)] = {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": deepcopy(metadata),
            }
    cloud = cluster.authority_cloud

    def namespace_created(body):
        from .test_integration import NAMESPACE_UID

        body["metadata"]["uid"] = NAMESPACE_UID
        cluster.namespaces[body["metadata"]["name"]] = deepcopy(body["metadata"])
        cluster.namespace_created_at = len(cluster.commands)

    cloud.namespace_created = namespace_created
    principals = {
        actor: f"arn:aws:iam::{target.account_id}:role/{actor}"
        for actor in ("registrar", "installer", "supervisor")
    }
    ca = path / "authority-ca.pem"
    ca.write_bytes(base64.b64decode(target.certificate_authority_data))

    class Runner:
        def __init__(self, actor):
            self.actor = actor

        def run(self, argv, *, data=None, timeout=120):
            result = cluster.run(argv, data=data, timeout=timeout)
            body = json.loads(data) if data and data.startswith("{") else {}
            if (
                body.get("metadata", {})
                .get("annotations", {})
                .get("superplane.aws-e/component-creation")
            ):
                for (kind, namespace, name), value in cluster.components.items():
                    cloud.objects[(kind, namespace or None, name)] = deepcopy(value)
            if body.get("kind") in {"SelfSubjectAccessReview", "SubjectAccessReview"}:
                spec = body["spec"]
                attrs = spec["resourceAttributes"]
                resource = attrs["resource"] + (
                    "." + attrs["group"] if attrs.get("group") else ""
                )
                existing = json.loads(result.stdout)["status"]["allowed"]
                if body["kind"] == "SubjectAccessReview" and spec.get(
                    "user", ""
                ).startswith("system:serviceaccount:"):
                    return result
                allowed = cloud.allowed(
                    principals[self.actor],
                    verb=attrs["verb"],
                    resource=resource,
                    namespace=attrs.get("namespace"),
                    name=attrs.get("name"),
                    groups=spec.get("groups")
                    if body["kind"] == "SubjectAccessReview"
                    else None,
                )
                if body["kind"] == "SelfSubjectAccessReview":
                    allowed = existing and allowed
                return cluster._ok(tuple(argv), {"status": {"allowed": allowed}})
            joined = " ".join(argv)
            kind = (
                "ClusterRoleBinding"
                if "get clusterrolebindings" in joined
                else "RoleBinding"
                if "get rolebindings" in joined
                else None
            )
            if kind:
                namespace = argv[argv.index("-n") + 1] if "-n" in argv else None
                payload = json.loads(result.stdout)
                payload["items"] += [
                    deepcopy(value)
                    for (k, ns, _), value in cloud.objects.items()
                    if k == kind and ns == namespace
                ]
                return cluster._ok(tuple(argv), payload)
            return result

    from dataclasses import replace

    access = arguments["access"]
    installer = replace(access, runner=Runner("installer"))
    supervisor = replace(access, runner=Runner("supervisor"))
    installer.controller_mode = supervisor.controller_mode = access.controller_mode
    binding = arguments["binding"]
    clients = BootstrapClients(
        binding,
        target,
        principals,
        lambda op, **kw: binding,
        {
            actor: lambda p=p: ProviderIdentity(target.account_id, p)
            for actor, p in principals.items()
        },
        cloud,
        cloud.entry_client,
        cloud.kube(principals["registrar"], ca),
        cloud.kube(principals["supervisor"], ca),
        installer,
        supervisor,
    )
    release = BootstrapRelease(
        arguments["namespace"],
        access.controller_service_account,
        "superplane-controller",
        arguments["enforce_version"],
        tuple(arguments["required_crds"]),
        tuple(arguments["required_system_workloads"]),
    )

    def resolve_observation(authority):
        from datetime import UTC, datetime, timedelta
        from superplane_bootstrap.management_observation import ManagementObservation

        observer = ManagementObservation(
            origin="https://api.example.invalid",
            credential="sp-bootstrap-read-" + "fixture" * 8,
            binding=binding,
            target=target,
            namespace=release.namespace,
            claim=authority.journal.claim,
        )

        def observe():
            now = datetime.now(UTC)
            document = {
                **observer._expected,
                "registry_ready": True,
                "target_status": "observed_execution_unavailable",
                "last_reconciled": now.isoformat(),
                "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
            }
            observer.verify(document)
            return document

        observer.observe = observe
        return observer

    return BootstrapAuthorityFactory(
        lambda *_: clients, release, resolve_observation=resolve_observation
    )
