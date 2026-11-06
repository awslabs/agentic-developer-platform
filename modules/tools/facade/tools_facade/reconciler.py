"""Independently scheduled, bounded execution cleanup."""

import base64
import hashlib
import json
import os
import re
import time
from uuid import uuid4

import boto3
import httpx
from botocore.config import Config

from tools_facade.encoding import canonical
from tools_facade.executions import ATLASSIAN_WRITES, SYNCHRONOUS_TOOLS, ExecutionStore
from tools_facade.native_admission import NativeCleanupHandoff


class CleanupDeadline(Exception):
    """The tick cannot start another request within its remaining window."""


class CleanupWindow:
    # Leave five seconds before the CronJob's hard termination. Socket timeouts
    # bound individual waits, not DNS/credential lookup or total request time;
    # Kubernetes remains the final bound on a stuck process.
    SECONDS = 45
    TIMEOUTS = {"dynamodb": (0.5, 1.5), "kms": (0.5, 2), "lambda": (1, 30)}
    PROVIDER_SECONDS = 38  # Claim, sign, invoke and persist the outcome.

    def __init__(self, monotonic=None):
        self.clock = monotonic or time.monotonic
        self.deadline = self.clock() + self.SECONDS
        self.clients = {}

    def permits(self, seconds):
        return self.deadline - self.clock() >= seconds

    def client(self, service):
        if service not in self.clients:
            connect, read = self.TIMEOUTS[service]
            client = boto3.client(service, region_name=os.environ["AWS_REGION"], config=Config(
                connect_timeout=connect, read_timeout=read,
                retries={"total_max_attempts": 1, "mode": "standard"}))
            self.clients[service] = _WindowClient(self, client, connect + read)
        return self.clients[service]


class _WindowClient:
    def __init__(self, window, client, seconds):
        self.window, self.client, self.seconds = window, client, seconds

    def __getattr__(self, name):
        method = getattr(self.client, name)

        def call(**kwargs):
            if not self.window.permits(self.seconds):
                raise CleanupDeadline()
            return method(**kwargs)

        return call


def native_cleanup_handoff(item):
    """Claim original gateway funding once; no fixture credential is installed."""
    from tools_facade.server import Decision, signed_headers

    endpoint = os.environ.get("ADP_TOOLS_NATIVE_CLEANUP_URL", "")
    if not endpoint.startswith("https://") or not endpoint.endswith("/internal/v1/tools/native-cleanup"):
        raise ValueError("Native cleanup authority unavailable")
    operation = str(uuid4())
    payload = {"tenant_id": item["tenant_id"]["S"], "run_id": item["native_test_run_id"]["S"],
               "operation_id": operation, "execution_id": item["execution_id"]["S"], "tool": item["tool"]["S"]}
    body = canonical(payload)
    headers = signed_headers(endpoint, body, "execute-api", os.environ["AWS_REGION"])
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=2)) as client:
        response = client.post(endpoint, content=body, headers=headers)
    response.raise_for_status()
    decision = Decision.model_validate(response.json())
    if (not decision.allowed or decision.native_cleanup is None
            or decision.native_cleanup.reservation_id != item["native_test_reservation_id"]["S"]
            or decision.principal.user_id != item["owner_id"]["S"]
            or decision.principal.root_human_id != item["owner_id"]["S"]
            or decision.principal.tenant_id != item["tenant_id"]["S"]
            or decision.principal.team_id != item["team_id"]["S"]):
        raise ValueError("Native cleanup owner changed")
    return NativeCleanupHandoff(admission=decision.native_cleanup, decision=decision,
        execution_id=item["execution_id"]["S"], tool=item["tool"]["S"],
        headers={"x-adp-native-test-operation": operation})


def stop_provider(item, *, window=None, native_handoff=None):
    root = item["tool"]["S"]
    if root not in {"browser___start", "code_interpreter___start"} or item["state"]["S"] != "closing":
        raise ValueError("Unsupported cleanup family")
    region = os.environ["AWS_REGION"]
    account = os.environ["ADP_TOOLS_ACCOUNT_ID"]
    environment = os.environ["ADP_TOOLS_ENVIRONMENT"]
    function = os.environ["ADP_TOOLS_CLEANUP_LAMBDA_ARN"]
    role = os.environ["ADP_TOOLS_RECONCILER_ROLE_ARN"]
    key = os.environ["ADP_TOOLS_RECONCILER_KEY_ARN"]
    if (function != f"arn:aws:lambda:{region}:{account}:function:adp-{environment}-shared-tools-shared" or
            role != f"arn:aws:iam::{account}:role/adp-{environment}-role-tools-reconciler" or
            not re.fullmatch(rf"arn:aws:kms:{re.escape(region)}:{account}:key/[0-9a-f-]{{36}}", key)):
        raise ValueError("Invalid cleanup provider configuration")
    identifier = item["execution_id"]["S"]
    binding = item["binding"]["S"]
    if binding == "human":
        run_id, workload_id = None, None
    elif binding.startswith("run:") and len(binding.split(":")) == 3:
        _, run_id, workload_id = binding.split(":")
        if not run_id or not workload_id:
            raise ValueError("Invalid cleanup binding")
    else:
        raise ValueError("Invalid cleanup binding")
    tool = root.replace("___start", "___execution_close").replace("code_interpreter___", "code-interpreter___")
    arguments = {"execution_id": identifier}
    now = int(time.time())
    claims = {"caller_role_arn": role, "cleanup_reconcile": True,
              "principal": {"tenant_id": item["tenant_id"]["S"], "team_id": item["team_id"]["S"],
                            "user_id": item["owner_id"]["S"]},
              "root_human_id": item["owner_id"]["S"], "run_id": run_id, "workload_id": workload_id,
              "run_expires_at": None, "receipt_id": "cleanup-" + identifier, "connection_scopes": [],
              "method": "tools/call", "tool": tool,
              "arguments_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
              "issued_at": now, "expires_at": now + 30}
    if item.get("native_test_run_id"):
        if type(native_handoff) is not NativeCleanupHandoff:
            raise ValueError("Native cleanup funding required")
        native_handoff.before_sign("tools/call", tool, arguments, native_handoff.decision)
        claims["receipt_id"] = native_handoff.admission.reservation_id
        if root == "code_interpreter___start":
            claims["native_interpreter"] = native_handoff.interpreter_binding()
    window = window or CleanupWindow()
    signature = window.client("kms").sign(
        KeyId=key, Message=canonical(claims), MessageType="RAW", SigningAlgorithm="RSASSA_PSS_SHA_256")
    assertion = base64.urlsafe_b64encode(canonical({"claims": claims,
        "signature": base64.urlsafe_b64encode(signature["Signature"]).decode().rstrip("=")})).decode().rstrip("=")
    context = base64.b64encode(canonical({"custom": {"bedrockAgentCoreToolName": tool}})).decode()
    if native_handoff is not None:
        native_handoff.before_send()
    reply = window.client("lambda").invoke(
        FunctionName=function, InvocationType="RequestResponse", ClientContext=context,
        Payload=canonical({**arguments, "adp_assertion": assertion}))
    try:
        content = reply["Payload"].read(16385)
    finally:
        reply["Payload"].close()
    if len(content) > 16384 or reply.get("FunctionError"):
        raise ValueError("Cleanup provider response unavailable")
    result = json.loads(content)
    if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
        raise ValueError("Cleanup provider result unavailable")
    return result["result"]


def repair_write_report(item, *, window=None):
    from tools_facade.server import signed_headers

    endpoint = os.environ.get("ADP_TOOLS_OUTCOME_REPAIR_URL", "")
    region = os.environ.get("AWS_REGION", "")
    if not re.fullmatch(
        rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/"
        r"[a-z0-9]+/internal/v1/tools/outcome-repair", endpoint,
    ):
        raise ValueError("Jira outcome repair route unavailable")
    original = json.loads(item["atlassian_binding"]["S"])
    release = original["release"]
    binding = item["binding"]["S"]
    if (item["tool"]["S"] not in ATLASSIAN_WRITES
            or type(release.get("backend_generation")) is not int
            or release["backend_generation"] < 1
            or item["execution_id"]["S"] != (
                f"OP#{item['tenant_id']['S']}#{item['owner_id']['S']}#"
                f"{item['tool']['S']}#{item['request_id']['S']}"
            ) or binding != "human" and not binding.startswith("run:")):
        raise ValueError("original Jira outcome required")
    body = canonical({
        "schema_version": "1.0", "receipt_id": item["receipt_id"]["S"],
        "tenant_id": item["tenant_id"]["S"], "user_id": item["owner_id"]["S"],
        "team_id": item["team_id"]["S"],
        "run_id": binding.split(":", 2)[1] if binding != "human" else None,
        "tool": item["tool"]["S"], "request_id": item["request_id"]["S"],
        "execution_id": item["operation_execution_id"]["S"],
        "operation_id": item["execution_id"]["S"],
        "payload_sha256": item["request_digest"]["S"],
        "backend_generation": release["backend_generation"],
        "result_status": ("confirmed" if item["status"]["S"] == "confirmed"
                          and item.get("result_status", {}).get("S") == "success" else "unknown"),
    })
    window = window or CleanupWindow()
    headers = signed_headers(endpoint, body, "execute-api", region)
    if not window.permits(7):
        raise CleanupDeadline()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=2)) as client:
        response = client.post(endpoint, content=body, headers=headers)
    response.raise_for_status()
    if response.json() != {"status": "recorded"}:
        raise ValueError("Jira outcome acknowledgement unavailable")
    return True


def readback_write(item, *, window=None):
    from tools_facade.server import signed_headers

    endpoint = os.environ.get("ADP_TOOLS_ATLASSIAN_READBACK_URL", "")
    region = os.environ.get("AWS_REGION", "")
    account = os.environ.get("ADP_TOOLS_ACCOUNT_ID", "")
    role = os.environ.get("ADP_TOOLS_RECONCILER_ROLE_ARN", "")
    key = os.environ.get("ADP_TOOLS_RECONCILER_KEY_ARN", "")
    if (not re.fullmatch(
            rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/"
            r"[a-z0-9]+/internal/v1/tools/atlassian/reconcile", endpoint)
            or not re.fullmatch(rf"arn:aws:kms:{re.escape(region)}:{account}:key/[0-9a-f-]{{36}}", key)
            or role != (f"arn:aws:iam::{account}:role/"
                        f"adp-{os.environ.get('ADP_TOOLS_ENVIRONMENT')}-role-tools-reconciler")):
        raise ValueError("Jira readback route unavailable")
    original = json.loads(item["atlassian_binding"]["S"])
    locator = original.get("readback_locator")
    connection = original["connection"]
    release = original["release"]
    if (item["tool"]["S"] not in ATLASSIAN_WRITES
            or item["status"]["S"] != "unknown"
            or item["dispatch_intent"]["S"] != "dispatched"
            or type(locator) is not dict
            or set(locator) != {"cloudId", "issueIdOrKey"}
            or not all(type(value) is str and 0 < len(value) <= 512 for value in locator.values())
            or locator["cloudId"] != connection["site_id"]
            or type(release.get("backend_generation")) is not int
            or release["backend_generation"] < 1
            or item["execution_id"]["S"] != (
                f"OP#{item['tenant_id']['S']}#{item['owner_id']['S']}#"
                f"{item['tool']['S']}#{item['request_id']['S']}")):
        raise ValueError("original Jira readback unavailable")
    binding = {"operation_id": item["execution_id"]["S"],
               "receipt_id": item["receipt_id"]["S"],
               "payload_sha256": item["request_digest"]["S"], "release": release}
    deadline_at_ms = int(time.time() * 1000) + 15000
    claims = {"purpose": "reconcile", "audience": "tool-adapters:reconcile",
              "caller_role_arn": role,
              "principal": {"tenant_id": item["tenant_id"]["S"],
                            "team_id": item["team_id"]["S"], "user_id": item["owner_id"]["S"]},
              "connection": connection,
              "atlassian_operation": {**binding, "tool": item["tool"]["S"],
                                      "request_id": item["request_id"]["S"],
                                      "connection": connection},
              "reconcile_tool": "get_jira_issue",
              "read_arguments_sha256": hashlib.sha256(canonical(locator)).hexdigest(),
              "deadline_at_ms": deadline_at_ms}
    window = window or CleanupWindow()
    signature = window.client("kms").sign(
        KeyId=key, Message=canonical(claims), MessageType="RAW", SigningAlgorithm="RSASSA_PSS_SHA_256")
    assertion = base64.urlsafe_b64encode(canonical({
        "claims": claims,
        "signature": base64.urlsafe_b64encode(signature["Signature"]).decode().rstrip("="),
    })).decode().rstrip("=")
    body = canonical({"schema_version": "1.0", "purpose": "reconcile", "connector": "atlassian",
                      "operation": "get_jira_issue", "binding": binding,
                      "read_arguments": locator, "signed_assertion": assertion,
                      "deadline_at_ms": deadline_at_ms})
    headers = signed_headers(endpoint, body, "execute-api", region)
    if not window.permits(7):
        raise CleanupDeadline()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=2)) as client:
        response = client.post(endpoint, content=body, headers=headers)
    response.raise_for_status()
    if (len(response.content) > 16384 or response.json() != {
            "status": "unknown", "operation_id": binding["operation_id"],
            "backend_generation": release["backend_generation"], "readback_performed": True}):
        raise ValueError("Jira readback inconclusive")
    return True


def run_once(store=None, *, max_records=50, stop_provider=None, window=None, native_authorizer=None,
             report_write=None, readback=None):
    if type(max_records) is not int or not 1 <= max_records <= 50:
        raise ValueError("cleanup batch must contain 1 to 50 records")
    window = window or CleanupWindow()
    summary = {"selected": 0, "processed": 0, "deferred": 0, "deadline_reached": False}
    if not window.permits(4):
        summary["deadline_reached"] = True
        return summary
    try:
        store = store or ExecutionStore(client=window.client("dynamodb"))
        due = store.due_cleanup(max_records)
    except CleanupDeadline:
        summary["deadline_reached"] = True
        return summary
    reports = store.due_write_reports(max_records) if hasattr(store, "due_write_reports") else []
    for item in reports:
        summary["selected"] += 1
        if not window.permits(8):
            summary["deferred"] += 1
            summary["deadline_reached"] = True
            continue
        try:
            acknowledged = (report_write(item) if report_write is not None
                            else repair_write_report(item, window=window))
            store.finish_write_report(item, recorded=acknowledged is True)
        except CleanupDeadline:
            summary["deadline_reached"] = True
            break
        except Exception:
            try:
                store.finish_write_report(item, recorded=False)
            except Exception:
                summary["deferred"] += 1
        summary["processed"] += 1
    readbacks = store.due_write_readbacks(max_records) if hasattr(store, "due_write_readbacks") else []
    for item in readbacks:
        summary["selected"] += 1
        if not window.permits(8):
            summary["deferred"] += 1
            summary["deadline_reached"] = True
            continue
        try:
            performed = (readback(item) if readback is not None else readback_write(item, window=window))
            store.finish_write_readback(item, performed=performed is True)
        except CleanupDeadline:
            summary["deadline_reached"] = True
            break
        except Exception:
            try:
                store.finish_write_readback(item, performed=False)
            except Exception:
                summary["deferred"] += 1
        summary["processed"] += 1
    for item in due:
        summary["selected"] += 1
        state = item.get("state", {}).get("S")
        provider = (state == "closing" and stop_provider is not None and
                    item.get("tool", {}).get("S") in {"browser___start", "code_interpreter___start"})
        if not window.permits(window.PROVIDER_SECONDS if provider else 4):
            # No claim/attempt is written for work we have not started. Later
            # cheap rows may still fit even when another provider call cannot.
            summary["deferred"] += 1
            summary["deadline_reached"] = True
            continue
        try:
            if state == "closed":
                store.repair_terminal_due(item)
            else:
                resumed = store.resume_cleanup(item)
                if resumed and state != "active":
                    native = None
                    if resumed.get("native_test_run_id"):
                        try:
                            native = (native_authorizer or native_cleanup_handoff)(resumed)
                            if type(native) is not NativeCleanupHandoff:
                                raise ValueError("Native cleanup funding required")
                            native.require_fresh()
                        except Exception:
                            store.cleanup_outcome(resumed, "native_funding_unavailable")
                            summary["processed"] += 1
                            continue
                    if resumed.get("tool", {}).get("S") in SYNCHRONOUS_TOOLS:
                        store.confirm_due_search(resumed)
                    elif stop_provider is None:
                        store.cleanup_outcome(resumed, "provider_unavailable")
                    else:
                        try:
                            evidence = (stop_provider(resumed, native_handoff=native) if native is not None
                                        else stop_provider(resumed))
                        except CleanupDeadline:
                            raise
                        except Exception:
                            store.cleanup_outcome(resumed, "provider_error")
                        else:
                            if not store.confirm_due_provider(resumed, evidence):
                                store.cleanup_outcome(resumed, "provider_unknown")
            summary["processed"] += 1
        except CleanupDeadline:
            # resume_cleanup already preserved unknown outcome and retry time
            # for any started work. Never replace uncertainty with completion.
            summary["deadline_reached"] = True
            break
    return summary


def main():
    window = CleanupWindow()
    result = run_once(max_records=int(os.environ.get("ADP_TOOLS_CLEANUP_BATCH_SIZE", "50")),
                      stop_provider=lambda item, **kwargs: stop_provider(item, window=window, **kwargs), window=window)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
