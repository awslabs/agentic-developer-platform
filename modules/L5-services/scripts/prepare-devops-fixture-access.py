"""Prepare, never apply, a replacement policy for a dedicated fixture connection.

The IAM snapshot helper is read-only and requires an injected operator client.
The CLI is offline: supplied retained records are preparation inputs, not proof
that consent, role state or a native space is still current at apply time.
"""

import argparse
import json
import os
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import rfc8785

INITIAL_POLICY = {"Version": "2012-10-17", "Statement": [{
    "Sid": "NoNativeActionsUntilPinnedSetup", "Effect": "Deny",
    "Action": ["securityagent:*", "aidevops:*"], "Resource": "*"}]}


def digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def snapshot_role(iam, *, role_arn, policy_name):
    """Read all role authority. Never mutate, simulate, or assume this role."""
    name = role_arn.rsplit("/", 1)[1]
    role = iam.get_role(RoleName=name)["Role"]
    attached = iam.list_attached_role_policies(RoleName=name)
    inline = iam.list_role_policies(RoleName=name)
    if (role.get("Arn") != role_arn or role.get("PermissionsBoundary")
            or attached.get("AttachedPolicies") != [] or attached.get("IsTruncated")
            or inline.get("PolicyNames") != [policy_name] or inline.get("IsTruncated")):
        raise ValueError("Only the selected dedicated role with one inline policy is supported")
    return {"role_arn": role_arn, "role_id": role["RoleId"], "policy_name": policy_name,
            "trust_policy": role["AssumeRolePolicyDocument"], "managed_policies": [], "permissions_boundary": None,
            "inline_policies": {policy_name: iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]}}


def selection(config):
    """Derive the same binding and scope fingerprint as retained DevOps setup."""
    intent = config["setup_intent"]
    if (intent.get("service_id") != "devops" or intent.get("binding_generation") != 1
            or intent.get("owned_resource_receipt") is not None
            or set(intent.get("request", {})) != {"action", "configuration_revision"}
            or intent["request"]["action"] != "create"
            or intent.get("authorization", {}).get("purpose") != "devops_setup"):
        raise ValueError("A retained purpose-specific create intent is required")
    UUID(intent["operation_id"])
    payload = {key: intent[key] for key in (
        "operation_id", "scope", "configuration_id", "request", "authorization", "binding_generation")}
    if digest(payload) != intent.get("intent_hash"):
        raise ValueError("Retained setup intent hash changed")
    if any(not isinstance(intent.get(key), str) or not intent[key] for key in ("tenant_id", "product_id", "project_id")):
        raise ValueError("Retained setup identity is incomplete")
    if any(intent.get("scope", {}).get(key) != intent[key] for key in ("tenant_id", "product_id", "project_id")):
        raise ValueError("Retained setup scope does not match its identity")
    binding = str(uuid5(NAMESPACE_URL, f"l5:devops:{intent['tenant_id']}:{intent['operation_id']}"))
    values = [intent["tenant_id"], intent["product_id"], intent["project_id"], binding, config["account_id"], config["region"]]
    return binding, sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def prepare(config, snapshot, *, previous_plan=None, now=None):
    required = {"schema_version", "stage", "account_id", "region", "connection_role_arn", "expires_at", "setup_intent"}
    stage = config.get("stage")
    if (config.get("schema_version") != 1 or stage not in {"setup", "resources"}
            or set(config) != required | ({"fixture", "native_readback"} if stage == "resources" else set())):
        raise ValueError("Use the exact version-1 setup or resources preparation input")
    for key, pattern in {"account_id": r"[0-9]{12}", "region": r"[a-z]{2}-[a-z]+-[1-9]",
                         "connection_role_arn": rf"arn:aws:iam::{config['account_id']}:role/[A-Za-z0-9+=,.@_-]{{1,64}}"}.items():
        if not isinstance(config[key], str) or not re.fullmatch(pattern, config[key]):
            raise ValueError("Select one exact account, region and dedicated connection role")
    now = now or datetime.now(UTC)
    expiry = datetime.fromisoformat(config["expires_at"])
    if expiry.tzinfo is None or not 0 < (expiry - now).total_seconds() <= 3600:
        raise ValueError("Review a future permission window of at most one hour")
    binding, fingerprint = selection(config)
    policy_name = snapshot.get("policy_name")
    if (snapshot.get("role_arn") != config["connection_role_arn"] or not snapshot.get("role_id")
            or not snapshot.get("trust_policy") or snapshot.get("managed_policies") != []
            or snapshot.get("permissions_boundary") is not None
            or not isinstance(policy_name, str) or not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,128}", policy_name)
            or set(snapshot.get("inline_policies", {})) != {policy_name}):
        raise ValueError("Read back the exact dedicated role, trust and sole inline policy")
    before = snapshot["inline_policies"][policy_name]
    if stage == "setup" and previous_plan is None:
        if before != INITIAL_POLICY:
            raise ValueError("Initial setup may only replace the reviewed native-deny policy")
    elif (not isinstance(previous_plan, dict) or previous_plan.get("stage") not in ({"setup"} if stage == "setup" else {"setup", "resources"})
          or previous_plan.get("role_arn") != snapshot["role_arn"] or previous_plan.get("role_id") != snapshot["role_id"]
          or previous_plan.get("trust_sha256") != digest(snapshot["trust_policy"])
          or previous_plan.get("binding_id") != binding or previous_plan.get("scope_fingerprint") != fingerprint
          or previous_plan.get("policy_name") != policy_name or previous_plan.get("after_policy") != before):
        raise ValueError("Continuation requires the exact applied prior plan and unchanged role")

    region = {"aws:RequestedRegion": config["region"]}
    deadline = {"DateLessThan": {"aws:CurrentTime": expiry.isoformat()}}
    regional = {**deadline, "StringEquals": region}
    prefix = f"arn:aws:aidevops:{config['region']}:{config['account_id']}:agentspace/"
    statements = []
    if stage == "setup":
        statements = [
            {"Effect": "Allow", "Action": ["aidevops:CreateAgentSpace", "aidevops:TagResource"], "Resource": prefix + "*", "Condition": {
                **deadline, "StringEquals": {**region, "aws:RequestTag/adp:scope": fingerprint},
                "ForAllValues:StringEquals": {"aws:TagKeys": ["adp:scope"]}}},
            {"Effect": "Allow", "Action": ["aidevops:GetAgentSpace", "aidevops:ListGoals"], "Resource": prefix + "*", "Condition": {
                **deadline, "StringEquals": {**region, "aws:ResourceTag/adp:scope": fingerprint}}},
            {"Effect": "Allow", "Action": ["aidevops:ListAgentSpaces"], "Resource": "*", "Condition": regional}]
    else:
        fixture, readback = config["fixture"], config["native_readback"]
        space_id = fixture.get("native_space_id")
        fixture_id = fixture.get("fixture_id")
        if (fixture.get("purpose") != "synthetic-investigation" or fixture.get("synthetic") is not True
                or fixture.get("account_id") != config["account_id"] or fixture.get("region") != config["region"]
                or not isinstance(space_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", space_id)
                or not isinstance(fixture_id, str) or not re.fullmatch(r"[a-f0-9]{16}", fixture_id)):
            raise ValueError("Select the prepared isolated fixture in the same account and region")
        name = "l5-investigation-" + fixture_id
        expected = {"function_arn": f"arn:aws:lambda:{config['region']}:{config['account_id']}:function:{name}",
                    "telemetry_arn": f"arn:aws:logs:{config['region']}:{config['account_id']}:log-group:/aws/lambda/{name}:*",
                    "resource_role_arn": f"arn:aws:iam::{config['account_id']}:role/{name}-reader",
                    "execution_role_arn": f"arn:aws:iam::{config['account_id']}:role/{name}"}
        if (any(fixture.get(key) != value for key, value in expected.items())
                or config["connection_role_arn"] in {expected["resource_role_arn"], expected["execution_role_arn"]}
                or readback.get("agentSpace", {}).get("agentSpaceId") != space_id
                or readback["agentSpace"].get("name") != "adp-devops-" + fingerprint[:32]
                or readback.get("tags", {}).get("adp:scope") != fingerprint):
            raise ValueError("Fixture role boundaries or actual native space readback do not match setup")
        space = prefix + space_id
        statements = [
            {"Effect": "Allow", "Action": ["aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:ListAssociations", "aidevops:GetAssociation"],
             "Resource": space, "Condition": regional},
            {"Effect": "Allow", "Action": ["aidevops:AssociateService"], "Resource": space,
             "Condition": {**deadline, "StringEquals": {**region, "aidevops:ServiceType": "aws"}}},
            {"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": fixture["resource_role_arn"],
             "Condition": {**deadline, "StringEquals": {"iam:PassedToService": "aidevops.amazonaws.com"}}},
            {"Effect": "Allow", "Action": ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"],
             "Resource": fixture["resource_role_arn"], "Condition": deadline},
            {"Effect": "Allow", "Action": ["lambda:GetFunction"], "Resource": fixture["function_arn"], "Condition": regional},
            {"Effect": "Allow", "Action": ["logs:FilterLogEvents"], "Resource": fixture["telemetry_arn"], "Condition": regional},
            {"Effect": "Allow", "Action": ["aidevops:GetAssociation"], "Resource": space + "/association/*", "Condition": regional}]
    allowed = sorted({action for statement in statements for action in statement["Action"]})
    # Replacement, not an ineffective Allow beneath the original Deny. This
    # dedicated role has no other policies; unrelated services remain denied.
    statements.append({"Sid": "NoOtherFixtureActions", "Effect": "Deny", "NotAction": allowed, "Resource": "*"})
    after = {"Version": "2012-10-17", "Statement": statements}
    return {"schema_version": 1, "stage": stage, "prepared": True, "applied": False, "aws_mutations": 0,
            "role_arn": snapshot["role_arn"], "role_id": snapshot["role_id"], "policy_name": policy_name,
            "binding_id": binding, "scope_fingerprint": fingerprint, "expires_at": expiry.isoformat(),
            "snapshot_sha256": digest(snapshot), "trust_sha256": digest(snapshot["trust_policy"]),
            "inputs_sha256": digest(config), "before_policy": before, "after_policy": after,
            "after_policy_sha256": digest(after),
            "pre_apply": ["Re-read this complete role snapshot and require its digest to match.",
                          "Verify current Product owner, disabled configuration, consent and retained setup work.",
                          "Review replacement of this single policy; never change role trust or attach another policy.",
                          "An enabled native goal remains blocked: this plan grants no UpdateGoal permission."],
            "remaining": ["Read back the exact replacement and simulate the selected operations before consuming setup work.",
                          "Remove setup authority after space readback; review the separate resource stage.",
                          "Resource verification and a bounded native run need separate current consent.",
                          "No UI session, native task, Lambda invocation, deletion or IAM write is authorized by this policy."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    plan = prepare(json.loads(args.config.read_text()), json.loads(args.snapshot.read_text()),
                   previous_plan=json.loads(args.previous_plan.read_text()) if args.previous_plan else None)
    os.umask(0o077)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(plan, indent=2) + "\n")
    args.output.chmod(0o600)
    print(json.dumps({"prepared": True, "applied": False, "aws_mutations": 0, "stage": plan["stage"]}))


if __name__ == "__main__":
    main()
