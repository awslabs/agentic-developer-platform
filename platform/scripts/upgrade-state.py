#!/usr/bin/env python3
"""Discover an existing deployment and retain its account-specific configuration.

Uses AWS CLI credentials (including credential_process). Snapshots stay in a
private run directory, never in a build archive or a committed tfvars file.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request


def aws(*args):
    proc = subprocess.run(["aws", *args, "--output", "json"], text=True, capture_output=True)
    if proc.returncode:
        # No secret values or CLI input payloads in diagnostics.
        raise RuntimeError(f"AWS {args[0]} {args[1]} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def resources(state, kind=None):
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed" or (kind and resource["type"] != kind):
            continue
        for instance in resource.get("instances", []):
            yield resource, instance["attributes"]


def output(state, name, default=None):
    return state.get("outputs", {}).get(name, {}).get("value", default)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def preserve_access(state, cluster, extra=(), requested_cidrs=()):
    # Only retain entries already owned as cluster admins by platform Terraform.
    # Listing every EKS principal here would promote namespace-scoped users.
    admins = [a["principal_arn"] for r, a in resources(state, "aws_eks_access_entry")
              if r.get("module") == "module.eks" and r["name"] == "admins"]
    cidrs = cluster["resourcesVpcConfig"].get("publicAccessCidrs", [])
    for cidr in [*cidrs, *requested_cidrs]:
        ipaddress.ip_network(cidr)
    return {"extra_cluster_admin_principal_arns": sorted(set(admins) | set(extra)),
            "eks_public_access_cidrs": sorted(set(cidrs) | set(requested_cidrs)),
            "eks_endpoint_public_access": cluster["resourcesVpcConfig"].get("endpointPublicAccess", True),
            "eks_endpoint_private_access": cluster["resourcesVpcConfig"].get("endpointPrivateAccess", True)}


def repository_encryption(state):
    # ECR encryption is immutable. Older repositories can use AWS-managed KMS
    # keys or AES256; selecting the new module key would replace their images.
    result = {}
    for resource, attrs in resources(state, "aws_ecr_repository"):
        if resource.get("module") != "module.ecr" or resource["name"] != "main":
            continue
        configuration = attrs.get("encryption_configuration", [])
        if len(configuration) != 1:
            raise ValueError("Cannot recover existing ECR repository encryption")
        encryption = configuration[0]
        kind, key = encryption.get("encryption_type"), encryption.get("kms_key")
        if kind not in ("AES256", "KMS", "KMS_DSSE") or (kind != "AES256" and not key):
            raise ValueError("Cannot recover existing ECR repository encryption key")
        result[attrs["name"]] = {"encryption_type": kind, "kms_key": key if kind != "AES256" else None}
    return result


def broker_settings(variables):
    mapping = {"ALLOWLIST_MODE": "github_auth_allowlist_mode",
               "ALLOWED_ORGS": "github_auth_allowed_orgs",
               "GITHUB_TOKEN_SECRET_ARN": "github_auth_token_secret_arn"}
    result = {dest: variables[key] for key, dest in mapping.items() if key in variables}
    # Older brokers did not have the explicit open-signup opt-in variable.
    result["github_auth_allow_open_signup"] = variables.get(
        "ALLOW_OPEN_SIGNUP", str(variables.get("ALLOWLIST_MODE") == "open").lower()) == "true"
    return result


def gateway_engine_settings(gateway_state, webhook_state, account, region, environment):
    """Retain an already-wired tick without enabling an unconfigured engine.

    Match its existing selectors to the owning webhook state. Missing ownership
    evidence refuses preparation rather than clearing live selectors or guessing
    queue/table permissions. Preserve the existing tenant-secret scope exactly.
    """
    module = "module.orchestration_tick[0]"
    ticks = [a for r, a in resources(gateway_state, "aws_lambda_function")
             if r.get("module") == module and r["name"] == "tick"]
    if not ticks:
        return {}
    if len(ticks) != 1:
        raise ValueError("Ambiguous existing orchestration tick")
    variables = (ticks[0].get("environment") or [{}])[0].get("variables") or {}
    if not isinstance(variables, dict):
        raise ValueError("Cannot recover existing orchestration tick environment")
    result = {}
    partitions = set()

    def owned(kind, selector, key):
        matches = [a for _, a in resources(webhook_state, kind) if a.get(key) == selector]
        if len(matches) != 1:
            raise ValueError("Cannot recover existing orchestration resource ownership")
        attrs = matches[0]
        arn = attrs.get("arn", "")
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[0] != "arn" or parts[3:5] != [region, account]:
            raise ValueError("Existing orchestration resource belongs to another target")
        partitions.add(parts[1])
        if len(partitions) != 1:
            raise ValueError("Existing orchestration resources belong to different partitions")
        return attrs, parts[1]

    queue = variables.get("BG_ORCH_DISPATCH_QUEUE_URL", "")
    if queue:
        attrs, _ = owned("aws_sqs_queue", queue, "url")
        result.update(orchestration_dispatch_queue_url=queue,
                      orchestration_dispatch_queue_arn=attrs["arn"])
    table = variables.get("WEBHOOK_EVENTS_TABLE", "")
    if table:
        attrs, _ = owned("aws_dynamodb_table", table, "name")
        encryption = attrs.get("server_side_encryption") or []
        if len(encryption) != 1 or not encryption[0].get("kms_key_arn"):
            raise ValueError("Cannot recover existing orchestration table encryption")
        result.update(orchestration_webhook_events_table=table,
                      orchestration_webhook_events_kms_key_arn=encryption[0]["kms_key_arn"])

    # The optional acknowledgement grant is independent of queue/table wiring.
    # An observed policy with no grant means it was never enabled; a missing
    # policy for a wired tick cannot establish that absence safely.
    policies = [a for r, a in resources(gateway_state, "aws_iam_role_policy")
                if r.get("module") == module and r["name"] == "tick"]
    if not policies and not result:
        return result
    if len(policies) != 1:
        raise ValueError("Cannot recover existing orchestration tenant-secret scope policy")
    document = json.loads(policies[0]["policy"])
    if not isinstance(document, dict):
        raise ValueError("Cannot recover existing orchestration tenant-secret scope policy")
    statements = document.get("Statement", [])
    statements = [statements] if isinstance(statements, dict) else statements
    if not isinstance(statements, list) or any(not isinstance(statement, dict) for statement in statements):
        raise ValueError("Cannot recover existing orchestration tenant-secret scope policy")
    scopes = []
    for statement in statements:
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
            raise ValueError("Cannot recover existing orchestration tenant-secret scope policy")
        if not any(action == "*" or action.lower().startswith("secretsmanager:")
                   or "*" in action.partition(":")[0] or "?" in action.partition(":")[0]
                   for action in actions) and "NotAction" not in statement:
            continue
        # Terraform exposes one unconditional GetSecretValue resource string.
        # Refuse any policy shape that would lose permissions or restrictions
        # when represented by that input, including conditions and deny rules.
        if (statement.get("Effect") != "Allow" or actions != ["secretsmanager:GetSecretValue"]
                or any(key in statement for key in ("Condition", "NotAction", "NotResource"))):
            raise ValueError("Cannot preserve existing orchestration tenant-secret scope policy")
        values = statement.get("Resource", [])
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or not values:
            raise ValueError("Cannot recover existing orchestration tenant-secret scope policy")
        scopes.extend(values)
    if not scopes:
        return result
    if len(scopes) != 1 or not isinstance(scopes[0], str):
        raise ValueError("Cannot represent existing orchestration tenant-secret scope exactly")
    scope = scopes[0]
    parts = scope.split(":", 5)
    if (len(parts) != 6 or parts[0] != "arn" or not re.fullmatch(r"aws(?:-[a-z0-9]+)*", parts[1]) or parts[2] != "secretsmanager"
            or parts[3:5] != [region, account] or (partitions and parts[1] not in partitions)
            or not parts[5].startswith(f"secret:adp/{environment}/tenants/")
            or parts[5] == f"secret:adp/{environment}/tenants/"):
        raise ValueError("Existing orchestration tenant-secret scope is outside the target")
    result["orchestration_github_app_secret_arn_pattern"] = scope
    return result


def factory_settings(state):
    result = {"enable_github_apps": False, "seed_agent_registry": False}
    configured_org = output(state, "github_org")
    if configured_org is not None:
        result["github_org"] = configured_org
    prefix = output(state, "secrets_prefix", "")
    match = re.match(r"adp/([^/]+)/gh-app", prefix)
    if match:
        result["github_org"] = match[1]
    for resource, attrs in resources(state):
        if (resource["type"] == "aws_iam_role" and resource["name"] == "runner"
                and resource.get("module") == "module.runner_iam"):
            result["runner_role_name"] = attrs["name"]
        if resource["type"] == "aws_dynamodb_table_item" and resource["name"] == "scaledjob_worker_agent":
            result["seed_agent_registry"] = True
        if resource["type"] == "kubernetes_secret" and resource.get("module", "").startswith("module.arc_runner"):
            data = attrs.get("data", {})
            installation = data.get("github_app_installation_id")
            if not installation:
                raise ValueError("Existing ARC secret has no installation ID; refusing to reset it")
            result.update(enable_github_apps=True, github_app_dev_installation_id=installation,
                          runner_namespace=attrs["metadata"][0]["namespace"])
        if resource["type"] == "helm_release" and resource["name"] == "arc_runner_set":
            # Helm records the final chart values as JSON in metadata.
            metadata = attrs.get("metadata", [])
            if isinstance(metadata, list):
                metadata = metadata[0] if metadata else {}
            values = json.loads(metadata.get("values", "{}"))
            url = values.get("githubConfigUrl", "")
            match = re.fullmatch(r"https://github.com/([^/]+)(?:/([^/]+))?/?", url)
            if not match:
                raise ValueError("Cannot recover existing ARC GitHub URL; refusing default organization")
            result.update(github_org=match[1], github_repo=match[2] or "")
    if result["enable_github_apps"] and not result.get("github_org"):
        raise ValueError("Cannot recover existing GitHub organization")
    return result


def integration_snapshot(states, environment):
    prefixes = (f"adp/{environment}/",)
    secrets = {}
    for item in aws("secretsmanager", "list-secrets")["SecretList"]:
        name = item["Name"]
        # Include legacy org-owned apps and per-tenant apps, without reading values.
        relevant = (name.startswith(prefixes) and any(k in name for k in ("github", "webhook", "oauth"))) or "/gh-app-" in name
        if relevant:
            meta = aws("secretsmanager", "describe-secret", "--secret-id", item["ARN"])
            secrets[item["ARN"]] = sorted(v for v, stages in meta.get("VersionIdsToStages", {}).items() if "AWSCURRENT" in stages)
    mappings = {}
    for state in states.values():
        for _, attrs in resources(state, "aws_dynamodb_table"):
            if attrs.get("hash_key") != "identity_type" or "identity-index" not in attrs["name"]:
                continue
            rows = []
            for kind in ("github_installation_id", "org_installation"):
                data = aws("dynamodb", "query", "--table-name", attrs["name"], "--consistent-read",
                           "--key-condition-expression", "identity_type = :kind",
                           "--expression-attribute-values", json.dumps({":kind": {"S": kind}}))
                for row in data.get("Items", []):
                    rows.append({k: v for k, v in row.items() if k in (
                        "identity_type", "identity_value", "org_id", "tenant_id", "installation_id", "app_id")})
            mappings[attrs["name"]] = rows
    return {"secrets": secrets, "mappings": mappings}


def assert_integrations_preserved(before, after):
    for arn, versions in before["secrets"].items():
        if after["secrets"].get(arn) != versions:
            raise ValueError(f"GitHub credential version changed or disappeared: {arn}")
    for table, rows in before["mappings"].items():
        for row in rows:
            if not any(all(candidate.get(k) == v for k, v in row.items()) for candidate in after["mappings"].get(table, [])):
                raise ValueError(f"Existing GitHub installation mapping changed in {table}")


def prepare(args):
    directory = Path(args.directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    identity = aws("sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise ValueError("Target account differs from caller identity")
    bucket = f"adp-terraform-state-{args.account}"
    keys = {x["Key"] for x in aws("s3api", "list-objects-v2", "--bucket", bucket,
                                   "--prefix", args.environment + "/").get("Contents", [])}
    states = {}
    for name in ("platform", "gateway", "webhook-ingress", "agent-factory", "agent-context"):
        key = f"{args.environment}/" + ("platform" if name == "platform" else "modules/" + name) + "/terraform.tfstate"
        if key not in keys:
            continue
        path = directory / f"{name}-before.tfstate"
        aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(path))
        path.chmod(0o600)
        state = json.loads(path.read_text())
        if list(resources(state)):
            states[name] = state
    if "platform" not in states:
        raise ValueError("--update requires existing platform state")
    cluster_name = output(states["platform"], "eks_cluster_name")
    if cluster_name != f"adp-{args.environment}-eks-cluster":
        raise ValueError("Platform state names an unexpected EKS cluster")
    cluster = aws("eks", "describe-cluster", "--name", cluster_name)["cluster"]
    if cluster["status"] != "ACTIVE":
        raise ValueError("--update requires an ACTIVE EKS cluster")
    requested = json.loads(os.environ.get("TF_VAR_eks_public_access_cidrs", "[]"))
    if cluster["resourcesVpcConfig"].get("endpointPublicAccess"):
        if not requested:
            with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as response:
                address = ipaddress.ip_address(response.read().decode().strip())
            requested = [str(address) + ("/32" if address.version == 4 else "/128")]
    platform = preserve_access(states["platform"], cluster,
                               json.loads(os.environ.get("TF_VAR_extra_cluster_admin_principal_arns", "[]")), requested)
    platform.update(environment=args.environment, aws_region=args.region)
    platform["ecr_repository_encryption"] = repository_encryption(states["platform"])
    platform["retained_upgrade_kms_key_ids"] = [a["id"] for r, a in resources(states["platform"], "aws_kms_key")
                                               if not r.get("module") and r["name"] == "retained_upgrade"]
    write_json(directory / "platform.tfvars.json", platform)
    write_json(directory / "eks-access.json", {"publicAccessCidrs": platform["eks_public_access_cidrs"]})
    gateway = {"environment": args.environment, "aws_region": args.region}
    gateway_state = states.get("gateway", {})
    gateway.update(gateway_engine_settings(gateway_state, states.get("webhook-ingress", {}),
                                           args.account, args.region, args.environment))
    brokers = [a for r, a in resources(gateway_state, "aws_lambda_function") if "github_auth_broker" in r.get("module", "")]
    gateway["enable_github_auth_broker"] = bool(brokers)
    broker_before = {}
    if brokers:
        broker_before = aws("lambda", "get-function-configuration", "--function-name", brokers[0]["function_name"])["Environment"]["Variables"]
        gateway.update(broker_settings(broker_before))
    # Retain customer CloudFront hostnames and their certificates.
    for resource, attrs in resources(gateway_state):
        if resource["type"] == "aws_cloudfront_vpc_origin" and "gitlab" in resource["name"]:
            gateway["gitlab_origin_arn"] = attrs["vpc_origin_endpoint_config"][0]["arn"]
        if resource["type"] != "aws_cloudfront_distribution":
            continue
        for origin in attrs.get("origin", []):
            if "gitlab" in origin.get("origin_id", "").lower():
                gateway["gitlab_origin_dns"] = origin["domain_name"]
        aliases = attrs.get("aliases", [])
        if len(aliases) > 1:
            raise ValueError("Multiple CloudFront aliases require explicit environment configuration")
        if aliases:
            gateway["frontend_domain_name"] = aliases[0]
            gateway["frontend_acm_certificate_arn"] = attrs["viewer_certificate"][0]["acm_certificate_arn"]
    write_json(directory / "gateway.tfvars.json", gateway)
    factory = factory_settings(states.get("agent-factory", {}))
    if "agent-factory" in states and "github_org" not in factory:
        raise ValueError("Existing factory state has no organization; provide its original configuration before upgrading")
    factory.update(environment=args.environment, aws_region=args.region, gateway_deployed="gateway" in states,
                   enable_agent_context_rbac="agent-context" in states)
    write_json(directory / "agent-factory.tfvars.json", factory)
    write_json(directory / "agent-context.tfvars.json", {"environment": args.environment, "aws_region": args.region})
    webhook = {"environment": args.environment, "aws_region": args.region, "eks_cluster_name": cluster_name}
    ws = states.get("webhook-ingress", {})
    webhook["gitlab_webhook_enabled"] = any("gitlab" in r["name"] for r, _ in resources(ws, "aws_lambda_function"))
    webhook["enable_adversarial_e2e"] = any(r["name"] == "adversarial_evidence" for r, _ in resources(ws, "aws_s3_bucket"))
    for r, attrs in resources(ws, "aws_lambda_function"):
        if r["name"] == "github_webhook":
            variables = aws("lambda", "get-function-configuration", "--function-name", attrs["function_name"])["Environment"]["Variables"]
            for key, var in (("INTERNAL_API_KEY_ARN", "internal_api_key_arn"), ("GATEWAY_API_URL", "gateway_api_url")):
                if variables.get(key):
                    webhook[var] = variables[key]
            for key, var in (("ORG_TENANT_AUTO_CREATE", "org_tenant_auto_create"),
                             ("REQUIRE_SIGNED_PROVENANCE", "require_signed_provenance")):
                if key in variables:
                    webhook[var] = variables[key].lower() == "true"
            webhook["identity_index_table_name"] = variables["IDENTITY_INDEX_TABLE"]
    write_json(directory / "webhook-ingress.tfvars.json", webhook)
    snapshot = integration_snapshot(states, args.environment)
    snapshot.update(account=args.account, environment=args.environment, modules=list(states),
                    outputs={name: {key: value["value"] for key, value in state.get("outputs", {}).items()
                             if key in ("webhook_url", "github_oauth_callback_url", "github_sign_in_url", "gateway_ws_endpoint")}
                             for name, state in states.items()},
                    broker_function=brokers[0]["function_name"] if brokers else None,
                    broker_settings={k: v for k, v in broker_before.items() if k in (
                        "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET_ARN", "CALLBACK_URL", "FRONTEND_URL",
                        "ALLOWLIST_MODE", "ALLOWED_ORGS", "ALLOW_OPEN_SIGNUP", "GITHUB_TOKEN_SECRET_ARN")})
    write_json(directory / "integration-before.json", snapshot)
    old_cidrs = set(cluster["resourcesVpcConfig"].get("publicAccessCidrs", []))
    env = {"UPGRADE_MODULES": ",".join(states), "UPGRADE_BROKER_ENABLED": str(bool(brokers)).lower(),
           "UPGRADE_NEEDS_EKS_ACCESS": str(cluster["resourcesVpcConfig"].get("endpointPublicAccess", False)
               and set(platform["eks_public_access_cidrs"]) != old_cidrs).lower()}
    (directory / "context.env").write_text("\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items()) + "\n")
    print("Discovered existing modules: " + ", ".join(states))
    print(f"Saved GitHub preservation snapshot ({len(snapshot['secrets'])} secrets, "
          f"{sum(map(len, snapshot['mappings'].values()))} installation rows); secret values were not read")


def open_access(args):
    directory = Path(args.directory)
    before = json.loads((directory / "integration-before.json").read_text())
    if aws("sts", "get-caller-identity")["Account"] != before["account"]:
        raise ValueError("EKS access update account differs from the snapshot")
    cluster = f"adp-{before['environment']}-eks-cluster"
    result = aws("eks", "update-cluster-config", "--name", cluster,
                 "--resources-vpc-config", "file://" + str(directory / "eks-access.json"))
    update_id = result["update"]["id"]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        update = aws("eks", "describe-update", "--name", cluster, "--update-id", update_id)["update"]
        if update["status"] == "Successful":
            print("EKS access update completed; existing CIDRs retained")
            return
        if update["status"] in ("Failed", "Cancelled"):
            raise ValueError(f"EKS access update {update['status']}")
        print("Waiting for EKS access update...", flush=True)
        time.sleep(10)
    raise ValueError("Timed out waiting for EKS access update")


def prepare_factory(args):
    """Prepare an additive factory install when an older deployment omitted it."""
    directory = Path(args.directory)
    before = json.loads((directory / "integration-before.json").read_text())
    if aws("sts", "get-caller-identity")["Account"] != before["account"]:
        raise ValueError("Factory installation account differs from upgrade account")
    target = directory / "agent-factory.tfvars.json"
    settings = json.loads(target.read_text())
    if not settings.get("runner_role_name"):
        # A legacy CodeBuild role can occupy the IRSA role's original name.
        # Never import or repurpose an unowned role, even on a partial retry.
        names = {role["RoleName"] for role in aws("iam", "list-roles")["Roles"]}
        prefix = f"adp-{settings['environment']}-agent"
        available = next((name for name in (f"{prefix}-runner-role", f"{prefix}-factory-runner-role")
                          if name not in names), None)
        if not available:
            raise ValueError("Both factory runner role names are occupied outside factory state; refusing to adopt them")
        settings["runner_role_name"] = available
        print(f"Factory runner will use a new dedicated IAM role: {available}")
    if "agent-factory" in before["modules"]:
        write_json(target, settings)
        return  # prepare already recovered the existing integration settings.
    missing = {"gateway", "webhook-ingress"} - set(before["modules"])
    if missing:
        raise ValueError("Required agent-factory installation needs existing " + ", ".join(sorted(missing)) +
                         "; restore those prerequisites before upgrading")
    # Recover the legacy secret namespace from the existing environment, never
    # from the repository's platform-account terraform.tfvars. This does not
    # register an App or enable ARC; existing tenant Apps remain untouched.
    allowed = before.get("broker_settings", {}).get("ALLOWED_ORGS", "").strip()
    valid_org = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    if args.github_org and not re.fullmatch(valid_org, args.github_org):
        raise ValueError("ADP_GITHUB_ORG must name a single valid GitHub organization")
    org = args.github_org or (allowed if re.fullmatch(valid_org, allowed) else "")
    settings.update(github_org=org, github_repo="", github_app_dev_installation_id="",
                    enable_github_apps=False, seed_agent_registry=False,
                    runner_namespace="arc-runners", gateway_deployed=True)
    write_json(target, settings)
    print("Required agent-factory is missing; the upgrade will install it through the saved-plan gate")


def verify(args):
    directory = Path(args.directory)
    before = json.loads((directory / "integration-before.json").read_text())
    if aws("sts", "get-caller-identity")["Account"] != before["account"]:
        raise ValueError("Verification account differs from upgrade account")
    states = {name: json.loads((directory / f"{name}-before.tfstate").read_text()) for name in before["modules"]}
    required = set(getattr(args, "require_module", []))
    modules = set(before.get("outputs", {})) | required
    for name in sorted(modules):
        expected = before.get("outputs", {}).get(name, {})
        key = f"{before['environment']}/" + ("platform" if name == "platform" else "modules/" + name) + "/terraform.tfstate"
        target = directory / f"{name}-after.tfstate"
        aws("s3api", "get-object", "--bucket", f"adp-terraform-state-{before['account']}", "--key", key, str(target))
        target.chmod(0o600)
        current = json.loads(target.read_text())
        if name in required and not list(resources(current)):
            raise ValueError(f"Required module {name} has no deployed resources")
        for key, value in expected.items():
            if output(current, key) != value:
                raise ValueError(f"Existing integration endpoint changed: {name}/{key}")
        states[name] = current
    after = integration_snapshot(states, before["environment"])
    assert_integrations_preserved(before, after)
    if before["broker_function"]:
        variables = aws("lambda", "get-function-configuration", "--function-name", before["broker_function"])["Environment"]["Variables"]
        for key, value in before["broker_settings"].items():
            if variables.get(key) != value:
                raise ValueError(f"Existing GitHub broker setting changed: {key}")
    write_json(directory / "integration-verification.json", {"preserved": True, "secret_count": len(before["secrets"]),
               "installation_rows": sum(map(len, before["mappings"].values()))})
    write_json(directory / "module-verification.json", {"required": sorted(required), "verified": sorted(modules)})
    print("Existing GitHub credentials, installation mappings and broker settings preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify", "open-access", "prepare-factory"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--account")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--github-org", default="")
    parser.add_argument("--require-module", action="append", default=[],
                        choices=("platform", "gateway", "webhook-ingress", "agent-factory", "agent-context"))
    args = parser.parse_args()
    os.environ["AWS_REGION"] = args.region
    os.environ["AWS_DEFAULT_REGION"] = args.region
    {"prepare": prepare, "verify": verify, "open-access": open_access, "prepare-factory": prepare_factory}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        sys.exit(str(exc))
