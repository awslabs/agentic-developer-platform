"""Pinned cleanup transport using the operation-delivered AWS session only."""

import asyncio
import uuid
from contextlib import asynccontextmanager

from superplane_bootstrap.kube_grants import KubeGrants

from .cluster_clients import kubernetes_client
from .credentials import assume_session, canonical_role_identity
from .process import WorkerProcesses
from .runtime_config import LifecycleRefused
from .terraform import operation_directory


def open_cleanup_client(operation, context, config, session, target, outputs, verify):
    """Runs in a thread; refresh callbacks join the current async authority."""
    role = f"arn:aws:iam::{target.account_id}:role/{config['actor_role_names']['installer']}"
    actor = assume_session(session, role_arn=role, region=target.region, verify=verify)
    canonical_role_identity(actor, session, role, verify=verify)
    lease = operation.grant.lease
    directory = operation_directory(
        context.state_root,
        lease.org_id,
        lease.workspace_id,
        lease.operation_id,
        create=True,
    ) / ("cleanup-client-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    process = WorkerProcesses(
        binaries=config["binaries"],
        directory=directory,
        session=actor,
        region=target.region,
        verify=verify,
    )
    dynamic = kubernetes_client(process, outputs, name="cleanup")
    try:
        return KubeGrants(dynamic, target)
    except BaseException:
        dynamic.client.close()
        raise


@asynccontextmanager
async def cleanup_client(facts, effects, clients):
    from .retirement_access_clients import access_delivery_session

    operation, context = facts.operation, effects.context
    await effects.authority()
    session = await access_delivery_session(
        operation, context, clients.target.account_id, clients.target.region
    )
    loop = asyncio.get_running_loop()

    def verify():
        future = asyncio.run_coroutine_threadsafe(effects.authority(), loop)
        try:
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    grant = facts.plan.grants[0]
    identity = await asyncio.to_thread(clients.eks.observe, grant)
    if identity is None:
        raise LifecycleRefused("cleanup EKS mapping must exist before fence activation")
    clients.eks.verify(grant, identity)
    grants = await asyncio.to_thread(
        open_cleanup_client,
        operation,
        context,
        facts.config,
        session,
        clients.target,
        clients.outputs,
        verify,
    )
    try:
        yield grants
    finally:
        await asyncio.to_thread(grants.client.client.close)
