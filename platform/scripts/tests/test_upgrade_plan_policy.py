import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("policy", Path(__file__).resolve().parents[1] / "upgrade-plan-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def change(address, kind, before, after, actions=("delete", "create")):
    return {"address": address, "type": kind, "change": {"before": before, "after": after, "actions": list(actions)}}


class PlanPolicyTests(unittest.TestCase):
    def evaluate(self, resource, module="gateway"):
        return policy.evaluate({"resource_changes": [resource]}, module, "123456789012")

    def factory_plan(self, retirement):
        runner = "adp-dev-agent-factory-runner-role"
        arn = "arn:aws:iam::123456789012:role/" + runner
        role = {"name": runner, "arn": arn,
                "permissions_boundary": "arn:aws:iam::123456789012:policy/adp-dev-agent-runner-boundary"}
        return {"variables": {"environment": {"value": "dev"},
                              "aws_region": {"value": "us-east-1"},
                              "runner_role_name": {"value": runner}},
                "resource_changes": [retirement,
                    change("module.runner_iam.aws_iam_role.runner", "aws_iam_role", role, role, ("no-op",))]}

    def test_factory_runner_eks_edit_retirement_is_exact(self):
        runner = "adp-dev-agent-factory-runner-role"
        before = {"cluster_name": "adp-dev-eks-cluster",
                  "principal_arn": "arn:aws:iam::123456789012:role/" + runner,
                  "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy",
                  "access_scope": [{"type": "namespace", "namespaces": ["adp-agents", "arc-runners"]}]}
        retired = change("aws_eks_access_policy_association.runner_edit",
                         "aws_eks_access_policy_association", before, None, ("delete",))
        plan = self.factory_plan(retired)
        self.assertEqual(policy.evaluate(plan, "agent-factory", "123456789012")["routine"], [retired["address"]])
        for field, value in (("policy_arn", "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"),
                             ("cluster_name", "other")):
            bad = copy.deepcopy(plan)
            bad["resource_changes"][0]["change"]["before"][field] = value
            self.assertEqual(policy.evaluate(bad, "agent-factory", "123456789012")["blocked"], [retired["address"]])

    def test_factory_intake_inline_retirement_requires_attached_superset(self):
        gateway = "adp-dev-role-gateway-service"
        name = "adp-dev-policy-gateway-intake"
        sessions = "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-agent-gateway-sessions"
        old = [
            {"Sid": "IntakeSessionsRead", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query"],
             "Resource": [sessions, sessions + "/index/*"]},
            {"Sid": "IntakeDraftRead", "Effect": "Allow", "Action": ["dynamodb:GetItem"],
             "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-chat-context"]},
            {"Sid": "IntakeTablesKMSDecrypt", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:DescribeKey"],
             "Resource": ["arn:aws:kms:us-east-1:123456789012:key/271615d4-2e8f-4b9d-af44-33427bf8e38d"]},
            {"Sid": "IntakeDispatchInvoke", "Effect": "Allow", "Action": ["lambda:InvokeFunction"],
             "Resource": ["arn:aws:lambda:us-east-1:123456789012:function:adp-dev-agent-gateway-ingest"]},
        ]
        hosted = {"Sid": "HostedTaskChatSessionsWrite", "Effect": "Allow", "Action": ["dynamodb:PutItem"],
                  "Resource": [sessions], "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["chat-*"]}}}
        replacement = copy.deepcopy(old)
        replacement.insert(1, hosted)
        replacement[3]["Action"].append("kms:GenerateDataKey")
        doc = lambda statements: json.dumps({"Version": "2012-10-17", "Statement": statements})
        retired = change("aws_iam_role_policy.gateway_intake_access[0]", "aws_iam_role_policy",
                         {"role": gateway, "name": name, "id": gateway + ":" + name, "policy": doc(old)},
                         None, ("delete",))
        plan = self.factory_plan(retired)
        arn = "arn:aws:iam::123456789012:policy/" + name
        managed = {"name": name, "arn": arn, "policy": doc(replacement)}
        attached = {"role": gateway, "policy_arn": arn}
        plan["resource_changes"].extend([
            change("aws_iam_policy.gateway_intake_access[0]", "aws_iam_policy", managed, managed, ("no-op",)),
            change("aws_iam_role_policy_attachment.gateway_intake_access[0]", "aws_iam_role_policy_attachment",
                   attached, attached, ("no-op",)),
        ])
        self.assertEqual(policy.evaluate(plan, "agent-factory", "123456789012")["routine"], [retired["address"]])
        # Existing deployments may already have the newer bounded chat grant.
        # An identical attached replacement is equally safe to cut over to.
        current = copy.deepcopy(plan)
        current["resource_changes"][0]["change"]["before"]["policy"] = doc(replacement)
        self.assertEqual(policy.evaluate(current, "agent-factory", "123456789012")["routine"], [retired["address"]])
        # Delegated chat (#7899) adds exactly one bounded chat-context write grant;
        # the retirement stays routine with it, and refuses a widened variant.
        chat_grant = {"Sid": "ChatDataPlaneWrite", "Effect": "Allow",
                      "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                                 "dynamodb:ConditionCheckItem", "dynamodb:Query"],
                      "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-chat-context"],
                      "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["chat-input#*", "session#*"]}}}
        with_chat = copy.deepcopy(replacement) + [chat_grant]
        # Crew host lease (#8223): one bounded crew# write grant is routine; widening it is not.
        lease_grant = {"Sid": "CrewHostLeaseWrite", "Effect": "Allow",
                       "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
                       "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-chat-context"],
                       "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["crew#*"]}}}
        with_lease = copy.deepcopy(with_chat) + [lease_grant]
        widened_lease = copy.deepcopy(with_chat) + [{**lease_grant, "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]}]
        unbounded_lease = copy.deepcopy(with_chat) + [{**lease_grant, "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["crew#*", "session#*"]}}}]

        def managed_entry(candidate):
            return next(item for item in candidate["resource_changes"] if item["address"] == "aws_iam_policy.gateway_intake_access[0]")

        delegated = copy.deepcopy(plan)
        for side in ("before", "after"):
            managed_entry(delegated)["change"][side]["policy"] = doc(with_chat)
        self.assertEqual(policy.evaluate(delegated, "agent-factory", "123456789012")["routine"], [retired["address"]])
        # The pre-#7983 three-action shape is still accepted so an existing environment
        # can cut over; the grant may grow only to the exact transaction shape.
        narrow = copy.deepcopy(delegated)
        narrowed = json.loads(managed_entry(narrow)["change"]["before"]["policy"])
        narrowed["Statement"][-1]["Action"] = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        managed_entry(narrow)["change"]["before"]["policy"] = json.dumps(narrowed)
        self.assertEqual(policy.evaluate(narrow, "agent-factory", "123456789012")["routine"], [retired["address"]])
        for widen in ({"Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["*"]}}},
                      {"Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"]},
                      {"Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                                  "dynamodb:ConditionCheckItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]},
                      {"Resource": ["*"]}):
            wide = copy.deepcopy(delegated)
            widened = json.loads(managed_entry(wide)["change"]["before"]["policy"])
            widened["Statement"][-1].update(widen)
            managed_entry(wide)["change"]["before"]["policy"] = json.dumps(widened)
            self.assertEqual(policy.evaluate(wide, "agent-factory", "123456789012")["blocked"], [retired["address"]], widen)
        for candidate, outcome in ((with_lease, "routine"), (widened_lease, "blocked"), (unbounded_lease, "blocked")):
            leased = copy.deepcopy(plan)
            for side in ("before", "after"):
                managed_entry(leased)["change"][side]["policy"] = doc(candidate)
            self.assertEqual(policy.evaluate(leased, "agent-factory", "123456789012")[outcome], [retired["address"]], candidate[-1])
        # The chat memory/artifact stores (#8028) add exactly three bounded grants as a set;
        # the retirement stays routine with them and refuses any widened or partial variant.
        memory_grant = {"Sid": "ChatMemoryStore", "Effect": "Allow",
                        "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem"],
                        "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-agent-memory"],
                        "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["memory#*", "memory-owner#*"]}}}
        catalog_grant = {"Sid": "ChatArtifactCatalog", "Effect": "Allow",
                         "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:ConditionCheckItem"],
                         "Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-chat-artifacts"],
                         "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["session#*"]}}}
        objects_grant = {"Sid": "ChatArtifactObjects", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
                         "Resource": ["arn:aws:s3:::adp-dev-chat-artifacts-123456789012/o/*/t/*/u/*/s/*/*"]}
        with_stores = with_chat + [memory_grant, catalog_grant, objects_grant]
        stores = copy.deepcopy(plan)
        for side in ("before", "after"):
            managed_entry(stores)["change"][side]["policy"] = doc(with_stores)
        self.assertEqual(policy.evaluate(stores, "agent-factory", "123456789012")["routine"], [retired["address"]])
        # An environment already carrying the store grants inline cuts over to the identical set.
        current_stores = copy.deepcopy(stores)
        current_stores["resource_changes"][0]["change"]["before"]["policy"] = doc(with_stores)
        self.assertEqual(policy.evaluate(current_stores, "agent-factory", "123456789012")["routine"], [retired["address"]])
        # ...but never to a replacement that dropped them.
        dropped = copy.deepcopy(current_stores)
        for side in ("before", "after"):
            managed_entry(dropped)["change"][side]["policy"] = doc(with_chat)
        self.assertEqual(policy.evaluate(dropped, "agent-factory", "123456789012")["blocked"], [retired["address"]])
        for label, index, widen in (
                ("memory any partition", -3, {"Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["*"]}}}),
                ("memory delete", -3, {"Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem"]}),
                ("memory index", -3, {"Resource": ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-agent-memory",
                                                   "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-agent-memory/index/*"]}),
                ("catalog scan", -2, {"Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:Scan"]}),
                ("catalog no condition", -2, {"Condition": None}),
                ("objects delete", -1, {"Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]}),
                ("objects whole bucket", -1, {"Resource": ["arn:aws:s3:::adp-dev-chat-artifacts-123456789012/*"]}),
                ("objects other bucket", -1, {"Resource": ["arn:aws:s3:::adp-dev-crew-checkpoints-123456789012/o/*/t/*/u/*/s/*/*"]})):
            wide = copy.deepcopy(stores)
            widened = json.loads(managed_entry(wide)["change"]["before"]["policy"])
            widened["Statement"][index].update(widen)
            managed_entry(wide)["change"]["before"]["policy"] = json.dumps(widened)
            self.assertEqual(policy.evaluate(wide, "agent-factory", "123456789012")["blocked"], [retired["address"]], label)
        for missing in (memory_grant, catalog_grant, objects_grant):
            partial = copy.deepcopy(stores)
            partial_doc = [item for item in with_stores if item is not missing]
            for side in ("before", "after"):
                managed_entry(partial)["change"][side]["policy"] = doc(partial_doc)
            self.assertEqual(policy.evaluate(partial, "agent-factory", "123456789012")["blocked"], [retired["address"]], missing["Sid"])
        for shape in (old, replacement):
            for mutation in ("extra statement", "duplicate sid", "changed condition", "permission removed"):
                bad = copy.deepcopy(plan)
                bad_old = copy.deepcopy(shape)
                if mutation == "extra statement":
                    bad_old.append({"Sid": "Unexpected", "Effect": "Allow", "Action": ["s3:*"], "Resource": ["*"]})
                elif mutation == "duplicate sid":
                    bad_old.append(copy.deepcopy(bad_old[0]))
                elif mutation == "changed condition":
                    bad_old[0]["Condition"] = {"StringEquals": {"aws:PrincipalTag/test": "required"}}
                else:
                    bad_old[0]["Action"].append("dynamodb:Scan")
                bad["resource_changes"][0]["change"]["before"]["policy"] = doc(bad_old)
                with self.subTest(shape=len(shape), mutation=mutation):
                    self.assertEqual(policy.evaluate(bad, "agent-factory", "123456789012")["blocked"], [retired["address"]])
        for key in ("attachment missing", "policy still creating", "permission removed"):
            bad = copy.deepcopy(plan)
            if key == "attachment missing":
                bad["resource_changes"].pop()
            elif key == "policy still creating":
                bad["resource_changes"][2]["change"]["actions"] = ["create"]
            else:
                new_doc = json.loads(bad["resource_changes"][2]["change"]["before"]["policy"])
                new_doc["Statement"][0]["Action"] = ["dynamodb:GetItem"]
                bad["resource_changes"][2]["change"]["before"]["policy"] = json.dumps(new_doc)
            with self.subTest(key=key):
                self.assertEqual(policy.evaluate(bad, "agent-factory", "123456789012")["blocked"], [retired["address"]])

    def gateway_role_plan(self, retired, groups=None):
        """Pass 2 of the gateway-role cutover (#7944): consolidated policies attached, legacy grant retired."""
        gateway = "adp-dev-role-gateway-service"
        groups = groups if groups is not None else self.gateway_role_groups()
        changes = [retired]
        for group, statements in groups.items():
            name = f"adp-dev-policy-gateway-{group}"
            arn = "arn:aws:iam::123456789012:policy/" + name
            managed = {"name": name, "arn": arn, "policy": json.dumps({"Version": "2012-10-17", "Statement": statements})}
            attached = {"role": gateway, "policy_arn": arn}
            changes.append(change(f'aws_iam_policy.gateway_role["{group}"]', "aws_iam_policy", managed, managed, ("no-op",)))
            changes.append(change(f'aws_iam_role_policy_attachment.gateway_role["{group}"]', "aws_iam_role_policy_attachment",
                                  attached, attached, ("no-op",)))
        return {"variables": {"environment": {"value": "dev"}, "aws_region": {"value": "us-east-1"}}, "resource_changes": changes}

    @staticmethod
    def gateway_role_groups():
        pool = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_AbCdEfGhI"
        kms = "arn:aws:kms:us-east-1:123456789012:key/271615d4-2e8f-4b9d-af44-33427bf8e38d"
        return {
            "identity": [
                {"Sid": "CognitoReadUsers", "Effect": "Allow", "Resource": pool,
                 "Action": ["cognito-idp:ListUsers", "cognito-idp:ListGroups", "cognito-idp:ListUsersInGroup",
                            "cognito-idp:AdminGetUser", "cognito-idp:AdminUpdateUserAttributes"]},
                {"Sid": "CognitoIdentityLifecycle", "Effect": "Allow", "Resource": pool, "Action": ["cognito-idp:AdminCreateUser"]},
                {"Sid": "UserIdentityIndexAccess", "Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem"],
                 "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-user-identity-index"},
                {"Sid": "DynamoDBKMSAccess", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"], "Resource": [kms]},
            ],
            "data": [
                {"Sid": "CfnTemplateList", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::bedrockgw-dev-frontend-123456789012",
                 "Condition": {"StringLike": {"s3:prefix": ["cfn-templates/*"]}}},
            ],
            "agents": [
                {"Sid": "ToolsCatalogueListGateways", "Effect": "Allow", "Action": ["bedrock-agentcore:ListGateways"], "Resource": "*"},
                {"Sid": "ToolsCatalogueReadTargets", "Effect": "Allow", "Resource": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/*",
                 "Action": ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:GetGatewayTarget"]},
                {"Sid": "CrewRunBindingKms", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"], "Resource": [kms]},
            ],
        }

    def test_gateway_legacy_inline_retirement_requires_attached_consolidated_superset(self):
        gateway = "adp-dev-role-gateway-service"
        name = "bedrockgw-dev-policy-gateway-user-identity-index"
        kms = "arn:aws:kms:us-east-1:123456789012:key/271615d4-2e8f-4b9d-af44-33427bf8e38d"
        old = [
            {"Sid": "UserIdentityIndexAccess", "Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem"],
             "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-user-identity-index"},
            {"Sid": "DynamoDBKMSAccess", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"], "Resource": [kms]},
        ]
        doc = lambda statements: json.dumps({"Version": "2012-10-17", "Statement": statements})
        retired = change('aws_iam_role_policy.gateway_role_legacy["user-identity-index"]', "aws_iam_role_policy",
                         {"role": gateway, "name": name, "id": gateway + ":" + name, "policy": doc(old)}, None, ("delete",))
        for module in ("gateway", "gateway-alb-wire", "gateway-final"):
            self.assertEqual(policy.evaluate(self.gateway_role_plan(retired), module, "123456789012")["routine"], [retired["address"]], module)
        self.assertEqual(policy.evaluate(self.gateway_role_plan(retired), "agent-factory", "123456789012")["blocked"], [retired["address"]])
        # Statements without a Sid (the old crew-run-binding shape) match on content.
        unnamed = copy.deepcopy(retired)
        unnamed["change"]["before"]["name"] = "bedrockgw-dev-gateway-crew-run-binding"
        unnamed["change"]["before"]["id"] = gateway + ":bedrockgw-dev-gateway-crew-run-binding"
        unnamed["address"] = 'aws_iam_role_policy.gateway_role_legacy["crew-run-binding"]'
        unnamed["change"]["before"]["policy"] = doc([{"Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"], "Resource": [kms]}])
        self.assertEqual(policy.evaluate(self.gateway_role_plan(unnamed), "gateway", "123456789012")["routine"], [unnamed["address"]])
        # The replacement may be mid-update as long as both versions still carry the statement.
        updating = self.gateway_role_plan(retired)
        identity = next(item for item in updating["resource_changes"] if item["address"] == 'aws_iam_policy.gateway_role["identity"]')
        identity["change"]["actions"] = ["update"]
        identity["change"]["after"] = dict(identity["change"]["before"], description="wider")
        self.assertEqual(policy.evaluate(updating, "gateway", "123456789012")["routine"], [retired["address"]])
        widened_after = json.loads(identity["change"]["after"]["policy"])
        widened_after["Statement"] = [s for s in widened_after["Statement"] if s["Sid"] != "UserIdentityIndexAccess"]
        identity["change"]["after"]["policy"] = json.dumps(widened_after)
        self.assertEqual(policy.evaluate(updating, "gateway", "123456789012")["blocked"], [retired["address"]])
        for key in ("attachment missing", "policy still creating", "attachment on another role", "statement dropped",
                    "action removed", "resource changed", "condition added", "sid renamed", "wrong policy name"):
            groups = self.gateway_role_groups()
            bad_retired = copy.deepcopy(retired)
            if key == "statement dropped":
                groups["identity"] = [s for s in groups["identity"] if s["Sid"] != "UserIdentityIndexAccess"]
            elif key == "action removed":
                groups["identity"][2]["Action"] = ["dynamodb:PutItem", "dynamodb:GetItem"]
            elif key == "resource changed":
                groups["identity"][2]["Resource"] = "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-other"
            elif key == "condition added":
                groups["identity"][2]["Condition"] = {"StringEquals": {"aws:PrincipalTag/test": "required"}}
            elif key == "sid renamed":
                groups["identity"][2]["Sid"] = "UserIdentityIndexAccessV2"
            elif key == "wrong policy name":
                bad_retired["change"]["before"]["name"] = "bedrockgw-dev-policy-gateway-identity-index"
                bad_retired["change"]["before"]["id"] = gateway + ":bedrockgw-dev-policy-gateway-identity-index"
            bad = self.gateway_role_plan(bad_retired, groups)
            if key == "attachment missing":
                bad["resource_changes"] = [c for c in bad["resource_changes"] if c["address"] != 'aws_iam_role_policy_attachment.gateway_role["identity"]']
            elif key == "policy still creating":
                next(c for c in bad["resource_changes"] if c["address"] == 'aws_iam_policy.gateway_role["identity"]')["change"]["actions"] = ["create"]
            elif key == "attachment on another role":
                next(c for c in bad["resource_changes"] if c["address"] == 'aws_iam_role_policy_attachment.gateway_role["identity"]')["change"]["before"]["role"] = "adp-dev-role-other"
            with self.subTest(key=key):
                self.assertEqual(policy.evaluate(bad, "gateway", "123456789012")["blocked"], [bad_retired["address"]])
        # Other inline policies on the same role (platform / webhook-ingress) are never this rule's business.
        foreign = copy.deepcopy(retired)
        foreign["address"] = "aws_iam_role_policy.gateway_activity_read"
        foreign["change"]["before"].update({"name": "adp-dev-policy-gateway-activity-read", "id": gateway + ":adp-dev-policy-gateway-activity-read"})
        self.assertEqual(policy.evaluate(self.gateway_role_plan(foreign), "gateway", "123456789012")["blocked"], [foreign["address"]])

    def test_gateway_retired_managed_policies_follow_the_same_gate(self):
        gateway = "adp-dev-role-gateway-service"
        name = "bedrockgw-dev-gateway-tools-catalogue-read"
        arn = "arn:aws:iam::123456789012:policy/" + name
        catalogue = [
            {"Effect": "Allow", "Action": ["bedrock-agentcore:ListGateways"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:GetGatewayTarget"],
             "Resource": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/*"},
        ]
        managed = change("aws_iam_policy.gateway_tools_catalogue_read[0]", "aws_iam_policy",
                         {"name": name, "arn": arn, "policy": json.dumps({"Version": "2012-10-17", "Statement": catalogue})}, None, ("delete",))
        attachment = change("aws_iam_role_policy_attachment.gateway_tools_catalogue_read[0]", "aws_iam_role_policy_attachment",
                            {"role": gateway, "policy_arn": arn}, None, ("delete",))
        plan = self.gateway_role_plan(managed)
        plan["resource_changes"].insert(1, attachment)
        result = policy.evaluate(plan, "gateway", "123456789012")
        self.assertEqual(result["routine"], [managed["address"], attachment["address"]])
        self.assertEqual(result["blocked"], [])
        # The attachment is only routine together with its policy's retirement.
        alone = self.gateway_role_plan(attachment)
        self.assertEqual(policy.evaluate(alone, "gateway", "123456789012")["blocked"], [attachment["address"]])
        # A narrower replacement blocks both.
        groups = self.gateway_role_groups()
        groups["agents"] = [s for s in groups["agents"] if s["Sid"] != "ToolsCatalogueReadTargets"]
        narrow = self.gateway_role_plan(managed, groups)
        narrow["resource_changes"].insert(1, attachment)
        self.assertEqual(policy.evaluate(narrow, "gateway", "123456789012")["blocked"], [managed["address"], attachment["address"]])
        # Accounts where Cognito was already unattached can retire the policy alone.
        lifecycle_name = "bedrockgw-dev-policy-gateway-cognito-lifecycle"
        lifecycle = change("aws_iam_policy.gateway_cognito_lifecycle[0]", "aws_iam_policy",
                           {"name": lifecycle_name, "arn": "arn:aws:iam::123456789012:policy/" + lifecycle_name,
                            "policy": json.dumps({"Version": "2012-10-17", "Statement": [
                                {"Sid": "CognitoIdentityLifecycle", "Effect": "Allow", "Action": ["cognito-idp:AdminCreateUser"],
                                 "Resource": "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_AbCdEfGhI"}]})},
                           None, ("delete",))
        self.assertEqual(policy.evaluate(self.gateway_role_plan(lifecycle), "gateway", "123456789012")["routine"], [lifecycle["address"]])
        other = copy.deepcopy(lifecycle)
        other["address"] = "aws_iam_policy.gateway_task_api[0]"
        other["change"]["before"]["name"] = "adp-dev-policy-gateway-task-api"
        other["change"]["before"]["arn"] = "arn:aws:iam::123456789012:policy/adp-dev-policy-gateway-task-api"
        self.assertEqual(policy.evaluate(self.gateway_role_plan(other), "gateway", "123456789012")["blocked"], [other["address"]])

    def cognito_retirement_plan(self):
        name = "bedrockgw-dev-policy-gateway-cognito-lifecycle"
        arn = "arn:aws:iam::123456789012:policy/" + name
        pool = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_AbCdEfGhI"
        statements = [
            {"Sid": "CognitoIdentityLifecycle", "Effect": "Allow", "Action": ["cognito-idp:AdminCreateUser"], "Resource": pool},
            {"Sid": "CognitoMachineClientLifecycle", "Effect": "Allow", "Action": ["cognito-idp:CreateUserPoolClient"], "Resource": pool},
            {"Sid": "CognitoCliLoginMint", "Effect": "Allow", "Action": ["cognito-idp:AdminInitiateAuth"], "Resource": pool},
        ]
        managed = change("aws_iam_policy.gateway_cognito_lifecycle[0]", "aws_iam_policy",
                         {"name": name, "arn": arn, "policy": json.dumps({"Version": "2012-10-17", "Statement": statements})},
                         None, ("delete",))
        attachment = change("aws_iam_role_policy_attachment.gateway_cognito_lifecycle[0]", "aws_iam_role_policy_attachment",
                            {"role": "adp-dev-role-gateway-service", "policy_arn": arn}, None, ("delete",))
        groups = self.gateway_role_groups()
        groups["identity"].extend(statements[1:])
        plan = self.gateway_role_plan(managed, groups)
        plan["resource_changes"].insert(1, attachment)
        for item in plan["resource_changes"]:
            item["change"]["after"] = copy.deepcopy(item["change"]["after"])
        return plan

    def test_cognito_attachment_retires_only_with_its_already_attached_superset(self):
        plan = self.cognito_retirement_plan()
        retired_addresses = [item["address"] for item in plan["resource_changes"][:2]]
        for module in ("gateway", "gateway-alb-wire", "gateway-final"):
            result = policy.evaluate(plan, module, "123456789012")
            self.assertEqual(result["routine"], retired_addresses)
            self.assertEqual(result["blocked"], [])
        # More actions are safe; only an already attached policy may supply them.
        updating = copy.deepcopy(plan)
        replacement = updating["resource_changes"][2]["change"]
        replacement["actions"] = ["update"]
        doc = json.loads(replacement["before"]["policy"])
        doc["Statement"][1]["Action"].append("cognito-idp:AdminDeleteUser")
        replacement["after"]["policy"] = json.dumps(doc)
        self.assertEqual(policy.evaluate(updating, "gateway", "123456789012")["routine"], retired_addresses)

    def test_cognito_retirement_refuses_unattached_or_changing_replacements(self):
        for mutation in ("policy creating", "attachment creating", "attachment missing", "wrong role", "wrong account",
                         "missing policy document", "action removed", "pool changed", "condition added", "sid changed",
                         "after scope changed"):
            plan = copy.deepcopy(self.cognito_retirement_plan())
            retired_addresses = [item["address"] for item in plan["resource_changes"][:2]]
            managed, attached = plan["resource_changes"][2:4]
            if mutation == "policy creating":
                managed["change"].update(actions=["create"], before=None)
            elif mutation == "attachment creating":
                attached["change"].update(actions=["create"], before=None)
            elif mutation == "attachment missing":
                plan["resource_changes"].remove(attached)
            elif mutation == "wrong role":
                attached["change"]["before"]["role"] = "adp-dev-role-other"
            elif mutation == "wrong account":
                managed["change"]["before"]["arn"] = managed["change"]["before"]["arn"].replace("123456789012", "111111111111")
                attached["change"]["before"]["policy_arn"] = managed["change"]["before"]["arn"]
            elif mutation == "missing policy document":
                managed["change"]["before"]["policy"] = None
            else:
                side = "after" if mutation == "after scope changed" else "before"
                managed["change"]["actions"] = ["update"]
                doc = json.loads(managed["change"][side]["policy"])
                lifecycle = doc["Statement"][1]
                if mutation == "action removed":
                    lifecycle["Action"] = []
                elif mutation in ("pool changed", "after scope changed"):
                    lifecycle["Resource"] = "*"
                elif mutation == "condition added":
                    lifecycle["Condition"] = {"StringEquals": {"aws:PrincipalTag/test": "required"}}
                elif mutation == "sid changed":
                    lifecycle["Sid"] = "ChangedLifecycle"
                managed["change"][side]["policy"] = json.dumps(doc)
            with self.subTest(mutation=mutation):
                self.assertEqual(policy.evaluate(plan, "gateway", "123456789012")["blocked"], retired_addresses)

    def test_cognito_attachment_gate_checks_the_exact_retired_pair(self):
        for mutation in ("policy missing", "policy retained", "policy replaced", "policy after present", "wrong policy type",
                         "wrong policy name", "wrong policy arn", "foreign account", "other role", "other attachment",
                         "attachment deposed", "policy deposed"):
            plan = self.cognito_retirement_plan()
            managed, attachment = plan["resource_changes"][:2]
            if mutation == "policy missing":
                plan["resource_changes"].remove(managed)
            elif mutation == "policy retained":
                managed["change"]["actions"] = ["no-op"]
            elif mutation == "policy replaced":
                managed["change"]["actions"] = ["delete", "create"]
            elif mutation == "policy after present":
                managed["change"]["after"] = managed["change"]["before"]
            elif mutation == "wrong policy type":
                managed["type"] = "aws_iam_role_policy"
            elif mutation == "wrong policy name":
                managed["change"]["before"]["name"] = "other"
            elif mutation == "wrong policy arn":
                attachment["change"]["before"]["policy_arn"] += "-other"
            elif mutation == "foreign account":
                arn = managed["change"]["before"]["arn"].replace("123456789012", "111111111111")
                managed["change"]["before"]["arn"] = attachment["change"]["before"]["policy_arn"] = arn
            elif mutation == "other role":
                attachment["change"]["before"]["role"] = "adp-dev-role-other"
            elif mutation == "other attachment":
                attachment["address"] = "aws_iam_role_policy_attachment.gateway_task_api[0]"
            elif mutation == "attachment deposed":
                attachment["deposed"] = "old"
            elif mutation == "policy deposed":
                managed["deposed"] = "old"
            with self.subTest(mutation=mutation):
                self.assertFalse(policy.gateway_role_policy_consolidation(attachment, plan, "123456789012"))

    def test_api_revision_is_routine_only_with_same_api_and_create_before_delete(self):
        r = change("module.api_gateway[0].aws_api_gateway_deployment.main", "aws_api_gateway_deployment",
                   {"rest_api_id": "existing"}, {"rest_api_id": "existing"}, ("create", "delete"))
        self.assertEqual(self.evaluate(r)["routine"], [r["address"]])
        for after, order in (("different", ["create", "delete"]), ("existing", ["delete", "create"]), (None, ["create", "delete"])):
            bad = copy.deepcopy(r)
            bad["change"].update(after={"rest_api_id": after}, actions=order)
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_release_layer_version_preserves_identity_and_uses_immutable_package(self):
        before = {"layer_name": "bedrockgw-dev-pyjwt-py313", "compatible_runtimes": ["python3.13"],
                  "compatible_architectures": ["x86_64"], "s3_bucket": "adp-terraform-state-123456789012", "s3_key": "lambda-layers/pyjwt-py313.zip"}
        after = dict(before, s3_key="adp-releases/sha256/" + "a" * 64 + "/pyjwt-py313.zip", skip_destroy=True)
        resource = change("module.lambda_authorizer[0].aws_lambda_layer_version.pyjwt", "aws_lambda_layer_version", before, after, ("create", "delete"))
        self.assertTrue(self.evaluate(resource)["routine"])
        for key, value in [("layer_name", "different"), ("s3_bucket", "other"), ("s3_key", "mutable.zip"),
                           ("compatible_runtimes", ["python3.12"]), ("compatible_architectures", ["arm64"]), ("skip_destroy", False)]:
            bad = copy.deepcopy(resource)
            bad["change"]["after"][key] = value
            with self.subTest(key=key):
                self.assertTrue(self.evaluate(bad)["blocked"])
        resource["change"]["actions"] = ["delete", "create"]
        self.assertTrue(self.evaluate(resource)["blocked"])

    def test_layer_build_trigger_can_change_only_within_the_same_bucket(self):
        triggers = {"build_script": "a" * 64, "layer_recipe": "b" * 64, "state_bucket": "adp-terraform-state-123456789012"}
        resource = change("null_resource.build_pyjwt_layer[0]", "null_resource", {"triggers": triggers}, {"triggers": dict(triggers, build_script="c" * 64)})
        self.assertTrue(self.evaluate(resource)["routine"])
        resource["change"]["after"]["triggers"]["state_bucket"] = "other-account"
        self.assertTrue(self.evaluate(resource)["blocked"])

    def test_layer_build_marker_accepts_create_before_delete_plan_shape(self):
        triggers = {"build_script": "a" * 64, "layer_recipe": "b" * 64,
                    "state_bucket": "adp-terraform-state-123456789012"}
        resource = change("null_resource.build_psycopg2_layer[0]", "null_resource",
                          {"triggers": triggers},
                          {"triggers": dict(triggers, build_script="c" * 64)},
                          ("create", "delete"))
        self.assertEqual(self.evaluate(resource)["routine"], [resource["address"]])

    def test_s3_permission_only_allows_source_account_tightening(self):
        before = {"function_name": "existing", "action": "lambda:InvokeFunction", "principal": "s3.amazonaws.com", "source_arn": "existing-bucket"}
        r = change("module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3", "aws_lambda_permission", before,
                   dict(before, source_account="123456789012"))
        self.assertTrue(self.evaluate(r)["routine"])
        for key, value in (("source_account", "other-account"), ("principal", "*"), ("source_arn", "other-bucket")):
            bad = copy.deepcopy(r)
            bad["change"]["after"][key] = value
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_s3_permission_with_unknown_source_account_stays_blocked(self):
        before = {"function_name": "existing", "action": "lambda:InvokeFunction",
                  "principal": "s3.amazonaws.com", "source_arn": "existing-bucket",
                  "source_account": "123456789012", "statement_id": "AllowS3Invoke"}
        resource = change("module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3",
                          "aws_lambda_permission", before,
                          {key: value for key, value in before.items() if key != "source_account"})
        resource["change"]["after_unknown"] = {"source_account": True}
        self.assertEqual(self.evaluate(resource)["blocked"], [resource["address"]])

    def test_known_worker_manifests_can_change_in_same_cluster(self):
        old = {"namespace": "adp-agents", "cluster_name": "existing", "cluster_region": "us-east-1", "manifest_sha": "old"}
        r = change("null_resource.keda_scaledjob", "null_resource", {"triggers": old},
                   {"triggers": dict(old, manifest_sha="new")}, ("create", "delete"))
        self.assertTrue(self.evaluate(r, "webhook-ingress")["routine"])
        for key in ("namespace", "cluster_name", "cluster_region"):
            bad = copy.deepcopy(r)
            bad["change"]["after"]["triggers"][key] = "different"
            self.assertTrue(self.evaluate(bad, "webhook-ingress")["blocked"])
        r["address"] = "null_resource.unreviewed"
        self.assertTrue(self.evaluate(r, "webhook-ingress")["blocked"])

    def test_nodepool_manifest_replacement_keeps_live_pool(self):
        old = {"cluster_name": "adp-dev-eks-cluster", "cluster_region": "us-east-1",
               "manifest_sha": "a" * 64}
        resource = change("null_resource.aggressive_packer_nodepool", "null_resource",
                          {"triggers": old}, {"triggers": dict(old, manifest_sha="b" * 64)})
        self.assertEqual(self.evaluate(resource, "platform")["routine"], [resource["address"]])
        mutations = {
            "unexpected order": lambda r: r["change"].update(actions=["create", "delete"]),
            "different cluster": lambda r: r["change"]["after"]["triggers"].update(cluster_name="adp-prod-eks-cluster"),
            "different region": lambda r: r["change"]["after"]["triggers"].update(cluster_region="us-west-2"),
            "unknown trigger": lambda r: r["change"]["after"]["triggers"].update(extra="value"),
            "invalid digest": lambda r: r["change"]["after"]["triggers"].update(manifest_sha="unknown"),
            "wrong kind": lambda r: r.update(type="aws_eks_nodegroup"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                bad = copy.deepcopy(resource)
                mutate(bad)
                self.assertEqual(self.evaluate(bad, "platform")["blocked"], [bad["address"]])

    def test_security_retirements_still_require_plan_review(self):
        for address, kind in (
            ("module.codebuild.aws_iam_role.codebuild", "aws_iam_role"),
            ("module.eks.aws_eks_access_entry.admins[\"arn:aws:iam::123456789012:role/adp-dev-agent-runner-role\"]", "aws_eks_access_entry"),
        ):
            with self.subTest(address=address):
                resource = change(address, kind, {"id": "existing"}, None, ("delete",))
                self.assertEqual(self.evaluate(resource, "platform")["blocked"], [address])

    def test_scaledjob_delete_first_and_unknown_target_remain_blocked(self):
        old = {"namespace": "adp-agents", "cluster_name": "existing", "cluster_region": "us-east-1", "manifest_sha": "old"}
        r = change("null_resource.keda_scaledjob", "null_resource", {"triggers": old},
                   {"triggers": dict(old, manifest_sha="new")})
        self.assertTrue(self.evaluate(r, "webhook-ingress")["blocked"])
        r["change"]["actions"] = ["create", "delete"]
        for key in old:
            bad = copy.deepcopy(r)
            del bad["change"]["after"]["triggers"][key]
            with self.subTest(key=key):
                self.assertTrue(self.evaluate(bad, "webhook-ingress")["blocked"])

    def gitlab_migration(self):
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/gitlab-webhook-secret-aB1234"
        secret = {"arn": arn, "name": "adp/dev/gitlab-webhook-secret"}
        return {"variables": {"environment": {"value": "dev"}}, "resource_changes": [
            change("aws_secretsmanager_secret_version.gitlab_webhook_secret[0]",
                   "aws_secretsmanager_secret_version", {"arn": arn, "secret_id": arn, "version_id": "terraform-example", "secret_string": "unchanged"}, None, ("forget",)),
            change("aws_secretsmanager_secret.gitlab_webhook_secret[0]", "aws_secretsmanager_secret",
                   secret, dict(secret), ("no-op",))]}

    def test_gitlab_version_forget_preserves_managed_secret(self):
        plan = self.gitlab_migration()
        self.assertEqual(policy.evaluate(plan, "webhook-ingress", "123456789012"),
                         {"routine": [plan["resource_changes"][0]["address"]], "blocked": [], "protected": []})

    def test_operator_version_forget_allows_only_retained_secret_metadata(self):
        for key, suffix in (
            ("github_app_id", "github-app/adp-agent-platform-id"),
            ("github_app_key", "github-app/adp-agent-platform-key"),
            ("marker_signing_key", "webhook-ingress/marker-signing-key"),
            ("webhook_secret", "webhook-ingress/github-webhook-secret"),
        ):
            with self.subTest(key=key):
                name = "adp/dev/" + suffix
                arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:" + name + "-aB1234"
                old = {"arn": arn, "name": name, "kms_key_id": "existing", "recovery_window_in_days": 7}
                new = dict(old, recovery_window_in_days=30)
                plan = {"variables": {"environment": {"value": "dev"}}, "resource_changes": [
                    change("aws_secretsmanager_secret_version." + key, "aws_secretsmanager_secret_version",
                           {"arn": arn, "secret_id": arn, "version_id": "terraform-example", "secret_string": "private"}, None, ("forget",)),
                    change("aws_secretsmanager_secret." + key, "aws_secretsmanager_secret", old, new, ("update",)),
                ]}
                self.assertEqual(policy.evaluate(plan, "webhook-ingress", "123456789012")["routine"],
                                 [plan["resource_changes"][0]["address"]])
                for mutation in (
                    lambda p: p["resource_changes"][1]["change"]["after"].update(kms_key_id="different"),
                    lambda p: p["resource_changes"][0]["change"].update(actions=["delete"]),
                    lambda p: p["variables"]["environment"].update(value="prod"),
                    lambda p: p["resource_changes"][0]["change"]["before"].pop("version_id"),
                ):
                    bad = copy.deepcopy(plan)
                    mutation(bad)
                    self.assertIn(bad["resource_changes"][0]["address"],
                                  policy.evaluate(bad, "webhook-ingress", "123456789012")["protected"])

    def test_worker_gateway_rollout_marker_requires_disabled_authority_and_same_target(self):
        old = {"configuration": "a" * 64, "marker_version": "disabled", "rollout_script": "b" * 64}
        resource = change("terraform_data.worker_gateway_rollout[0]", "terraform_data",
                          {"triggers_replace": old},
                          {"triggers_replace": dict(old, configuration="c" * 64)}, ("create", "delete"))
        variables = {k: {"value": v} for k, v in {
            "agent_authority_enabled": False, "environment": "dev", "eks_cluster_name": "adp-dev-eks-cluster",
            "gateway_namespace": "adp-gateway", "aws_region": "us-east-1"}.items()}
        plan = {"variables": variables, "resource_changes": [resource]}
        self.assertEqual(policy.evaluate(plan, "webhook-ingress", "123456789012")["routine"],
                         [resource["address"]])
        for mutation in (
            lambda p: p["variables"]["agent_authority_enabled"].update(value=True),
            lambda p: p["variables"]["eks_cluster_name"].update(value="other"),
            lambda p: p["resource_changes"][0]["change"]["after"]["triggers_replace"].update(marker_version="changed"),
            lambda p: p["resource_changes"][0]["change"]["after"]["triggers_replace"].update(configuration="a" * 64),
            lambda p: p["resource_changes"][0]["change"].update(actions=["delete", "create"]),
        ):
            bad = copy.deepcopy(plan)
            mutation(bad)
            self.assertIn(
                resource["address"],
                policy.evaluate(bad, "webhook-ingress", "123456789012")["blocked"],
            )

    def test_tainted_worker_rollout_recovery_retires_only_its_deposed_marker(self):
        address = "terraform_data.worker_gateway_rollout[0]"
        old = {
            "configuration": "a" * 64,
            "marker_version": "disabled",
            "rollout_script": "b" * 64,
        }
        current = change(
            address,
            "terraform_data",
            {"triggers_replace": old},
            {"triggers_replace": dict(old)},
            ("create", "delete"),
        )
        current["action_reason"] = "replace_because_tainted"
        deposed = change(
            address,
            "terraform_data",
            {"triggers_replace": dict(old, configuration="c" * 64)},
            None,
            ("delete",),
        )
        deposed["deposed"] = "c5aef7d6"
        variables = {
            k: {"value": v}
            for k, v in {
                "agent_authority_enabled": False,
                "environment": "dev",
                "eks_cluster_name": "adp-dev-eks-cluster",
                "gateway_namespace": "adp-gateway",
                "aws_region": "us-east-1",
            }.items()
        }
        plan = {"variables": variables, "resource_changes": [current, deposed]}
        self.assertEqual(
            policy.evaluate(plan, "webhook-ingress", "123456789012"),
            {"routine": [address, address], "blocked": [], "protected": []},
        )
        # A second timeout can leave another deposed state-only marker. The
        # reviewed #6729 timeout script also changes the marker's file hash.
        recovery = copy.deepcopy(plan)
        previous, extended = policy.WORKER_ROLLOUT_TIMEOUT_MIGRATION
        recovery["resource_changes"][0]["change"]["before"]["triggers_replace"]["rollout_script"] = previous
        recovery["resource_changes"][0]["change"]["after"]["triggers_replace"]["rollout_script"] = extended
        recovery["resource_changes"][1]["change"]["before"]["triggers_replace"]["rollout_script"] = previous
        second = copy.deepcopy(recovery["resource_changes"][1])
        second["deposed"] = "93fe14da"
        second["change"]["before"]["triggers_replace"]["configuration"] = "e" * 64
        recovery["resource_changes"].append(second)
        self.assertEqual(
            policy.evaluate(recovery, "webhook-ingress", "123456789012"),
            {"routine": [address] * 3, "blocked": [], "protected": []},
        )
        for mutate in (
            lambda p: p["resource_changes"][0]["change"]["after"]["triggers_replace"].update(rollout_script="f" * 64),
            lambda p: p["resource_changes"][2]["change"]["before"]["triggers_replace"].update(rollout_script="f" * 64),
            lambda p: p["resource_changes"][2].update(deposed="invalid"),
            lambda p: p["variables"]["agent_authority_enabled"].update(value=True),
        ):
            bad = copy.deepcopy(recovery)
            mutate(bad)
            self.assertTrue(policy.evaluate(bad, "webhook-ingress", "123456789012")["blocked"])
        for mutate in (
            lambda p: p["resource_changes"][0].pop("action_reason"),
            lambda p: p["resource_changes"][0]["change"]["after"][
                "triggers_replace"
            ].update(configuration="d" * 64),
            lambda p: p["resource_changes"][1].update(deposed="unknown"),
            lambda p: p["resource_changes"][1]["change"]["before"][
                "triggers_replace"
            ].update(rollout_script="d" * 64),
            lambda p: p["variables"]["agent_authority_enabled"].update(value=True),
        ):
            bad = copy.deepcopy(plan)
            mutate(bad)
            self.assertTrue(
                policy.evaluate(bad, "webhook-ingress", "123456789012")["blocked"]
            )
        self.assertIn(
            address,
            policy.evaluate(
                {"variables": variables, "resource_changes": [deposed]},
                "webhook-ingress",
                "123456789012",
            )["blocked"],
        )

    def test_gitlab_exception_never_accepts_other_credential_changes(self):
        mutations = {
            "delete version": lambda p: p["resource_changes"][0]["change"].update(actions=["delete"]),
            "replace version": lambda p: p["resource_changes"][0]["change"].update(actions=["delete", "create"]),
            "change value": lambda p: p["resource_changes"][0]["change"].update(actions=["update"], after={"secret_string": "replacement"}),
            "foreign secret id": lambda p: p["resource_changes"][0]["change"]["before"].update(secret_id="foreign"),
            "wrong version arn": lambda p: p["resource_changes"][0]["change"]["before"].update(arn="foreign"),
            "different credential": lambda p: p["resource_changes"][0].update(address="aws_secretsmanager_secret_version.github[0]"),
            "secret absent": lambda p: p["resource_changes"].pop(),
            "secret deleted": lambda p: p["resource_changes"][1]["change"].update(actions=["delete"], after=None),
            "secret forgotten": lambda p: p["resource_changes"][1]["change"].update(actions=["forget"], after=None),
            "secret updated": lambda p: p["resource_changes"][1]["change"].update(actions=["update"]),
            "secret replaced": lambda p: p["resource_changes"][1]["change"].update(actions=["create", "delete"]),
            "secret renamed": lambda p: p["resource_changes"][1]["change"]["after"].update(name="different"),
            "secret ambiguous": lambda p: p["resource_changes"].append(copy.deepcopy(p["resource_changes"][1])),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                plan = self.gitlab_migration()
                mutate(plan)
                self.assertIn(plan["resource_changes"][0]["address"],
                              policy.evaluate(plan, "webhook-ingress", "123456789012")["protected"])
        for module, account in [("gateway", "123456789012"), ("webhook-ingress", "999999999999"),
                                ("webhook-ingress", ""), ("webhook-ingress", ".*")]:
            with self.subTest(module=module, account=account):
                self.assertTrue(policy.evaluate(self.gitlab_migration(), module, account)["protected"])

    def test_empty_and_null_lambda_qualifiers_are_equivalent(self):
        before = {"function_name": "existing", "action": "lambda:InvokeFunction", "principal": "s3.amazonaws.com",
                  "source_arn": "existing-bucket", "qualifier": ""}
        after = dict(before, source_account="123456789012", qualifier=None)
        r = change("module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3", "aws_lambda_permission", before, after)
        self.assertTrue(self.evaluate(r)["routine"])
        for key, value in (("qualifier", "version-1"), ("principal_org_id", "different-org"),
                           ("function_url_auth_type", "NONE"), ("event_source_token", "different-token")):
            bad = copy.deepcopy(r)
            bad["change"]["after"][key] = value
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_stateful_delete_is_never_routine(self):
        for kind in ("aws_db_instance", "aws_s3_bucket", "aws_eks_access_entry", "aws_dynamodb_table"):
            r = change(kind + ".existing", kind, {"id": "existing"}, None, ("delete",))
            self.assertTrue(self.evaluate(r)["blocked"])

    def test_existing_credentials_cannot_be_reset_even_without_replacement(self):
        r = change("aws_secretsmanager_secret_version.github", "aws_secretsmanager_secret_version",
                   {"secret_id": "existing", "secret_string": "REAL-KEY"},
                   {"secret_id": "existing", "secret_string": "PLACEHOLDER"}, ("update",))
        self.assertTrue(self.evaluate(r)["protected"])

    def test_secret_metadata_updates_are_allowed(self):
        r = change("aws_secretsmanager_secret.github", "aws_secretsmanager_secret",
                   {"name": "existing", "description": "old"}, {"name": "existing", "description": "new"}, ("update",))
        self.assertFalse(self.evaluate(r)["protected"])

    def test_identity_table_and_installation_row_are_protected(self):
        table = change("aws_dynamodb_table.identity", "aws_dynamodb_table", {"name": "adp-dev-identity-index"}, None, ("delete",))
        row = change("aws_dynamodb_table_item.install", "aws_dynamodb_table_item", {"item": '{"identity_type":"github_installation_id"}'}, {"item": "{}"}, ("update",))
        for r in (table, row):
            self.assertTrue(self.evaluate(r)["protected"])


if __name__ == "__main__":
    unittest.main()
