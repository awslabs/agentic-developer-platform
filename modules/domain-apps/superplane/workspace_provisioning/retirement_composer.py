"""Native managed retirement through the maintained paid execution protocol."""

import asyncio
import json
import secrets
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from harness_jobs.execution import CallOutcome, OperationExecutor
from harness_jobs.execution_rpc import ExecutionRPCServer
from harness_jobs.identity import encode_payload, payload_digest
from harness_jobs.store import OperationStore
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_contracts.provisioning import (
    REQUIRED_PERMISSION,
    OperationBinding,
    ResolvedPrincipal,
)

from .artifacts import read_artifact
from .authority import load_policy
from .process import AsyncBridgeStore, WorkerProcesses
from .retirement_control import resolve_managed_control
from .retirement_inventory import load_bootstrap_retirement_inventory
from .retirement_runtime import RetirementRuntime, verify_retirement_inventory
from .runtime_config import LifecycleRefused

PHASE = "retire-workspace"


def binding_for(operation):
    lease = operation.grant.lease
    return OperationBinding(
        operation_id=lease.operation_id,
        principal=ResolvedPrincipal(
            subject=lease.holder, org_id=lease.org_id, workspace_id=lease.workspace_id
        ),
        action="teardown",
        permission=REQUIRED_PERMISSION,
        expires_at=lease.runtime_deadline,
    )


async def current_retirement(operation, context):
    current = await context.authority.resolve(operation.grant.lease.operation_id)
    if (
        current.request != operation.request
        or current.request.action != "teardown"
        or current.request.parameters.get("lifecycle_phase") != PHASE
        or current.job_id != operation.job_id
        or current.plan_digest != operation.plan_digest
        or current.plan_digest != payload_digest(current.request)
        or current.request_payload != encode_payload(current.request)
        or current.reservation_state != "confirmed"
        or any(
            getattr(current.grant.lease, key) != getattr(operation.grant.lease, key)
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            )
        )
    ):
        raise LifecycleRefused("retirement execution authority changed")
    await context.authority.preflight(current)
    return current


async def retirement_delivery_session(operation, context, account_id, region):
    from .credentials import assume_session

    current = await current_retirement(operation, context)

    async def verify_current():
        await current_retirement(operation, context)

    if getattr(context, "brokered_provider", False):
        session = await context.authority.provider_session(
            current, region, verify=verify_current
        )
        if not session._superplane_role_arn.startswith(
            f"arn:aws:iam::{account_id}:role/"
        ):
            raise LifecycleRefused("retirement provider names another AWS account")
        return session
    delivered = await context.authority.delivery_role(current)
    if not delivered["role_arn"].startswith(f"arn:aws:iam::{account_id}:role/"):
        raise LifecycleRefused("retirement provider names another AWS account")
    loop = asyncio.get_running_loop()

    async def verify_delivery():
        latest = await current_retirement(operation, context)
        if await context.authority.delivery_role(latest) != delivered:
            raise LifecycleRefused("retirement credential authority changed")

    def verify():
        future = asyncio.run_coroutine_threadsafe(verify_delivery(), loop)
        try:
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    return await asyncio.to_thread(
        assume_session,
        context.base_session,
        role_arn=delivered["role_arn"],
        region=region,
        verify=verify,
        external_id=delivered.get("external_id"),
    )


class ReadWorkspace:
    """Read-only adapter over the same CA/endpoint-pinned cleanup client."""

    def __init__(self, grants, authorize):
        self.grants, self.authorize = grants, authorize

    @staticmethod
    def path(target, kind):
        versions = {
            "Job": ("apis/batch/v1", "jobs"),
            "Deployment": ("apis/apps/v1", "deployments"),
        }
        prefix, resource = versions.get(kind, ("api/v1", kind.lower() + "s"))
        return f"/{prefix}/namespaces/{target['namespace']}/{resource}"

    async def request(self, operation, target, method, path):
        from superplane_bootstrap.kube_grants import _payload
        from superplane_bootstrap.retirement_fence import WORKLOADS

        if method != "GET" or (target["cluster_arn"], target["endpoint"]) != (
            self.grants.target.cluster_arn,
            self.grants.target.endpoint,
        ):
            raise LifecycleRefused("retirement read changed its pinned target")
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "api" and parts[1] == "v1":
            version, tail = "v1", parts[2:]
        elif parts[0] == "apis" and len(parts) >= 4:
            version, tail = "/".join(parts[1:3]), parts[3:]
        else:
            raise LifecycleRefused("retirement read path is unsupported")
        options = {}
        if len(tail) == 3 and tail[0] == "namespaces":
            if tail[1] != target["namespace"]:
                raise LifecycleRefused("retirement read left its namespace")
            options["namespace"] = tail[1]
            tail = tail[2:]
        kinds = {(v, r): k for v, k, r in WORKLOADS}
        kinds[("v1", "secrets")] = "Secret"
        if len(tail) != 1 or (version, tail[0]) not in kinds:
            raise LifecycleRefused("retirement read resource is unsupported")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) - {"limit", "continue", "labelSelector"} or any(
            len(v) != 1 for v in query.values()
        ):
            raise LifecycleRefused("retirement read query is unsupported")
        options["limit"] = 100
        if "continue" in query:
            options["_continue"] = query["continue"][0]
        if "labelSelector" in query:
            if query["labelSelector"] != ["superplane.ai/capacity"]:
                raise LifecycleRefused("retirement read selector changed")
            options["label_selector"] = query["labelSelector"][0]
        await self.authorize()

        def read():
            self.grants._verify_transport()
            return _payload(
                self.grants.client.resources.get(
                    api_version=version, kind=kinds[(version, tail[0])]
                ).get(**options)
            )

        body = await asyncio.to_thread(read)
        await self.authorize()
        return SimpleNamespace(status_code=200, json=lambda: body)


async def run_retirement(operation, context):
    """Resolve every authority before provider I/O; retain uncertain outcomes."""
    from .retirement_access_artifact import validate_access_artifact
    from .retirement_adapters import OwnedResourceRemover, SecurityGroupRules
    from .retirement_clients import open_cleanup_client
    from .retirement_destroy_producer import reviewed_destroy_from_access
    from .retirement_fence import validate_fence_metadata
    from .retirement_fence import verify as verify_fence
    from .retirement_finalizer import RetirementFinalizer
    from .retirement_lifecycle import RetirementLifecycle
    from .retirement_observation import RetirementObservations
    from .retirement_request import retirement_request
    from .retirement_terraform import TerraformDestroy
    from .terraform import operation_directory

    loop = asyncio.get_running_loop()
    registration = SqlRegistrationStore(AsyncBridgeStore(context.domain_connect, loop))
    operation = await current_retirement(operation, context)
    lease = operation.grant.lease
    inventory = await asyncio.to_thread(
        load_bootstrap_retirement_inventory,
        registration_store=registration,
        binding=binding_for(operation),
    )
    verify_retirement_inventory(operation, inventory)
    access_plan, _paid = await resolve_managed_control(operation, inventory, context)
    access = await read_artifact(
        context.domain_connect,
        artifact_id=operation.request.parameters["retirement_access_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    validate_access_artifact(access, access_plan)
    async with context.connect() as connection:
        bootstrap = await OperationStore().get(
            connection,
            operation.grant.principal,
            operation.request.parameters["retirement_source_operation_id"],
        )
    if bootstrap is None:
        raise LifecycleRefused("retirement original bootstrap is unavailable")
    policy = load_policy(context, lease.org_id)
    request, plan = retirement_request(
        inventory, access_plan, access, bootstrap, policy
    )
    if request != operation.request:
        raise LifecycleRefused(
            "retirement request differs from the reconstructed approval"
        )
    artifact = reviewed_destroy_from_access(access, context)
    fence = validate_fence_metadata(
        json.loads(access["artifact_metadata_json"])["retirement_fence"]
    )
    config = policy["runtime"]
    session = await retirement_delivery_session(
        operation,
        context,
        operation.request.parameters["aws_account_id"],
        operation.request.parameters["region"],
    )
    revoked = threading.Event()

    async def authorize():
        if revoked.is_set():
            raise LifecycleRefused("retirement worker lost execution authority")
        current = await current_retirement(operation, context)
        expected, _ = retirement_request(
            inventory,
            access_plan,
            access,
            bootstrap,
            load_policy(context, lease.org_id),
        )
        if expected != current.request:
            raise LifecycleRefused("retirement policy or approval changed")
        return current

    def verify():
        future = asyncio.run_coroutine_threadsafe(authorize(), loop)
        try:
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    from .retirement_target import retirement_target

    target, outputs = await retirement_target(
        operation, context, access_plan, session, verify
    )
    grants = await asyncio.to_thread(
        open_cleanup_client,
        operation,
        context,
        config,
        session,
        target,
        outputs,
        verify,
    )
    async with AsyncExitStack() as stack:
        stack.push_async_callback(asyncio.to_thread, grants.client.client.close)
        from superplane_bootstrap.eks_grants import EksGrants

        from .cluster_clients import scoped_entry_client
        from .retirement_access_clients import GuardedClient

        eks = EksGrants(
            GuardedClient(session.client("eks", region_name=target.region), verify),
            target,
            entry_client=await asyncio.to_thread(
                scoped_entry_client,
                session._superplane_source_session,
                role_arn=session._superplane_role_arn,
                region=target.region,
                verify=verify,
                external_id=session._superplane_external_id,
            ),
        )

        async def cluster_state():
            await authorize()
            try:
                value = await asyncio.to_thread(
                    session.client("eks", region_name=target.region).describe_cluster,
                    name=target.cluster_name,
                )
            except Exception as error:
                if (
                    getattr(error, "response", {}).get("Error", {}).get("Code")
                    == "ResourceNotFoundException"
                ):
                    return "ABSENT"
                raise
            observed = value["cluster"]
            if (
                observed["arn"],
                observed["endpoint"],
                observed["certificateAuthority"]["data"],
            ) != (
                target.cluster_arn,
                target.endpoint,
                target.certificate_authority_data,
            ):
                raise LifecycleRefused("retirement cluster was replaced")
            return observed["status"]

        async def cluster_absent():
            return await cluster_state() == "ABSENT"

        async def managed_fence(_operation, _inventory):
            state = await cluster_state()
            if state == "ABSENT":
                return True
            if state == "DELETING":
                # EKS deletion is irreversible. Its endpoint may disappear before its
                # DescribeCluster identity, but only the already-intended exact destroy
                # may continue through that state. This is never absence evidence.
                from harness_jobs.execution import CallStage, read_call
                from harness_jobs.execution_plan import admitted_steps, step_key

                async with context.connect() as connection:
                    record = await OperationStore().get(
                        connection, _operation.grant.principal, lease.operation_id
                    )
                    matching = [
                        step
                        for step in admitted_steps(record)
                        if step.step_id == artifact.step().step_id
                    ]
                    if (
                        len(matching) != 1
                        or matching[0].target != artifact.step().target
                    ):
                        raise LifecycleRefused(
                            "deleting cluster has no exact approved destroy"
                        )
                    call = await read_call(
                        connection, idempotency_key=step_key(record, matching[0])
                    )
                    if (
                        call is None
                        or call.stage is not CallStage.INTENDED
                        or call.attempt_id != lease.attempt_id
                        or call.fence_token != lease.fence_token
                        or call.outcome is not None
                    ):
                        raise LifecycleRefused(
                            "deleting cluster has no live original destroy intent"
                        )
                return True
            return await asyncio.to_thread(verify_fence, grants, inventory, fence)

        async def resolve(_identifier):
            current = await authorize()
            if _identifier != lease.operation_id:
                raise LifecycleRefused("retirement finalizer changed operation")
            return current, inventory, artifact

        async def hook_context(_call):
            current = await authorize()
            return current, binding_for(current)

        token = secrets.token_urlsafe(48)

        async def authenticate(supplied):
            if not isinstance(supplied, str) or not secrets.compare_digest(
                supplied, token
            ):
                raise LifecycleRefused("retirement inventory authority is unavailable")
            return (await authorize()).grant

        execution_pool = SimpleNamespace(acquire=context.connect)
        domain_pool = SimpleNamespace(acquire=context.domain_connect)
        observations = RetirementObservations(
            session=session, kubernetes=grants, eks=eks
        )

        async def settle_related(current, _target):
            from .retirement_control_settlement import settle_control_allocation

            return [
                await settle_control_allocation(
                    current,
                    context,
                    inventory,
                    eks=eks,
                    cluster_absent=cluster_absent,
                    authorize=authorize,
                    authenticate=authenticate,
                    token=token,
                )
            ]

        finalizer = RetirementFinalizer(
            execution_pool=execution_pool,
            domain_pool=domain_pool,
            resolve=resolve,
            observations=observations,
            authenticate=authenticate,
            token_for=lambda _op: token,
            settle_related=settle_related,
        )
        lifecycle = RetirementLifecycle(
            domain_pool=domain_pool,
            execution_pool=execution_pool,
            workspace=ReadWorkspace(grants, authorize),
            managed_objects=fence["managed_workload_inventory"],
            managed_fence=managed_fence,
        )
        directory = operation_directory(
            context.state_root,
            lease.org_id,
            lease.workspace_id,
            lease.operation_id,
        )
        process = WorkerProcesses(
            binaries=config["binaries"],
            directory=directory,
            session=session,
            region=target.region,
            verify=verify,
        )
        terraform = TerraformDestroy(
            python_binary=config["binaries"]["python"],
            guard_script=artifact.module_dir / "scripts" / "apply_workspace_plan.py",
            terraform_binary=config["binaries"]["terraform"],
            process=process,
        )
        from .retirement_target import expected_prerequisites

        network = SecurityGroupRules(
            session=session,
            target=target,
            expected=expected_prerequisites(outputs, config),
        )

        async def control_verify(_op, _inv, control, row):
            if await cluster_absent():
                return
            identity = validate_access_artifact(row, control)["cleaner-entry"]
            if await asyncio.to_thread(eks.observe, control.grants[0]) != identity:
                raise LifecycleRefused("retirement cleanup identity changed")
            await managed_fence(_op, _inv)

        async def artifact_for(_operation, _inventory):
            return artifact

        runtime = RetirementRuntime(
            connect=context.connect,
            domain_connect=context.domain_connect,
            context=hook_context,
            registration_store=registration,
            removals=OwnedResourceRemover(kubernetes=grants, eks=eks, network=network),
            lifecycle=lifecycle,
            verify_inventory=finalizer.verify_step,
            artifact_for=artifact_for,
            terraform=terraform,
            control_access_for=lambda op, inv: resolve_managed_control(
                op, inv, context
            ),
            control_verify=control_verify,
        )
        server = ExecutionRPCServer(
            connect=context.connect,
            provider_call=runtime,
            authenticate=authenticate,
            after_step=finalizer,
        )
        executor = OperationExecutor(
            lease, connect=context.connect, provider_call=runtime
        )

        async def heartbeat():
            active = executor
            while True:
                await asyncio.sleep(10)
                try:
                    await authorize()
                    active = await active.renew(duration=timedelta(seconds=45))
                except BaseException:
                    revoked.set()
                    raise

        async with asyncio.TaskGroup() as tasks:
            renewal = tasks.create_task(heartbeat())
            try:
                for step in plan.steps:
                    await authorize()
                    result = await server.execute_step(
                        operation.grant, executor, step.step_id
                    )
                    if result[0].outcome is not CallOutcome.SUCCEEDED:
                        raise LifecycleRefused(
                            "retirement step requires reconciliation"
                        )
            finally:
                revoked.set()
                renewal.cancel()
        return {
            "status": "retired",
            "operation_id": lease.operation_id,
            "workspace_id": lease.workspace_id,
        }
