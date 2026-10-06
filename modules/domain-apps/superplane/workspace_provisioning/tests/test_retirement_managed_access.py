"""The proposed managed entry is separate from its sealed bootstrap allocation."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants

from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_plan,
    compile_managed_access_review,
    managed_recipe_inputs,
    verify_managed_access_artifact,
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


def test_managed_review_does_not_upgrade_legacy_bootstrap_grants(runtime):
    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    review_arguments = {
        key: value
        for key, value in arguments.items()
        if key
        not in {
            "inventory",
            "runtime",
            "kubernetes",
            "eks",
            "release",
            "principals",
            "controller_mode",
        }
    }
    with pytest.raises(BootstrapRefused, match="cleanup grant UID, rules"):
        compile_managed_access_review(
            arguments["inventory"],
            arguments["runtime"],
            **review_arguments,
            **managed_recipe_inputs(arguments["inventory"], arguments["runtime"]),
        )


def test_review_digest_matches_live_and_uid_drift_refuses_execution(runtime):
    arguments = inputs(runtime)

    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    review_arguments = {
        key: value
        for key, value in arguments.items()
        if key not in {"inventory", "runtime", "kubernetes", "eks"}
    }
    review = compile_managed_access_review(
        arguments["inventory"], arguments["runtime"], **review_arguments
    )
    live = compile_managed_access_plan(**arguments)
    assert review == live
    assert review.revision == live.revision
    assert review.recipe() == live.recipe()
    with pytest.raises(LifecycleRefused, match="complete owned dedicated"):
        compile_managed_access_plan(**{**arguments, "eks": None})

    grant = next(
        item
        for item in arguments["inventory"].grants
        if item.spec.get("key") == "cleanup-cluster-role"
    )
    key = (grant.spec["body"]["kind"], None, grant.spec["body"]["metadata"]["name"])
    runtime.cloud.objects[key]["metadata"]["uid"] = "replaced"
    assert (
        review.revision
        == compile_managed_access_review(
            arguments["inventory"], arguments["runtime"], **review_arguments
        ).revision
    )
    with pytest.raises(BootstrapRefused, match="live UID"):
        compile_managed_access_plan(**arguments)


def test_review_digest_cannot_authorize_foreign_mapping(runtime):
    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    review = compile_managed_access_review(
        arguments["inventory"],
        arguments["runtime"],
        **{
            key: value
            for key, value in arguments.items()
            if key not in {"inventory", "runtime", "kubernetes", "eks"}
        },
    )
    runtime.cloud.entries[review.grants[0]["principal_arn"]] = {
        "principalArn": review.grants[0]["principal_arn"],
        "kubernetesGroups": [review.cleanup_group],
    }
    with pytest.raises(BootstrapRefused):
        compile_managed_access_plan(**arguments)


@pytest.mark.parametrize(
    "change", [None, "mode", "ownership", "account", "target", "lineage", "output"]
)
def test_managed_access_uses_original_apply_target_not_request_supplied_cluster(
    runtime, change
):
    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    account = plan.cluster_arn.split(":")[4]
    region = plan.cluster_arn.split(":")[3]
    request = SimpleNamespace(
        mode=SimpleNamespace(value="existing-account-managed"),
        cluster_ownership=SimpleNamespace(value="adp-created"),
        target_account_id=account,
        region=region,
    )
    target = {
        "account_id": account,
        "aws_region": region,
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
    }
    outputs = {**target, "cluster_arn": plan.cluster_arn}
    metadata = {
        "next_phase": "bootstrap-workspace",
        "allocation_source_operation_id": "paid-apply",
        "outputs": {
            key: {"value": value, "type": "string", "sensitive": False}
            for key, value in outputs.items()
        },
    }
    row = {
        "account_id": account,
        "source_operation_id": "paid-apply",
        "target_json": json.dumps(target),
        "artifact_metadata_json": json.dumps(metadata),
    }
    if change == "mode":
        request.mode.value = "bring-existing-cluster"
    elif change == "ownership":
        request.cluster_ownership.value = "adopted"
    elif change == "account":
        request.target_account_id = "000000000009"
    elif change == "target":
        target["workspace_id"] = "other-workspace"
        row["target_json"] = json.dumps(target)
    elif change == "lineage":
        metadata["allocation_source_operation_id"] = "other-operation"
        row["artifact_metadata_json"] = json.dumps(metadata)
    elif change == "output":
        metadata["outputs"]["cluster_arn"]["value"] = "different-cluster"
        row["artifact_metadata_json"] = json.dumps(metadata)
    if change is None:
        assert verify_managed_access_artifact(row, request, plan) == outputs
    else:
        with pytest.raises(LifecycleRefused):
            verify_managed_access_artifact(row, request, plan)


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
        state="succeeded",
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        admitted_request=lambda: SimpleNamespace(
            parameters={
                "allocation_id": plan.original_allocation_id,
                "lifecycle_phase": "apply-infrastructure",
            }
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
                    SimpleNamespace(**{**vars(source), "org_id": "other-org"}),
                    plan,
                )
            with pytest.raises(LifecycleRefused, match="exchange"):
                await require_managed_sealed_plan(
                    connection,
                    SimpleNamespace(
                        state="succeeded",
                        org_id=plan.org_id,
                        workspace_id=plan.workspace_id,
                        admitted_request=lambda: SimpleNamespace(
                            parameters={
                                "allocation_id": plan.allocation_id,
                                "lifecycle_phase": "apply-infrastructure",
                            }
                        ),
                    ),
                    plan,
                )

    with Harness.started(tmp_path_factory, request.node.name) as harness:
        harness.run(check(harness))


@pytest.mark.parametrize(
    "changed",
    [
        "unchanged",
        "artifact",
        "allocation",
        "mode",
        "approval_policy",
        "missing_paid",
        "bootstrap_reused",
    ],
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
            "allocation_id": "bootstrap-allocation",
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_source_operation_id": "completed-apply",
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
    paid_request = OperationRequest(
        action="provision",
        idempotency_key="apply-request",
        parameters={
            "allocation_id": "wrong"
            if changed == "allocation"
            else "original-allocation",
            "lifecycle_phase": "apply-infrastructure",
        },
    )
    paid = SimpleNamespace(
        state="succeeded",
        operation_id="completed-apply",
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        admitted_request=lambda: paid_request,
    )
    allocation_source = (
        None
        if changed == "missing_paid"
        else source
        if changed == "bootstrap_reused"
        else paid
    )
    if changed in {
        "artifact",
        "allocation",
        "mode",
        "missing_paid",
        "bootstrap_reused",
    }:
        with pytest.raises(LifecycleRefused):
            access_request(
                plan, source, deployment, allocation_source=allocation_source
            )
        return
    request = access_request(
        plan, source, deployment, allocation_source=allocation_source
    )
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


def test_confirmed_cleanup_entry_has_one_exact_mapping_and_can_be_revoked(runtime):
    from workspace_provisioning.retirement_inventory import (
        require_cleanup_group_mapping,
        require_dormant_cleanup_group,
        retained_cleanup_capability,
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
    capability = retained_cleanup_capability(
        arguments["inventory"],
        original_allocation_id=arguments["original_allocation_id"],
        release=arguments["release"],
        principals=arguments["principals"],
        controller_mode=arguments["controller_mode"],
        kubernetes=arguments["kubernetes"],
    )
    eks = arguments["eks"]
    grant = plan.grants[0]
    identity = eks.create(grant)
    require_cleanup_group_mapping(capability, eks, spec=grant, identity=identity)
    with pytest.raises(BootstrapRefused, match="unapproved EKS mapping"):
        require_dormant_cleanup_group(capability, eks)
    with pytest.raises(BootstrapRefused, match="confirmed entry"):
        require_cleanup_group_mapping(
            capability, eks, spec=grant, identity={**identity, "arn": "substituted"}
        )
    other = f"arn:aws:iam::{runtime.target.account_id}:role/foreign"
    runtime.cloud.entries[other] = {
        "principalArn": other,
        "kubernetesGroups": [capability.group],
    }
    with pytest.raises(BootstrapRefused, match="unapproved EKS mapping"):
        require_cleanup_group_mapping(capability, eks, spec=grant, identity=identity)
    runtime.cloud.entries.pop(other)
    eks.delete(grant, identity)
    require_dormant_cleanup_group(capability, eks)


@pytest.mark.parametrize(
    "failure", ["none", "lost_reply", "foreign_mapping", "changed_grant"]
)
def test_managed_control_grant_checks_dormant_group_and_journals_effect(
    runtime, failure
):
    import asyncio
    from unittest.mock import AsyncMock

    from workspace_bootstrap.tests.test_authority_runtime_postgres import Crash
    from workspace_provisioning.retirement_access_grants import establish_access_grants

    from .test_retirement_access_grants import Journal

    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    journal = Journal(plan)
    eks = arguments["eks"]
    kubernetes = arguments["kubernetes"]
    before = runtime.cloud.mutations

    async def run():
        return await establish_access_grants(
            plan,
            journal,
            eks=eks,
            kubernetes=kubernetes,
            verify_target=AsyncMock(),
        )

    if failure == "foreign_mapping":
        principal = f"arn:aws:iam::{runtime.target.account_id}:role/unattributed"
        runtime.cloud.entries[principal] = {
            "principalArn": principal,
            "kubernetesGroups": [plan.cleanup_group],
        }
        with pytest.raises(BootstrapRefused, match="unapproved EKS mapping"):
            asyncio.run(run())
        assert journal.events == {"cleaner-entry": None}
        assert runtime.cloud.mutations == before
        return
    if failure == "changed_grant":
        retained = next(
            item
            for item in plan.retained_grants
            if item["spec"]["key"] == "cleanup-cluster-role"
        )
        body = retained["spec"]["body"]
        resource = runtime.cloud.objects[(body["kind"], None, body["metadata"]["name"])]
        resource["metadata"]["uid"] = "replaced"
        with pytest.raises(LifecycleRefused, match="retained cleanup grant changed"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before
        return
    if failure == "lost_reply":
        runtime.cloud.crash = ("create-entry", plan.grants[0]["principal_arn"])
        with pytest.raises(Crash):
            asyncio.run(run())
        assert journal.events == {"cleaner-entry": None}
        assert runtime.cloud.mutations == before + 1
        with pytest.raises(LifecycleRefused, match="ambiguous"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before + 1
        return
    recorded = asyncio.run(run())
    assert recorded == journal.events
    assert set(recorded) == {"cleaner-entry"}
    assert runtime.cloud.mutations == before + 1
    assert asyncio.run(run()) == recorded
    assert runtime.cloud.mutations == before + 1


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "lost_reply",
        "changed_identity",
        "changed_artifact",
        "revoked_control",
        "revoked_during_delete",
        "stale_authority",
        "stale_cluster",
        "changed_recipe",
    ],
)
def test_managed_revocation_requires_artifact_and_protected_intent(runtime, failure):
    import asyncio
    from unittest.mock import AsyncMock

    from workspace_bootstrap.tests.test_authority_runtime_postgres import Crash
    from workspace_provisioning.artifacts import canonical, digest
    from workspace_provisioning.retirement_access_artifact import (
        access_metadata,
        access_target,
    )
    from workspace_provisioning.retirement_access_grants import (
        managed_revocation_recipe,
        revoke_managed_access_grant,
    )

    from .test_retirement_access_grants import Journal

    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    eks = arguments["eks"]
    identity = eks.create(plan.grants[0])
    row = {
        "artifact_id": "f" * 64,
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
        "account_id": plan.cluster_arn.split(":")[4],
        "target_json": canonical(access_target(plan)),
        "parameters_json": canonical(
            {
                "lifecycle_phase": "prepare-retirement-access",
                "allocation_id": plan.allocation_id,
                "original_allocation_id": plan.original_allocation_id,
                "retirement_request_id": plan.retirement_request_id,
                "retirement_inventory_sha256": plan.inventory_sha256,
                "retirement_access_plan_sha256": plan.revision,
                "retirement_access_recipe_sha256": digest(plan.recipe()),
            }
        ),
        "artifact_metadata_json": canonical(
            access_metadata(plan, {"cleaner-entry": identity})
        ),
    }
    journal = Journal(
        SimpleNamespace(recipe=lambda: managed_revocation_recipe(plan, row))
    )
    verify_producer = AsyncMock()
    verify_cluster = AsyncMock()
    before = runtime.cloud.mutations

    async def run():
        return await revoke_managed_access_grant(
            plan,
            row,
            journal,
            eks=eks,
            verify_cluster=verify_cluster,
            verify_producer=verify_producer,
        )

    if failure == "changed_artifact":
        row["workspace_id"] = "another-workspace"
        with pytest.raises(LifecycleRefused, match="artifact"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before
        return
    if failure == "changed_identity":
        runtime.cloud.entries[plan.grants[0]["principal_arn"]]["username"] = "replaced"
        with pytest.raises(LifecycleRefused, match="approved readback"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before
        return
    if failure == "stale_cluster":
        verify_cluster.side_effect = LifecycleRefused("cluster replaced")
        with pytest.raises(LifecycleRefused, match="cluster replaced"):
            asyncio.run(run())
        assert journal.events == {}
        assert runtime.cloud.mutations == before
        return
    if failure == "changed_recipe":
        journal.recipe = {"unapproved": {}}
        with pytest.raises(LifecycleRefused, match="admitted recipe"):
            asyncio.run(run())
        assert journal.events == {}
        assert runtime.cloud.mutations == before
        return
    if failure == "stale_authority":
        journal.authority.side_effect = LifecycleRefused("expired cleanup lease")
        with pytest.raises(LifecycleRefused, match="expired cleanup lease"):
            asyncio.run(run())
        assert journal.events == {}
        assert runtime.cloud.mutations == before
        return
    if failure == "revoked_control":
        verify_producer.side_effect = LifecycleRefused("control approval released")
        with pytest.raises(LifecycleRefused, match="control approval released"):
            asyncio.run(run())
        assert journal.events == {}
        assert runtime.cloud.mutations == before
        return
    if failure == "revoked_during_delete":
        verify_producer.side_effect = [
            None,
            None,
            None,
            LifecycleRefused("control approval released after delete"),
        ]
        with pytest.raises(LifecycleRefused, match="released after delete"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before + 1
        assert next(iter(journal.events.values())) is None
        verify_producer.side_effect = None
        with pytest.raises(LifecycleRefused, match="ambiguous"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before + 1
        return
    if failure == "lost_reply":
        runtime.cloud.crash = ("delete-entry", plan.grants[0]["principal_arn"])
        with pytest.raises(Crash):
            asyncio.run(run())
        assert runtime.cloud.mutations == before + 1
        with pytest.raises(LifecycleRefused, match="ambiguous"):
            asyncio.run(run())
        assert runtime.cloud.mutations == before + 1
        return
    assert asyncio.run(run()) == identity
    assert runtime.cloud.mutations == before + 1
    assert asyncio.run(run()) == identity
    assert runtime.cloud.mutations == before + 1
    assert verify_cluster.await_count > 1
