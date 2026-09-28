"""Concrete removals bound to the complete durable bootstrap ownership journal.

The execution service calls these only after shared admission and ordered drain.
They return success only after a fresh provider read establishes absence. An
accepted asynchronous deletion or lost response remains UNKNOWN for recovery.
"""

from copy import deepcopy

from harness_jobs.execution import CallOutcome
from superplane_bootstrap.component_journal import component_identity
from superplane_bootstrap.errors import BootstrapRefused

from .retirement_plan import (
    AWS,
    DELETE_COMPONENT,
    KUBERNETES,
    REVOKE_GRANT,
    REVOKE_PREREQUISITE,
    compose_retirement_plan,
)


def _payload(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


class OwnedResourceRemover:
    def __init__(self, *, kubernetes, eks, network=None):
        # Maintained KubeGrants/EksGrants adapters carry pinned transports and the
        # EKS immutable-entry scoped deletion credential, not ambient kubeconfig.
        self.kubernetes, self.eks = kubernetes, eks
        self.network = network

    def execute(self, step, inventory):
        plan = compose_retirement_plan(inventory)
        if step not in plan.steps:
            raise BootstrapRefused(
                "retirement step no longer matches durable ownership"
            )
        if (step.provider, step.operation_kind) == (KUBERNETES, DELETE_COMPONENT):
            index = int(step.step_id.removeprefix("delete-component-"))
            return self._component(inventory, inventory.components[index])
        if (step.provider, step.operation_kind) in {
            (KUBERNETES, REVOKE_GRANT),
            (AWS, REVOKE_GRANT),
        }:
            index = int(step.step_id.removeprefix("revoke-grant-"))
            return self._grant(inventory.grants[index])
        if (step.provider, step.operation_kind) == (AWS, REVOKE_PREREQUISITE):
            index = int(step.step_id.removeprefix("revoke-prerequisite-"))
            prerequisite = inventory.prerequisites[index]
            if prerequisite.kind == "EksAccessEntry":
                matches = [
                    grant
                    for grant in inventory.grants
                    if grant.spec.get("kind") == "eks-entry"
                    and grant.identity.get("arn")
                    == prerequisite.identifier.partition("#")[0]
                ]
                if len(matches) != 1:
                    raise BootstrapRefused(
                        "access prerequisite lacks complete retained grant identity"
                    )
                return self._grant(matches[0])
            if self.network is not None:
                return self.network.revoke(inventory, prerequisite)
        raise BootstrapRefused("retirement removal adapter is not implemented")

    def _component(self, inventory, component):
        body, identity = component.desired, component.identity
        if (
            not component.owned
            or body.get("kind")
            not in {"ServiceAccount", "Role", "RoleBinding", "Deployment"}
            or body.get("metadata", {}).get("namespace") != inventory.namespace
            or not identity.get("creation")
        ):
            raise BootstrapRefused(
                "component lacks exclusive namespaced creation ownership"
            )
        spec = {"cluster_arn": inventory.cluster_arn, "body": body}
        observed = self.kubernetes._get(spec)
        if observed is None:
            return CallOutcome.SUCCEEDED, "owned component absent", identity["uid"]
        if component_identity(observed) != identity:
            raise BootstrapRefused(
                "component immutable identity or specification changed"
            )
        version = observed["metadata"].get("resourceVersion")
        if not version:
            raise BootstrapRefused("component deletion requires resourceVersion")
        self.kubernetes._resource(spec).delete(
            **self.kubernetes._args(spec),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": identity["uid"], "resourceVersion": version},
            },
        )
        if self.kubernetes._get(spec) is not None:
            return (
                CallOutcome.UNKNOWN,
                "component deletion awaiting observation",
                identity["uid"],
            )
        return CallOutcome.SUCCEEDED, "owned component absent", identity["uid"]

    def _grant(self, grant):
        spec, identity = deepcopy(grant.spec), deepcopy(grant.identity)
        if spec.get("kind") == "kubernetes":
            if spec.get("body", {}).get("kind") not in {"Role", "RoleBinding"}:
                raise BootstrapRefused("shared or unsupported grant is preserved")
            adapter, reference = self.kubernetes, identity["uid"]
        elif spec.get("kind") == "eks-entry":
            adapter, reference = self.eks, identity["arn"]
        else:
            raise BootstrapRefused("unsupported retained grant")
        observed = adapter.observe(spec)
        if observed is not None:
            # delete rechecks the full journal identity/generation/digest at the
            # actual mutation, then applies provider-native deletion preconditions.
            adapter.delete(spec, identity)
        if adapter.observe(spec) is not None:
            return (
                CallOutcome.UNKNOWN,
                "grant revocation awaiting observation",
                reference,
            )
        return CallOutcome.SUCCEEDED, "owned grant absent", reference


class SecurityGroupRules:
    def __init__(self, *, session, target, expected):
        from superplane_bootstrap.prerequisites import ExpectedPrerequisites
        from superplane_bootstrap.target import VerifiedTarget

        if not isinstance(target, VerifiedTarget) or not isinstance(
            expected, ExpectedPrerequisites
        ):
            raise BootstrapRefused(
                "network retirement requires verified target and prerequisite outputs"
            )
        self.session, self.target, self.expected = session, target, expected

    def revoke(self, inventory, prerequisite):
        if (
            not prerequisite.removable
            or prerequisite.kind
            not in {
                "SecurityGroupRule/cluster-endpoint",
                "SecurityGroupRule/private-sts",
            }
            or prerequisite.workspace_id != inventory.workspace_id
            or inventory.cluster_arn != self.target.cluster_arn
        ):
            raise BootstrapRefused("network prerequisite has no owned target binding")
        account = self.session.client(
            "sts", region_name=self.target.region
        ).get_caller_identity()["Account"]
        if account != self.target.account_id or account != self.expected.account_id:
            raise BootstrapRefused("network retirement provider account changed")
        ec2 = self.session.client("ec2", region_name=self.target.region)
        cluster = self.session.client(
            "eks", region_name=self.target.region
        ).describe_cluster(name=inventory.cluster_arn.rsplit("/", 1)[-1])["cluster"]
        if (
            cluster["arn"] != inventory.cluster_arn
            or cluster["endpoint"] != self.target.endpoint
        ):
            raise BootstrapRefused("network retirement cluster identity changed")

        def read():
            try:
                rows = ec2.describe_security_group_rules(
                    SecurityGroupRuleIds=[prerequisite.identifier]
                )["SecurityGroupRules"]
            except Exception as exc:
                if (
                    getattr(exc, "response", {}).get("Error", {}).get("Code")
                    == "InvalidSecurityGroupRuleId.NotFound"
                ):
                    return None
                raise
            if (
                len(rows) != 1
                or rows[0]["SecurityGroupRuleId"] != prerequisite.identifier
            ):
                raise BootstrapRefused("network prerequisite identity unavailable")
            return rows[0]

        rule = read()
        if rule is not None:
            expected = self.expected
            if prerequisite.kind == "SecurityGroupRule/cluster-endpoint":
                group, source, vpc = (
                    expected.cluster_security_group_id,
                    expected.management_security_group_id,
                    expected.vpc_id,
                )
            else:
                group, source, vpc = (
                    expected.sts_endpoint_security_group_id,
                    expected.node_security_group_id,
                    expected.sts_endpoint_vpc_id,
                )
            tags = {tag["Key"]: tag["Value"] for tag in rule.get("Tags", [])}
            if (
                rule["GroupId"] != group
                or rule.get("ReferencedGroupInfo", {}).get("GroupId") != source
                or rule.get("IpProtocol") != expected.protocol
                or rule.get("FromPort") != expected.api_server_port
                or rule.get("ToPort") != expected.api_server_port
                or tags.get("OrgId") != inventory.org_id
                or tags.get("WorkspaceId") != inventory.workspace_id
            ):
                raise BootstrapRefused(
                    "owned network prerequisite specification or attribution changed"
                )
            groups = ec2.describe_security_groups(GroupIds=[rule["GroupId"]])[
                "SecurityGroups"
            ]
            if (
                len(groups) != 1
                or groups[0]["OwnerId"] != account
                or groups[0]["VpcId"] != vpc
            ):
                raise BootstrapRefused(
                    "owned network rule moved outside the verified workspace VPC"
                )
            if type(rule.get("IsEgress")) is not bool:
                raise BootstrapRefused("network prerequisite direction unavailable")
            revoke = (
                ec2.revoke_security_group_egress
                if rule["IsEgress"]
                else ec2.revoke_security_group_ingress
            )
            revoke(
                GroupId=rule["GroupId"], SecurityGroupRuleIds=[prerequisite.identifier]
            )
        if read() is not None:
            return (
                CallOutcome.UNKNOWN,
                "network revocation awaiting observation",
                prerequisite.identifier,
            )
        return (
            CallOutcome.SUCCEEDED,
            "owned network rule absent",
            prerequisite.identifier,
        )
