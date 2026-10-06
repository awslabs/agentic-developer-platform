"""The proposed managed entry is separate from its sealed bootstrap allocation."""

from dataclasses import replace
from uuid import uuid4

import pytest
from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants

from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_plan,
)
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import policy
from .test_retirement_inventory import load
from .test_retirement_plan import component


def inputs(runtime):
    runtime.factory.original_allocation_id = "original-allocation"
    assert runtime.run().ready
    owned = load(runtime)
    config = policy()["runtime"]
    config["namespace"] = owned.namespace
    return {
        "inventory": owned,
        "runtime": config,
        "original_allocation_id": "original-allocation",
        "bootstrap_artifact_id": "a" * 64,
        "retirement_request_id": str(uuid4()),
        "release": runtime.factory.release,
        "principals": runtime.clients.principals,
        "controller_mode": "legacy",
        "kubernetes": KubeGrants(runtime.clients.supervisor_kubernetes, runtime.target),
        "eks": EksGrants(
            runtime.cloud, runtime.target, entry_client=runtime.cloud.entry_client
        ),
    }


def test_managed_cleanup_plan_binds_live_uids_without_broad_grants(runtime):
    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    initial = runtime.cloud.mutations
    plan = compile_managed_access_plan(**arguments)
    assert plan == compile_managed_access_plan(**arguments)
    assert plan.original_allocation_id == "original-allocation"
    assert plan.bootstrap_artifact_id == "a" * 64
    assert plan.allocation_id not in {plan.original_allocation_id, plan.request_id}
    assert {item["spec"]["key"] for item in plan.retained_grants} == {
        f"cleanup-{scope}-{kind}"
        for scope in ("cluster", "namespace", "system")
        for kind in ("role", "binding")
    }
    assert all(item["identity"]["uid"] for item in plan.retained_grants)
    assert list(plan.recipe()) == ["cleaner-entry"]
    assert plan.grants[0]["groups"] == [plan.cleanup_group]
    assert plan.revocation_order == ("cleaner-entry",)
    assert plan.registrar_namespaces == ()
    assert runtime.cloud.mutations == initial
    assert not any(
        plan.cleanup_group in entry["kubernetesGroups"]
        for entry in runtime.cloud.entries.values()
    )
    changed = compile_managed_access_plan(
        **{**arguments, "retirement_request_id": str(uuid4())}
    )
    assert changed.allocation_id != plan.allocation_id
    assert changed.revision != plan.revision
    replacement = compile_managed_access_plan(
        **{**arguments, "bootstrap_artifact_id": "b" * 64}
    )
    assert replacement.revision != plan.revision
    assert replacement.recipe() != plan.recipe()


@pytest.mark.parametrize(
    "changed",
    [
        "allocation",
        "adopted",
        "incomplete",
        "foreign",
        "uid",
        "mapped",
        "role",
        "artifact",
    ],
)
def test_managed_cleanup_plan_denies_foreign_or_changed_authority(runtime, changed):
    arguments = inputs(runtime)
    if changed != "incomplete":
        arguments["inventory"] = replace(
            arguments["inventory"], components_complete=True
        )
    initial = runtime.cloud.mutations
    if changed == "allocation":
        arguments["original_allocation_id"] = "wrong-allocation"
    elif changed == "adopted":
        arguments["inventory"] = replace(
            arguments["inventory"], cluster_ownership="adopted"
        )
    elif changed == "incomplete":
        arguments["inventory"] = replace(
            arguments["inventory"], components_complete=False
        )
    elif changed == "foreign":
        arguments["inventory"] = replace(
            arguments["inventory"], workspace_id="other-workspace"
        )
    elif changed == "artifact":
        arguments["bootstrap_artifact_id"] = "invalid"
    elif changed == "role":
        arguments["runtime"]["actor_role_names"]["installer"] = "other-role"
    elif changed == "uid":
        grant = next(
            item
            for item in arguments["inventory"].grants
            if item.spec.get("key") == "cleanup-cluster-role"
        )
        key = (
            grant.spec["body"]["kind"],
            None,
            grant.spec["body"]["metadata"]["name"],
        )
        runtime.cloud.objects[key]["metadata"]["uid"] = "replaced"
    else:
        generation = arguments["inventory"].grants[0].spec["generation"]
        principal = "arn:aws:iam::000000000002:role/unattributed"
        runtime.cloud.entries[principal] = {
            "principalArn": principal,
            "kubernetesGroups": ["sp-bootstrap-" + generation[:24] + ":cleanup"],
        }
    with pytest.raises((LifecycleRefused, BootstrapRefused)):
        compile_managed_access_plan(**arguments)
    assert runtime.cloud.mutations == initial


def test_managed_cleanup_plan_requires_real_original_seal(
    runtime, tmp_path_factory, request
):
    from types import SimpleNamespace

    from workspace_provisioning.retirement_managed_access import (
        require_managed_sealed_plan,
    )

    from .postgres_bridge import Harness

    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    source = SimpleNamespace(
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        admitted_request=lambda: SimpleNamespace(
            parameters={"allocation_id": plan.original_allocation_id}
        ),
    )

    async def check(harness):
        async with harness.connect() as connection:
            with pytest.raises(LifecycleRefused, match="sealed"):
                await require_managed_sealed_plan(connection, source, plan)
            await connection.execute(
                "INSERT INTO harness_allocation_seal "
                "(org_id,workspace_id,allocation_id,sealed_revision,operation_id,"
                "attempt_id,executor_id,fence_token) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                plan.org_id,
                plan.workspace_id,
                plan.original_allocation_id,
                "original-revision",
                "bootstrap-operation",
                "bootstrap-attempt",
                "bootstrap-worker",
                1,
            )
            await require_managed_sealed_plan(connection, source, plan)
            with pytest.raises(LifecycleRefused, match="exchange"):
                await require_managed_sealed_plan(
                    connection,
                    SimpleNamespace(
                        org_id=plan.org_id,
                        workspace_id=plan.workspace_id,
                        admitted_request=lambda: SimpleNamespace(
                            parameters={"allocation_id": plan.allocation_id}
                        ),
                    ),
                    plan,
                )

    with Harness.started(tmp_path_factory, request.node.name) as harness:
        harness.run(check(harness))


@pytest.mark.parametrize(
    "changed", ["unchanged", "artifact", "allocation", "mode", "approval_policy"]
)
def test_managed_request_binds_original_artifact_and_separate_allocation(
    runtime, changed
):
    import json
    from types import SimpleNamespace

    from harness_jobs.identity import (
        OperationRequest,
        encode_payload,
        payload_digest,
    )

    from workspace_provisioning.retirement_access_authority import (
        access_request,
        validate_request,
    )

    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    account_id, region = plan.cluster_arn.split(":")[4], plan.cluster_arn.split(":")[3]
    deployment = policy()
    deployment["runtime"] = arguments["runtime"]
    deployment["permitted_target_accounts"] = [account_id]
    deployment["permitted_regions"] = [region]
    deployment["credential_references"][account_id] = deployment[
        "credential_references"
    ].pop("000000000002")
    original = OperationRequest(
        action="provision",
        idempotency_key="bootstrap-request",
        parameters={
            "allocation_id": "original-allocation"
            if changed != "allocation"
            else "wrong",
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_request": json.dumps(
                {
                    "mode": "existing-account-managed"
                    if changed != "mode"
                    else "bring-existing-cluster",
                    "region": region,
                    "target_account_id": account_id,
                    "workspace_id": plan.workspace_id,
                }
            ),
            "lifecycle_inputs": json.dumps({"isolation_mode": "dedicated"}),
            "lifecycle_artifact_id": "a" * 64 if changed != "artifact" else "b" * 64,
            "aws_account_id": account_id,
        },
    )
    source = SimpleNamespace(
        state="succeeded",
        operation_id="completed-bootstrap",
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        job_id="source-job",
        attempt_id="source-attempt",
        plan_digest=payload_digest(original),
        request_payload=encode_payload(original),
        admitted_request=lambda: original,
    )
    if changed in {"artifact", "allocation", "mode"}:
        with pytest.raises(LifecycleRefused):
            access_request(plan, source, deployment)
        return
    request = access_request(plan, source, deployment)
    if changed == "approval_policy":
        deployment["permitted_modes"] = ["adopt"]
        with pytest.raises(LifecycleRefused, match="policy"):
            validate_request(
                request,
                org_id=plan.org_id,
                workspace_id=plan.workspace_id,
                policy=deployment,
            )
    else:
        assert (
            validate_request(
                request,
                org_id=plan.org_id,
                workspace_id=plan.workspace_id,
                policy=deployment,
            )
            == arguments["runtime"]
        )
        assert request.parameters["retirement_access_plan_sha256"] == plan.revision
        assert request.parameters["allocation_id"] == plan.allocation_id
        assert request.parameters["allocation_id"] != plan.original_allocation_id
        assert list(plan.recipe()) == ["cleaner-entry"]
