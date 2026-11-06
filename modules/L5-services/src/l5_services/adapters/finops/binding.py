"""Read an existing native agent and its selected billing authorities before adoption."""

import asyncio
import json
import re
from datetime import UTC, datetime
from hashlib import sha256

from fastapi import HTTPException
from botocore.exceptions import ClientError


BILLING_ACTIONS = ("ce:GetCostAndUsage", "ce:GetDimensionValues")


def scoped_read_policy(policies, view):
    allowed, fenced = set(), set()
    required = {action.lower() for action in BILLING_ACTIONS}
    for policy in policies:
        document = policy.get("document")
        statements = document.get("Statement") if isinstance(document, dict) else None
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list) or not statements:
            return False
        for statement in statements:
            if not isinstance(statement, dict):
                return False
            actions = statement.get("Action")
            actions = [actions] if isinstance(actions, str) else actions
            if not isinstance(actions, list) or not actions or any(not isinstance(action, str) for action in actions):
                return False
            resources = statement.get("Resource")
            resources = [resources] if isinstance(resources, str) else resources
            if statement.get("Effect") == "Allow":
                if (set(statement) - {"Sid", "Effect", "Action", "Resource", "Condition"}
                        or not isinstance(resources, list) or not resources
                        or any(resource != view for resource in resources)
                        or not {action.lower() for action in actions} <= required):
                    return False
                allowed.update(action.lower() for action in actions)
            elif statement.get("Effect") == "Deny":
                excluded = statement.get("NotResource")
                excluded = [excluded] if isinstance(excluded, str) else excluded
                if (set(statement) <= {"Sid", "Effect", "Action", "NotResource"}
                        and isinstance(excluded, list) and excluded and all(resource == view for resource in excluded)):
                    fenced.update(action for action in required
                                  if any(pattern.lower() in {action, "ce:*", "*"} for pattern in actions))
            else:
                return False
    return allowed == required and fenced == required


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def binding_readback(binding):
    records = [item.get("native_readback") for item in binding.readiness or []
               if isinstance(item, dict) and item.get("capability") == "cost_inquiry"]
    if len(records) != 1 or not isinstance(records[0], dict):
        return None
    record = records[0]
    if (binding.state != "ready" or binding.gaps
            or record.get("scope") != binding.scope
            or record.get("binding_generation") != binding.generation
            or record.get("native_resource_ref") != binding.native_resource_ref
            or record.get("provider_identity") != binding.provider_identity
            or not record.get("native_agent_display_name")
            or (record.get("billing_scope") or {}).get("scope_generation") != binding.generation):
        return None
    return record


class FinOpsBindingProvider:
    def __init__(self, bridge, authority, billing, iam, archive):
        self.bridge = bridge
        self.authority = authority
        self.billing = billing
        self.iam = iam
        self.archive = archive

    async def _role_policies(self, role_name):
        policies = []
        for method, member in (("list_role_policies", "PolicyNames"),
                               ("list_attached_role_policies", "AttachedPolicies")):
            marker = None
            for _ in range(5):
                response = await asyncio.to_thread(getattr(self.iam, method), RoleName=role_name,
                                                  **({"Marker": marker} if marker else {}))
                for policy in response[member]:
                    if method == "list_role_policies":
                        document = await asyncio.to_thread(self.iam.get_role_policy, RoleName=role_name, PolicyName=policy)
                        policies.append({"name": policy, "document": document["PolicyDocument"]})
                    else:
                        selected = await asyncio.to_thread(self.iam.get_policy, PolicyArn=policy["PolicyArn"])
                        version = selected["Policy"]["DefaultVersionId"]
                        document = await asyncio.to_thread(self.iam.get_policy_version, PolicyArn=policy["PolicyArn"], VersionId=version)
                        policies.append({"arn": policy["PolicyArn"], "version": version,
                                         "document": document["PolicyVersion"]["Document"]})
                    if len(policies) > 30:
                        raise ValueError("FinOps data role exceeds bounded policy readback")
                if not response.get("IsTruncated"):
                    break
                if not response.get("Marker") or response["Marker"] == marker:
                    raise ValueError("FinOps data role policy readback is incomplete")
                marker = response["Marker"]
            else:
                raise ValueError("FinOps data role policy readback is incomplete")
        if not policies:
            raise ValueError("FinOps data role has no observed billing policy")
        return policies

    async def _effective_permissions(self, role, policies, view):
        denied = HTTPException(409, detail={"code": "billing_authority_unverified"})
        if not scoped_read_policy(policies, view):
            raise denied
        observed = await asyncio.to_thread(self.iam.simulate_principal_policy,
            PolicySourceArn=role["Arn"], ActionNames=list(BILLING_ACTIONS), ResourceArns=[view], MaxItems=100,
            ContextEntries=[{"ContextKeyName": "aws:CurrentTime", "ContextKeyType": "date",
                             "ContextKeyValues": [datetime.now(UTC).isoformat().replace("+00:00", "Z")]}])
        if not isinstance(observed, dict):
            raise denied
        results = observed.get("EvaluationResults")
        if (observed.get("IsTruncated", False) is not False or not isinstance(results, list)
                or len(results) != len(BILLING_ACTIONS)):
            raise denied
        seen = set()
        for result in results:
            if not isinstance(result, dict):
                raise denied
            action = result.get("EvalActionName")
            resources = result.get("ResourceSpecificResults")
            if (action not in BILLING_ACTIONS or action in seen
                    or result.get("EvalDecision") != "allowed" or result.get("MissingContextValues")
                    or not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], dict)):
                raise denied
            resource = resources[0]
            if (resource.get("EvalResourceName") != view or resource.get("EvalResourceDecision") != "allowed"
                    or resource.get("MissingContextValues")
                    or (role.get("PermissionsBoundary") and not any(
                        "PermissionsBoundaryDecisionDetail" in decision for decision in (result, resource)))):
                raise denied
            for decision in (result, resource):
                for field, allowed in (("PermissionsBoundaryDecisionDetail", "AllowedByPermissionsBoundary"),
                                       ("OrganizationsDecisionDetail", "AllowedByOrganizations")):
                    if field in decision and (not isinstance(decision[field], dict) or decision[field].get(allowed) is not True):
                        raise denied
            seen.add(action)
        return observed

    async def read(self, reference, verified):
        selection = verified.get("finops") or {}
        selected = selection.get("billing_scope") or {}
        role_arn = selection.get("data_role_ref")
        identity = {field: verified[field] for field in ("account_id", "role_arn", "region")}
        if (selection.get("agent_mode") != "adopt" or reference.get("record_type") != "finops_agent"
                or reference.get("record_id") != selection.get("native_agent_ref")
                or any(reference.get(field) != verified["scope"].get(field) for field in ("tenant_id", "project_id"))
                or reference.get("source_connection_id") != verified["connection_ref"].get("source_connection_id")
                or not isinstance(role_arn, str)
                or re.fullmatch(rf"arn:aws:iam::{identity['account_id']}:role/[A-Za-z0-9_+=,.@/-]+", role_arn) is None):
            raise HTTPException(409, detail={"code": "native_scope_unverified"})
        native = await self.bridge.read_agent(native_job_ref=reference, authority=self.authority)
        if (not isinstance(native, dict) or native.get("native_resource_ref") != reference
                or native.get("provider_identity") != identity or native.get("state") != "ready"
                or native.get("data_role_ref") != role_arn
                or native.get("native_agent_display_name") != selection.get("native_agent_display_name")
                or not isinstance(native.get("native_agent_display_name"), str)
                or not native["native_agent_display_name"].strip()):
            raise HTTPException(409, detail={"code": "native_scope_unverified"})
        response = await asyncio.to_thread(self.billing.get_billing_view, arn=selected["view_ref"])
        view = response.get("billingView") or {}
        dimensions = (view.get("dataFilterExpression") or {}).get("dimensions") or {}
        accounts = dimensions.get("values") if dimensions.get("key") == "LINKED_ACCOUNT" else None
        if (view.get("arn") != selected["view_ref"] or view.get("ownerAccountId") != identity["account_id"]
                or view.get("sourceAccountId") != identity["account_id"]
                or not accounts or sorted(accounts) != sorted(selected.get("account_ids") or [])
                or (view.get("healthStatus") or {}).get("statusCode") != "HEALTHY"):
            raise HTTPException(409, detail={"code": "billing_scope_unverified"})
        role = (await asyncio.to_thread(self.iam.get_role, RoleName=role_arn.rsplit("/", 1)[-1]))["Role"]
        if role.get("Arn") != role_arn:
            raise HTTPException(409, detail={"code": "billing_scope_unverified"})
        try:
            policies = await self._role_policies(role_arn.rsplit("/", 1)[-1])
            permissions = await self._effective_permissions(role, policies, selected["view_ref"])
        except (ClientError, ValueError):
            raise HTTPException(409, detail={"code": "billing_authority_unverified"}) from None
        generation = self.authority["binding_generation"]
        snapshot = {"scope": verified["scope"], "configuration_revision": verified["configuration_revision"],
                    "binding_generation": generation, "native": native, "billing_view": view,
                    "data_role": role, "policies": policies, "effective_permissions": permissions}
        content = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
        if len(content) > 1024 * 1024:
            raise ValueError("FinOps scope readback exceeds the archive bound")
        checksum = sha256(content).hexdigest()
        key = f"l5/finops/work/{self.authority['operation_id']}/billing/scope/{checksum}.json"
        saved = await asyncio.to_thread(self.archive.client.put_object, Bucket=self.archive.bucket, Key=key,
                                       Body=content, Metadata={"sha256": checksum})
        if not saved.get("VersionId"):
            raise ValueError("FinOps scope readback requires a versioned archive")
        billing_scope = {**selected, "scope_generation": generation, "scope_digest": checksum,
                         "filter_digest": digest(view.get("dataFilterExpression") or {}),
                         "role_policy_digest": digest(policies), "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                         "snapshot_artifact": {"artifact_id": key, "version": saved["VersionId"], "sha256": checksum}}
        record = {"scope": verified["scope"], "configuration_revision": verified["configuration_revision"],
                  "binding_generation": generation, "native_resource_ref": reference, "provider_identity": identity,
                  "native_agent_display_name": native["native_agent_display_name"], "billing_scope": billing_scope}
        return {"state": "ready", "native_resource_ref": reference, "provider_identity": identity,
                "readiness": [{"capability": "cost_inquiry", "status": "ready", "gaps": [], "native_readback": record}],
                "gaps": []}
