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


def routine(resource, module, account, plan):
    change = resource["change"]
    before, after = change.get("before") or {}, change.get("after") or {}
    order = change["actions"]
    address = resource["address"]
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
        # A failed create can leave its marker tainted and a predecessor deposed.
        # Recreating reruns the same rollout; retiring the predecessor only
        # removes Terraform state.
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
                and current[0]["change"]["before"]["triggers_replace"]
                == current[0]["change"]["after"]["triggers_replace"]
                and routine(current[0], module, account, plan)
                and old["rollout_script"]
                == current[0]["change"]["after"]["triggers_replace"]["rollout_script"]
            )
        return (
            order == ["create", "delete"]
            and set(new) == keys
            and new.get("marker_version") == "disabled"
            and old["rollout_script"] == new["rollout_script"]
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
