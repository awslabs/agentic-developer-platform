"""Conservative, read-only supplied-key preflight; no key or grant is created."""

from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import subprocess

from workspace_ownership import WorkspaceOwnershipError


def _aws(service, operation, *flags, region):
    result = subprocess.run(
        [
            "aws",
            service,
            operation,
            *flags,
            "--region",
            region,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise WorkspaceOwnershipError(
            f"Workspace preflight could not verify {service}:{operation}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise WorkspaceOwnershipError("Invalid AWS preflight response") from exc


def _items(value):
    return value if isinstance(value, list) else [value]


def verify_account_prerequisites(target):
    """Read shared account prerequisites without adopting their lifecycle."""
    account, region = target["account_id"], target["aws_region"]
    if _aws("sts", "get-caller-identity", region=region).get("Account") != account:
        raise WorkspaceOwnershipError(
            "Account prerequisite check has the wrong caller account"
        )
    expected = (
        f"arn:aws:iam::{account}:role/aws-service-role/"
        "autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
    )
    try:
        role = _aws(
            "iam",
            "get-role",
            "--role-name",
            "AWSServiceRoleForAutoScaling",
            region=region,
        )["Role"]
    except (WorkspaceOwnershipError, KeyError) as exc:
        raise WorkspaceOwnershipError(
            "Auto Scaling service-linked role prerequisite is unavailable. "
            "Have account bootstrap establish AWSServiceRoleForAutoScaling, then prepare again; "
            "workspace provisioning never creates or adopts this account-wide role."
        ) from exc
    if role.get("Arn") != expected:
        raise WorkspaceOwnershipError(
            "Auto Scaling service-linked role has the wrong identity"
        )
    return {"account_id": account, "autoscaling_role_arn": expected}


def _allows(statement, required, action, key_arn, account_root):
    if statement.get("Effect") != "Allow":
        return False
    principal = statement.get("Principal")
    wanted = required["Principal"]
    if not isinstance(principal, dict):
        return False
    matches = all(
        value in _items(principal.get(kind)) for kind, value in wanted.items()
    )
    # The account-root statement delegates key access to IAM. It can satisfy the
    # provisioning caller only after the independent identity checks below; never a service.
    if not matches and "AWS" in wanted:
        matches = account_root in _items(
            principal.get("AWS")
        ) and "ProvisioningCaller" in required.get("Sid", "")
    if not matches:
        return False
    actions = _items(statement.get("Action"))
    if not any(
        isinstance(pattern, str)
        and fnmatch.fnmatchcase(action.lower(), pattern.lower())
        for pattern in actions
    ):
        return False
    if not any(
        resource in ("*", key_arn) for resource in _items(statement.get("Resource"))
    ):
        return False
    conditions = statement.get("Condition") or {}
    # An absent condition is less restrictive. Unknown/extra conditional semantics
    # are deliberately refused, rather than implementing an incomplete IAM evaluator.
    return not conditions or conditions == required.get("Condition", {})


def verify_policy(policy, requirements, key_arn, account_root):
    statements = policy.get("Statement") if isinstance(policy, dict) else None
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or not statements:
        raise WorkspaceOwnershipError("Supplied key has no verifiable policy")
    if any(
        not isinstance(s, dict)
        or s.get("Effect") == "Deny"
        or "NotPrincipal" in s
        or "NotAction" in s
        or "NotResource" in s
        for s in statements
    ):
        raise WorkspaceOwnershipError(
            "Supplied key uses policy semantics this preflight cannot prove; no apply is allowed"
        )
    required_statements = requirements.get("required_statements")
    if (
        requirements.get("key_arn") != key_arn
        or not isinstance(required_statements, list)
        or not required_statements
    ):
        raise WorkspaceOwnershipError(
            "Supplied key plan lacks its rendered required-statements contract"
        )
    for required in required_statements:
        for action in _items(required["Action"]):
            if not any(
                _allows(s, required, action, key_arn, account_root) for s in statements
            ):
                raise WorkspaceOwnershipError(
                    f"Supplied key does not establish {required['Sid']} / {action}"
                )


def verify_provisioning_principal(plan, *, approved=None):
    try:
        caller = plan["planned_values"]["outputs"]["provisioning_principal_arn"][
            "value"
        ]
        account = plan["variables"]["account_id"]["value"]
        region = plan["variables"]["aws_region"]["value"]
    except (KeyError, TypeError) as exc:
        raise WorkspaceOwnershipError(
            "Plan lacks canonical provisioning principal evidence"
        ) from exc
    observed = _aws("sts", "get-caller-identity", region=region)
    actual = observed["Arn"]
    if ":assumed-role/" in actual:
        role_name = actual.rsplit("/", 2)[-2]
        actual = _aws("iam", "get-role", "--role-name", role_name, region=region)[
            "Role"
        ]["Arn"]
    if observed.get("Account") != account or actual != caller:
        raise WorkspaceOwnershipError(
            "Live provisioning principal differs from the reviewed caller"
        )
    receipt = {"account_id": account, "principal_arn": actual}
    if approved is not None and approved != receipt:
        raise WorkspaceOwnershipError(
            "Authorization provisioning principal differs from live caller"
        )
    return receipt


def verify_supplied_key(plan, *, approved=None):
    key_arn = plan.get("variables", {}).get("kms_key_arn", {}).get("value", "")
    if not key_arn:
        if approved is not None:
            raise WorkspaceOwnershipError(
                "Supplied-key evidence was attached to an owned-key plan"
            )
        return None
    outputs = plan.get("planned_values", {}).get("outputs", {})
    try:
        requirements = json.loads(outputs["supplied_kms_key_required_policy"]["value"])
        caller = outputs["provisioning_principal_arn"]["value"]
        account = plan["variables"]["account_id"]["value"]
        region = plan["variables"]["aws_region"]["value"]
    except (ValueError, KeyError, TypeError) as exc:
        raise WorkspaceOwnershipError(
            "Supplied-key plan lacks known permission requirements"
        ) from exc
    verify_provisioning_principal(plan)
    key = _aws("kms", "describe-key", "--key-id", key_arn, region=region)["KeyMetadata"]
    if not (
        key.get("Arn") == key_arn
        and key.get("Enabled") is True
        and key.get("KeyState") == "Enabled"
        and key.get("KeyManager") == "CUSTOMER"
        and key.get("KeyUsage") == "ENCRYPT_DECRYPT"
        and key.get("KeySpec") == "SYMMETRIC_DEFAULT"
    ):
        raise WorkspaceOwnershipError(
            "Supplied key is not an enabled symmetric customer encryption key"
        )
    raw = _aws(
        "kms",
        "get-key-policy",
        "--key-id",
        key_arn,
        "--policy-name",
        "default",
        region=region,
    )["Policy"]
    policy = json.loads(raw)
    partition = key_arn.split(":")[1]
    verify_policy(policy, requirements, key_arn, f"arn:{partition}:iam::{account}:root")
    simulation = _aws(
        "iam",
        "simulate-principal-policy",
        "--policy-source-arn",
        caller,
        "--action-names",
        "kms:DescribeKey",
        "kms:CreateGrant",
        "--resource-arns",
        key_arn,
        region=region,
    )
    results = simulation.get("EvaluationResults", [])
    if {r.get("EvalActionName") for r in results} != {
        "kms:DescribeKey",
        "kms:CreateGrant",
    } or any(
        r.get("EvalDecision") != "allowed"
        or r.get("MissingContextValues")
        or r.get("PermissionsBoundaryDecisionDetail", {}).get(
            "AllowedByPermissionsBoundary"
        )
        is False
        or r.get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations")
        is False
        for r in results
    ):
        raise WorkspaceOwnershipError(
            "Provisioning caller's effective KMS identity permissions were not established"
        )
    # This is KMS's own dry-run authorization check, not an actual grant. The
    # conservative policy contract above does not support grantee/operation conditions.
    dry_run = subprocess.run(
        [
            "aws",
            "kms",
            "create-grant",
            "--key-id",
            key_arn,
            "--grantee-principal",
            caller,
            "--operations",
            "Encrypt",
            "Decrypt",
            "GenerateDataKey",
            "GenerateDataKeyWithoutPlaintext",
            "ReEncryptFrom",
            "ReEncryptTo",
            "DescribeKey",
            "CreateGrant",
            "--dry-run",
            "--region",
            region,
            "--no-cli-pager",
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if not dry_run.returncode or "DryRunOperationException" not in dry_run.stderr:
        raise WorkspaceOwnershipError(
            "KMS did not confirm the caller's CreateGrant dry run; no apply is allowed"
        )
    receipt = {
        "key_arn": key_arn,
        "principal_arn": caller,
        "key_state": key["KeyState"],
        "policy_sha256": hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "identity_actions": ["kms:DescribeKey", "kms:CreateGrant"],
        "create_grant_dry_run": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if approved is not None and any(
        approved.get(name) != receipt[name]
        for name in ("key_arn", "principal_arn", "policy_sha256")
    ):
        raise WorkspaceOwnershipError(
            "Supplied-key policy or identity changed after plan review; regenerate authorization"
        )
    return receipt
