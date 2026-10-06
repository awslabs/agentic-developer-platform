"""Real operation-delivered clients for separately approved cleanup access."""

import asyncio
import json
from dataclasses import dataclass

from .cluster_clients import kubernetes_client, scoped_entry_client
from .credentials import assume_session, canonical_role_identity
from .process import WorkerProcesses
from .retirement_access_context import current_access_operation
from .runtime_config import LifecycleRefused


async def access_delivery_session(operation, context, account_id, region):
    """The dedicated control validator must guard delivery and every STS refresh."""
    current = await current_access_operation(operation, context)
    delivered = await context.authority.delivery_role(current)
    if not delivered["role_arn"].startswith(f"arn:aws:iam::{account_id}:role/"):
        raise LifecycleRefused("cleanup provider role names another AWS account")
    loop = asyncio.get_running_loop()

    async def current_delivery():
        latest = await current_access_operation(operation, context)
        if await context.authority.delivery_role(latest) != delivered:
            raise LifecycleRefused("approved cleanup credential reference changed")

    def verify():
        pending = asyncio.run_coroutine_threadsafe(current_delivery(), loop)
        try:
            return pending.result(timeout=20)
        except BaseException:
            pending.cancel()
            raise

    return await asyncio.to_thread(
        assume_session,
        context.base_session,
        role_arn=delivered["role_arn"],
        region=region,
        verify=verify,
        external_id=delivered.get("external_id"),
    )


def require_cluster(cluster, outputs):
    if (
        not isinstance(cluster, dict)
        or cluster.get("arn") != outputs["cluster_arn"]
        or cluster.get("name") != outputs["cluster_name"]
        or cluster.get("endpoint") != outputs["cluster_endpoint"]
        or cluster.get("certificateAuthority", {}).get("data")
        != outputs["cluster_certificate_authority_data"]
        or cluster.get("status") != "ACTIVE"
        or cluster.get("accessConfig", {}).get("authenticationMode") != "API"
    ):
        raise LifecycleRefused("cleanup cluster differs from its recorded identity")


class GuardedClient:
    """Check current authority immediately around every explicit SDK operation."""

    def __init__(self, client, verify):
        self.client, self.verify = client, verify

    def __getattr__(self, name):
        method = getattr(self.client, name)

        def guarded(**arguments):
            self.verify()
            result = method(**arguments)
            self.verify()
            return result

        return guarded


@dataclass
class AccessClients:
    eks: object
    kubernetes: object
    supervisor: object
    target: object
    outputs: dict
    inventory: object
    verify: object
    dynamic_clients: tuple
    provider_eks: object

    def observe_target(self):
        self.verify()
        require_cluster(
            self.provider_eks.describe_cluster(name=self.target.cluster_name).get(
                "cluster"
            ),
            self.outputs,
        )
        # Namespace GET is cluster-scoped. Use only the original retained
        # supervisor's verified read grants, never the new registrar AdminPolicy.
        retained = [
            owned
            for owned in self.inventory.grants
            if owned.spec.get("actor") == "supervisor"
        ]
        if (
            not any(item.spec.get("kind") == "eks-entry" for item in retained)
            or not any(
                item.spec.get("body", {}).get("kind") == "ClusterRole"
                for item in retained
            )
            or not any(
                item.spec.get("body", {}).get("kind") == "ClusterRoleBinding"
                for item in retained
            )
        ):
            raise LifecycleRefused(
                "cleanup lacks its retained supervisor read authority"
            )
        for owned in retained:
            adapter = self.eks if owned.spec["kind"] == "eks-entry" else self.supervisor
            self.verify()
            current = adapter.observe(owned.spec)
            if current != owned.identity:
                raise LifecycleRefused("retained supervisor authority changed")
            adapter.verify(owned.spec, current)
        self.supervisor._verify_transport()
        namespace = self.supervisor.client.resources.get(
            api_version="v1", kind="Namespace"
        ).get(name=self.inventory.namespace)
        namespace = namespace.to_dict() if hasattr(namespace, "to_dict") else namespace
        if (
            not isinstance(namespace, dict)
            or namespace.get("metadata", {}).get("name") != self.inventory.namespace
            or namespace.get("metadata", {}).get("uid") != self.inventory.namespace_uid
            or namespace.get("metadata", {}).get("deletionTimestamp")
        ):
            raise LifecycleRefused("cleanup namespace was replaced or is terminating")
        self.verify()

    async def verify_target(self):
        await asyncio.to_thread(self.observe_target)

    def close(self):
        for client in self.dynamic_clients:
            client.client.close()


def build_access_clients(facts, session, directory, verify):
    """Construct clients from immutable discovery and operation-delivered sessions.

    Called in a worker thread; verify synchronously joins the async authority
    resolver. This function performs no EKS/Kubernetes mutation.
    """
    from account_factory.modes import from_mapping
    from superplane_bootstrap.access import ClusterIdentity
    from superplane_bootstrap.eks_grants import EksGrants
    from superplane_bootstrap.kube_grants import KubeGrants
    from superplane_bootstrap.target import verify_target
    from superplane_contracts.provisioning import OperationBinding, ResolvedPrincipal

    from .adoption import verify_adoption_artifact
    from .retirement_managed_access import (
        ManagedRetirementAccessPlan,
        verify_managed_access_artifact,
    )

    operation, plan, config = facts.operation, facts.plan, facts.config
    lease = operation.grant.lease
    request = from_mapping(
        json.loads(operation.request.parameters["lifecycle_request"])
    )
    managed = isinstance(plan, ManagedRetirementAccessPlan)
    outputs = (
        verify_managed_access_artifact(facts.artifact, request, plan)
        if managed
        else verify_adoption_artifact(facts.artifact, request)
    )
    if outputs["cluster_arn"] != plan.cluster_arn:
        raise LifecycleRefused("cleanup plan changed its original discovered cluster")
    provider = canonical_role_identity(
        session, session, session._superplane_role_arn, verify=verify
    )
    eks_client = GuardedClient(
        session.client("eks", region_name=request.region), verify
    )
    cluster = eks_client.describe_cluster(name=outputs["cluster_name"])["cluster"]
    require_cluster(cluster, outputs)
    binding = OperationBinding(
        operation_id=lease.operation_id,
        principal=ResolvedPrincipal(
            subject=lease.holder, org_id=lease.org_id, workspace_id=lease.workspace_id
        ),
        action="provision",
        permission="workspace:provision",
        expires_at=lease.runtime_deadline,
    )
    target = verify_target(
        binding=binding,
        provider=provider,
        observed=ClusterIdentity(
            account_id=outputs["account_id"],
            region=request.region,
            name=cluster["name"],
            arn=cluster["arn"],
            endpoint=cluster["endpoint"],
            certificate_authority_data=cluster["certificateAuthority"]["data"],
            status=cluster["status"],
        ),
        expected_account_id=outputs["account_id"],
        expected_region=request.region,
        expected_cluster_name=outputs["cluster_name"],
        expected_cluster_arn=outputs["cluster_arn"],
        expected_certificate_authority_data=outputs[
            "cluster_certificate_authority_data"
        ],
        cluster_ownership="adp-created" if managed else "adopted",
    )
    dynamics = {}
    try:
        actors = (
            (
                ("registrar", "registrar"),
                ("supervisor", "supervisor"),
                ("cleaner", "installer"),
            )
            if not managed
            else (("supervisor", "supervisor"), ("cleaner", "installer"))
        )
        for actor, configured in actors:
            role_arn = f"arn:aws:iam::{target.account_id}:role/{config['actor_role_names'][configured]}"
            actor_session = assume_session(
                session, role_arn=role_arn, region=request.region, verify=verify
            )
            canonical_role_identity(actor_session, session, role_arn, verify=verify)
            if actor == "cleaner":
                # Confirm its exact IAM identity before binding it, but the access
                # preparation operation does not use the cleaner's permissions.
                continue
            actor_directory = directory / actor
            actor_directory.mkdir(mode=0o700)
            process = WorkerProcesses(
                binaries=config["binaries"],
                directory=actor_directory,
                session=actor_session,
                region=request.region,
                verify=verify,
            )
            dynamics[actor] = kubernetes_client(
                process, outputs, name="retirement-access"
            )
        clients = AccessClients(
            eks=EksGrants(
                eks_client,
                target,
                entry_client=scoped_entry_client(
                    session._superplane_source_session,
                    role_arn=session._superplane_role_arn,
                    region=request.region,
                    verify=verify,
                    external_id=session._superplane_external_id,
                ),
            ),
            kubernetes=KubeGrants(
                dynamics["supervisor" if managed else "registrar"], target
            ),
            supervisor=KubeGrants(dynamics["supervisor"], target),
            target=target,
            outputs=outputs,
            inventory=facts.inventory,
            verify=verify,
            dynamic_clients=tuple(dynamics.values()),
            provider_eks=eks_client,
        )
        clients.observe_target()
        return clients
    except BaseException:
        for dynamic in dynamics.values():
            dynamic.client.close()
        raise
