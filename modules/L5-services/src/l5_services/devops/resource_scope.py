"""Explicit resource scope for a single DevOps binding verification."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass


class ScopeError(ValueError):
    """The effective role scope cannot be proved against this selection."""


def evaluate_role_scope(iam, role_arn: str, space_arn: str, host_account: str,
                        allowed_actions: dict[str, set[str]], *, inline_only=False) -> dict:
    """Inspect every identity grant; an additional allow cannot hide behind a scoped grant."""
    role_name = role_arn.rsplit("/", 1)[-1]
    role = iam.get_role(RoleName=role_name).get("Role") or {}
    if role.get("Arn") != role_arn:
        raise ScopeError("resource_role_mismatch")
    trust = role.get("AssumeRolePolicyDocument") or {}
    statements = trust.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    expected_condition = {"StringEquals": {"aws:SourceAccount": host_account},
                          "ArnEquals": {"aws:SourceArn": space_arn}}
    if not statements or any(
        not isinstance(statement, dict) or statement.get("Effect") != "Allow"
        or statement.get("Action") not in ("sts:AssumeRole", ["sts:AssumeRole"])
        or statement.get("Principal") != {"Service": "aidevops.amazonaws.com"}
        or statement.get("Condition") != expected_condition
        for statement in statements
    ):
        raise ScopeError("resource_role_trust_outside_selected_space")

    def pages(method, field):
        marker = None
        visited = set()
        while True:
            response = method(RoleName=role_name, **({"Marker": marker} if marker else {}))
            yield from response.get(field) or []
            if not response.get("IsTruncated"):
                return
            marker = response.get("Marker")
            if not marker or marker in visited or len(visited) >= 20:
                raise ScopeError("role_policy_discovery_incomplete")
            visited.add(marker)

    def policy_document(policy_arn):
        version = iam.get_policy(PolicyArn=policy_arn)["Policy"]["DefaultVersionId"]
        document = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version)["PolicyVersion"]["Document"]
        return {"arn": policy_arn, "version_id": version, "document": document}

    attached = list(pages(iam.list_attached_role_policies, "AttachedPolicies"))
    if inline_only and (attached or role.get("PermissionsBoundary")):
        raise ScopeError("resource_role_requires_inline_policy")
    managed = sorted((policy_document(policy["PolicyArn"])
                      for policy in attached),
                     key=lambda item: item["arn"])
    inline = [{"name": name, "document": iam.get_role_policy(
        RoleName=role_name, PolicyName=name)["PolicyDocument"]}
        for name in sorted(pages(iam.list_role_policies, "PolicyNames"))]
    policies = [item["document"] for item in [*managed, *inline]]
    if not policies:
        raise ScopeError("role_permissions_unknown")
    boundary = role.get("PermissionsBoundary")
    boundary_snapshot = policy_document(boundary["PermissionsBoundaryArn"]) if boundary else None
    boundary_policy = boundary_snapshot["document"] if boundary_snapshot else None
    granted = set()
    boundary_granted = set()
    for policy in [*policies, *([boundary_policy] if boundary_policy else [])]:
        if not isinstance(policy, dict) or "Statement" not in policy:
            raise ScopeError("role_permissions_unknown")
        statements = policy["Statement"]
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list) or not statements:
            raise ScopeError("role_permissions_unknown")
        for statement in statements:
            if statement.get("Effect") == "Deny":
                raise ScopeError("role_permission_deny_requires_review")
            if statement.get("Effect") != "Allow" or "NotAction" in statement or "NotResource" in statement:
                raise ScopeError("role_permissions_unknown")
            if statement.get("Condition"):
                raise ScopeError("role_permission_condition_requires_review")
            actions = statement.get("Action")
            resources = statement.get("Resource")
            actions = [actions] if isinstance(actions, str) else actions
            resources = [resources] if isinstance(resources, str) else resources
            if not isinstance(actions, list) or not actions:
                raise ScopeError("role_permissions_unknown")
            for action in actions:
                if action not in allowed_actions:
                    raise ScopeError("unselected_policy_action")
                if not isinstance(resources, list) or not resources or any(
                    resource not in allowed_actions[action] for resource in resources
                ):
                    raise ScopeError("unselected_policy_resource")
                if policy is boundary_policy:
                    boundary_granted.add(action)
                else:
                    granted.add(action)
    if not granted.issuperset(allowed_actions):
        raise ScopeError("missing_selected_permission")
    if boundary_policy and not boundary_granted.issuperset(allowed_actions):
        raise ScopeError("permission_boundary_excludes_selection")
    for action, resources in allowed_actions.items():
        for resource in resources:
            result = iam.simulate_principal_policy(PolicySourceArn=role_arn, ActionNames=[action],
                                                   ResourceArns=[resource])
            evaluations = result.get("EvaluationResults") or []
            if len(evaluations) != 1 or evaluations[0].get("EvalDecision") != "allowed":
                raise ScopeError("effective_permission_not_allowed")
    snapshot = {"version": 1, "role_arn": role_arn, "trust_document": trust,
                "managed_policies": managed, "inline_policies": inline,
                "permissions_boundary": boundary_snapshot,
                "selected_resources": {action: sorted(resources)
                                       for action, resources in sorted(allowed_actions.items())}}
    return {"trust": "selected_space", "policy_documents": len(policies) + bool(boundary_policy),
            "selected_actions": sorted(granted), "policy_snapshot": snapshot,
            "policy_sha256": policy_digest(snapshot)}


def policy_digest(document: dict) -> str:
    """Content address an exact provider policy observation, without aliases."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ResourceSelection:
    account_id: str
    region: str
    role_arn: str
    setup_role_arn: str
    connection_ref: dict
    workload_kind: str
    cluster_name: str | None = None
    namespace: str | None = None
    resource_arn: str | None = None
    telemetry_arn: str | None = None

    def fingerprint(self) -> str:
        if not (isinstance(self.account_id, str) and self.account_id.isascii()
                and self.account_id.isdigit() and len(self.account_id) == 12):
            raise ValueError("selected resource account is invalid")
        if not isinstance(self.region, str) or not self.region:
            raise ValueError("selected resource Region is required")
        if not isinstance(self.role_arn, str) or not re.fullmatch(
            rf"arn:aws:iam::{self.account_id}:role/[A-Za-z0-9+=,.@_/-]+", self.role_arn
        ):
            raise ValueError("selected resource role belongs to another account")
        if (not isinstance(self.setup_role_arn, str)
                or not re.fullmatch(rf"arn:aws:iam::{self.account_id}:role/[A-Za-z0-9+=,.@_/-]+", self.setup_role_arn)
                or self.setup_role_arn == self.role_arn):
            raise ValueError("setup and resource roles must be distinct in the selected account")
        if not isinstance(self.connection_ref, dict) or set(self.connection_ref) != {
            "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"
        } or any(not isinstance(value, str) or not value for value in self.connection_ref.values()):
            raise ValueError("selected connection must be a pinned broker reference")
        if self.workload_kind == "eks":
            if (not isinstance(self.cluster_name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", self.cluster_name)
                    or not isinstance(self.namespace, str) or len(self.namespace) > 63
                    or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", self.namespace)
                    or self.resource_arn or self.telemetry_arn):
                raise ValueError("EKS verification requires a selected cluster and namespace")
        elif self.workload_kind == "lambda":
            function_prefix = f"arn:aws:lambda:{self.region}:{self.account_id}:function:"
            log_prefix = f"arn:aws:logs:{self.region}:{self.account_id}:log-group:"
            if (self.cluster_name is not None or self.namespace is not None
                    or not isinstance(self.resource_arn, str) or not self.resource_arn.startswith(function_prefix)
                    or not self.resource_arn[len(function_prefix):].isascii()
                    or not self.resource_arn[len(function_prefix):].replace("-", "").replace("_", "").isalnum()
                    or not isinstance(self.telemetry_arn, str) or not self.telemetry_arn.startswith(log_prefix)
                    or not self.telemetry_arn.endswith(":*") or "*" in self.telemetry_arn[:-2]
                    or not self.telemetry_arn[len(log_prefix):-2]):
                raise ValueError("Lambda requires a selected function and log group without Kubernetes fields")
        else:
            raise ValueError("unsupported DevOps workload kind")
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
