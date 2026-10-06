"""Historical recovery across real admitted native prepare/apply operations.

Cloud/process transports are doubled. Bootstrap admission is real but execution
is deliberately not performed; these tests establish lineage, never live Ready.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from account_factory.modes import OwnershipMode
from workspace_provisioning import runtime
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.lineage import verified_native_lineage
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import requires_harness_postgres
from .test_lifecycle_runtime_postgres import Scenario, harness as lifecycle_harness

harness = lifecycle_harness
pytestmark = requires_harness_postgres


async def applied(scenario):
    from workspace_provisioning.adoption import prepare_adoption

    prepared = await scenario.prepared()
    applying = await scenario.admit(continuation_parameters(prepared))
    discovery = replace(
        scenario.request,
        mode=OwnershipMode.BRING_EXISTING_CLUSTER,
        existing_cluster_name="adopted",
        vpc_cidr=None,
        availability_zones=(),
        cluster_version=None,
    )
    _, metadata = await prepare_adoption(
        applying, scenario.context, discovery, scenario.session
    )
    scenario.outputs = metadata["outputs"]
    scenario.outputs["workspace_node_group"] = {
        "value": {
            "name": "nodes",
            "arn": scenario.responses["describe_nodegroup"]["nodegroup"][
                "nodegroupArn"
            ],
            "launch_template_id": "lt-0123456789abcdef0",
            "launch_template_version": "2",
        }
    }
    vpc = scenario.responses["describe_cluster"]["cluster"]["resourcesVpcConfig"]
    vpc["clusterSecurityGroupId"] = "sg-22222222222222222"
    vpc["securityGroupIds"] = ["sg-11111111111111111"]
    scenario.responses["describe_launch_template_versions"]["LaunchTemplateVersions"][
        0
    ]["LaunchTemplateData"].pop("SecurityGroupIds")
    result = await runtime.run_lifecycle(applying, scenario.context)
    return prepared, await scenario.row(result["artifact_id"])


def test_native_lineage_preserves_original_across_admitted_phases_and_expiry(
    harness, tmp_path, monkeypatch
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )

    async def run():
        prepared, apply_result = await applied(scenario)
        bootstrap = await scenario.admit(continuation_parameters(apply_result))
        root_id = prepared["source_operation_id"]
        arguments = dict(org_id="org-a", workspace_id="ws-1", root_operation_id=root_id)
        for current, count in (
            (root_id, 1),
            (apply_result["source_operation_id"], 2),
            (bootstrap.grant.lease.operation_id, 3),
        ):
            proof = await verified_native_lineage(
                harness.connect,
                harness.connect,
                current_operation_id=current,
                **arguments,
            )
            assert proof["root_operation_id"] == root_id
            assert proof["current_operation_id"] == current
            assert len(proof["phases"]) == count
            assert all(item["state"] == "succeeded" for item in proof["phases"][:-1])
            assert "ready" not in proof
        # Admitted but unexecuted bootstrap is observable progress, never success.
        assert proof["phases"][-1]["state"] != "succeeded"
        assert not scenario.bootstrap_calls
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE workspace_lifecycle_artifacts SET created_at=$1",
                datetime.now(UTC) - timedelta(days=2),
            )
        recovered = await verified_native_lineage(
            harness.connect,
            harness.connect,
            current_operation_id=bootstrap.grant.lease.operation_id,
            **arguments,
        )
        assert recovered == proof
        with pytest.raises(LifecycleRefused, match="expired"):
            await scenario.row(prepared["artifact_id"])

    harness.run(run())


@pytest.mark.parametrize(
    "invalid",
    [
        "foreign_org",
        "foreign_workspace",
        "missing_root",
        "different_root",
        "payload",
        "paid_digest",
        "unpaid",
        "artifact_hash",
        "source_failed",
        "skip_apply",
        "shared",
        "reordered",
    ],
)
def test_native_lineage_refuses_substitution_and_incomplete_provenance(
    harness, tmp_path, monkeypatch, invalid
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )

    async def run():
        prepared = await scenario.prepared()
        parameters = continuation_parameters(prepared)
        if invalid == "skip_apply":
            parameters["lifecycle_phase"] = "bootstrap-workspace"
        if invalid == "shared":
            parameters["lifecycle_inputs"] = json.dumps({"cluster_placement": "shared"})
        if invalid == "reordered":
            parameters["lifecycle_source_operation_id"] = "other-source"
        child = await scenario.admit(parameters)
        root_id, child_id = (
            prepared["source_operation_id"],
            child.grant.lease.operation_id,
        )
        arguments = dict(
            org_id="org-a",
            workspace_id="ws-1",
            root_operation_id=root_id,
            current_operation_id=child_id,
        )
        if invalid == "foreign_org":
            arguments["org_id"] = "org-other"
        elif invalid == "foreign_workspace":
            arguments["workspace_id"] = "ws-other"
        elif invalid == "missing_root":
            arguments["root_operation_id"] = "missing"
        elif invalid == "different_root":
            other = await scenario.admit(
                {**scenario.parameters, "workspace_name": "other"}
            )
            arguments["root_operation_id"] = other.grant.lease.operation_id
        async with harness.connect() as connection:
            if invalid == "payload":
                await connection.execute(
                    "UPDATE harness_operations SET plan_digest=$1 WHERE operation_id=$2",
                    "0" * 64,
                    child_id,
                )
            elif invalid == "paid_digest":
                await connection.execute(
                    "UPDATE harness_approval_consumption SET plan_digest=$1 WHERE operation_id=$2",
                    "0" * 64,
                    child_id,
                )
            elif invalid == "unpaid":
                await connection.execute(
                    "DELETE FROM harness_approval_consumption WHERE operation_id=$1",
                    child_id,
                )
            elif invalid == "artifact_hash":
                await connection.execute(
                    "UPDATE workspace_lifecycle_artifacts SET account_id='999999999999' WHERE artifact_id=$1",
                    prepared["artifact_id"],
                )
            elif invalid == "source_failed":
                await connection.execute(
                    "UPDATE harness_operations SET state='failed' WHERE operation_id=$1",
                    root_id,
                )
        with pytest.raises((LifecycleRefused, ValueError, KeyError)):
            await verified_native_lineage(harness.connect, harness.connect, **arguments)
        assert not scenario.bootstrap_calls

    harness.run(run())
