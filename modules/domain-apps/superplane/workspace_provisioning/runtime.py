"""Finite approved lifecycle phases executed by the authenticated paid worker."""

import asyncio
import json
import threading
from datetime import timedelta

from .artifacts import (
    continuation_parameters,
    digest,
    initial_execution_steps,
    read_artifact,
    record_artifact,
)
from .process import WorkerProcesses
from .runtime_config import LifecycleRefused
from .authority import current_operation, validated_request
from .terraform import operation_directory, require_owned_networking


async def delivery_session(operation, context, account_id, region):
    from .credentials import assume_session

    await current_operation(operation, context)
    if getattr(context, "brokered_provider", False):

        async def verify_broker():
            await current_operation(operation, context)

        session = await context.authority.provider_session(
            operation, region, verify=verify_broker
        )
        if not session._superplane_role_arn.startswith(
            f"arn:aws:iam::{account_id}:role/"
        ):
            raise LifecycleRefused("brokered provider names another AWS account")
        return session
    delivered = await context.authority.delivery_role(operation)
    if not delivered["role_arn"].startswith(f"arn:aws:iam::{account_id}:role/"):
        raise LifecycleRefused("delivered provider role names another AWS account")
    loop = asyncio.get_running_loop()

    async def current_delivery():
        current = await current_operation(operation, context)
        latest = await context.authority.delivery_role(current)
        if latest != delivered:
            raise LifecycleRefused("approved vault role changed during execution")

    def verify():
        future = asyncio.run_coroutine_threadsafe(current_delivery(), loop)
        try:
            return future.result(timeout=20)
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


async def validate_phase(operation, context, *, require_fresh=True):
    """Validate local policy and immutable SQL lineage without execution/provider I/O.

    Recovery selection may use this with its original expired lease. It does not
    resolve a grant, deliver credentials, or confer permission to execute a phase.
    """
    from harness_jobs.execution_descriptors import parse_execution_steps

    config, request, authorization = validated_request(operation, context)
    parameters = operation.request.parameters
    lease = operation.grant.lease
    row = None
    if parameters.get("lifecycle_phase") == "prepare-partial-apply":
        from .partial_apply_request import registration_values

        async with context.connect() as connection, context.domain_connect() as domain:
            await registration_values(
                connection,
                domain,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
            )
        (step,) = parse_execution_steps(parameters["execution_steps"])
        return config, request, authorization, None, step
    if parameters.get("lifecycle_artifact_id"):
        from .bootstrap_review import read_continuation_artifact

        try:
            row = await read_continuation_artifact(
                context.domain_connect,
                context.connect,
                artifact_id=parameters["lifecycle_artifact_id"],
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                require_fresh=require_fresh,
            )
        except LifecycleRefused as exc:
            if str(exc) != "plan proposal expired; prepare a fresh plan":
                raise
            if parameters.get("lifecycle_phase") == "bootstrap-workspace":
                from .bootstrap_retry import retry_artifact

                row = await retry_artifact(operation, context)
                if row is None:
                    raise
            elif parameters.get("lifecycle_phase") == "apply-infrastructure":
                from .partial_apply_execution import selected_repair

                if await selected_repair(operation, context, fresh=True) is None:
                    raise
                row = await read_artifact(
                    context.domain_connect,
                    artifact_id=parameters["lifecycle_artifact_id"],
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    require_fresh=False,
                )
            else:
                raise
        from .planning_budget import verify_budget_artifact

        await verify_budget_artifact(row, context.connect, context.domain_connect)
        expected = continuation_parameters(row)
        if dict(parameters) != expected:
            raise LifecycleRefused(
                "continuation differs from the recorded reviewed proposal"
            )
        async with context.connect() as connection:
            source = await connection.fetchrow(
                "SELECT state,org_id,workspace_id,job_id,attempt_id,plan_digest,request_payload FROM harness_operations WHERE operation_id=$1",
                row["source_operation_id"],
            )
        if (
            source is None
            or source["state"] != "succeeded"
            or any(
                source[key] != row["source_" + key] for key in ("job_id", "attempt_id")
            )
            or (source["org_id"], source["workspace_id"])
            != (lease.org_id, lease.workspace_id)
            or source["plan_digest"] != row["source_payload_digest"]
            or source["request_payload"] != row["source_request_payload"]
        ):
            raise LifecycleRefused(
                "source phase is not the completed original admission"
            )
    elif parameters.get("execution_steps") != initial_execution_steps(parameters):
        raise LifecycleRefused(
            "initial lifecycle descriptors differ from the approved recipe"
        )
    (step,) = parse_execution_steps(parameters["execution_steps"])
    return config, request, authorization, row, step


async def run_lifecycle(operation, context):
    """Complete one admitted phase; proposals never claim workspace readiness."""
    from .shared_membership import approved_membership, verify

    membership = approved_membership(
        operation.request.parameters,
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
    )
    if membership is not None:
        async with context.domain_connect() as connection:
            await verify(connection, membership, states={"reserved", "active"})
        # Admission may survive an API/worker version skew. Never run the
        # dedicated network, EKS actor or all-node bootstrap recipes for a
        # shared request, even when its membership was correctly reserved.
        raise LifecycleRefused(
            "shared bootstrap requires namespace admission, isolated bootstrap "
            "delegation and renewable workspace credential delivery; the "
            "dedicated lifecycle runtime cannot execute this placement"
        )
    phase_state = await validate_phase(operation, context)
    _, request, _, _, _ = phase_state
    if request.mode.value == "new-account-managed":
        raise LifecycleRefused(
            "new-account runtime requires its complete reviewed account recipe"
        )
    return await _run_validated_lifecycle(operation, context, phase_state)


async def _run_validated_lifecycle(operation, context, phase_state):
    """Shared phase engine; callers validate immutable admission before entry."""
    from harness_jobs.execution import CallOutcome, OperationExecutor
    from harness_jobs.execution_rpc import ExecutionRPCServer

    config, request, authorization, row, step = phase_state
    if request.mode.creates_cluster:
        require_owned_networking(config)
    operation = await current_operation(operation, context)
    lease = operation.grant.lease
    phase = step.step_id
    result = {}
    loop = asyncio.get_running_loop()
    revoked = threading.Event()

    def verify():
        if revoked.is_set():
            raise LifecycleRefused("lifecycle worker lost its execution authority")
        future = asyncio.run_coroutine_threadsafe(
            current_operation(operation, context), loop
        )
        try:
            return future.result(timeout=35)
        except BaseException:
            future.cancel()
            raise

    async def hook(call):
        current = await current_operation(operation, context)
        if (
            call.operation_id,
            call.org_id,
            call.workspace_id,
            call.job_id,
            call.attempt_id,
            call.fence_token,
            call.provider,
            call.operation_kind,
            call.target,
        ) != (
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            operation.job_id,
            lease.attempt_id,
            lease.fence_token,
            step.provider,
            step.operation_kind,
            step.target,
        ):
            raise LifecycleRefused(
                "provider call differs from its admitted lifecycle descriptor"
            )
        account_id = (
            row["account_id"]
            if row
            else request.target_account_id or request.management_account_id
        )
        lineage = {}
        amendment = None
        if phase == "prepare-infrastructure" and row is None:
            from .planning_budget import approved_budget

            amendment = await approved_budget(
                context.connect,
                context.domain_connect,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                plan_digest=current.plan_digest,
                parameters=current.request.parameters,
            )
        if request.mode.value == "new-account-managed":
            from dataclasses import asdict
            from .account_registration import created_account_registration
            from .account_runtime import child_session

            if row is None:
                raise LifecycleRefused(
                    "new-account infrastructure requires completed account bootstrap"
                )
            management = await delivery_session(
                current, context, request.management_account_id, request.region
            )
            registration = await created_account_registration(
                current, context, request, authorization, row, management
            )
            prior = json.loads(row["artifact_metadata_json"])
            if prior.get("created_account_registration") != asdict(registration):
                raise LifecycleRefused(
                    "new-account infrastructure differs from maintained account registration"
                )
            lineage = {
                "creation_artifact_id": prior["creation_artifact_id"],
                "created_account_registration": asdict(registration),
            }
            session = await child_session(
                current, context, config, request, row, management, bootstrap=False
            )
        else:
            session = await delivery_session(
                current, context, account_id, request.region
            )
        completion_receipt = None
        directory = None
        repair = None
        if phase == "bootstrap-workspace":
            from .bootstrap_retry import retry_directory

            directory = await retry_directory(current, context, call)
        if phase == "apply-infrastructure":
            from .partial_apply_execution import selected_repair, repair_directory

            repair = await selected_repair(current, context)
            if repair is not None:
                directory = repair_directory(current, context, repair[0], call)
        if phase == "prepare-infrastructure" and row is None:
            from harness_jobs.provider_retry import receipt_for
            from .planning_boundary import archive_preparation

            async with context.connect() as connection:
                receipt = await receipt_for(connection, lease.operation_id)
            if receipt is not None and json.loads(receipt["proof"]).get(
                "preparation_boundary"
            ):
                if (
                    receipt["replacement_key"] != call.idempotency_key
                    or receipt["source_fence"] >= lease.fence_token
                    or receipt["recovery_scope"]
                    not in {
                        "unstarted-planning",
                        "saved-plan-completion",
                        "interrupted-planning",
                    }
                    or any(
                        receipt[k] != getattr(lease, k)
                        for k in ("org_id", "workspace_id", "operation_id")
                    )
                ):
                    raise LifecycleRefused(
                        "retained preparation recovery differs from current call"
                    )
                await current_operation(current, context)
                if receipt["recovery_scope"] == "saved-plan-completion":
                    from .saved_plan_completion import stage_saved_plan

                    completion_receipt = dict(receipt)
                    directory = await asyncio.to_thread(
                        stage_saved_plan,
                        context.state_root,
                        (lease.org_id, lease.workspace_id, lease.operation_id),
                        completion_receipt,
                    )
                else:
                    await asyncio.to_thread(
                        archive_preparation,
                        context.state_root,
                        (lease.org_id, lease.workspace_id, lease.operation_id),
                        dict(receipt),
                    )
                    if receipt["recovery_scope"] == "interrupted-planning":
                        from .interrupted_plan import release_plan_lock

                        await asyncio.to_thread(
                            release_plan_lock,
                            session,
                            config,
                            json.loads(receipt["proof"]),
                            verify=verify,
                        )
                        await current_operation(current, context)
                await current_operation(current, context)
        directory = directory or operation_directory(
            context.state_root,
            lease.org_id,
            lease.workspace_id,
            lease.operation_id,
            create=True,
        )
        process = WorkerProcesses(
            binaries=config["binaries"],
            directory=directory,
            session=session,
            region=request.region,
            verify=verify,
        )
        if phase in {"prepare-infrastructure", "prepare-partial-apply"}:
            from .terraform import prepare

            repair_snapshot = None
            if phase == "prepare-partial-apply":
                from .partial_apply_request import registration_values
                from .partial_apply_backend import read_snapshot

                async with (
                    context.connect() as connection,
                    context.domain_connect() as domain,
                ):
                    _, evidence = await registration_values(
                        connection,
                        domain,
                        operation_id=lease.operation_id,
                        org_id=lease.org_id,
                        workspace_id=lease.workspace_id,
                    )
                repair_snapshot = evidence["snapshot"]
                repair_target = {
                    "account_id": account_id,
                    "aws_region": request.region,
                    "environment": config["environment"],
                    "org_id": lease.org_id,
                    "workspace_id": lease.workspace_id,
                }
                await asyncio.to_thread(
                    read_snapshot,
                    session,
                    config["backend"],
                    repair_target,
                    expected=repair_snapshot,
                )
                await current_operation(current, context)
            if completion_receipt is not None:
                from .saved_plan_completion import finish

                prepare = finish
            target, metadata = await asyncio.to_thread(
                prepare,
                current,
                context,
                config,
                request,
                account_id,
                process,
                daily_budget_micros=amendment["daily_micros"] if amendment else None,
                **(
                    {"receipt": completion_receipt}
                    if completion_receipt is not None
                    else {}
                ),
                **(
                    {"partial_repair": repair_snapshot}
                    if repair_snapshot is not None
                    else {}
                ),
            )
            if repair_snapshot is not None:
                await asyncio.to_thread(
                    read_snapshot,
                    session,
                    config["backend"],
                    repair_target,
                    expected=repair_snapshot,
                )
                await current_operation(current, context)
                metadata["partial_repair"].update(
                    source_operation_id=evidence["operation_id"],
                    source_artifact_id=evidence["original_artifact_id"],
                    owner_evidence_id=evidence["record_id"],
                    owner_evidence_sha256=digest(evidence),
                )
            if amendment is not None:
                from .planning_budget import approved_budget, amended_review

                await current_operation(current, context)
                latest = await approved_budget(
                    context.connect,
                    context.domain_connect,
                    operation_id=lease.operation_id,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    plan_digest=current.plan_digest,
                    parameters=current.request.parameters,
                )
                if latest != amendment:
                    raise LifecycleRefused("planning budget changed during preparation")
                metadata["daily_budget_amendment"] = amended_review(
                    current.request.parameters, amendment, request, authorization
                )
        elif phase == "prepare-adoption" and row is None:
            from .adoption import prepare_adoption

            target, metadata = await prepare_adoption(
                current, context, request, session
            )
        elif phase == "apply-infrastructure" and row is not None:
            from .terraform import apply
            from .provider_observation import observe_applied_target

            selected = repair[1] if repair is not None else row
            if repair is not None:
                from .partial_apply_backend import read_snapshot

                repair_snapshot = json.loads(repair[0]["proof"])["owner_evidence"][
                    "snapshot"
                ]
                await asyncio.to_thread(
                    read_snapshot,
                    session,
                    config["backend"],
                    json.loads(selected["target_json"]),
                    expected=repair_snapshot,
                )
            target, metadata = await asyncio.to_thread(
                apply, current, context, config, selected, process
            )
            if repair is not None:
                from .partial_apply_execution import verify_completed_inventory

                verify_completed_inventory(metadata, repair_snapshot)
                metadata.update(
                    source_artifact_id=row["artifact_id"],
                    repair_artifact_id=selected["artifact_id"],
                    partial_repair_receipt=repair[0]["reauthorization_id"],
                )

            async def sdk_read(service, method, **arguments):
                await current_operation(current, context)
                result = await asyncio.to_thread(
                    getattr(
                        session.client(service, region_name=request.region), method
                    ),
                    **arguments,
                )
                await current_operation(current, context)
                return result

            outputs = {
                key: value["value"] for key, value in metadata["outputs"].items()
            }
            metadata["provider_snapshot"] = await observe_applied_target(
                outputs, sdk_read
            )
            if repair is not None:
                from .partial_apply_plan import EIP, require

                old_eip = repair_snapshot["resources"][EIP]["id"]
                absent = await sdk_read(
                    "ec2",
                    "describe_addresses",
                    Filters=[{"Name": "allocation-id", "Values": [old_eip]}],
                )
                require(
                    absent.get("Addresses") == [],
                    "replaced EIP absence has not been observed",
                )
                metadata["replaced_eip_absent"] = old_eip
        elif phase == "bootstrap-workspace":
            from .bootstrap_runtime import bootstrap
            from .bootstrap_result import bootstrap_result_anchor
            from .network import establish_network

            if row is None:
                raise LifecycleRefused(
                    "bootstrap requires its reviewed target artifact"
                )
            prior = json.loads(row["artifact_metadata_json"])
            outputs = {key: value["value"] for key, value in prior["outputs"].items()}
            if request.mode.value == "bring-existing-cluster":
                from .adoption import verify_adoption_artifact

                outputs = verify_adoption_artifact(row, request)
            network = await establish_network(
                current, context, config, outputs, session
            )

            outcome = await asyncio.to_thread(
                bootstrap,
                current,
                context,
                config,
                request,
                row,
                session,
                process,
                loop,
                verify,
                network,
            )
            if not outcome.ready:
                raise LifecycleRefused(
                    "workspace bootstrap has not positively registered readiness"
                ) from outcome.refusal
            anchor = await bootstrap_result_anchor(current, context, outcome)
            result.update(
                await record_artifact(
                    current,
                    context,
                    account_id=account_id,
                    target=json.loads(row["target_json"]),
                    metadata={
                        "next_phase": "complete",
                        "source_artifact_id": row["artifact_id"],
                        "allocation_source_operation_id": prior.get(
                            "allocation_source_operation_id"
                        ),
                        "bootstrap_anchor": anchor,
                        **lineage,
                    },
                )
            )
            return (
                CallOutcome.SUCCEEDED,
                "canonical workspace bootstrap registered",
                outcome.target.cluster_arn,
            )
        else:
            raise LifecycleRefused("lifecycle phase is not implemented by this worker")
        result.update(
            await record_artifact(
                current,
                context,
                account_id=account_id,
                target=target,
                metadata={**metadata, **lineage},
            )
        )
        return (
            CallOutcome.SUCCEEDED,
            "bounded lifecycle phase prepared its next reviewed proposal",
            None if phase == "apply-infrastructure" else result["artifact_id"],
        )

    async def authenticate(_token):
        return (await current_operation(operation, context)).grant

    async def after_step(_grant, step_result):
        if phase == "apply-infrastructure":
            from .applied_inventory import seal_applied_inventory

            await seal_applied_inventory(operation, context, step_result)

    # Harness deliberately treats an exception as an uncertain provider call.
    # Preserve its original code locations before that boundary consumes it.
    from .diagnostics import diagnostic_hook

    hook = diagnostic_hook(hook)
    server = ExecutionRPCServer(
        connect=context.connect,
        provider_call=hook,
        authenticate=authenticate,
        after_step=after_step,
    )
    runtime = OperationExecutor(lease, connect=context.connect, provider_call=hook)

    async def heartbeat():
        active = runtime
        while True:
            await asyncio.sleep(10)
            try:
                current = await current_operation(operation, context)
                validated_request(current, context)
                active = await active.renew(duration=timedelta(seconds=45))
            except BaseException:
                revoked.set()
                raise

    async with asyncio.TaskGroup() as tasks:
        renewal = tasks.create_task(heartbeat())
        try:
            await server.execute_step(operation.grant, runtime, phase)
        finally:
            revoked.set()
            renewal.cancel()
    if not result:
        async with context.domain_connect() as connection:
            rows = await connection.fetch(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
            )
        if len(rows) != 1:
            raise LifecycleRefused(
                "completed lifecycle phase has no unique durable result"
            )
        from .artifacts import proposal

        verified = await read_artifact(
            context.domain_connect,
            artifact_id=rows[0]["artifact_id"],
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            require_fresh=False,
        )
        result.update(proposal(verified))
    return result
