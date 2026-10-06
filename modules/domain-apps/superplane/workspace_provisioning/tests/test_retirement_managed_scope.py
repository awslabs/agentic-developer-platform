"""Managed cleanup must not publish authority broader than its exact-name RBAC."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.retirement_access_grants import establish_access_grants
from workspace_provisioning.retirement_managed_access import compile_managed_access_plan

from .test_retirement_access_grants import Journal
from .test_retirement_managed_access import inputs
from .test_retirement_plan import component


@pytest.mark.parametrize("stage", ["after-create", "confirmed-retry"])
@pytest.mark.parametrize(
    "response", ["policy", "paginated-policy", "unanswered", "pagination-cycle"]
)
def test_managed_access_refuses_unapproved_policy_scope(
    runtime, monkeypatch, stage, response
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
    journal = Journal(plan)
    before = runtime.cloud.mutations

    async def run():
        return await establish_access_grants(
            plan,
            journal,
            eks=arguments["eks"],
            kubernetes=arguments["kubernetes"],
            verify_target=AsyncMock(),
        )

    if stage == "confirmed-retry":
        assert asyncio.run(run()) == journal.events

    observed = []

    def policies(**parameters):
        assert parameters["clusterName"] == runtime.target.cluster_name
        assert parameters["principalArn"] == plan.grants[0]["principal_arn"]
        observed.append(parameters.get("nextToken"))
        if response == "unanswered":
            return {}
        if response == "pagination-cycle" or (
            response == "paginated-policy" and not parameters.get("nextToken")
        ):
            return {"associatedAccessPolicies": [], "nextToken": "next-policy-page"}
        return {
            "associatedAccessPolicies": [
                {
                    "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                    "accessScope": {"type": "cluster"},
                }
            ]
        }

    monkeypatch.setattr(runtime.cloud, "list_associated_access_policies", policies)
    message = {
        "policy": "unapproved EKS access policies",
        "paginated-policy": "unapproved EKS access policies",
        "unanswered": "enumeration was not answered",
        "pagination-cycle": "pagination did not advance",
    }[response]
    with pytest.raises(BootstrapRefused, match=message):
        asyncio.run(run())
    assert observed == (
        [None, "next-policy-page"]
        if response in {"paginated-policy", "pagination-cycle"}
        else [None]
    )
    assert runtime.cloud.mutations == before + 1
    assert journal.events["cleaner-entry"] == arguments["eks"].observe(plan.grants[0])
    with pytest.raises(BootstrapRefused, match=message):
        asyncio.run(run())
    assert runtime.cloud.mutations == before + 1
