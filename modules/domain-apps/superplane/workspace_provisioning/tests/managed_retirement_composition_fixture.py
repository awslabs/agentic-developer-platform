"""Explicit successful provider facts plus real retirement SQL and paid admissions."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import asyncpg
from harness_jobs import (
    REQUIRED_PERMISSION,
    OperationFacadeService,
    OperationStore,
    apply,
)
from harness_jobs.execution import CallOutcome, OperationExecutor
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import ResolvedPrincipal
from superplane_bootstrap.component_journal import (
    component_identity,
    expected_component_keys,
)
from superplane_bootstrap.grant_plan import compile_grants
from superplane_bootstrap.kube_grants import _digest
from superplane_bootstrap.retirement_fence import documents

from workspace_bootstrap.tests import conftest as ids
from workspace_bootstrap.tests.test_cli import _outputs
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.execution_contract import (
    ExecutionStep,
    encode_execution_steps,
)
from workspace_provisioning.retirement_access_artifact import (
    access_metadata,
    access_target,
)
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_destroy_producer import FILES
from workspace_provisioning.retirement_inventory import (
    load_bootstrap_retirement_inventory,
)
from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_review,
    managed_recipe_inputs,
)
from workspace_provisioning.retirement_request import retirement_request
from workspace_provisioning.terraform import (
    copy_source,
    operation_directory,
    sha,
    source_digest,
)

from .postgres_bridge import Harness
from .test_lifecycle_policy import policy
from .test_retirement_execution_postgres import _Approves, _Ledger, _Resolver


def management_journal(runtime):
    """The provider's successful management bootstrap result is a fixture fact.

    Retirement still reconstructs every inventory and immutable identity through
    the production SQL loader; this does not substitute that loader or compiler.
    """
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    store = runtime.store.store
    row = store.execute("SELECT * FROM workspace_bootstrap_authority", {})[0]
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    journal = SimpleNamespace(
        target=runtime.target,
        generation=row["generation"],
        original_allocation_id="original-allocation",
    )
    grants = compile_grants(
        journal,
        runtime.factory.release,
        runtime.clients.principals,
        controller_mode="management",
    )["grants"]
    for grant in grants:
        status = progress.get(grant["key"], {})
        if grant["kind"] == "kubernetes" and status.get("phase") in {
            "granted",
            "adopted",
        }:
            body = deepcopy(grant["body"])
            meta = body["metadata"]
            key = (body["kind"], meta.get("namespace"), meta["name"])
            previous = runtime.cloud.objects.get(key)
            if previous:
                body["metadata"].update(
                    uid=previous["metadata"]["uid"], resourceVersion="1"
                )
                runtime.cloud.objects[key] = body
            status["identity"]["digest"] = _digest(grant["body"])
    plan["grants"] = grants
    components = {}
    for key in expected_component_keys(ids.NAMESPACE, "superplane-controller"):
        kind, namespace, name = json.loads(key)
        body = {
            "apiVersion": "v1"
            if kind == "ServiceAccount"
            else "rbac.authorization.k8s.io/v1",
            "kind": kind,
            "metadata": {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "annotations": {
                    "superplane.aws-e/component-creation": "created-" + name
                },
            },
        }
        observed = deepcopy(body)
        observed["metadata"].update(
            uid="component-" + kind + "-" + name, resourceVersion="1"
        )
        identity = component_identity(observed)
        components[key] = {
            "desired": body,
            "identity": identity,
            "phase": "owned",
            "creation": identity["creation"],
        }
        runtime.cloud.objects[(kind, namespace or None, name)] = observed
    progress.update(
        components=components,
        component_inventory_complete=True,
        component_inventory_mode="management",
        system_workload_baseline={
            "version": 1,
            "cluster_arn": ids.CLUSTER_ARN,
            "operation_id": row["operation_id"],
            "original_allocation_id": "original-allocation",
            "generation": row["generation"],
            "objects": [],
        },
    )
    with store.transaction():
        store.execute(
            "UPDATE workspace_bootstrap_authority SET plan_json=:plan,progress_json=:progress",
            {"plan": canonical(plan), "progress": canonical(progress)},
        )
    return load_bootstrap_retirement_inventory(
        registration_store=runtime.store,
        binding=replace(runtime.binding, action="teardown"),
    )


async def build(runtime, server, tmp_path):
    database = runtime.store.store._connection._params.database
    pool = await asyncpg.create_pool(
        server,
        database=database,
        min_size=1,
        max_size=10,
        server_settings={"statement_timeout": "15000"},
    )
    runtime.retirement_pool = pool
    harness = Harness(runtime.store.store._loop, pool)
    async with harness.connect() as connection:
        await apply(connection)
    principal = ResolvedPrincipal(
        ids.ORG_ID, ids.WORKSPACE_ID, "requester", frozenset({REQUIRED_PERMISSION})
    )
    facade = OperationFacadeService(
        connect=harness.connect,
        resolver=_Resolver(principal),
        approvals=_Approves(),
        ledger=_Ledger(),
    )
    policy_doc = policy()
    policy_doc.update(
        permitted_target_accounts=[ids.ACCOUNT_ID], permitted_regions=[ids.REGION]
    )
    policy_doc["credential_references"] = {
        ids.ACCOUNT_ID: next(iter(policy_doc["credential_references"].values()))
    }
    policy_doc["runtime"].update(
        namespace=ids.NAMESPACE,
        enforce_version=ids.ENFORCE_VERSION,
        management_security_group_id=ids.MANAGEMENT_SG_ID,
    )
    config = policy_doc["runtime"]
    config["binaries"] = {
        "python": "/usr/bin/python3",
        "terraform": "/usr/bin/true",
        "kubectl": "/usr/bin/true",
        "aws": "/usr/bin/true",
    }
    request = {
        "mode": "existing-account-managed",
        "organization_id": "o-fixture1234",
        "management_account_id": "000000000001",
        "management_cluster": "management",
        "region": ids.REGION,
        "workspace_id": ids.WORKSPACE_ID,
        "target_account_id": ids.ACCOUNT_ID,
        "vpc_cidr": "10.64.0.0/16",
        "availability_zones": ["us-east-1a", "us-east-1b"],
        "cluster_version": "1.31",
    }
    parameters = {
        "lifecycle_request": canonical(request),
        "lifecycle_inputs": canonical({"isolation_mode": "dedicated"}),
        "aws_account_id": ids.ACCOUNT_ID,
        "workspace_name": "fixture",
        "plan_revision": "a" * 64,
    }

    async def admit(values, identity, action="provision", *, succeeded=False):
        progress = await facade.open_operation(
            action=action,
            workspace_id=ids.WORKSPACE_ID,
            org_id=ids.ORG_ID,
            permission=REQUIRED_PERMISSION,
            parameters={**values, "idempotency_key": identity},
        )
        async with harness.connect() as connection:
            if succeeded:
                await connection.execute(
                    "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                    progress.operation_id,
                )
            return await OperationStore().get(
                connection, principal, progress.operation_id
            )

    paid = await admit(
        {
            **parameters,
            "allocation_id": "original-allocation",
            "lifecycle_phase": "apply-infrastructure",
            "execution_steps": encode_execution_steps(
                [
                    ExecutionStep(
                        "apply-infrastructure",
                        "superplane-lifecycle",
                        "apply-infrastructure",
                        "fixture-target",
                    )
                ]
            ),
        },
        "paid-apply",
    )
    lease = await harness.lease(paid.operation_id, holder="original-worker")
    grant = ExecutionGrant(replace(principal, subject=lease.holder), lease)

    async def provider(_call):
        return CallOutcome.SUCCEEDED, "fixture provider apply result", None

    from harness_jobs.inventory import AllocationResource, InventoryAuthority

    from workspace_provisioning.retirement_observation import reference

    async def seal_original(_grant, _result):
        authority = InventoryAuthority(
            connect=harness.connect, authenticate=lambda _: None
        )
        async with harness.connect() as connection:
            key = await connection.fetchval(
                "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
                paid.operation_id,
            )
            members = tuple(
                AllocationResource(
                    ref, "aws", ref, "terraform-resource", frozenset({key})
                )
                for ref in (
                    reference(
                        "terraform-resource",
                        resource_type="aws_vpc",
                        identity={"id": ids.VPC_ID},
                    ),
                    reference(
                        "terraform-resource",
                        resource_type="aws_eks_cluster",
                        identity={"name": ids.CLUSTER_NAME, "arn": ids.CLUSTER_ARN},
                    ),
                )
            )
            await authority.enumerate_resources(connection, lease, resources=members)
            enumeration = await authority.begin_provider_enumeration(
                connection, lease, provider="aws"
            )
            await authority.record_provider_enumeration(
                connection,
                lease,
                provider="aws",
                provider_references=frozenset(
                    member.provider_reference for member in members
                ),
                attempt=enumeration,
            )
            # This fixture's sole maintained wrapper reports no resource handle;
            # its complete successful provider result is the explicit source fact.
            wrapper_enumeration = await authority.begin_provider_enumeration(
                connection, lease, provider="superplane-lifecycle"
            )
            await authority.record_provider_enumeration(
                connection,
                lease,
                provider="superplane-lifecycle",
                provider_references=frozenset(),
                attempt=wrapper_enumeration,
            )
            await authority.seal_allocation(connection, lease)

    executor = OperationExecutor(lease, connect=harness.connect, provider_call=provider)
    execution = ExecutionRPCServer(
        connect=harness.connect,
        provider_call=provider,
        authenticate=lambda _: None,
        after_step=seal_original,
    )
    await execution.execute_step(grant, executor, "apply-infrastructure")
    async with harness.connect() as connection:
        paid = await OperationStore().get(connection, principal, paid.operation_id)
    target = {
        "account_id": ids.ACCOUNT_ID,
        "aws_region": ids.REGION,
        "org_id": ids.ORG_ID,
        "workspace_id": ids.WORKSPACE_ID,
        "environment": "dev",
        "workspace_name": "fixture",
    }
    outputs = {key: value["value"] for key, value in _outputs().items()}
    outputs.update(
        cluster_endpoint=ids.ENDPOINT,
        workspace_api_security_group_id=ids.CLUSTER_SG_ID,
        sts_endpoint_rule_id=ids.MANAGEMENT_RULE_ID,
    )

    async def artifact(
        source, target, metadata, *, producer_attempt="producer-attempt"
    ):
        values = {
            "org_id": ids.ORG_ID,
            "workspace_id": ids.WORKSPACE_ID,
            "source_operation_id": source.operation_id,
            "source_job_id": source.job_id,
            "source_attempt_id": source.attempt_id,
            "source_payload_digest": source.plan_digest,
            "source_request_payload": source.request_payload,
            "producer_holder": "producer",
            "producer_attempt_id": producer_attempt,
            "producer_fence_token": 1,
            "request_revision": source.admitted_request().parameters["plan_revision"],
            "account_id": ids.ACCOUNT_ID,
            "target_json": canonical(target),
            "parameters_json": canonical(dict(source.admitted_request().parameters)),
            "artifact_metadata_json": canonical(metadata),
        }
        artifact_id = digest(values)
        async with harness.connect() as connection:
            await connection.execute(
                "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
                + ",".join(values)
                + ") VALUES ($1,"
                + ",".join("$" + str(i) for i in range(2, len(values) + 2))
                + ")",
                artifact_id,
                *values.values(),
            )
            return dict(
                await connection.fetchrow(
                    "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                    artifact_id,
                )
            )

    apply_row = await artifact(
        paid,
        target,
        {
            "next_phase": "bootstrap-workspace",
            "allocation_source_operation_id": paid.operation_id,
            "outputs": {
                key: {"value": value, "type": "string", "sensitive": False}
                for key, value in outputs.items()
            },
        },
    )
    bootstrap = await admit(
        {
            **parameters,
            "allocation_id": "bootstrap-allocation",
            "execution_steps": encode_execution_steps(
                [
                    ExecutionStep(
                        "bootstrap-workspace",
                        "superplane-lifecycle",
                        "bootstrap-workspace",
                        "fixture-target",
                    )
                ]
            ),
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_source_operation_id": paid.operation_id,
            "lifecycle_artifact_id": apply_row["artifact_id"],
        },
        "bootstrap",
    )
    bootstrap_lease = await harness.lease(
        bootstrap.operation_id, holder="bootstrap-worker"
    )
    bootstrap_grant = ExecutionGrant(
        replace(principal, subject=bootstrap_lease.holder), bootstrap_lease
    )

    async def bootstrap_provider(_call):
        return (
            CallOutcome.SUCCEEDED,
            "fixture bootstrap provider result",
            ids.CLUSTER_ARN,
        )

    bootstrap_executor = OperationExecutor(
        bootstrap_lease, connect=harness.connect, provider_call=bootstrap_provider
    )
    bootstrap_execution = ExecutionRPCServer(
        connect=harness.connect,
        provider_call=bootstrap_provider,
        authenticate=lambda _: None,
    )
    await bootstrap_execution.execute_step(
        bootstrap_grant, bootstrap_executor, "bootstrap-workspace"
    )
    async with harness.connect() as connection:
        bootstrap = await OperationStore().get(
            connection, principal, bootstrap.operation_id
        )
    inventory = await asyncio.to_thread(management_journal, runtime)
    async with harness.connect() as connection:
        await connection.execute(
            "UPDATE workspaces SET provisioning_operation_id=$1 WHERE id::text=$2",
            bootstrap.operation_id,
            ids.WORKSPACE_ID,
        )
    plan = compile_managed_access_review(
        inventory,
        config,
        original_allocation_id="original-allocation",
        bootstrap_artifact_id=apply_row["artifact_id"],
        retirement_request_id="721c2c9c-8ba1-42d5-94eb-393de2a628b7",
        prepare_destroy=True,
        **managed_recipe_inputs(inventory, config),
    )
    preparation = access_request(
        plan, bootstrap, policy_doc, allocation_source=paid, prepare_destroy=True
    )
    control = await admit(dict(preparation.parameters), preparation.idempotency_key)
    control_lease = await harness.lease(control.operation_id, holder="control-worker")
    control_grant = ExecutionGrant(
        replace(principal, subject=control_lease.holder), control_lease
    )
    control_executor = OperationExecutor(
        control_lease, connect=harness.connect, provider_call=provider
    )
    control_execution = ExecutionRPCServer(
        connect=harness.connect, provider_call=provider, authenticate=lambda _: None
    )
    await control_execution.execute_step(
        control_grant, control_executor, "prepare-retirement-access"
    )
    async with harness.connect() as connection:
        control = await OperationStore().get(
            connection, principal, control.operation_id
        )
    from superplane_bootstrap.eks_grants import EksGrants

    eks = EksGrants(
        runtime.cloud, runtime.target, entry_client=runtime.cloud.entry_client
    )
    grant_identity = eks.create(plan.grants[0])
    for original in inventory.grants:
        if original.spec.get("key") in {
            "retirement-fence-policy",
            "retirement-fence-binding",
        }:
            key = (
                original.spec["body"]["kind"],
                None,
                original.spec["body"]["metadata"]["name"],
            )
            body = runtime.cloud.objects[key]
            body["spec"] = documents(
                body["metadata"]["name"], original.spec["generation"], active=True
            )[0 if key[0] == "ValidatingAdmissionPolicy" else 1]["spec"]
            body["metadata"]["generation"] = 2
            body["status"] = {
                "observedGeneration": 2,
                "typeChecking": {"expressionWarnings": []},
            }
    root = operation_directory(
        tmp_path / "state",
        ids.ORG_ID,
        ids.WORKSPACE_ID,
        control.operation_id,
        create=True,
    )
    saved = root / ("access-" + digest(["producer-attempt", 1])[:24]) / "destroy"
    saved.mkdir(parents=True)
    copy_source(saved / "module")
    review = saved / "review"
    review.mkdir()
    (review / "workspace.tfplan").write_bytes(b"approved-destroy")
    changes = [
        {
            "type": "aws_vpc",
            "change": {"actions": ["delete"], "before": {"id": ids.VPC_ID}},
        },
        {
            "type": "aws_eks_cluster",
            "change": {
                "actions": ["delete"],
                "before": {"name": ids.CLUSTER_NAME, "arn": ids.CLUSTER_ARN},
            },
        },
    ]
    (review / "workspace-plan.json").write_text(
        canonical({"resource_changes": changes})
    )
    backend = {"bucket": "fixture-state", "key": "fixture-workspace.tfstate"}
    (review / "workspace-authorization.proposed.json").write_text(
        canonical(
            {
                **target,
                "plan_file_sha256": sha(review / "workspace.tfplan"),
                "plan_sha256": sha(review / "workspace-plan.json"),
                "backend": backend,
            }
        )
    )
    for name in FILES - {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
    }:
        (review / name).write_text("{}")
    destroy = {
        "version": 1,
        "target": target,
        "files": {name: sha(review / name) for name in FILES},
        "module_sha256": source_digest(saved / "module"),
        "plan_file_sha256": sha(review / "workspace.tfplan"),
        "plan_json_sha256": sha(review / "workspace-plan.json"),
        "backend_sha256": digest(backend),
        "original_allocation_id": "original-allocation",
    }
    fence = {
        "version": 1,
        "identity": plan.fence_recipe["activate-retirement-fence"]["arguments"],
        "managed_workload_inventory": [],
        "managed_workload_inventory_sha256": digest([]),
    }
    access = await artifact(
        control,
        access_target(plan),
        access_metadata(
            plan,
            {"cleaner-entry": grant_identity},
            reviewed_destroy=destroy,
            retirement_fence=fence,
        ),
    )
    removal, _ = retirement_request(inventory, plan, access, bootstrap, policy_doc)
    record = await admit(dict(removal.parameters), removal.idempotency_key, "teardown")
    lease = await harness.lease(record.operation_id, holder="retirement-worker")
    operation = SimpleNamespace(
        grant=ExecutionGrant(replace(principal, subject=lease.holder), lease),
        request=record.admitted_request(),
        request_payload=record.request_payload,
        plan_digest=record.plan_digest,
        job_id=record.job_id,
        reservation_state="confirmed",
        max_runtime_seconds=3600,
    )
    async with harness.connect() as connection:
        await connection.execute(
            "UPDATE workspaces SET teardown_operation_id=$1,status='Teardown' WHERE id::text=$2",
            record.operation_id,
            ids.WORKSPACE_ID,
        )
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        canonical({"version": 1, "tenants": {ids.ORG_ID: policy_doc}})
    )
    return SimpleNamespace(
        harness=harness,
        pool=pool,
        operation=operation,
        inventory=inventory,
        plan=plan,
        access=access,
        bootstrap=bootstrap,
        paid=paid,
        control=control,
        outputs=outputs,
        config=config,
        policy_file=policy_file,
        state_root=tmp_path / "state",
        runtime=runtime,
    )
