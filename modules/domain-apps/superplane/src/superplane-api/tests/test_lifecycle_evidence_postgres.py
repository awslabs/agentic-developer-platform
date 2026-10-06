"""Read-only evidence with real paid admissions and separate SQL authorities.

Provider/process transports are explicit doubles. Bootstrap is admitted, not run;
no live readiness, provider inventory completeness, or deletion is established.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from account_factory.modes import OwnershipMode
from app.services.lifecycle_evidence import native_evidence
from workspace_provisioning import runtime
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.tests.postgres_bridge import requires_harness_postgres
from workspace_provisioning.tests.test_lifecycle_runtime_postgres import (
    Scenario,
    harness as harness,
)

pytestmark = requires_harness_postgres


@asynccontextmanager
async def split_authorities(harness, *, managed=False):  # noqa: F811
    """Distinct read-only roles; neither connector can read the other's tables."""
    suffix = uuid4().hex
    operation_role, domain_role = "op_" + suffix, "domain_" + suffix
    tables = {
        operation_role: "harness_operations,harness_approval_consumption"
        + (",harness_allocation_seal,harness_allocation_epoch" if managed else ""),
        domain_role: "workspace_lifecycle_artifacts"
        + (",workspace_lifecycle_control_operations,workspaces" if managed else ""),
    }
    async with harness.connect() as connection:
        for role, allowed in tables.items():
            await connection.execute(f"CREATE ROLE {role} NOLOGIN")
            await connection.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            await connection.execute(f"GRANT SELECT ON {allowed} TO {role}")

    def connect(role):
        @asynccontextmanager
        async def factory():
            async with (
                harness.connect() as connection,
                connection.transaction(readonly=True),
            ):
                await connection.execute(f"SET LOCAL ROLE {role}")
                yield connection

        return factory

    operation, domain = connect(operation_role), connect(domain_role)
    try:
        for connector, forbidden in (
            (operation, "workspace_lifecycle_artifacts"),
            (domain, "harness_operations"),
            (domain, "harness_approval_consumption"),
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connector() as connection:
                    await connection.fetch(f"SELECT * FROM {forbidden}")
        yield SimpleNamespace(operation_connect=operation, domain_connect=domain)
    finally:
        async with harness.connect() as connection:
            for role in tables:
                await connection.execute(f"DROP OWNED BY {role}")
                await connection.execute(f"DROP ROLE {role}")


async def applied(scenario, *, sensitive=False):
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
    scenario.outputs.update(
        {
            "private_subnet_ids": {
                "value": ["subnet-11111111111111111", "subnet-22222222222222222"]
            },
            "public_subnet_ids": {"value": []},
            "network_ownership": {"value": "supplied"},
        }
    )
    # Terraform's output-json shape includes the sensitive marker. Never infer
    # that unmarked/sensitive fixture outputs are safe to publish.
    for value in scenario.outputs.values():
        value["sensitive"] = False
    scenario.outputs["cluster_name"]["sensitive"] = sensitive
    scenario.outputs["private_credential"] = {
        "value": "MUST-NOT-LEAK",
        "sensitive": True,
    }
    vpc = scenario.responses["describe_cluster"]["cluster"]["resourcesVpcConfig"]
    vpc["clusterSecurityGroupId"] = "sg-22222222222222222"
    vpc["securityGroupIds"] = ["sg-11111111111111111"]
    scenario.responses["describe_launch_template_versions"]["LaunchTemplateVersions"][
        0
    ]["LaunchTemplateData"].pop("SecurityGroupIds")
    result = await runtime.run_lifecycle(applying, scenario.context)
    return prepared, await scenario.row(result["artifact_id"])


@pytest.mark.parametrize("sensitive", [False, True])
def test_native_evidence_uses_separate_read_authorities(
    harness,  # noqa: F811
    tmp_path,
    monkeypatch,
    sensitive,
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )

    async def run():
        prepared, applied_row = await applied(scenario, sensitive=sensitive)
        bootstrap = await scenario.admit(continuation_parameters(applied_row))
        scope = dict(
            org_id="org-a",
            workspace_id="ws-1",
            root_operation_id=prepared["source_operation_id"],
            current_operation_id=bootstrap.grant.lease.operation_id,
        )
        async with harness.connect() as connection:
            before = await connection.fetchval(
                "SELECT count(*) FROM harness_operations"
            )
        process_calls = list(scenario.process_calls)
        async with split_authorities(harness) as composition:
            partial, partial_ownership = await native_evidence(
                composition,
                **{**scope, "current_operation_id": applied_row["source_operation_id"]},
            )
            assert len(partial["phases"]) == 2 and partial_ownership is None
            lineage, ownership = await native_evidence(composition, **scope)
            assert len(lineage["phases"]) == 3
            assert lineage["phases"][-1]["state"] != "succeeded"
            if sensitive:
                assert ownership is None
                return
            assert ownership is not None
            assert ownership["artifact_id"] == applied_row["artifact_id"]
            assert ownership["current_operation_id"] == scope["current_operation_id"]
            assert ownership["inventory_complete"] is False
            assert ownership["status"] == "OBSERVED"
            assert ownership["owned_resources"] and ownership["preserved_resources"]
            assert "MUST-NOT-LEAK" not in str(ownership)
            assert "private_credential" not in str(ownership)
            assert "ready" not in str(ownership).lower()
            async with harness.connect() as connection:
                await connection.execute(
                    "UPDATE workspace_lifecycle_artifacts SET created_at=$1",
                    datetime.now(UTC) - timedelta(days=2),
                )
            # Expired historical evidence remains observable but grants no execution.
            _, expired = await native_evidence(composition, **scope)
            assert expired["artifact_id"] == ownership["artifact_id"]
            assert expired["recorded_at"] != ownership["recorded_at"]
            for field in ("org_id", "workspace_id"):
                with pytest.raises(LifecycleRefused):
                    await native_evidence(composition, **{**scope, field: "other"})
            async with harness.connect() as connection:
                await connection.execute(
                    "DELETE FROM harness_approval_consumption WHERE operation_id=$1",
                    applied_row["source_operation_id"],
                )
            with pytest.raises(LifecycleRefused):
                await native_evidence(composition, **scope)
        async with harness.connect() as connection:
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == before
            )
        assert not scenario.bootstrap_calls
        assert scenario.process_calls == process_calls

    harness.run(run())
