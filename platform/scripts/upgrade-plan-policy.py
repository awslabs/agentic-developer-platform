#!/usr/bin/env python3
"""Allow narrowly defined deployment replacements; protect existing integrations."""
import importlib.util
import json
import re
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("actions", Path(__file__).with_name("plan-delete-actions.py"))
actions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(actions)

# The reviewed gateway rollout change in #6729 only extends its bounded wait.
# Keep this migration explicit: another script change must review the gate again.
WORKER_ROLLOUT_TIMEOUT_MIGRATION = (
    "c77563053075fc3ef8487094a1b99e12aba81d006eb37e5c281d7a5c5fb5165d",
    "a7445dc5de28a2001adb32c29d98e93cbf0dc97bbffa044aeb96bf86551bc5a6",
)


def agent_factory_retirement(resource, plan, account):
    """Retire only the runner grants removed by the reviewed automation split.

    The intake policy is a separate ordered cutover: the managed replacement
    must already be attached and contain every permission in the old inline
    policy. Any drift in a retired grant keeps the destroy gate closed.
    """
    change = resource["change"]
    before = change.get("before") or {}
    if change["actions"] != ["delete"] or change.get("after") is not None:
        return False
    variables = plan.get("variables", {})
    environment = variables.get("environment", {}).get("value")
    region = variables.get("aws_region", {}).get("value")
    runner = variables.get("runner_role_name", {}).get("value")
    if (environment not in ("dev", "staging", "prod")
            or not re.fullmatch(r"[0-9]{12}", account or "")
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region or "")
            or not re.fullmatch(rf"adp-{environment}-agent-(?:factory-)?runner-role", runner or "")):
        return False
    address = resource["address"]
    gateway_role = f"adp-{environment}-role-gateway-service"
    runner_arn = f"arn:aws:iam::{account}:role/{runner}"
    retained = next((item for item in plan["resource_changes"]
                     if item["address"] == "module.runner_iam.aws_iam_role.runner"), None)
    if retained is None or retained["change"]["actions"] not in (["no-op"], ["update"]):
        return False
    old_role = retained["change"].get("before") or {}
    new_role = retained["change"].get("after") or {}
    if any(old_role.get(key) != new_role.get(key) for key in ("name", "arn", "permissions_boundary")):
        return False
    if old_role.get("name") != runner or old_role.get("arn") != runner_arn:
        return False

    if address == "aws_eks_access_policy_association.runner_edit":
        allowed_namespaces = {"adp-agents", "adp-gateway", "adp-gateway-agents",
                              "agent-context", "arc-runners", "arc-systems", "keda"}
        scopes = before.get("access_scope") or []
        return (resource.get("type") == "aws_eks_access_policy_association"
                and before.get("cluster_name") == f"adp-{environment}-eks-cluster"
                and before.get("principal_arn") == runner_arn
                and before.get("policy_arn") == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
                and len(scopes) == 1 and scopes[0].get("type") == "namespace"
                and bool(scopes[0].get("namespaces"))
                and set(scopes[0]["namespaces"]) <= allowed_namespaces)

    policy_names = {
        "aws_iam_role_policy.gateway_intake_access[0]": (gateway_role, f"adp-{environment}-policy-gateway-intake"),
        "aws_iam_role_policy.runner_gateway_dynamodb": (runner, "gateway-dynamodb"),
        "aws_iam_role_policy.runner_gateway_sqs": (runner, "gateway-sqs"),
        "module.runner_iam.aws_iam_role_policy.bedrock_invocation_logging": (runner, "bedrock-invocation-logging-deploy"),
        "module.runner_iam.aws_iam_role_policy.runner_security_scan_upload[0]": (runner, "security-scan-s3-upload"),
    }
    if address not in policy_names or resource.get("type") != "aws_iam_role_policy":
        return False
    role, name = policy_names[address]
    if before.get("role") != role or before.get("name") != name or before.get("id") != f"{role}:{name}":
        return False
    try:
        document = json.loads(before["policy"])
    except (ValueError, TypeError, KeyError):
        return False
    if document.get("Version") != "2012-10-17":
        return False
    statements = document.get("Statement")
    if not isinstance(statements, list) or any(
            not isinstance(item, dict) or item.get("Effect") != "Allow" for item in statements):
        return False

    sessions = f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-agent-gateway-sessions"
    scan_bucket = f"arn:aws:s3:::adp-{environment}-security-scans-{account}"
    if address == "aws_iam_role_policy.gateway_intake_access[0]":
        policy_address = "aws_iam_policy.gateway_intake_access[0]"
        attachment_address = "aws_iam_role_policy_attachment.gateway_intake_access[0]"
        managed = next((item for item in plan["resource_changes"] if item["address"] == policy_address), None)
        attachment = next((item for item in plan["resource_changes"] if item["address"] == attachment_address), None)
        if (not managed or not attachment or managed["change"]["actions"] != ["no-op"]
                or attachment["change"]["actions"] != ["no-op"]):
            return False
        managed_before = managed["change"].get("before") or {}
        attached_before = attachment["change"].get("before") or {}
        if (managed_before.get("name") != name
                or managed_before.get("arn") != f"arn:aws:iam::{account}:policy/{name}"
                or attached_before.get("role") != gateway_role
                or attached_before.get("policy_arn") != managed_before["arn"]):
            return False
        try:
            replacement = json.loads(managed_before["policy"])
        except (ValueError, TypeError, KeyError):
            return False
        old_by_sid = {item.get("Sid"): item for item in statements}
        new_statements = replacement.get("Statement", [])
        if not isinstance(new_statements, list) or any(not isinstance(item, dict) for item in new_statements):
            return False
        new_by_sid = {item.get("Sid"): item for item in new_statements}
        if (set(old_by_sid) != {"IntakeSessionsRead", "IntakeDraftRead", "IntakeTablesKMSDecrypt", "IntakeDispatchInvoke"}
                or set(new_by_sid) != set(old_by_sid) | {"HostedTaskChatSessionsWrite"}
                or replacement.get("Version") != document["Version"]):
            return False
        for sid, old in old_by_sid.items():
            new = new_by_sid[sid]
            if (new.get("Effect") != "Allow" or new.get("Resource") != old.get("Resource")
                    or new.get("Condition") != old.get("Condition")
                    or not set(old.get("Action", [])) <= set(new.get("Action", []))):
                return False
        expected_actions = {
            "IntakeSessionsRead": {"dynamodb:GetItem", "dynamodb:Query"},
            "HostedTaskChatSessionsWrite": {"dynamodb:PutItem"},
            "IntakeDraftRead": {"dynamodb:GetItem"},
            "IntakeTablesKMSDecrypt": {"kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"},
            "IntakeDispatchInvoke": {"lambda:InvokeFunction"},
        }
        if any(set(new_by_sid[sid].get("Action", [])) != actions
               for sid, actions in expected_actions.items()):
            return False
        kms_resource = new_by_sid["IntakeTablesKMSDecrypt"].get("Resource") or []
        return (
            new_by_sid["IntakeSessionsRead"].get("Resource") == [sessions, f"{sessions}/index/*"]
            and new_by_sid["HostedTaskChatSessionsWrite"].get("Effect") == "Allow"
            and new_by_sid["HostedTaskChatSessionsWrite"].get("Resource") == [sessions]
            and new_by_sid["HostedTaskChatSessionsWrite"].get("Condition") == {
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["chat-*"]}}
            and new_by_sid["IntakeDraftRead"].get("Resource") == [
                f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-chat-context"]
            and len(kms_resource) == 1
            and bool(re.fullmatch(rf"arn:aws:kms:{region}:{account}:key/[0-9a-f-]{{36}}", kms_resource[0]))
            and new_by_sid["IntakeDispatchInvoke"].get("Resource") == [
                f"arn:aws:lambda:{region}:{account}:function:adp-{environment}-agent-gateway-ingest"]
        )

    if address == "aws_iam_role_policy.runner_gateway_sqs":
        return statements == [
            {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
             "Resource": f"arn:aws:sqs:{region}:{account}:adp-{environment}-agent-gateway-tasks"},
            {"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
             "Resource": f"arn:aws:sqs:{region}:{account}:adp-{environment}-agent-gateway-responses.fifo"},
        ]
    if address == "module.runner_iam.aws_iam_role_policy.runner_security_scan_upload[0]":
        return statements == [
            {"Sid": "SecurityScanUpload", "Effect": "Allow", "Action": ["s3:PutObject"],
             "Resource": [f"{scan_bucket}/sarif/*", f"{scan_bucket}/findings/*"]},
            {"Sid": "SecurityScanReadFindings", "Effect": "Allow", "Action": ["s3:GetObject"],
             "Resource": f"{scan_bucket}/findings/*"},
            {"Sid": "SecurityScanListFindings", "Effect": "Allow", "Action": ["s3:ListBucket"],
             "Condition": {"StringLike": {"s3:prefix": ["findings/*"]}}, "Resource": scan_bucket},
        ]
    if address == "aws_iam_role_policy.runner_gateway_dynamodb":
        if len(statements) != 2:
            return False
        kms_arn = (statements[1].get("Resource") or [None])[0]
        if not re.fullmatch(rf"arn:aws:kms:{region}:{account}:key/[0-9a-f-]{{36}}", kms_arn or ""):
            return False
        return statements == [
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query"],
             "Resource": [sessions, f"{sessions}/index/*"]},
            {"Sid": "DynamoDBKMSAccess", "Effect": "Allow",
             "Action": ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"], "Resource": [kms_arn]},
        ]
    if address == "module.runner_iam.aws_iam_role_policy.bedrock_invocation_logging":
        return statements == [
            {"Sid": "BedrockInvocationLogging", "Effect": "Allow", "Resource": "*",
             "Action": ["bedrock:GetModelInvocationLoggingConfiguration", "bedrock:PutModelInvocationLoggingConfiguration",
                        "bedrock:DeleteModelInvocationLoggingConfiguration", "logs:DescribeLogGroups"],
             "Condition": {"StringEquals": {"aws:RequestedRegion": region}}},
            {"Sid": "BedrockInvocationLogGroups", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:ListTagsForResource",
                        "logs:ListTagsLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
                        "logs:AssociateKmsKey", "logs:DisassociateKmsKey", "logs:TagLogGroup",
                        "logs:TagResource", "logs:UntagLogGroup", "logs:UntagResource"],
             "Resource": [f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock/adp-{environment}-agent/model-invocations",
                          f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock/adp-{environment}-agent/model-invocations:*"]},
            {"Sid": "BedrockLogBucketOwnership", "Effect": "Allow",
             "Action": ["s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls", "s3:DeleteBucketOwnershipControls"],
             "Resource": f"arn:aws:s3:::adp-{environment}-agent-bedrock-logs-{account}-{region}"},
        ]
    return False


def routine(resource, module, account, plan):
    change = resource["change"]
    before, after = change.get("before") or {}, change.get("after") or {}
    order = change["actions"]
    address = resource["address"]
    if module == "agent-factory" and agent_factory_retirement(resource, plan, account):
        return True
    if module == "platform" and address == "null_resource.aggressive_packer_nodepool":
        # This marker has no destroy provisioner. Its delete-first replacement
        # only retires Terraform state; the create step reapplies the manifest.
        # Keep this exception tied to the exact marker and target cluster.
        old, new = before.get("triggers", {}), after.get("triggers", {})
        keys = {"manifest_sha", "cluster_name", "cluster_region"}
        return (resource.get("type") == "null_resource"
                and order == ["delete", "create"]
                and set(old) == set(new) == keys
                and old["cluster_name"] == new["cluster_name"]
                and old["cluster_region"] == new["cluster_region"]
                and bool(re.fullmatch(r"adp-(?:dev|staging|prod)-eks-cluster", old["cluster_name"]))
                and bool(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", old["cluster_region"]))
                and all(re.fullmatch(r"[0-9a-f]{64}", value)
                        for value in (old["manifest_sha"], new["manifest_sha"])))
    if module in ("gateway", "gateway-alb-wire", "gateway-final"):
        if address in ("module.lambda_authorizer[0].aws_lambda_layer_version.pyjwt",
                       "module.budget_lambda[0].aws_lambda_layer_version.psycopg2"):
            # A release publishes a new immutable version of the same layer.
            # Retain release versions so recovery can reuse their packages.
            fixed = ("layer_name", "compatible_runtimes", "compatible_architectures", "s3_bucket")
            return (order == ["create", "delete"] and after.get("skip_destroy") is True
                    and all(before.get(k) and before[k] == after.get(k) for k in fixed)
                    and before.get("s3_bucket") == f"adp-terraform-state-{account}"
                    and bool(re.fullmatch(r"adp-releases/sha256/[0-9a-f]{64}/(?:pyjwt-py313|psycopg2-py312)\.zip", after.get("s3_key", ""))))
        if address in ("null_resource.build_pyjwt_layer[0]", "null_resource.build_psycopg2_layer[0]"):
            # These have create-time build provisioners only; no destroy action
            # against AWS. In release mode the helper validates staged packages.
            old, new = before.get("triggers", {}), after.get("triggers", {})
            # Terraform reports replacements as create/delete when the
            # null_resource lifecycle can create the new marker first. These
            # markers have no destroy provisioner, so either ordering is safe;
            # the exact trigger contract below remains the security boundary.
            return (order in (["delete", "create"], ["create", "delete"])
                    and set(old) == set(new) == {"build_script", "layer_recipe", "state_bucket"}
                    and old["state_bucket"] == new["state_bucket"] == f"adp-terraform-state-{account}"
                    and all(re.fullmatch(r"[0-9a-f]{64}", new[k]) for k in ("build_script", "layer_recipe")))
        if address == "module.api_gateway[0].aws_api_gateway_deployment.main":
            return (order == ["create", "delete"] and bool(before.get("rest_api_id"))
                    and before["rest_api_id"] == after.get("rest_api_id"))
        if address == "module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3":
            fixed = ("function_name", "action", "principal", "source_arn", "statement_id",
                     "principal_org_id", "event_source_token", "function_url_auth_type", "invoked_via_function_url")
            return (order in (["delete", "create"], ["create", "delete"])
                    and all(before.get(k) == after.get(k) for k in fixed)
                    # Older provider states store an omitted qualifier as "";
                    # newer plans use null. Both invoke the unqualified function.
                    and (before.get("qualifier") or None) == (after.get("qualifier") or None)
                    and before.get("principal") == "s3.amazonaws.com"
                    and before.get("action") == "lambda:InvokeFunction"
                    and bool(before.get("function_name")) and bool(before.get("source_arn"))
                    and not before.get("source_account") and after.get("source_account") == account
                    and bool(account))
    if module == "webhook-ingress" and address in (
            "null_resource.keda_scaledjob", "null_resource.agent_warm_pool", "null_resource.agent_image_prepull[0]"):
        old, new = before.get("triggers", {}), after.get("triggers", {})
        # The ScaledJob carrier uses create_before_destroy so Terraform skips
        # its destroy provisioner on replacement. The old delete/create order
        # could delete the live ScaledJob and is no longer a routine upgrade.
        expected = ["create", "delete"] if address == "null_resource.keda_scaledjob" else ["delete", "create"]
        return (
            order == expected
            and all(
                old.get(k) and old[k] == new.get(k)
                for k in ("namespace", "cluster_name", "cluster_region")
            )
            and set(old) == set(new)
            and set(new)
            <= {
                "namespace",
                "cluster_name",
                "cluster_region",
                "manifest_sha",
                "replicas",
            }
            and bool(old.get("manifest_sha"))
            and bool(new.get("manifest_sha"))
        )
    if (
        module == "webhook-ingress"
        and address == "terraform_data.worker_gateway_rollout[0]"
    ):
        # This carrier only runs the rollout script on create. The old marker
        # has no destroy provisioner, and protected authority stays disabled.
        # Repeated failed creates can leave a tainted marker and multiple
        # predecessors deposed. Retiring them only removes Terraform state.
        old, new = before.get("triggers_replace", {}), after.get("triggers_replace", {})
        variables = plan.get("variables", {})

        def value(name):
            return variables.get(name, {}).get("value")

        target_ok = (
            value("agent_authority_enabled") is False
            and value("environment") in ("dev", "staging", "prod")
            and value("eks_cluster_name") == f"adp-{value('environment')}-eks-cluster"
            and value("gateway_namespace") == "adp-gateway"
            and bool(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", value("aws_region") or ""))
            and bool(re.fullmatch(r"[0-9]{12}", account or ""))
        )
        keys = {"configuration", "marker_version", "rollout_script"}
        old_valid = (
            set(old) == keys
            and old.get("marker_version") == "disabled"
            and all(
                re.fullmatch(r"[0-9a-f]{64}", old[k])
                for k in ("configuration", "rollout_script")
            )
        )
        if not (resource.get("type") == "terraform_data" and target_ok and old_valid):
            return False
        if resource.get("deposed") is not None:
            current = [
                item
                for item in plan["resource_changes"]
                if item["address"] == address and item.get("deposed") is None
            ]
            return (
                bool(re.fullmatch(r"[0-9a-f]{8}", resource["deposed"]))
                and order == ["delete"]
                and change.get("after") is None
                and len(current) == 1
                and current[0].get("action_reason") == "replace_because_tainted"
                and routine(current[0], module, account, plan)
                and old["rollout_script"]
                == current[0]["change"]["before"]["triggers_replace"]["rollout_script"]
            )
        return (
            order == ["create", "delete"]
            and set(new) == keys
            and new.get("marker_version") == "disabled"
            and (
                old["rollout_script"] == new["rollout_script"]
                or (old["rollout_script"], new["rollout_script"])
                == WORKER_ROLLOUT_TIMEOUT_MIGRATION
            )
            and bool(re.fullmatch(r"[0-9a-f]{64}", new["configuration"]))
            and (
                (
                    old["configuration"] != new["configuration"]
                    and resource.get("action_reason") != "replace_because_tainted"
                )
                or (
                    resource.get("action_reason") == "replace_because_tainted"
                    and old["configuration"] == new["configuration"]
                )
            )
        )
    return False


def retained_operator_version(resource, plan, module, account):
    """Recognize operator-owned version migrations, never value deletion.

    The corresponding secret must remain managed with its identity and KMS key
    unchanged. Only recovery-window and tag metadata may change in this plan.
    """
    change = resource["change"]
    before = change.get("before") or {}
    names = {
        "github_app_id": "github-app/adp-agent-platform-id",
        "github_app_key": "github-app/adp-agent-platform-key",
        "marker_signing_key": "webhook-ingress/marker-signing-key",
        "webhook_secret": "webhook-ingress/github-webhook-secret",
        "gitlab_webhook_secret[0]": "gitlab-webhook-secret",
    }
    prefix = "aws_secretsmanager_secret_version."
    key = resource["address"][len(prefix):] if resource["address"].startswith(prefix) else ""
    environment = plan.get("variables", {}).get("environment", {}).get("value")
    if (module != "webhook-ingress"
            or key not in names or environment not in ("dev", "staging", "prod")
            or resource.get("type") != "aws_secretsmanager_secret_version"
            or change["actions"] != ["forget"] or change.get("after") is not None
            or not re.fullmatch(r"[0-9]{12}", account)):
        return False
    arn = before.get("secret_id", "")
    secret_name = f"adp/{environment}/{names[key]}"
    pattern = (r"arn:aws(?:-[a-z]+)*:secretsmanager:[a-z0-9-]+:" + account
               + r":secret:" + re.escape(secret_name) + r"-[A-Za-z0-9]{6}")
    if not re.fullmatch(pattern, arn) or before.get("arn") != arn or not before.get("version_id"):
        return False
    retained = [r for r in plan["resource_changes"]
                if r["address"] == "aws_secretsmanager_secret." + key
                and r.get("type") == "aws_secretsmanager_secret"]
    if len(retained) != 1:
        return False
    secret = retained[0]["change"]
    old, new = secret.get("before") or {}, secret.get("after") or {}
    permitted_metadata = {"recovery_window_in_days", "tags", "tags_all"}
    return (secret["actions"] in (["no-op"], ["update"])
            and (secret["actions"] != ["no-op"] or old == new)
            and (secret["actions"] == ["no-op"] or any(
                old.get(field) != new.get(field) for field in permitted_metadata))
            and old.get("arn") == new.get("arn") == arn
            and old.get("name") == new.get("name") == secret_name
            and all(old.get(field) == new.get(field)
                    for field in set(old) | set(new) if field not in permitted_metadata))


def protected_change(resource):
    change = resource["change"]
    before, after = change.get("before"), change.get("after")
    if not before or change["actions"] in (["no-op"], ["read"]):
        return False
    kind = resource.get("type", "")
    if kind in ("aws_secretsmanager_secret", "aws_secretsmanager_secret_version"):
        keys = ("name", "secret_id", "secret_string", "secret_binary")
        return after is None or any(before.get(k) != after.get(k) for k in keys)
    if kind == "aws_dynamodb_table_item":
        item = before.get("item", "")
        if any(key in item for key in ("github_installation_id", "org_installation")):
            return before.get("item") != (after or {}).get("item")
    if kind == "aws_dynamodb_table" and "identity" in before.get("name", ""):
        return "delete" in change["actions"] or "forget" in change["actions"]
    return False


def evaluate(plan, module, account):
    actions.deletions(plan)
    allowed, blocked, protected = [], [], []
    for resource in plan["resource_changes"]:
        retained_version = retained_operator_version(resource, plan, module, account)
        if protected_change(resource) and not retained_version:
            protected.append(resource["address"])
        if set(resource["change"]["actions"]) & {"delete", "forget"}:
            (allowed if retained_version or routine(resource, module, account, plan) else blocked).append(resource["address"])
    return {"routine": allowed, "blocked": blocked, "protected": protected}


if __name__ == "__main__":
    try:
        result = evaluate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], sys.argv[3])
        if result["protected"]:
            sys.exit("Upgrade would change existing credentials or installation mappings: " + ", ".join(result["protected"]))
        for address in result["routine"]:
            print("Routine deployment replacement: " + address, file=sys.stderr)
        print("\n".join(result["blocked"]))
    except (ValueError, KeyError, TypeError, OSError, IndexError):
        sys.exit("Cannot validate Terraform upgrade plan")
