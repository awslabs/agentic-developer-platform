"""Read deployed ingress through the selected AWS connection, never an HTTP claim."""

import fnmatch
import json
import re
import subprocess
import time


def validate_policy_scope(manifest):
    scope = manifest.get("ingress_policy", {})
    if not isinstance(scope, dict):
        raise ValueError("private ingress policy scope unavailable")
    role = scope.get("role_arn", "")
    account = scope.get("account_id", "")
    path = scope.get("launch_path", "")
    functions = scope.get("functions")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scope.get("connection_label", ""))
            or not re.fullmatch(r"\d{12}", account)
            or not re.fullmatch(rf"arn:aws:iam::{account}:role/[\w+=,.@/-]+", role)
            or not re.fullmatch(r"[A-Z0-9]{8,32}", scope.get("distribution_id", ""))
            or not re.fullmatch(r"[\w.-]{1,128}", scope.get("gateway_origin_id", ""))
            or not re.fullmatch(r"[\w.-]{1,253}", scope.get("gateway_origin_domain", ""))
            or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", scope.get("frontend_hostname", ""))
            or not re.fullmatch(r"/api/v1/products/[^/]+/projects/[^/]+/services/devops/bindings/[^/]+/browser-launch", path)
            or not isinstance(functions, list) or not functions
            or any(not isinstance(name, str) or not re.fullmatch(r"[\w-]{1,64}", name) for name in functions)
            or len(set(functions)) != len(functions)
            or not isinstance(scope.get("allowed_invoke_statements"), dict)):
        raise ValueError("private ingress policy scope needs exact deployed gateway, role and functions")
    return scope


def aws_read(scope, service, operation, *arguments, deadline, missing_ok=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("ingress readback exceeded authorized deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", scope["connection_label"],
               "--exec", "aws", service, operation, *arguments, "--output", "json"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=min(remaining, 20), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("selected AWS ingress readback unavailable") from None
    if result.returncode:
        if missing_ok and b"ResourceNotFoundException" in result.stderr:
            return None
        raise ValueError("selected AWS ingress readback unavailable")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("selected AWS ingress readback malformed") from None


def verify_distribution(scope, distribution):
    if not isinstance(distribution, dict) or distribution.get("Status") != "Deployed":
        raise ValueError("gateway distribution is not deployed")
    config = distribution.get("DistributionConfig", {})
    aliases = config.get("Aliases", {}).get("Items", [])
    if (not isinstance(aliases, list) or any(not isinstance(alias, str) or "*" in alias for alias in aliases)
            or scope["frontend_hostname"] not in {distribution.get("DomainName"), *aliases}):
        raise ValueError("selected first-party frontend is not served by the gateway distribution")
    origins = config.get("Origins", {}).get("Items", [])
    if (config.get("Enabled") is not True or not isinstance(origins, list)
            or any(not isinstance(origin, dict) for origin in origins)
            or not any(origin.get("Id") == scope["gateway_origin_id"]
                       and origin.get("DomainName") == scope["gateway_origin_domain"]
                       and ("CustomOriginConfig" in origin or "VpcOriginConfig" in origin)
                       for origin in origins)
            or any(re.search(r"\.lambda-url\.[\w-]+\.on\.aws$", origin.get("DomainName", ""))
                   for origin in origins)):
        raise ValueError("gateway distribution exposes an unapproved launcher origin")
    behaviors = config.get("CacheBehaviors", {}).get("Items", [])
    if not isinstance(behaviors, list):
        raise ValueError("gateway distribution behaviors unavailable")
    matched = next((behavior for behavior in behaviors
                    if fnmatch.fnmatchcase(scope["launch_path"], behavior.get("PathPattern", ""))),
                   config.get("DefaultCacheBehavior", {}))
    if (matched.get("TargetOriginId") != scope["gateway_origin_id"]
            or "POST" not in matched.get("AllowedMethods", {}).get("Items", [])
            or matched.get("ViewerProtocolPolicy") not in ("https-only", "redirect-to-https")
            or matched.get("LambdaFunctionAssociations", {}).get("Quantity", 0)):
        raise ValueError("native launch is not routed exclusively through the gateway")
    for behavior in behaviors:
        if ("browser-launch" in behavior.get("PathPattern", "")
                and behavior.get("TargetOriginId") != scope["gateway_origin_id"]):
            raise ValueError("dedicated public native launcher behavior")
    return {"distribution_id": scope["distribution_id"], "launch_behavior": matched.get("PathPattern"),
            "gateway_origin_id": scope["gateway_origin_id"], "frontend_hostname": scope["frontend_hostname"],
            "status": distribution["Status"]}


def verify_policy(statements, allowed, role):
    if not isinstance(statements, list) or not isinstance(allowed, list):
        raise ValueError("Lambda policy and approved grants must be complete")
    if statements != allowed:
        raise ValueError("unexpected deployed Lambda invoke grant")
    for statement in statements:
        if (not isinstance(statement, dict) or statement.get("Effect") != "Allow"
                or statement.get("Action") != "lambda:InvokeFunction"):
            raise ValueError("unsafe Lambda invoke or Function URL grant")
        principal = statement.get("Principal")
        if principal == {"AWS": role}:
            continue
        condition = statement.get("Condition", {})
        source = condition.get("ArnLike", {}).get("AWS:SourceArn") or condition.get("ArnEquals", {}).get("AWS:SourceArn")
        if principal != {"Service": "events.amazonaws.com"} or not isinstance(source, str) or any(char in source for char in ("*", "?")):
            raise ValueError("Lambda invoke grant lacks exact service-role or event source")


def read_ingress_policy(manifest, *, deadline, max_requests, read=aws_read):
    scope = validate_policy_scope(manifest)
    calls = 0

    def fetch(service, operation, *arguments, missing_ok=False):
        nonlocal calls
        calls += 1
        if calls > max_requests:
            raise ValueError("ingress policy readback exceeded request bound")
        return read(scope, service, operation, *arguments, deadline=deadline, missing_ok=missing_ok)

    identity = fetch("sts", "get-caller-identity")
    role_name = scope["role_arn"].split("/")[-1]
    if (not isinstance(identity, dict) or identity.get("Account") != scope["account_id"]
            or not isinstance(identity.get("Arn"), str) or not re.fullmatch(rf"arn:aws:sts::{scope['account_id']}:assumed-role/{re.escape(role_name)}/[^/]+",
                                    identity.get("Arn", ""))):
        raise ValueError("selected AWS connection does not match approved account and role")
    distribution = fetch("cloudfront", "get-distribution", "--id", scope["distribution_id"])
    checked = verify_distribution(scope, distribution.get("Distribution", {}) if isinstance(distribution, dict) else None)
    expected = scope["allowed_invoke_statements"]
    seen = set()
    for name in scope["functions"]:
        urls = fetch("lambda", "list-function-url-configs", "--function-name", name)
        if not isinstance(urls, dict) or urls.get("FunctionUrlConfigs") != []:
            raise ValueError("deployed SSO function or alias exposes a Function URL")
        aliases = fetch("lambda", "list-aliases", "--function-name", name)
        if not isinstance(aliases, dict) or not isinstance(aliases.get("Aliases"), list):
            raise ValueError("deployed SSO aliases unavailable")
        if any(not isinstance(alias, dict) for alias in aliases["Aliases"]):
            raise ValueError("deployed SSO alias malformed")
        for qualifier in [None, *(alias.get("Name") for alias in aliases["Aliases"])]:
            if qualifier is not None and (not isinstance(qualifier, str) or not qualifier):
                raise ValueError("deployed SSO alias malformed")
            key = name if qualifier is None else f"{name}:{qualifier}"
            seen.add(key)
            args = ("--function-name", name) if qualifier is None else ("--function-name", name, "--qualifier", qualifier)
            policy = fetch("lambda", "get-policy", *args, missing_ok=True)
            try:
                statements = json.loads(policy["Policy"])["Statement"] if policy is not None else []
            except (KeyError, TypeError, ValueError):
                raise ValueError("deployed Lambda policy malformed") from None
            verify_policy(statements, expected.get(key), scope["role_arn"])
    if set(expected) != seen:
        raise ValueError("deployed alias inventory differs from approved policy scope")
    return {"aws_account": scope["account_id"], "distribution": checked,
            "verified_functions": sorted(seen), "function_urls": [], "public_launchers": []}
