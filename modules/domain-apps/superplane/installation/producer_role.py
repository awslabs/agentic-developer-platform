"""Exact managed producer intent and saved-plan/applied identity checks."""

import copy
import json
import re
from types import SimpleNamespace

from .api_adapters import closed, verify_role
from .config import require


def expected_name(env):
    return f"adp-{env.get('environment')}-superplane-api-producer"


def expected_arn(env):
    return f"arn:aws:iam::{env.get('account_id')}:role/{expected_name(env)}"


def has_unknown(value):
    if isinstance(value, dict):
        return any(has_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(has_unknown(v) for v in value)
    return bool(value)


def validate(env):
    selected = env.get("api_adapters", {}).get("dispatcher", {})
    intent = env.get("api_producer_role")
    require(
        selected.get("role_arn") != expected_arn(env) or intent is not None,
        "The managed API role requires api_producer_role on every upgrade",
    )
    if intent is None:
        return
    closed(intent, {"api_id", "stage"}, "api_producer_role")
    require(
        isinstance(intent["api_id"], str)
        and re.fullmatch(r"[a-z0-9]{10}", intent["api_id"])
        and isinstance(intent["stage"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", intent["stage"]),
        "Managed producer target is invalid",
    )
    require(
        not selected or all(selected.get(k) == v for k, v in dispatcher(env).items()),
        "Managed API role and selected dispatcher must agree exactly",
    )


def dispatcher(env):
    intent = env["api_producer_role"]
    return {
        **intent,
        "region": env["region"],
        "role_arn": expected_arn(env),
        "endpoint": f"https://{intent['api_id']}.execute-api.{env['region']}.amazonaws.com/{intent['stage']}",
    }


def role_installer(installer):
    env = copy.deepcopy(installer.env)
    env.setdefault("api_adapters", {})["dispatcher"] = dispatcher(env)
    return SimpleNamespace(env=env, aws=installer.aws, json=installer.json)


def preflight(installer):
    from .adapter_staging import role_identity

    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", installer.env["cluster"])
    )["cluster"]
    require(
        cluster["arn"]
        == f"arn:aws:eks:{installer.env['region']}:{installer.env['account_id']}:cluster/{installer.env['cluster']}",
        "Managed role cluster identity changed",
    )
    return {
        **role_identity(role_installer(installer), cluster, allow_missing=True),
        "oidc": cluster["identity"]["oidc"]["issuer"],
    }


def documents(env, oidc):
    issuer = oidc.removeprefix("https://")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {
                    "Federated": f"arn:aws:iam::{env['account_id']}:oidc-provider/{issuer}"
                },
                "Condition": {
                    "StringEquals": {
                        issuer
                        + ":sub": f"system:serviceaccount:{env['namespace']}:superplane-api",
                        issuer + ":aud": "sts.amazonaws.com",
                    }
                },
            }
        ],
    }
    target = dispatcher(env)
    prefix = f"arn:aws:execute-api:{env['region']}:{env['account_id']}:{target['api_id']}/{target['stage']}/POST/internal/v1/controller-execution/"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                "Resource": [
                    prefix + r for r in ("producer-readiness", "verify-run", "dispatch")
                ],
            }
        ],
    }
    return trust, policy


def inspect_plan(installer, plan):
    env = installer.env
    if not env.get("api_producer_role"):
        return
    evidence = installer.receipt["api_producer_role_preflight"]
    trust, policy = documents(env, evidence["oidc"])
    configurations = {
        r["address"]: r
        for r in plan.get("configuration", {})
        .get("root_module", {})
        .get("resources", [])
    }
    for resource in plan.get("resource_changes", []):
        if resource.get("address") == "aws_iam_role.api_producer[0]":
            continue
        after = resource.get("change", {}).get("after") or {}
        targets = [after.get("role"), after.get("name"), *(after.get("roles") or [])]
        require(
            not any(v in (expected_name(env), expected_arn(env)) for v in targets),
            "Another Terraform resource targets the dedicated managed role",
        )
        unknown_targets = resource.get("change", {}).get("after_unknown", {})
        for field in ("role", "roles"):
            if not has_unknown(unknown_targets.get(field)):
                continue
            configuration = configurations.get(resource["address"].split("[")[0], {})
            references = (
                configuration.get("expressions", {})
                .get(field, {})
                .get("references", [])
            )
            allowed = {
                f"aws_iam_role.{role}{attribute}"
                for role in ("control_plane", "skypilot")
                for attribute in ("", ".id", ".name")
            }
            require(
                bool(references) and set(references) <= allowed,
                "Unknown IAM role target is not bound to an existing maintained role",
            )
    changes = {r["address"]: r["change"] for r in plan.get("resource_changes", [])}
    for address, expected in (
        (
            "aws_iam_role.api_producer[0]",
            {"name": expected_name(env), "path": "/", "assume_role_policy": trust},
        ),
    ):
        change = changes.get(address, {})
        actions = change.get("actions")
        require(
            actions == (["create"] if evidence.get("role_missing") else ["no-op"]),
            "Managed role plan must create the absent identity or preserve the verified identity",
        )
        after = change.get("after", {})
        unknown = change.get("after_unknown", {})
        if evidence.get("role_missing"):
            require(
                change.get("before") is None,
                "Absent managed role has prior Terraform identity",
            )
        else:
            require(
                after.get("arn") == expected_arn(env)
                and after.get("unique_id") == evidence["role_id"]
                and not unknown.get("arn")
                and not unknown.get("unique_id"),
                "Existing managed role plan identity differs from live preflight",
            )
        require(
            not any(
                has_unknown(v)
                for k, v in unknown.items()
                if k not in {"arn", "id", "unique_id", "create_date", "tags_all"}
            ),
            "Managed role plan has unknown authority",
        )
        for key, value in expected.items():
            observed = after.get(key)
            if isinstance(value, dict):
                try:
                    observed = json.loads(observed)
                except (ValueError, TypeError):
                    observed = None
            require(
                observed == value,
                "Managed role plan authority differs from reviewed intent",
            )
        if address.startswith("aws_iam_role."):
            require(
                after.get("arn") in (None, expected_arn(env))
                and not after.get("permissions_boundary")
                and not after.get("managed_policy_arns")
                and len(after.get("inline_policy", [])) == 1,
                "Managed role plan substitutes identity or adds authority",
            )
            inline = after["inline_policy"][0]
            try:
                inline_document = json.loads(inline.get("policy"))
            except (TypeError, ValueError):
                inline_document = None
            require(
                inline.get("name") == expected_name(env) and inline_document == policy,
                "Managed role inline policy differs from reviewed intent",
            )
    verify_role(
        role_installer(installer).env,
        {"Arn": expected_arn(env), "AssumeRolePolicyDocument": trust},
        [policy],
        evidence["oidc"],
    )


def verify_applied(installer):
    from .adapter_staging import role_identity

    result = installer.json(
        installer.commands.call(
            [
                "terraform",
                f"-chdir={installer.directory / 'terraform'}",
                "output",
                "-json",
                "api_producer_role",
            ]
        )
    )
    require(
        result.get("arn") == expected_arn(installer.env)
        and isinstance(result.get("role_id"), str)
        and result["role_id"],
        "Applied managed role output is incomplete or changed",
    )
    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", installer.env["cluster"])
    )["cluster"]
    before = installer.receipt["api_producer_role_preflight"]
    require(
        cluster.get("arn")
        == f"arn:aws:eks:{installer.env['region']}:{installer.env['account_id']}:cluster/{installer.env['cluster']}"
        and cluster.get("identity", {}).get("oidc", {}).get("issuer") == before["oidc"],
        "Applied managed role cluster or OIDC identity changed",
    )
    actual = role_identity(role_installer(installer), cluster)
    require(
        actual["role_arn"] == result["arn"] and actual["role_id"] == result["role_id"],
        "Applied managed role output differs from live identity",
    )
    require(
        before.get("role_missing") or before["role_id"] == actual["role_id"],
        "Existing managed role was replaced",
    )
    installer.receipt["api_producer_role_applied"] = actual
    installer.save()
