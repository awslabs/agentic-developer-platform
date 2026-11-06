"""DevOps credentials are confined to one verified native space and deadline."""

import json
import re
from types import SimpleNamespace

from fastapi import HTTPException

READ_ACTIONS = (
    "GetAgentSpace",
    "GetBacklogTask",
    "ListBacklogTasks",
    "ListExecutions",
    "ListJournalRecords",
    "ListRecommendations",
    "GetRecommendation",
    "GetAssociation",
)


def space_arn(binding):
    identity = binding.provider_identity or {}
    reference = binding.native_resource_ref or {}
    connection = binding.connection_ref or {}
    account, region, role = (identity.get(field) for field in ("account_id", "region", "role_arn"))
    space = reference.get("record_id")
    if (
        binding.service_id != "devops"
        or reference.get("record_type") != "agent_space"
        or (reference.get("tenant_id"), reference.get("project_id"), reference.get("source_connection_id"))
        != (binding.tenant_id, binding.project_id, connection.get("source_connection_id"))
        or not isinstance(account, str)
        or not re.fullmatch(r"[0-9]{12}", account)
        or not isinstance(region, str)
        or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region)
        or not isinstance(role, str)
        or not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", role)
        or not isinstance(space, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", space)
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    return f"arn:aws:aidevops:{region}:{account}:agentspace/{space}"


def run_policy(binding, run, expires_at, *, work_kind=None):
    if (
        run.service_id != "devops"
        or run.input.get("capability") not in {"investigation", "release_readiness", "release_verification"}
        or run.admission.get("native_space_ref") != binding.native_resource_ref
        or run.admission.get("provider_identity") != binding.provider_identity
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    mutations = ("CreateBacklogTask", "UpdateBacklogTask")
    if getattr(run, "native_job_ref", None):
        # A known native job can be observed/collected without authorizing a
        # replacement investigation, including after setup proof expires.
        mutations = ("UpdateBacklogTask",) if work_kind == "cancel" else ()
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["aidevops:" + action for action in (*READ_ACTIONS, *mutations)],
                    "Resource": space_arn(binding),
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
                },
                {
                    "Effect": "Allow",
                    "Action": "aidevops:GetAssociation",
                    "Resource": space_arn(binding) + "/association/*",
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
                },
            ],
        },
        separators=(",", ":"),
    )


def setup_policy(intent, verified, expires_at, binding=None):
    from l5_services.devops.setup import setup_selection

    denied = HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    action = intent.request.get("action")
    reference = binding.native_resource_ref if binding is not None else intent.request.get("native_resource_ref")
    identity = {field: verified.get(field) for field in ("account_id", "region", "role_arn")}
    if (
        intent.service_id != "devops"
        or action not in {"create", "link", "check"}
        or (
            binding is not None
            and (
                (binding.tenant_id, binding.product_id, binding.project_id, binding.service_id)
                != (intent.tenant_id, intent.product_id, intent.project_id, "devops")
                or any((binding.provider_identity or {}).get(field) != value for field, value in identity.items())
                or binding.scope != intent.scope
                or binding.state in {"cleaned", "cleanup_pending", "unlinked"}
            )
        )
    ):
        raise denied
    target = SimpleNamespace(
        tenant_id=intent.tenant_id,
        project_id=intent.project_id,
        service_id="devops",
        provider_identity=identity,
        connection_ref=verified["connection_ref"],
        native_resource_ref=reference or {**verified["connection_ref"], "record_type": "agent_space", "record_id": "new-space"},
    )
    resource = space_arn(target)
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}, "StringEquals": {"aws:RequestedRegion": verified["region"]}}
    if action == "create":
        fingerprint = setup_selection(intent, verified).fingerprint()
        resource = resource.rsplit("/", 1)[0] + "/*"
        statements = [
            {
                "Effect": "Allow",
                # AWS separately authorizes tags supplied during space creation.
                "Action": ["aidevops:CreateAgentSpace", "aidevops:TagResource"],
                "Resource": resource,
                "Condition": {
                    **condition,
                    "StringEquals": {**condition["StringEquals"], "aws:RequestTag/adp:scope": fingerprint},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["adp:scope"]},
                },
            },
            {
                "Effect": "Allow",
                "Action": ["aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:UpdateGoal"],
                "Resource": resource,
                "Condition": {**condition, "StringEquals": {**condition["StringEquals"], "aws:ResourceTag/adp:scope": fingerprint}},
            },
            {"Effect": "Allow", "Action": ["aidevops:ListAgentSpaces"], "Resource": "*", "Condition": condition},
        ]
    else:
        if reference is None:
            raise denied
        statements = [{"Effect": "Allow", "Action": ["aidevops:GetAgentSpace", "aidevops:ListGoals"], "Resource": resource, "Condition": condition}]
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


def resource_policy(intent, verified, expires_at, binding):
    """One association and read-only verification; resource roles use inline policies."""
    from l5_services.devops.resource_scope import ResourceSelection

    selection = ResourceSelection(**verified["resource_selection"])
    selection.fingerprint()
    if (
        binding is None
        or intent.request.get("action") != "check"
        or intent.authorization.get("purpose") != "devops_resource_verification"
        or intent.request.get("resource_selection") != verified["resource_selection"]
        or selection.workload_kind != "lambda"
        or selection.connection_ref != {**verified["connection_ref"], "record_type": "credential"}
        or (selection.account_id, selection.region, selection.setup_role_arn) != (verified["account_id"], verified["region"], verified["role_arn"])
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    # Reuse the established native identity validation and exact space ARN.
    policy = json.loads(setup_policy(intent, verified, expires_at, binding))
    # These reads share the exact resource and conditions. Keeping them together
    # avoids exceeding STS's 2,048-character inline session-policy limit.
    policy["Statement"][0]["Action"].extend(["aidevops:ListAssociations", "aidevops:GetAssociation"])
    # GetAssociation authorizes BOTH the space and association resource types.
    # The other reads in this statement support only the parent space ARN.
    policy["Statement"][0]["Resource"] = [space_arn(binding), space_arn(binding) + "/association/*"]
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}
    policy["Statement"].extend(
        [
            {
                "Effect": "Allow",
                "Action": "aidevops:AssociateService",
                "Resource": space_arn(binding),
                "Condition": {**condition, "StringEquals": {"aws:RequestedRegion": selection.region, "aidevops:ServiceType": "aws"}},
            },
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": selection.role_arn,
                "Condition": {**condition, "StringEquals": {"iam:PassedToService": "aidevops.amazonaws.com"}},
            },
            {
                "Effect": "Allow",
                "Action": ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"],
                "Resource": selection.role_arn,
                "Condition": condition,
            },
            {
                "Effect": "Allow",
                # These actions each support only their own service's resource
                # ARN. Shared conditions keep this policy within STS's limit.
                "Action": ["lambda:GetFunction", "logs:FilterLogEvents"],
                "Resource": [selection.resource_arn, selection.telemetry_arn],
                "Condition": {**condition, "StringEquals": {"aws:RequestedRegion": selection.region}},
            },
        ]
    )
    return json.dumps(policy, separators=(",", ":"))
