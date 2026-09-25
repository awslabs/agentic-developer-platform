"""One original-instance command per purpose, observed under the original lease."""

import asyncio
import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from botocore.config import Config
from botocore.exceptions import ClientError
from harness_jobs.identity import OperationRefused

from .node_command_journal import Journal
from .node_command_plan import DOCUMENTS, KINDS, canonical, digest


def ssm_client(session, region):
    # SendCommand has no ClientToken: an SDK retry can execute twice.
    return session.client(
        "ssm",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10
        ),
    )


async def sdk(authorize, method, **arguments):
    await authorize()
    result = await asyncio.to_thread(method, **arguments)
    await authorize()
    return result


async def preflight(provider, operation, plan, authorize):
    """Only installed image/document facts; never require an uncreated node Online."""
    if plan.node_bootstrap is None:
        return
    session, _ = await provider.session_for(operation, plan)
    account = plan.data["provider_account_id"]
    manifest = plan.node_bootstrap["runtime_manifest"]
    for binding in plan.region_bindings:
        region = binding["region"]
        ec2 = session.client("ec2", region_name=region)
        images = (
            await sdk(authorize, ec2.describe_images, ImageIds=[binding["image_id"]])
        )["Images"]
        if len(images) != 1:
            raise OperationRefused("approved prepared node image unavailable")
        image = images[0]
        tags = {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}
        if (
            image.get("ImageId") != binding["image_id"]
            or image.get("OwnerId") != account
            or image.get("State") != "available"
            or image.get("Architecture") != manifest["architecture"]
            or tags.get("superplane-node-runtime-sha256") != manifest["artifact_sha256"]
        ):
            raise OperationRefused("prepared GPU image provenance is unavailable")
        ssm = ssm_client(session, region)
        for name, expected_hash, _, _ in DOCUMENTS.values():
            arn = f"arn:aws:ssm:{region}:{account}:document/{name}"
            document = (
                await sdk(
                    authorize, ssm.describe_document, Name=arn, DocumentVersion="1"
                )
            )["Document"]
            content = await sdk(
                authorize,
                ssm.get_document,
                Name=arn,
                DocumentVersion="1",
                DocumentFormat="JSON",
            )
            if (
                document.get("Owner") != account
                or document.get("Name") not in {name, arn}
                or document.get("DocumentVersion") != "1"
                or document.get("DocumentType") != "Command"
                or document.get("Status") != "Active"
                or document.get("HashType") != "Sha256"
                or document.get("Hash") != expected_hash
                or content.get("Name") not in {name, arn}
                or content.get("DocumentVersion") != "1"
                or content.get("DocumentType") != "Command"
                or not isinstance(content.get("Content"), str)
                or hashlib.sha256(content["Content"].encode()).hexdigest()
                != expected_hash
            ):
                raise OperationRefused(
                    "installed native command document differs from approval"
                )


def contract_for(operation, plan, instance, purpose):
    lease = operation.grant.lease
    manifest = plan.node_bootstrap["runtime_manifest"]
    value = {
        "version": 1,
        "purpose": purpose,
        "operation_id": lease.operation_id,
        "attempt_id": lease.attempt_id,
        "fence_token": lease.fence_token,
        "allocation_id": operation.request.parameters["allocation_id"],
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "cluster_arn": plan.data["cluster_arn"],
        "account_id": plan.data["provider_account_id"],
        "region": instance["SuperplaneRegion"],
        "availability_zone": instance["Placement"]["AvailabilityZone"],
        "instance_id": instance["InstanceId"],
        "image_id": instance["ImageId"],
        "wrapper_sha256": plan.node_bootstrap[
            "bootstrap_wrapper_sha256"
            if purpose == "node-bootstrap"
            else "probe_wrapper_sha256"
        ],
        "runtime_deadline": lease.runtime_deadline.isoformat(),
        "nonce": digest(
            [
                lease.org_id,
                lease.workspace_id,
                lease.operation_id,
                instance["InstanceId"],
                purpose,
            ]
        ),
        "endpoint": plan.data["endpoint"],
        "certificate_authority": plan.data["certificate_authority"],
        "cidrs": [plan.network["cluster"]["network"]["vpc_cidr"]],
        "runtime_manifest": manifest,
    }
    if purpose == "node-bootstrap":
        value["node_config"] = plan.node_config(operation)
    if len(base64.b64encode(canonical(value).encode())) > 16384:
        raise OperationRefused("native command contract exceeds transport bound")
    return value


def validate_instances(plan, instances):
    bindings = {b["region"]: b for b in plan.region_bindings}
    if (
        len(instances) != plan.data["node_count"]
        or len({i["InstanceId"] for i in instances}) != len(instances)
        or len({i["SuperplaneRegion"] for i in instances}) != 1
    ):
        raise OperationRefused(
            "native command requires exact original allocation inventory"
        )
    for instance in instances:
        binding = bindings[instance["SuperplaneRegion"]]
        profile = instance.get("IamInstanceProfile", {}).get("Arn", "")
        if (
            instance.get("State", {}).get("Name") != "running"
            or instance.get("ImageId") != binding["image_id"]
            or not re.fullmatch(r"i-[a-f0-9]{17}", instance["InstanceId"])
            or not instance["Placement"]["AvailabilityZone"].startswith(
                binding["region"]
            )
            or profile
            != f"arn:aws:iam::{plan.data['provider_account_id']}:instance-profile/{binding['instance_profile']}"
        ):
            raise OperationRefused(
                "native command instance image/profile/identity differs"
            )


async def original_instances(provider, operation, plan, instances):
    validate_instances(plan, instances)
    lease = operation.grant.lease
    for instance in instances:
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        if (
            tags.get("superplane-org") != lease.org_id
            or tags.get("superplane-workspace") != lease.workspace_id
        ):
            raise OperationRefused("native command instance tenant tags differ")
    source = operation.request.parameters.get(
        "controller_source_operation_id", lease.operation_id
    )
    async with provider.execution_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT DISTINCT r.provider_reference
 FROM harness_allocation_resource r JOIN harness_provider_call_intent i
 ON i.idempotency_key=ANY(r.operation_keys) AND i.operation_id=r.operation_id
 AND i.org_id=r.org_id AND i.workspace_id=r.workspace_id
 WHERE r.org_id=$1 AND r.workspace_id=$2 AND r.allocation_id=$3 AND r.operation_id=$4
 AND r.provider='aws' AND r.kind='instance' AND i.provider='aws' AND i.operation_kind='launch'""",
            lease.org_id,
            lease.workspace_id,
            operation.request.parameters["allocation_id"],
            source,
        )
    expected = {
        plan.resource_reference("instance", i["InstanceId"], i["SuperplaneRegion"])
        for i in instances
    }
    if {row["provider_reference"] for row in rows} != expected:
        raise OperationRefused(
            "native command instance differs from original launch membership"
        )


def validate_receipt(raw, contract):
    if not isinstance(raw, str) or len(raw.encode()) > 8192:
        raise OperationRefused("native command output exceeds bound")
    try:
        value = json.loads(raw)
        expected = {
            key: contract[key]
            for key in (
                "version",
                "purpose",
                "nonce",
                "instance_id",
                "account_id",
                "region",
                "availability_zone",
                "image_id",
            )
        }
        expected.update(contract_sha256=digest(contract), status="succeeded")
        if set(value) != set(expected) | {"probe"} or any(
            value[k] != v for k, v in expected.items()
        ):
            raise ValueError("receipt identity differs")
        if contract["purpose"] == "node-api-dns-tls":
            from .network_observation import receipt

            receipt(
                canonical(value["probe"]),
                nonce=contract["nonce"],
                source="node",
                endpoint=contract["endpoint"],
                cidrs=contract["cidrs"],
            )
        elif value["probe"] is not None:
            raise ValueError("bootstrap is not packet evidence")
        return value
    except (KeyError, TypeError, ValueError):
        raise OperationRefused("native command evidence invalid") from None


async def invocation(ssm, row, authorize):
    contract = json.loads(row["contract"])
    name, _, plugin, _ = DOCUMENTS[row["purpose"]]
    response = await sdk(
        authorize,
        ssm.get_command_invocation,
        CommandId=row["command_id"],
        InstanceId=row["instance_id"],
        PluginName=plugin,
    )
    arn = f"arn:aws:ssm:{row['region']}:{row['account_id']}:document/{name}"
    if (
        response.get("CommandId") != row["command_id"]
        or response.get("InstanceId") != row["instance_id"]
        or response.get("DocumentName") not in {name, arn}
        or response.get("DocumentVersion") != "1"
        or response.get("PluginName") != plugin
    ):
        raise OperationRefused("original native invocation identity differs")
    status = response.get("Status")
    if status == "Success":
        if (
            response.get("StatusDetails") != "Success"
            or response.get("ResponseCode") != 0
            or response.get("StandardErrorContent", "")
        ):
            raise OperationRefused("native invocation result is not successful")
        return validate_receipt(response.get("StandardOutputContent"), contract)
    if status not in {"Pending", "InProgress", "Delayed"}:
        raise OperationRefused(
            "native invocation failed or unresolved; recovery required"
        )
    return None


async def execute(provider, operation, target, plan, call, authorize):
    provider.workspace.require_dedicated_node_authority(target)
    if plan.node_bootstrap is None:
        raise OperationRefused("native command was not approved")
    purpose = KINDS[call.operation_kind]
    name, document_hash, _, seconds = DOCUMENTS[purpose]
    await preflight(provider, operation, plan, authorize)
    instances = await provider.instances(operation, plan)
    await original_instances(provider, operation, plan, instances)
    originals = {i["InstanceId"]: i for i in instances}
    session, _ = await provider.session_for(operation, plan)
    journal = Journal(provider, operation, call, authorize)
    references = []
    for instance in sorted(instances, key=lambda i: i["InstanceId"]):
        contract = contract_for(operation, plan, instance, purpose)
        ssm = ssm_client(session, contract["region"])
        async with journal.locked(contract) as (connection, row):
            references.append(row["reference"])
            if row["state"] == "prepared":
                # Enrollment is post-launch and bounded; no target wildcard or
                # hybrid managed-instance ID can enter this native path.
                for attempt in range(3):
                    info = await sdk(
                        authorize,
                        ssm.describe_instance_information,
                        Filters=[
                            {"Key": "InstanceIds", "Values": [contract["instance_id"]]}
                        ],
                        MaxResults=5,
                    )
                    nodes = info.get("InstanceInformationList", [])
                    if (
                        not info.get("NextToken")
                        and len(nodes) == 1
                        and (
                            nodes[0].get("InstanceId") == contract["instance_id"]
                            and nodes[0].get("PingStatus") == "Online"
                            and nodes[0].get("PlatformType") == "Linux"
                            and nodes[0].get("AgentVersion")
                            == plan.node_bootstrap["runtime_manifest"][
                                "ssm_agent_version"
                            ]
                        )
                    ):
                        break
                    if attempt == 2:
                        raise OperationRefused(
                            "original instance SSM enrollment unavailable"
                        )
                    await asyncio.sleep(2)
                remaining = (
                    operation.grant.lease.runtime_deadline - datetime.now(UTC)
                ).total_seconds()
                if remaining < (330 if purpose == "node-bootstrap" else 75):
                    raise OperationRefused(
                        "original deadline cannot contain native command"
                    )
                fresh = await provider.instances(operation, plan)
                await original_instances(provider, operation, plan, fresh)
                selected_instance = next(
                    (i for i in fresh if i["InstanceId"] == contract["instance_id"]),
                    None,
                )
                if (
                    selected_instance is None
                    or contract_for(operation, plan, selected_instance, purpose)
                    != contract
                ):
                    raise OperationRefused(
                        "original command identity changed before dispatch"
                    )
                row = await journal.dispatching(connection, row, seconds)
                if row is None:
                    raise OperationRefused(
                        "original native command dispatch already consumed"
                    )
                await authorize()
                # No generic sdk() here: retain accepted handle BEFORE a
                # post-call authority check, even after in-flight revocation.
                response = await asyncio.to_thread(
                    ssm.send_command,
                    InstanceIds=[contract["instance_id"]],
                    DocumentName=f"arn:aws:ssm:{contract['region']}:{contract['account_id']}:document/{name}",
                    DocumentVersion="1",
                    DocumentHash=document_hash,
                    DocumentHashType="Sha256",
                    TimeoutSeconds=30,
                    Parameters={
                        "Contract": [
                            base64.b64encode(canonical(contract).encode()).decode()
                        ]
                    },
                    Comment=row["contract_sha256"],
                )
                command_id = response.get("Command", {}).get("CommandId", "")
                if (
                    not isinstance(command_id, str)
                    or str(uuid.UUID(command_id)) != command_id
                ):
                    raise OperationRefused(
                        "accepted command handle unavailable; never resubmit"
                    )
                row = await journal.remember_handle(connection, row, command_id)
                await authorize()
            if row["command_id"] is None:
                raise OperationRefused(
                    "native command acceptance uncertain; never resubmit"
                )
            result = None
            while datetime.now(UTC) < row["observation_deadline"]:
                await authorize()
                try:
                    result = await invocation(ssm, row, authorize)
                except ClientError as error:
                    if (
                        error.response.get("Error", {}).get("Code")
                        != "InvocationDoesNotExist"
                    ):
                        raise
                if result is not None:
                    break
                await asyncio.sleep(2)
            if result is None:
                raise OperationRefused("original native observation deadline exhausted")
            current = await provider.instances(operation, plan)
            await original_instances(provider, operation, plan, current)
            if {i["InstanceId"] for i in current} != set(originals) or any(
                contract_for(operation, plan, i, purpose)
                != contract_for(operation, plan, originals[i["InstanceId"]], purpose)
                for i in current
            ):
                raise OperationRefused(
                    "original native allocation changed during command"
                )
            await journal.result(connection, row, result)
    return references[0]
