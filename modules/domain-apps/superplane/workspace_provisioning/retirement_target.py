"""Verify teardown transport against immutable paid apply outputs."""

import asyncio
import json
from dataclasses import replace

from account_factory.modes import from_mapping
from superplane_bootstrap.target import VerifiedTarget

from .artifacts import read_artifact
from .credentials import canonical_role_identity
from .retirement_access_clients import GuardedClient, require_cluster
from .retirement_managed_access import verify_managed_access_artifact
from .runtime_config import LifecycleRefused


async def retirement_target(operation, context, plan, session, verify):
    lease = operation.grant.lease
    row = await read_artifact(
        context.domain_connect,
        artifact_id=plan.bootstrap_artifact_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    request = from_mapping(
        json.loads(operation.request.parameters["lifecycle_request"])
    )
    outputs = verify_managed_access_artifact(row, request, plan)

    def observe():
        provider = canonical_role_identity(
            session, session, session._superplane_role_arn, verify=verify
        )
        if provider.account_id != request.target_account_id:
            raise LifecycleRefused("retirement provider belongs to another account")
        client = GuardedClient(
            session.client("eks", region_name=request.region), verify
        )
        try:
            cluster = client.describe_cluster(name=outputs["cluster_name"])["cluster"]
        except Exception as error:
            if (
                getattr(error, "response", {}).get("Error", {}).get("Code")
                != "ResourceNotFoundException"
            ):
                raise
            # This permits observation/reconciliation only. The execution journal
            # still refuses to reissue an unknown destructive operation.
        else:
            require_cluster(cluster, outputs)
        return VerifiedTarget(
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            account_id=provider.account_id,
            region=request.region,
            cluster_name=outputs["cluster_name"],
            cluster_arn=outputs["cluster_arn"],
            endpoint=outputs["cluster_endpoint"],
            certificate_authority_data=outputs["cluster_certificate_authority_data"],
            principal_arn=provider.principal_arn,
            cluster_ownership="adp-created",
        )

    return await asyncio.to_thread(observe), outputs


def expected_prerequisites(outputs, config):
    from superplane_bootstrap.cli import _expected_prerequisites

    return replace(
        _expected_prerequisites(outputs, config["management_security_group_id"]),
        retained_sts_rule_id=outputs["sts_endpoint_rule_id"],
    )
