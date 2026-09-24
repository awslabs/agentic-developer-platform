"""Finite account creation and bootstrap effects under separately admitted phases."""

import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
import re
from types import SimpleNamespace

from .artifacts import canonical, read_artifact
from .authority import current_operation
from .credentials import assume_session
from .effects import LifecycleEffects
from .runtime_config import LifecycleRefused


def materialize(value, account_id):
    if isinstance(value, str):
        result = value.replace("${ACCOUNT_ID}", account_id)
        if "${" in result:
            raise LifecycleRefused(
                "account policy contains an unsupported template variable"
            )
        return result
    if isinstance(value, list):
        return [materialize(item, account_id) for item in value]
    if isinstance(value, dict):
        return {key: materialize(item, account_id) for key, item in value.items()}
    return value


async def read_sdk(operation, context, method, **arguments):
    await current_operation(operation, context)
    result = await asyncio.to_thread(method, **arguments)
    await current_operation(operation, context)
    return result


async def creation_proof(operation, context, row, management, request):
    from account_provisioning.creation_runner import creation_target, _decode_reference
    from harness_jobs.execution import CallOutcome, read_call

    await current_operation(operation, context)
    if operation.request.parameters.get("lifecycle_artifact_id") != row["artifact_id"]:
        raise LifecycleRefused(
            "child credentials require the separately admitted source artifact"
        )
    supplied = dict(row)
    row = await read_artifact(
        context.domain_connect,
        artifact_id=row["artifact_id"],
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
        require_fresh=False,
    )
    if row != supplied:
        raise LifecycleRefused(
            "supplied child artifact differs from its authenticated stored identity"
        )
    metadata = json.loads(row["artifact_metadata_json"])
    source = row
    if "creation_request_id" not in metadata:
        source = await read_artifact(
            context.domain_connect,
            artifact_id=metadata["creation_artifact_id"],
            org_id=row["org_id"],
            workspace_id=row["workspace_id"],
            require_fresh=False,
        )
        metadata = json.loads(source["artifact_metadata_json"])
    if (
        json.loads(source["parameters_json"])["lifecycle_request"]
        != operation.request.parameters["lifecycle_request"]
        or source["account_id"] != row["account_id"]
    ):
        raise LifecycleRefused(
            "child account provenance differs from the original creation request"
        )
    async with context.connect() as connection:
        original = await connection.fetchrow(
            "SELECT state,job_id,attempt_id,plan_digest,request_payload FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            source["source_operation_id"],
            source["org_id"],
            source["workspace_id"],
        )
        expected_call = metadata.get("creation_call")
        if not isinstance(expected_call, dict) or set(expected_call) != {
            "idempotency_key",
            "provider",
            "operation_kind",
            "target",
            "provider_ref",
        }:
            raise LifecycleRefused(
                "child provenance has no original shared creation call"
            )
        calls = await connection.fetch(
            "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
            source["source_operation_id"],
        )
        if [item["idempotency_key"] for item in calls] != [
            expected_call["idempotency_key"]
        ]:
            raise LifecycleRefused("child provenance has an unapproved provider call")
        call = await read_call(
            connection, idempotency_key=expected_call["idempotency_key"]
        )
    if (
        original is None
        or original["state"] != "succeeded"
        or original["request_payload"] != source["source_request_payload"]
        or (original["job_id"], original["attempt_id"], original["plan_digest"])
        != (
            source["source_job_id"],
            source["source_attempt_id"],
            source["source_payload_digest"],
        )
    ):
        raise LifecycleRefused(
            "child account creation is not the completed original admission"
        )
    if (
        call is None
        or call.outcome is not CallOutcome.SUCCEEDED
        or (
            call.operation_id,
            call.org_id,
            call.workspace_id,
            call.job_id,
            call.attempt_id,
            call.fence_token,
            call.provider,
            call.operation_kind,
            call.target,
        )
        != (
            source["source_operation_id"],
            source["org_id"],
            source["workspace_id"],
            source["source_job_id"],
            source["producer_attempt_id"],
            source["producer_fence_token"],
            "aws-organizations",
            "create-account",
            creation_target(request),
        )
        or any(getattr(call, key) != value for key, value in expected_call.items())
        or _decode_reference(call.provider_ref)
        != (row["account_id"], None, metadata["creation_request_id"])
    ):
        raise LifecycleRefused(
            "child account identity differs from the original shared creation evidence"
        )
    observed = await read_sdk(
        operation,
        context,
        management.client("organizations").describe_create_account_status,
        CreateAccountRequestId=metadata["creation_request_id"],
    )
    status = observed["CreateAccountStatus"]
    if (status.get("Id"), status.get("State"), status.get("AccountId")) != (
        metadata["creation_request_id"],
        "SUCCEEDED",
        row["account_id"],
    ):
        raise LifecycleRefused(
            "AWS does not confirm this child account for the recorded creation request"
        )
    if status.get("AccountName") != "adp-" + request.workspace_id:
        raise LifecycleRefused("recorded creation request names another account")
    return source


async def child_session(
    operation, context, config, request, row, management, *, bootstrap=True
):
    await creation_proof(operation, context, row, management, request)
    account = row["account_id"]
    new = config["new_account"]
    role_name = (
        new["child_access_role_name"]
        if bootstrap
        else new["runtime_roles"]["provider"]["role_name"]
    )
    role = f"arn:aws:iam::{account}:role/{role_name}"
    loop = asyncio.get_running_loop()

    def verify():
        future = asyncio.run_coroutine_threadsafe(
            current_operation(operation, context), loop
        )
        try:
            return future.result(timeout=20)
        except BaseException:
            future.cancel()
            raise

    return await asyncio.to_thread(
        assume_session, management, role_arn=role, region=request.region, verify=verify
    )


async def creation_preflight(
    operation, context, config, request, authorization, session
):
    # Prove the entire configured bootstrap recipe parses before creating a paid
    # child whose numeric identity does not exist yet. Only ACCOUNT_ID substitution
    # is allowed; all roles and policies remain explicit approved inputs.
    bootstrap_recipe(config, request, authorization, "000000000000", "r-preflight")
    organizations = session.client("organizations")
    organization = (
        await read_sdk(operation, context, organizations.describe_organization)
    )["Organization"]
    if (
        organization.get("Id") != request.organization_id
        or organization.get("ManagementAccountId", organization.get("MasterAccountId"))
        != request.management_account_id
    ):
        raise LifecycleRefused(
            "account creation provider is not the authorized AWS organization"
        )
    unit = (
        await read_sdk(
            operation,
            context,
            organizations.describe_organizational_unit,
            OrganizationalUnitId=request.organizational_unit_id,
        )
    )["OrganizationalUnit"]
    if (
        unit.get("Id") != request.organizational_unit_id
        or f"/{request.organization_id}/" not in unit.get("Arn", "")
    ):
        raise LifecycleRefused(
            "account destination is not the approved organization unit"
        )
    from account_provisioning.baseline import read_audit
    from account_factory.recovery import StepState

    trail_arn = config["new_account"]["audit_trail_arn"]
    if not isinstance(trail_arn, str) or not re.fullmatch(
        rf"arn:aws:cloudtrail:[a-z0-9-]+:{request.management_account_id}:trail/[A-Za-z0-9._-]+",
        trail_arn,
    ):
        raise LifecycleRefused(
            "new-account audit must name the approved management organization trail"
        )
    trail_client = session.client("cloudtrail", region_name=trail_arn.split(":")[3])
    trail = await read_sdk(
        operation,
        context,
        trail_client.describe_trails,
        trailNameList=[trail_arn],
        includeShadowTrails=True,
    )
    if (
        len(trail["trailList"]) != 1
        or trail["trailList"][0].get("IsOrganizationTrail") is not True
    ):
        raise LifecycleRefused("new-account audit must already cover the organization")
    audit = await asyncio.to_thread(
        read_audit,
        SimpleNamespace(cloudtrail=trail_client),
        request.management_account_id,
        trail_arn,
    )
    await current_operation(operation, context)
    if audit.state is not StepState.ESTABLISHED:
        raise LifecycleRefused(
            "approved organization audit trail is not healthy before account creation"
        )


def role_recipe(account, key, role_name, trust, arn, document):
    from account_provisioning.baseline import canonical as policy_canonical

    if isinstance(trust, str):
        trust = json.loads(trust)
    if (
        not isinstance(trust, dict)
        or trust.get("Version") != "2012-10-17"
        or not isinstance(trust.get("Statement"), list)
        or not trust["Statement"]
    ):
        raise LifecycleRefused("reviewed role trust policy is incomplete")
    if not isinstance(arn, str) or not re.fullmatch(
        rf"arn:aws:iam::{account}:policy/[A-Za-z0-9+=,.@_/-]+", arn
    ):
        raise LifecycleRefused(
            "reviewed account permission policy names another account"
        )
    policy = policy_canonical(document)
    if not json.loads(policy)["Statement"]:
        raise LifecycleRefused("reviewed role permission policy is empty")
    path_and_name = arn.split(":policy/", 1)[1]
    path, _, name = path_and_name.rpartition("/")
    return {
        name: {
            "service": "iam",
            "method": method,
            "account_id": account,
            "arguments": arguments,
        }
        for name, method, arguments in (
            (
                "policy-" + key,
                "create_policy",
                {
                    "PolicyName": name or path_and_name,
                    "Path": "/" + path + "/" if path else "/",
                    "PolicyDocument": policy,
                },
            ),
            (
                "role-" + key,
                "create_role",
                {
                    "RoleName": role_name,
                    "AssumeRolePolicyDocument": canonical(trust),
                    "Description": "Approved Superplane " + key,
                },
            ),
            (
                "attach-" + key,
                "attach_role_policy",
                {"RoleName": role_name, "PolicyArn": arn},
            ),
        )
    }


def bootstrap_recipe(config, request, authorization, account, source_parent):
    from account_factory.bootstrap import (
        RoleTier,
        bootstrap_plan,
        AUTOSCALING_SERVICE_PRINCIPAL,
    )
    from account_provisioning.bootstrap_runner import role_name_for
    from account_provisioning.baseline import PUBLIC_ACCESS_BLOCK

    plan = bootstrap_plan(request, authorization)
    new = materialize(config["new_account"], account)
    roles = [step for step in plan.steps if isinstance(step.tier, RoleTier)]
    expected = {step.name for step in roles}
    if any(
        set(new[key]) != expected
        for key in (
            "trust_policies",
            "permission_policy_arns",
            "permission_policy_documents",
        )
    ):
        raise LifecycleRefused(
            "new-account policies must exactly cover the maintained three-role plan"
        )
    recipe = {
        "place-account": {
            "service": "organizations",
            "method": "move_account",
            "account_id": request.management_account_id,
            "arguments": {
                "AccountId": account,
                "SourceParentId": source_parent,
                "DestinationParentId": request.organizational_unit_id,
            },
        }
    }
    names = [role_name_for(step) for step in roles]
    names += [value["role_name"] for value in new["runtime_roles"].values()]
    names += [new["child_access_role_name"]]
    if len(set(names)) != len(names):
        raise LifecycleRefused("new-account role tiers cannot share identities")
    for step in roles:
        recipe.update(
            role_recipe(
                account,
                step.name,
                role_name_for(step),
                new["trust_policies"][step.name],
                new["permission_policy_arns"][step.name],
                new["permission_policy_documents"][step.name],
            )
        )
    for actor in ("provider", "registrar", "installer", "supervisor"):
        definition = new["runtime_roles"][actor]
        recipe.update(
            role_recipe(
                account,
                "runtime-" + actor,
                definition["role_name"],
                definition["trust_policy"],
                definition["policy_arn"],
                definition["policy_document"],
            )
        )
    policy_arns = [
        value["arguments"]["PolicyArn"]
        for value in recipe.values()
        if value["method"] == "attach_role_policy"
    ]
    if len(set(policy_arns)) != len(policy_arns):
        raise LifecycleRefused("new-account role policies must remain separate")
    recipe["autoscaling-service-linked-role"] = {
        "service": "iam",
        "method": "create_service_linked_role",
        "account_id": account,
        "arguments": {"AWSServiceName": AUTOSCALING_SERVICE_PRINCIPAL},
    }
    recipe["public-access-block"] = {
        "service": "s3control",
        "method": "put_public_access_block",
        "account_id": account,
        "arguments": {
            "AccountId": account,
            "PublicAccessBlockConfiguration": dict(PUBLIC_ACCESS_BLOCK),
        },
    }
    return plan, new, recipe


async def bootstrap_account_phase(
    operation, context, config, request, authorization, row, management
):
    from account_provisioning.bootstrap_runner import _trust_fingerprint
    from account_provisioning.baseline import canonical as policy_canonical

    source = await creation_proof(operation, context, row, management, request)
    account = row["account_id"]
    source_parent = json.loads(source["artifact_metadata_json"])[
        "creation_source_parent_id"
    ]
    plan, new, recipe = bootstrap_recipe(
        config, request, authorization, account, source_parent
    )
    session = await child_session(operation, context, config, request, row, management)
    organizations = management.client("organizations")
    iam, s3 = session.client("iam"), session.client("s3control")
    journal = LifecycleEffects(
        operation, context, phase="bootstrap-account", recipe=recipe
    )

    async def call(method, **arguments):
        return await read_sdk(operation, context, method, **arguments)

    async def observe(descriptor):
        arguments, method = descriptor["arguments"], descriptor["method"]
        try:
            if method == "move_account":
                response = await call(organizations.list_parents, ChildId=account)
                parents = response["Parents"]
                if response.get("NextToken") or len(parents) != 1:
                    raise LifecycleRefused("child account placement is ambiguous")
                parent = parents[0]["Id"]
                if (
                    parent == arguments["DestinationParentId"]
                    and parents[0].get("Type") == "ORGANIZATIONAL_UNIT"
                ):
                    return {"parent_id": parent}
                if parent != arguments["SourceParentId"]:
                    raise LifecycleRefused(
                        "child account was moved outside its approved source placement"
                    )
                return None
            if method == "create_policy":
                arn = (
                    f"arn:aws:iam::{account}:policy"
                    + arguments["Path"]
                    + arguments["PolicyName"]
                )
                policy = (await call(iam.get_policy, PolicyArn=arn))["Policy"]
                version = (
                    await call(
                        iam.get_policy_version,
                        PolicyArn=arn,
                        VersionId=policy["DefaultVersionId"],
                    )
                )["PolicyVersion"]
                if policy.get("Arn") != arn or policy_canonical(
                    version["Document"]
                ) != policy_canonical(json.loads(arguments["PolicyDocument"])):
                    raise LifecycleRefused(
                        "existing managed policy differs from the approved document"
                    )
                return {"policy_arn": arn, "policy_id": policy["PolicyId"]}
            if method in {"create_role", "create_service_linked_role"}:
                name = arguments.get("RoleName", "AWSServiceRoleForAutoScaling")
                role = (await call(iam.get_role, RoleName=name))["Role"]
                if (
                    not role.get("Arn", "").startswith(f"arn:aws:iam::{account}:role/")
                    or role.get("RoleName") != name
                    or not role.get("RoleId")
                ):
                    raise LifecycleRefused("role identity names another account")
                if method == "create_role" and _trust_fingerprint(
                    role["AssumeRolePolicyDocument"]
                ) != _trust_fingerprint(arguments["AssumeRolePolicyDocument"]):
                    raise LifecycleRefused(
                        "existing role trust differs from the approved document"
                    )
                if method == "create_role" and (
                    role["Arn"] != f"arn:aws:iam::{account}:role/{name}"
                    or role.get("PermissionsBoundary")
                ):
                    raise LifecycleRefused(
                        "role path or permission boundary differs from the reviewed identity"
                    )
                if method == "create_service_linked_role" and not role["Arn"].endswith(
                    "/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
                ):
                    raise LifecycleRefused(
                        "autoscaling service-linked role identity differs"
                    )
                return {"role_arn": role["Arn"], "role_id": role["RoleId"]}
            if method == "attach_role_policy":
                attached = await call(
                    iam.list_attached_role_policies, RoleName=arguments["RoleName"]
                )
                if attached.get("IsTruncated"):
                    raise LifecycleRefused(
                        "role policy attachment inventory is incomplete"
                    )
                inline = await call(
                    iam.list_role_policies, RoleName=arguments["RoleName"]
                )
                actual_arns = {
                    item["PolicyArn"] for item in attached["AttachedPolicies"]
                }
                if (
                    inline.get("IsTruncated")
                    or inline.get("PolicyNames")
                    or actual_arns - {arguments["PolicyArn"]}
                ):
                    raise LifecycleRefused(
                        "role has additional or incompletely inventoried permissions"
                    )
                if arguments["PolicyArn"] not in actual_arns:
                    return None
                return {
                    "role_name": arguments["RoleName"],
                    "policy_arn": arguments["PolicyArn"],
                }
            if method == "put_public_access_block":
                block = (await call(s3.get_public_access_block, AccountId=account))[
                    "PublicAccessBlockConfiguration"
                ]
                return (
                    {"account_id": account, "enabled": True}
                    if all(
                        block.get(key) is True
                        for key in arguments["PublicAccessBlockConfiguration"]
                    )
                    else None
                )
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if (
                method in {"create_role", "create_policy", "create_service_linked_role"}
                and code == "NoSuchEntity"
            ) or (
                method == "put_public_access_block"
                and code == "NoSuchPublicAccessBlockConfiguration"
            ):
                return None
            raise
        raise LifecycleRefused("account observation is outside the finite recipe")

    for key, descriptor in recipe.items():
        recorded = await journal.intend(key, descriptor)
        actual = await observe(descriptor)
        if recorded is not None:
            if actual != recorded:
                raise LifecycleRefused("confirmed account effect identity changed")
            continue
        if actual is None:
            method, arguments = descriptor["method"], descriptor["arguments"]
            if method == "move_account":
                await call(organizations.move_account, **arguments)
            elif method == "create_policy":
                await call(iam.create_policy, **arguments)
            elif method == "create_role":
                await call(iam.create_role, **arguments)
            elif method == "attach_role_policy":
                await call(iam.attach_role_policy, **arguments)
            elif method == "create_service_linked_role":
                await call(iam.create_service_linked_role, **arguments)
            elif method == "put_public_access_block":
                await call(s3.put_public_access_block, **arguments)
            else:
                raise LifecycleRefused("account mutation is outside the finite recipe")
            for _ in range(10):
                actual = await observe(descriptor)
                if actual is not None:
                    break
                await asyncio.sleep(1)
            if actual is None:
                raise LifecycleRefused(
                    "account effect has not been positively observed"
                )
        await journal.confirm(key, descriptor, actual)
    await journal.complete()
    # The maintained account runner performs full live policy/trust/audit checks.
    # Its mutation surface is refused here: every allowed effect already has its
    # separate durable recipe entry and a second dispatch is never a fallback.
    loop = asyncio.get_running_loop()

    def verify():
        future = asyncio.run_coroutine_threadsafe(
            current_operation(operation, context), loop
        )
        try:
            return future.result(timeout=20)
        except BaseException:
            future.cancel()
            raise

    def inspect():
        from account_provisioning.bootstrap_runner import bootstrap_account
        from account_provisioning.placement import read_placement

        class ReadOnlyClient:
            def __init__(self, client):
                self.client = client

            def __getattr__(self, name):
                if not name.startswith(("get_", "list_", "describe_")):
                    raise LifecycleRefused(
                        "account verification cannot dispatch a mutation"
                    )
                method = getattr(self.client, name)

                def read(**kwargs):
                    verify()
                    result = method(**kwargs)
                    verify()
                    return result

                return read

        class ReadOnlyExecutor:
            operation_id = operation.grant.lease.operation_id
            org_id = operation.grant.lease.org_id
            workspace_id = operation.grant.lease.workspace_id

            async def provider_calls(self, **kwargs):
                verify()
                return ()

            async def execute_provider(self, **kwargs):
                raise LifecycleRefused("account readback cannot add provider effects")

            async def observe_success(self, **kwargs):
                raise LifecycleRefused("account readback cannot invent effect evidence")

        class Credentials:
            async def child_account(self, *, operation_id, account_id):
                verify()
                if (operation_id, account_id) != (
                    operation.grant.lease.operation_id,
                    account,
                ):
                    raise LifecycleRefused("account readback credential target differs")
                return SimpleNamespace(
                    iam=ReadOnlyClient(iam),
                    s3control=ReadOnlyClient(s3),
                    cloudtrail=ReadOnlyClient(
                        session.client(
                            "cloudtrail",
                            region_name=new["audit_trail_arn"].split(":")[3],
                        )
                    ),
                )

        placement = read_placement(
            ReadOnlyClient(organizations),
            account_id=account,
            organizational_unit_id=request.organizational_unit_id,
        )
        verify()
        return asyncio.run(
            bootstrap_account(
                ReadOnlyExecutor(),
                Credentials(),
                plan,
                account_id=account,
                placement=placement,
                trust_policies={
                    key: value if isinstance(value, str) else canonical(value)
                    for key, value in new["trust_policies"].items()
                },
                permission_policy_arns=new["permission_policy_arns"],
                permission_policy_documents=new["permission_policy_documents"],
                audit_trail_arn=new["audit_trail_arn"],
            )
        )

    report = await asyncio.to_thread(inspect)
    await current_operation(operation, context)
    if not report.complete:
        raise LifecycleRefused(
            "maintained account bootstrap verification is incomplete"
        )
    from .account_registration import created_account_registration

    registration = await created_account_registration(
        operation, context, request, authorization, row, management
    )
    return (
        account,
        {"account_id": account, "aws_region": request.region},
        {
            "next_phase": "prepare-infrastructure",
            "creation_artifact_id": source["artifact_id"],
            "created_account_registration": asdict(registration),
        },
    )


async def account_phase(
    operation, context, config, request, authorization, phase, row, session
):
    if request.mode.value != "new-account-managed" or "new_account" not in config:
        raise LifecycleRefused(
            "account effects require the explicit approved new-account recipe"
        )
    if phase == "bootstrap-account" and row is not None:
        return await bootstrap_account_phase(
            operation, context, config, request, authorization, row, session
        )
    raise LifecycleRefused("account phase is outside the finite approved recipe")


async def run_account_bootstrap(operation, context):
    """Private separately admitted account bootstrap; public mode remains closed."""
    from harness_jobs.execution import CallOutcome, OperationExecutor
    from harness_jobs.execution_rpc import ExecutionRPCServer
    from .artifacts import record_artifact, proposal
    from .runtime import delivery_session, validate_phase

    config, request, authorization, row, step = await validate_phase(operation, context)
    if (
        request.mode.value != "new-account-managed"
        or row is None
        or step.step_id != "bootstrap-account"
    ):
        raise LifecycleRefused(
            "account bootstrap requires its separately reviewed creation handoff"
        )
    result = {}

    async def hook(call):
        current = await current_operation(operation, context)
        lease = current.grant.lease
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
            current.job_id,
            lease.attempt_id,
            lease.fence_token,
            step.provider,
            step.operation_kind,
            step.target,
        ):
            raise LifecycleRefused(
                "account bootstrap call differs from its admitted phase"
            )
        management = await delivery_session(
            current, context, request.management_account_id, request.region
        )
        account, target, metadata = await bootstrap_account_phase(
            current, context, config, request, authorization, row, management
        )
        result.update(
            await record_artifact(
                current, context, account_id=account, target=target, metadata=metadata
            )
        )
        return (
            CallOutcome.SUCCEEDED,
            "account bootstrap verified and registered",
            result["artifact_id"],
        )

    async def authenticate(_token):
        return (await current_operation(operation, context)).grant

    runtime = OperationExecutor(
        operation.grant.lease, connect=context.connect, provider_call=hook
    )
    server = ExecutionRPCServer(
        connect=context.connect, provider_call=hook, authenticate=authenticate
    )

    async def heartbeat():
        active = runtime
        while True:
            await asyncio.sleep(10)
            await current_operation(operation, context)
            active = await active.renew(duration=timedelta(seconds=45))

    async with asyncio.TaskGroup() as tasks:
        renewal = tasks.create_task(heartbeat())
        try:
            call, _ = await server.execute_step(operation.grant, runtime, step.step_id)
        finally:
            renewal.cancel()
    if not result:
        if call.outcome is not CallOutcome.SUCCEEDED or not call.provider_ref:
            raise LifecycleRefused(
                "account bootstrap remains unresolved without a verified handoff"
            )
        completed = await read_artifact(
            context.domain_connect,
            artifact_id=call.provider_ref,
            org_id=operation.grant.lease.org_id,
            workspace_id=operation.grant.lease.workspace_id,
            require_fresh=False,
        )
        metadata = json.loads(completed["artifact_metadata_json"])
        if (
            completed["source_operation_id"] != operation.grant.lease.operation_id
            or completed["source_request_payload"] != operation.request_payload
            or metadata.get("creation_artifact_id") != row["artifact_id"]
            or metadata.get("next_phase") != "prepare-infrastructure"
        ):
            raise LifecycleRefused(
                "account bootstrap handoff differs from its successful phase"
            )
        result.update(proposal(completed))
    return result


async def run_account_infrastructure(operation, context):
    """Private new-account infrastructure join; public activation remains separate."""
    from .runtime import validate_phase, _run_validated_lifecycle

    phase_state = await validate_phase(operation, context)
    _, request, _, row, step = phase_state
    if (
        request.mode.value != "new-account-managed"
        or row is None
        or step.step_id
        not in {"prepare-infrastructure", "apply-infrastructure", "bootstrap-workspace"}
    ):
        raise LifecycleRefused(
            "new-account infrastructure requires its separately approved next phase"
        )
    return await _run_validated_lifecycle(operation, context, phase_state)
