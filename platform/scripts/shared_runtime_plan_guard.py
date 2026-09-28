"""Fail-closed saved-plan checks for the bounded platform shared-runtime rollout.

Raw plans contain signing material. Callers must keep them private and emit only
the returned address/action list. This is deliberately account-specific runtime
maintenance, not a general Terraform approval mechanism.
"""

from __future__ import annotations

import json

ACCOUNT = "879318057152"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-scaledjob-role"
KEY_NAME = "/adp/dev/gateway/run-report-signing-key"
KEY_ARN = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{KEY_NAME}"
KEY_ADDRESS = "aws_ssm_parameter.agent_run_reporting_key"
WIRING_ADDRESS = "aws_ssm_parameter.worker_runtime_wiring"
CONFIG_ADDRESS = "kubernetes_config_map.worker_gateway[0]"
TICK_ADDRESS = "module.orchestration_tick[0].aws_lambda_function.tick"
IAM_ADDRESS = "module.orchestration_tick[0].aws_iam_role_policy.tick"
FLAGS = ("ADP_SHARED_RUN_REPORTING_ENABLED", "ADP_SHARED_WORKER_CONTINUATION_ENABLED")
WIRING_FLAGS = ("shared_run_reporting_enabled", "shared_worker_continuation_enabled")
TARGETS = {
    "prerequisites": {KEY_ADDRESS, WIRING_ADDRESS, CONFIG_ADDRESS},
    "gateway-enable": {CONFIG_ADDRESS},
    "tick-wiring": {WIRING_ADDRESS},
    "tick": {IAM_ADDRESS, TICK_ADDRESS},
}


class Refused(ValueError):
    """Only constant, non-secret refusal text belongs in this exception."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def check_other_work(probe):
    """Ignore only ready stories with a proven early dispatch refusal.

    Reporting also applies to legacy flows. A pending flow, absent execution
    policy, empty queue, or zero current workers cannot prove non-eligibility.
    Running stories always block, even if their installation/approval is gone.
    """
    count = probe.get("other_ready_or_running_stories")
    rows = probe.get("other_ready_or_running_nodes")
    require(type(count) is int and 0 <= count <= 20, "other work inventory is unavailable or unbounded")
    require(isinstance(rows, list) and len(rows) == count and probe.get("other_ready_or_running_nodes_truncated") is False,
            "other work inventory is incomplete")
    seen = set()
    for row in rows:
        require(isinstance(row, dict), "other work record is invalid")
        identity = row.get("node_id")
        require(isinstance(identity, str) and identity and identity not in seen, "other work identity is missing or duplicated")
        require(all(isinstance(row.get(key), str) and row[key] for key in ("org_id", "flow_id")), "other work scope is missing")
        require(type(row.get("attempts")) is int and row["attempts"] >= 0, "other work attempt is unknown")
        seen.add(identity)
        require(row.get("state") == "ready", "other running work requires separate review before global shared activation")
        facts = row.get("dispatch_preflight")
        require(isinstance(facts, dict) and facts.get("contract") == "shared-runtime-dispatch-preflight/v1"
                and {"routing_blocker", "installation_id", "approval_decision_id", "human_approval_verified"} <= facts.keys(),
                "other ready work lacks dispatch eligibility evidence")
        require((facts["routing_blocker"] is None or isinstance(facts["routing_blocker"], str))
                and facts["routing_blocker"] in {None, "missing_issue_ref", "malformed_issue_ref"}
                and type(facts["human_approval_verified"]) is bool
                and (facts["installation_id"] is None or type(facts["installation_id"]) is int)
                and (facts["approval_decision_id"] is None or isinstance(facts["approval_decision_id"], str))
                and (not facts["human_approval_verified"] or bool(facts["approval_decision_id"])),
                "other ready work has invalid dispatch eligibility evidence")
        require(facts["routing_blocker"] is not None or facts["installation_id"] is None or not facts["human_approval_verified"],
                "other ready work may dispatch; separate review is required before global shared activation")


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted((normalized(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def unchanged(before, after, allowed, reason):
    require(
        {k: v for k, v in before.items() if k not in allowed} == {k: v for k, v in after.items() if k not in allowed}, reason,
    )


def known(value):
    if isinstance(value, dict):
        return all(known(v) for v in value.values())
    if isinstance(value, list):
        return all(known(v) for v in value)
    return value is not True


def check_wiring(before, after, *, enabled):
    allowed = {*WIRING_FLAGS, "shared_worker_role_arn", "run_report_key_parameter"}
    unchanged(before, after, allowed, "unrelated worker runtime wiring changed")
    require(all(after.get(k) is enabled for k in WIRING_FLAGS), "incorrect wiring flags")
    require(after.get("shared_worker_role_arn") == ROLE and after.get("run_report_key_parameter") == KEY_NAME, "incorrect shared reporting identity")
    require(before.get("dispatch_queue_url") == f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/adp-dev-agent-submit.fifo", "unexpected dispatch queue")
    require(before.get("webhook_events_table") == "adp-dev-webhook-events", "unexpected events table")


def check_iam(before, after):
    unchanged(before, after, {"policy"}, "tick IAM resource identity changed")
    old, new = json.loads(before["policy"]), json.loads(after["policy"])
    unchanged(old, new, {"Statement"}, "tick IAM policy metadata changed")
    old_statements = {s["Sid"]: s for s in old["Statement"]}
    new_statements = {s["Sid"]: s for s in new["Statement"]}
    require(len(old_statements) == len(old["Statement"]) and len(new_statements) == len(new["Statement"]), "duplicate IAM statement identifiers")
    require(all(k in new_statements and normalized(v) == normalized(new_statements[k]) for k, v in old_statements.items()), "existing tick IAM permissions changed")
    expected = {"Sid": "ReadRunReportSigningMaterial", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": [KEY_ARN]}
    require(set(new_statements) - set(old_statements) <= {expected["Sid"]}, "unexpected new tick IAM permission")
    require(normalized(new_statements.get(expected["Sid"])) == normalized(expected), "incorrect reporting key permission")


def check_plan(plan, *, stage, enabled=False, gateway_image, signing_key, kms_key):
    require(stage in TARGETS and not plan.get("errored"), "invalid plan stage")
    evidence = []
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        actions = change["actions"]
        require("delete" not in actions and "forget" not in actions, "plan includes destruction or state removal")
        require(not change.get("importing") and not resource.get("previous_address"), "plan imports or moves a resource")
        if actions == ["no-op"] or resource.get("mode") == "data" and actions == ["read"]:
            continue
        address = resource["address"]
        require(address in TARGETS[stage], "plan changes an unapproved resource")
        require(actions in (["update"], ["create"]), "unapproved plan action")
        before, after = change.get("before"), change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        if address == KEY_ADDRESS:
            require(actions == ["create"] and before is None, "existing reporting key must not change")
            require(after.get("overwrite") in {None, False}, "reporting key creation must not overwrite a parameter")
            require(after.get("name") == KEY_NAME and after.get("type") == "SecureString" and after.get("key_id") == kms_key, "incorrect reporting key target")
            require(known({k: unknown.get(k) for k in ("value", "name", "type", "key_id")}), "reporting key is unresolved")
            require(signing_key and after.get("value") == signing_key, "reporting key differs from existing gateway signing key")
        else:
            require(actions == ["update"] and isinstance(before, dict), "maintenance may not create existing runtime resources")
            if address == WIRING_ADDRESS:
                require(known(unknown.get("value")), "runtime wiring is unresolved")
                unchanged(before, after, {"value", "version"}, "SSM wiring metadata changed")
                check_wiring(json.loads(before["value"]), json.loads(after["value"]), enabled=enabled)
            elif address == CONFIG_ADDRESS:
                require(known(unknown.get("data")), "gateway config is unresolved")
                unchanged(before, after, {"data"}, "gateway ConfigMap identity changed")
                old, new = before["data"], after["data"]
                unchanged(old, new, {*FLAGS, "AGENT_WORKER_ROLE_ARN"}, "unrelated gateway configuration changed")
                require(new.get("AGENT_AUTHORITY_ENABLED") == "false", "protected authority changed")
                require(all(new.get(k) == str(enabled).lower() for k in FLAGS) and new.get("AGENT_WORKER_ROLE_ARN") == ROLE, "incorrect gateway reporting configuration")
            elif address == IAM_ADDRESS:
                require(known(unknown.get("policy")), "tick IAM policy is unresolved")
                check_iam(before, after)
            elif address == TICK_ADDRESS:
                require(known(unknown.get("environment")) and known(unknown.get("image_uri")), "tick configuration is unresolved")
                unchanged(before, after, {"environment", "last_modified"}, "unrelated Lambda configuration or code changed")
                require(before.get("image_uri") == after.get("image_uri") == gateway_image, "tick image differs from approved running release")
                require(len(before["environment"]) == len(after["environment"]) == 1, "unexpected Lambda environment structure")
                old, new = before["environment"][0]["variables"], after["environment"][0]["variables"]
                unchanged(old, new, {*FLAGS, "AGENT_WORKER_ROLE_ARN", "AGENT_RUN_CREDENTIAL_KEY_PARAMETER"}, "existing tick environment changed")
                require(new.get("AGENT_AUTHORITY_ENABLED") == "false" and all(new.get(k) == str(enabled).lower() for k in FLAGS), "incorrect tick authority or reporting flags")
                require(new.get("AGENT_WORKER_ROLE_ARN") == ROLE and new.get("AGENT_RUN_CREDENTIAL_KEY_PARAMETER") == KEY_NAME, "incorrect tick reporting identity")
        evidence.append({"address": address, "actions": actions})
    return evidence


def preserved_tick_inputs(configuration, policy, wiring, *, revision):
    """Recover actual CI inputs; defaults must not silently clear permissions."""
    env = configuration["Environment"]["Variables"]
    require(env.get("AGENT_AUTHORITY_ENABLED", "false") == "false", "protected authority enabled")
    require(env.get("BG_ORCH_DISPATCH_QUEUE_URL") == wiring["dispatch_queue_url"], "tick dispatch queue differs from worker wiring")
    require(env.get("WEBHOOK_EVENTS_TABLE") == wiring["webhook_events_table"], "tick events table differs from worker wiring")
    statements = {s["Sid"]: s for s in policy["Statement"]}
    dispatch = statements["PublishEngineDispatch"]
    dispatch_resources = dispatch["Resource"] if isinstance(dispatch["Resource"], list) else [dispatch["Resource"]]
    runs = statements["EngineRuns"]
    require(dispatch.get("Effect") == "Allow" and "sqs:SendMessage" in dispatch["Action"] and dispatch_resources == [wiring["dispatch_queue_arn"]], "tick existing queue publication permission is unavailable")
    require(runs.get("Effect") == "Allow" and {"dynamodb:GetItem", "dynamodb:PutItem"} <= set(runs["Action"]) and runs["Resource"] == [f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{wiring['webhook_events_table']}"] and runs.get("Condition") == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["orch:*"]}}, "tick existing run-store permission is unavailable")
    kms = statements["EngineCommandEventsKMSDecrypt"]["Resource"]
    credentials = statements["EngineCommandAckCredentials"]["Resource"]
    require(kms == [wiring["webhook_events_kms_key_arn"]] and len(credentials) == 1, "tick existing permission scope is unexpected")
    require(statements["EngineCommandEventsKMSDecrypt"].get("Effect") == "Allow" and "kms:Decrypt" in statements["EngineCommandEventsKMSDecrypt"]["Action"], "tick existing key decryption permission is unavailable")
    return {
        "orchestration_agent_authority_enabled": False,
        "orchestration_tick_image_tag": revision,
        "orchestration_dispatch_queue_url": env["BG_ORCH_DISPATCH_QUEUE_URL"],
        "orchestration_dispatch_queue_arn": wiring["dispatch_queue_arn"],
        "orchestration_dispatch_repo": env["BG_ORCH_DISPATCH_REPO"],
        "orchestration_dispatch_persona": env["BG_ORCH_DISPATCH_PERSONA"],
        "orchestration_dispatch_max_per_tick": int(env["BG_ORCH_DISPATCH_MAX_PER_TICK"]),
        "orchestration_webhook_events_table": env["WEBHOOK_EVENTS_TABLE"],
        "orchestration_webhook_events_kms_key_arn": kms[0],
        "orchestration_github_app_secret_arn_pattern": credentials[0],
        "orchestration_engine_command_signing_key_secret_arn": env.get("ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN", ""),
    }
