"""Reject partial or creative retention of the historical shared-state addresses."""

import json
import sys
from pathlib import Path


COUNTED = {
    "aws_api_gateway_resource.browser", "aws_api_gateway_method.browser",
    "aws_api_gateway_integration.browser", "aws_lambda_permission.browser",
    "aws_cloudwatch_log_group.browser", "aws_iam_role.browser_gateway",
    "aws_iam_role_policy.browser_gateway", "aws_lambda_function.browser",
    "aws_api_gateway_resource.code_interpreter", "aws_api_gateway_method.code_interpreter",
    "aws_api_gateway_integration.code_interpreter", "aws_lambda_permission.code_interpreter",
    "aws_api_gateway_resource.websearch", "aws_api_gateway_method.websearch",
    "aws_api_gateway_integration.websearch", "aws_lambda_permission.websearch",
}
POLICIES = {"browser_worker", "code_interpreter_worker", "websearch_worker"}
API_COUNTED = {
    f"aws_api_gateway_{kind}.{route}"
    for kind in ("resource", "method", "integration")
    for route in ("browser", "code_interpreter", "websearch")
}
API_ADDRESSES = {f"{name}[0]" for name in API_COUNTED}
SERVICE_POLICIES = {
    "aws_iam_role_policy.code_interpreter_provider[0]",
    "aws_iam_role_policy.service[0]",
    "aws_iam_role_policy.websearch_gateway[0]",
}
# Roles whose aggregate ``inline_policy`` snapshot may lag their separately managed
# policies: role address -> (configuration address, the exact owned policy addresses).
# The browser service role was added after dev run 39747910838 (2026-10-22) refused a
# release whose only role drift was that snapshot catching up with
# aws_iam_role_policy.browser_service (ConditionCheckItem, already in state).
COMPUTED_ROLES = {
    "aws_iam_role.service[0]": ("aws_iam_role.service", SERVICE_POLICIES),
    "aws_iam_role.browser_service[0]": ("aws_iam_role.browser_service", {"aws_iam_role_policy.browser_service[0]"}),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def one_resource(entries, address):
    if not isinstance(entries, list):
        raise ValueError("Coordinator: computed refresh requires known resource records")
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("address") == address]
    if len(matches) != 1:
        raise ValueError("Coordinator: computed refresh requires one independently managed resource")
    return matches[0]


def bound_refresh(plan, address, refreshed, *, noop):
    resources = plan.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
    prior = one_resource(resources, address).get("values")
    change = one_resource(plan.get("resource_changes", []), address).get("change", {})
    if (canonical(prior) != canonical(refreshed) or canonical(change.get("before")) != canonical(refreshed)
            or change.get("actions") not in (["no-op"], ["update"])):
        raise ValueError("Coordinator: computed refresh differs from the bound current resource")
    if noop and (change.get("actions") != ["no-op"] or canonical(change.get("after")) != canonical(refreshed)
                 or change.get("after_unknown") != {}):
        raise ValueError("Coordinator: computed IAM refresh requires unchanged independently managed policies")


def policy_document(value):
    def unique_keys(pairs):
        result = {}
        for key, entry in pairs:
            if key in result:
                raise ValueError("Coordinator: computed IAM policy contains duplicate JSON keys")
            result[key] = entry
        return result

    if not isinstance(value, str):
        raise ValueError("Coordinator: computed IAM policy must be known JSON")
    document = json.loads(value, object_pairs_hook=unique_keys)
    if not isinstance(document, dict) or not isinstance(document.get("Statement"), list):
        raise ValueError("Coordinator: computed IAM policy document is incomplete")
    return canonical(document)


def inline_policies(value):
    if not isinstance(value, list) or not value:
        raise ValueError("Coordinator: computed IAM refresh requires the complete inline-policy set")
    result = {}
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {"name", "policy"}
                or not isinstance(entry["name"], str) or not entry["name"] or entry["name"] in result):
            raise ValueError("Coordinator: computed IAM refresh contains missing or duplicate policy identities")
        result[entry["name"]] = policy_document(entry["policy"])
    return result


def verify_computed_role_refresh(plan, before, after, address="aws_iam_role.service[0]"):
    configuration_address, owned_policies = COMPUTED_ROLES[address]
    if (canonical({key: value for key, value in before.items() if key != "inline_policy"})
            != canonical({key: value for key, value in after.items() if key != "inline_policy"})
            or not isinstance(after.get("name"), str) or not after["name"] or after.get("id") != after["name"]):
        raise ValueError("Coordinator: role identity, trust or permissions drift requires independent reconciliation")
    old, refreshed = inline_policies(before.get("inline_policy")), inline_policies(after.get("inline_policy"))
    if old.keys() != refreshed.keys():
        raise ValueError("Coordinator: computed IAM refresh cannot add or remove inline policies")
    # inline_policy is also a configurable attribute. This exception applies only
    # to the provider's aggregate snapshot of the separately owned policies.
    configuration = one_resource(plan.get("configuration", {}).get("root_module", {}).get("resources", []),
                                 configuration_address)
    if "inline_policy" in configuration.get("expressions", {}):
        raise ValueError("Coordinator: role inline policies must remain separately managed")
    bound_refresh(plan, address, after, noop=True)
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    owned = [entry for entry in resources if isinstance(entry, dict)
             and str(entry.get("address", "")).startswith("aws_iam_role_policy.")
             and entry.get("values", {}).get("role") == after["name"]]
    if len(owned) != len(owned_policies) or {entry.get("address") for entry in owned} != owned_policies:
        raise ValueError("Coordinator: computed IAM refresh requires the exact known policy owners")
    planned = [entry for entry in plan.get("resource_changes", []) if isinstance(entry, dict)
               and str(entry.get("address", "")).startswith("aws_iam_role_policy.")
               and any(isinstance(entry.get("change", {}).get(side), dict)
                       and entry["change"][side].get("role") == after["name"] for side in ("before", "after"))]
    if len(planned) != len(owned_policies) or {entry.get("address") for entry in planned} != owned_policies:
        raise ValueError("Coordinator: computed IAM refresh cannot accompany new or unowned policies")
    managed = {}
    for entry in owned:
        value = entry["values"]
        bound_refresh(plan, entry["address"], value, noop=True)
        name = value.get("name")
        if (not isinstance(name, str) or not name or name in managed
                or value.get("id") != after["name"] + ":" + name):
            raise ValueError("Coordinator: computed IAM policy identity differs from its role")
        managed[name] = policy_document(value.get("policy"))
    if refreshed != managed:
        raise ValueError("Coordinator: computed IAM snapshot differs from the unchanged managed policies")


def retirement_drift(plan, drift):
    """Exclude only fully bound computed refreshes from API retirement checks."""
    computed = set(COMPUTED_ROLES) | {"kubernetes_deployment.browser[0]"}
    remaining, seen = [], set()
    for entry in drift:
        address = entry.get("address")
        if address not in computed:
            remaining.append(entry)
            continue
        change = entry.get("change", {})
        before, after = change.get("before"), change.get("after")
        if (address in seen or change.get("actions") != ["update"] or change.get("after_unknown") != {}
                or not isinstance(before, dict) or not isinstance(after, dict)):
            raise ValueError("Coordinator: computed refresh must be one complete known update")
        seen.add(address)
        if address in COMPUTED_ROLES:
            verify_computed_role_refresh(plan, before, after, address)
            continue
        old, refreshed = before.get("metadata"), after.get("metadata")
        if (not isinstance(old, list) or not isinstance(refreshed, list) or len(old) != 1 or len(refreshed) != 1
                or not isinstance(old[0], dict) or not isinstance(refreshed[0], dict)
                or not all(isinstance(value.get("resource_version"), str) and value["resource_version"]
                           for value in (old[0], refreshed[0]))
                or not all(isinstance(refreshed[0].get(key), str) and refreshed[0][key]
                           for key in ("name", "namespace", "uid"))
                or canonical({key: value for key, value in old[0].items() if key != "resource_version"})
                != canonical({key: value for key, value in refreshed[0].items() if key != "resource_version"})
                or canonical({key: value for key, value in before.items() if key != "metadata"})
                != canonical({key: value for key, value in after.items() if key != "metadata"})):
            raise ValueError("Coordinator: Browser drift exceeds its computed resource version")
        bound_refresh(plan, address, after, noop=False)
    return remaining


def verify_retired_api_drift(drift, bindings):
    """Accept only the complete refresh removal of already-absent API routes."""
    if not drift:
        return  # The state owner already recorded the reviewed retirement.
    if (len(drift) != len(API_ADDRESSES)
            or {entry.get("address") for entry in drift} != API_ADDRESSES):
        raise ValueError("Coordinator: API retirement requires exactly nine absent-route refresh records and no unrelated drift")
    previous = {}
    for entry in drift:
        change = entry.get("change", {})
        before = change.get("before")
        if (change.get("actions") != ["delete"] or "after" not in change or change["after"] is not None
                or not isinstance(before, dict) or not isinstance(before.get("id"), str) or not before["id"]):
            raise ValueError("Coordinator: API retirement requires provider-confirmed absence of known historical IDs")
        if before.get("rest_api_id") != bindings["rest_api_id"]["value"]:
            raise ValueError("Coordinator: retired route refresh differs from the private REST API")
        previous[entry["address"]] = before
    for route in ("browser", "code_interpreter", "websearch"):
        resource = previous[f"aws_api_gateway_resource.{route}[0]"]
        if (resource.get("parent_id") != bindings["tools_parent_resource_id"]["value"]
                or resource.get("path_part") != route.replace("_", "-")):
            raise ValueError("Coordinator: retired route refresh differs from the historical parent or path")
        for kind in ("method", "integration"):
            before = previous[f"aws_api_gateway_{kind}.{route}[0]"]
            if before.get("resource_id") != resource["id"] or before.get("http_method") != "POST":
                raise ValueError("Coordinator: retired route refresh differs from the historical method or integration")


def verify(plan):
    enabled = plan.get("variables", {}).get("retain_legacy_routes", {}).get("value") is True
    retain_api = plan.get("variables", {}).get("retain_legacy_api_routes", {}).get("value", True)
    if not isinstance(retain_api, bool):
        raise ValueError("Coordinator: API route retention must be an explicit boolean")
    roles = plan.get("variables", {}).get("worker_role_arns", {}).get("value", [])
    if not isinstance(roles, list) or not all(isinstance(role, str) and role.startswith("arn:aws:iam::")
                                                for role in roles):
        raise ValueError("Coordinator: bind the exact historical worker-policy keys")
    expected = {f"{name}[0]" for name in COUNTED}
    expected.update(f'aws_iam_role_policy.{policy}[{json.dumps(role)}]'
                    for policy in POLICIES for role in roles)
    if not retain_api:
        expected -= API_ADDRESSES
    resources = plan.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
    existing = {entry.get("address") for entry in resources if isinstance(entry, dict)}
    historical = {address for address in existing if isinstance(address, str)
                  and (address.split("[", 1)[0] in COUNTED
                       or address.split("[", 1)[0] in {f"aws_iam_role_policy.{policy}" for policy in POLICIES})}
    if enabled:
        if not roles or historical != expected:
            raise ValueError("Coordinator: retained routes require all existing historical state addresses and exact worker keys")
        if any(not isinstance(entry.get("values"), dict) or not entry["values"].get("id")
               for entry in resources if entry.get("address") in expected):
            raise ValueError("Coordinator: retained state identity has no independently known resource ID")
        bindings = plan["variables"]
        api_id = bindings.get("rest_api_id", {}).get("value")
        parent_id = bindings.get("tools_parent_resource_id", {}).get("value")
        source = bindings.get("api_execution_arn", {}).get("value")
        stage = bindings.get("stage_name", {}).get("value")
        if not all(isinstance(value, str) and value for value in (api_id, parent_id, source, stage)):
            raise ValueError("Coordinator: bind the exact existing REST API, parent, execution ARN and stage")
        for entry in resources:
            address = entry.get("address", "")
            values = entry.get("values", {})
            if address.startswith("aws_api_gateway_resource.") and address in expected:
                if values.get("rest_api_id") != api_id or values.get("parent_id") != parent_id:
                    raise ValueError("Coordinator: retained route identities differ from private state")
            if address.startswith("aws_lambda_permission.") and address in expected:
                route = address.split(".", 1)[1].split("[", 1)[0].replace("_", "-")
                if values.get("source_arn") != f"{source}/{stage}/POST/tools/{route}":
                    raise ValueError("Coordinator: retained route invocation scope differs from private state")
    elif historical:
        raise ValueError("Coordinator: bind and review the existing shared-state retention inputs before release")
    drift = plan.get("resource_drift", [])
    if not isinstance(drift, list) or not all(isinstance(entry, dict) for entry in drift):
        raise ValueError("Coordinator: retained resource drift must be independently reconciled")
    if enabled and not retain_api:
        verify_retired_api_drift(retirement_drift(plan, drift), bindings)
    for entry in drift:
        if entry.get("address") in expected:
            raise ValueError("Coordinator: retained resource drift, including routing, authorization or trust, requires independent reconciliation")
    for change in plan.get("resource_changes", []):
        if enabled and not retain_api and isinstance(change, dict) and change.get("address") in API_ADDRESSES:
            raise ValueError("Coordinator: already-retired API routes must have no planned resource operation")
        if not isinstance(change, dict) or change.get("address") not in expected:
            continue
        actions = change.get("change", {}).get("actions", [])
        if "delete" in actions or "create" in actions:
            raise ValueError("Coordinator: retained resource would be created, replaced or removed")
        if actions not in (["no-op"], ["update"]):
            raise ValueError("Coordinator: unexpected retained-resource operation")
        if actions == ["update"] and change["address"] != "aws_lambda_function.browser[0]":
            raise ValueError("Coordinator: complete the scoped worker and legacy-role policy migration before shared apply")


if __name__ == "__main__":
    try:
        verify(json.loads(Path(sys.argv[1]).read_text()))
    except (IndexError, OSError, ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from None
