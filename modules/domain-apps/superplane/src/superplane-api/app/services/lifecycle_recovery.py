"""API-owned read-only recovery transport; workers receive bounded facts only."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException


READS = {
    ("sts", "get_caller_identity"): "sts:GetCallerIdentity",
    ("eks", "describe_cluster"): "eks:DescribeCluster",
    ("eks", "list_nodegroups"): "eks:ListNodegroups",
    ("eks", "describe_nodegroup"): "eks:DescribeNodegroup",
    ("eks", "describe_addon"): "eks:DescribeAddon",
    ("ec2", "describe_vpc_endpoints"): "ec2:DescribeVpcEndpoints",
    ("ec2", "describe_security_groups"): "ec2:DescribeSecurityGroups",
    ("ec2", "describe_security_group_rules"): "ec2:DescribeSecurityGroupRules",
    ("ec2", "describe_launch_template_versions"): "ec2:DescribeLaunchTemplateVersions",
}


def bounded_json(value, limit):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("unsupported observation value")

    if (
        not isinstance(value, dict)
        or len(json.dumps(value, default=encode, allow_nan=False).encode()) > limit
    ):
        raise HTTPException(503, "lifecycle observation response exceeded its bound")
    return value


async def lifecycle_context(request, body):
    from app.config import settings
    from app.routers.controller_recovery import claim_operation, composition
    from workspace_provisioning.recovery_proposals import original_result

    operation = await claim_operation(request, body.claim)
    connect = composition(request).operation_connect
    async with connect() as connection:
        registered = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
            "AND provisioning_operation_id=$3)",
            body.claim.workspace_id,
            body.claim.org_id,
            body.claim.operation_id,
        )
    if (
        not registered
        or operation.request.parameters.get("lifecycle_phase") != "apply-infrastructure"
    ):
        # Bootstrap needs a separately verified canonical journal anchor and
        # exact read paths. An apply readback cannot substitute for that proof.
        raise HTTPException(
            403, "this lifecycle phase has no completed-result observer"
        )
    context = SimpleNamespace(
        connect=connect,
        domain_connect=connect,
        policy_file=Path(settings.superplane_lifecycle_config_file),
    )
    artifact, step, source = await original_result(
        operation, context, body.idempotency_key
    )
    if (
        step.step_id != "apply-infrastructure"
        or artifact["account_id"] != source.target_account_id
    ):
        raise HTTPException(403, "original lifecycle observation target changed")
    return operation, context, artifact, source


@asynccontextmanager
async def observation_provider(*, account_id, region, current):
    """Finite exact SDK read list under an API-configured target-account role."""
    async with scoped_observation_provider(
        account_id=account_id,
        region=region,
        current=current,
        reads=READS,
        role_environment="SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN",
        read_limit=64,
        session_name="superplane-lifecycle-observe",
    ) as provider:
        yield provider


@asynccontextmanager
async def scoped_observation_provider(
    *, account_id, region, current, reads, role_environment, read_limit, session_name
):
    """API-owned composition chooses the finite policy, never worker arguments."""
    import boto3
    from botocore.config import Config

    role = os.environ.get(role_environment, "")
    if not re.fullmatch(
        r"arn:aws:iam::" + re.escape(account_id) + r":role/[A-Za-z0-9+=,.@_/-]+", role
    ):
        raise HTTPException(403, "lifecycle recovery observation role account refused")
    config = Config(
        connect_timeout=3,
        read_timeout=3,
        retries={"total_max_attempts": 1},
        ignore_configured_endpoint_urls=True,
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": sorted(reads.values()), "Resource": "*"}
        ],
    }
    await current()

    def assume():
        base = boto3.Session()
        sts = base.client("sts", region_name=region, config=config)
        try:
            credentials = sts.assume_role(
                RoleArn=role,
                RoleSessionName=session_name,
                DurationSeconds=900,
                Policy=json.dumps(policy),
            )["Credentials"]
        finally:
            sts.close()
        return boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=region,
        )

    session = await asyncio.to_thread(assume)
    await current()

    class ReadProvider:
        reads = 0

        async def aws_read(self, service, method, **arguments):
            if (service, method) not in reads or self.reads >= read_limit:
                raise HTTPException(403, "lifecycle recovery read refused")
            self.reads += 1
            await current()

            def read():
                client = session.client(service, region_name=region, config=config)
                try:
                    return getattr(client, method)(**arguments)
                finally:
                    client.close()

            result = await asyncio.to_thread(read)
            await current()
            return bounded_json(result, 4 * 2**20)

    # No delivery_role, execution resolver, AWS session or credential is exposed
    # to the worker. This object is confined to the trusted API observer helper.
    yield ReadProvider()


async def observe_lifecycle(request, body):
    from superplane_executor.recovery_authority import same_recovery_operation
    from workspace_provisioning.recovery_observer import observe_result

    # Gateway's finite request timeout is25 seconds; finish/refuse inside it.
    async with asyncio.timeout(20):
        operation, context, artifact, source = await lifecycle_context(request, body)

        async def current():
            latest, _, row, approved = await lifecycle_context(request, body)
            if (
                not same_recovery_operation(latest, operation)
                or row != artifact
                or approved != source
            ):
                raise HTTPException(
                    403, "lifecycle recovery authority changed during observation"
                )

        async with observation_provider(
            account_id=artifact["account_id"], region=source.region, current=current
        ) as provider:
            facts = await observe_result(
                operation, context, artifact=artifact, provider=provider
            )
        await current()
    return {
        "phase": "apply-infrastructure",
        "idempotency_key": body.idempotency_key,
        "result_artifact_id": artifact["artifact_id"],
        "plan_digest": operation.plan_digest,
        "facts": bounded_json(facts, 65536),
    }
