"""Collect exact human/task/receipt evidence without issuing consent or native work."""

from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from urllib.parse import quote


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def get_json(client, path):
    response = client.get(path)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("first-party reuse observation is not an object")
    return value


def human(client):
    value = get_json(client, "/api/auth/me")
    if (value.get("account_type") != "human"
            or any(not isinstance(value.get(key), str) or not value[key] for key in ("user_id", "org_id"))):
        raise ValueError("reuse observation requires an authenticated human identity")
    return {key: value[key] for key in ("user_id", "org_id", "account_type")}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation timestamp needs an explicit timezone")
    return parsed


def task_identity(client, selection, task_id, run):
    task = get_json(client, "/api/v1/tasks/" + quote(task_id, safe=""))
    return verify_task(task, selection, task_id, run)


def verify_task(task, selection, task_id, run):
    """Revalidate retained canonical task evidence without accepting a pass flag."""
    scope = selection["scope"]
    admission = run.get("admission") or {}
    causal = {run.get("run_id"), run.get("trigger_id"), admission.get("task_id"), (run.get("input") or {}).get("task_id")}
    if (task.get("task_id") != task_id or task.get("status") != "completed"
            or (task.get("product_id"), task.get("project_id")) != (scope["product_id"], scope["project_id"])
            or task_id in causal or not task.get("invocation_id") or task["invocation_id"] in causal
            or task.get("external_reference") is not None and task["external_reference"] in causal
            or timestamp(task.get("created_at", "")) <= timestamp(admission.get("admitted_at", ""))):
        raise ValueError("reuse requires a completed, distinct, later canonical task in this Product/project")
    return {key: task.get(key) for key in ("task_id", "invocation_id", "persona", "status", "version", "created_at", "product_id", "project_id", "external_reference")}


def retained_authorization(client, path, selection, actor, result, publication):
    preview = get_json(client, path + "/reuse-scope")
    scope = preview.get("scope")
    if (not isinstance(scope, dict) or preview.get("scope_digest") != canonical(scope)
            or scope.get("actor_id") != actor["user_id"] or scope.get("tenant_id") != actor["org_id"]
            or any(scope.get(key) != selection["scope"][key] for key in ("tenant_id", "product_id", "project_id"))
            or scope.get("result_id") != result["result_id"] or scope.get("result_revision") != result["revision"]
            or scope.get("run_id") != result["run_id"] or scope.get("result_digest") != canonical(result)
            or scope.get("actions") != ["read", "use"] or scope.get("result_scope") != result["scope"]
            or not isinstance(scope.get("cleanup"), dict)
            or not re.fullmatch(r"[a-f0-9]{64}", scope["cleanup"].get("receipt_digest", ""))
            or not isinstance(scope.get("publications"), list) or len(scope["publications"]) != 2
            or {row.get("destination") for row in scope["publications"]} != {"product-context", "engineering-insights"}):
        raise ValueError("retained reuse scope differs from the exact selected result")
    peers = {row["destination"]: row for row in publication["consumers"]}
    for item in scope["publications"]:
        if (item.get("publication_id") != peers[item["destination"]].get("publication_id")
                or type(item.get("revision")) is not int or item["revision"] < 1
                or any(not re.fullmatch(r"[a-f0-9]{64}", item.get(key, "")) for key in ("contribution_hash", "receipt_digest"))):
            raise ValueError("retained consent covers different consumer publication revisions")
    now = datetime.now(UTC)
    grants = get_json(client, path + "/reuse-authorizations").get("items")
    if not isinstance(grants, list):
        raise ValueError("retained reuse consent receipt is missing")
    current = [row for row in grants if row.get("revoked_at") is None and timestamp(row.get("expires_at", "")) > now]
    if (len(current) != 1 or current[0].get("scope_digest") != preview["scope_digest"]
            or type(current[0].get("revision")) is not int or current[0]["revision"] < 1
            or not isinstance(current[0].get("authorization_id"), str) or not current[0]["authorization_id"]):
        raise ValueError("the exact retained result needs current explicit owner consent")
    return {"scope": scope, "authorization": current[0]}


def verify_usage(usage, result, actor, task, publication):
    reference = {"tenant_id": result["scope"]["tenant_id"], "project_id": result["scope"]["project_id"],
                 "source_connection_id": "l5-well-architected", "record_type": "well_architected_result",
                 "record_id": result["result_id"], "record_revision": str(result["revision"])}
    citation = f"well_architected_result:{result['result_id']}@{result['revision']}"
    consumers = publication.get("consumers")
    if (canonical(usage.get("result")) != canonical(result) or usage.get("citation") != citation
            or not isinstance(consumers, list) or len(consumers) != 2
            or {row.get("destination") for row in consumers} != {"product-context", "engineering-insights"}):
        raise ValueError("later task or consumer acknowledgement differs from the selected result")
    peers = {row["destination"]: row for row in consumers}
    for destination, name in (("product-context", "retrieval_receipt"), ("engineering-insights", "usage_receipt")):
        receipt, peer = usage.get(name), peers[destination]
        if (not isinstance(receipt, dict) or peer.get("state") != "published" or peer.get("projection_verified_on_ack") is not True
                or not peer.get("receipt_id") or not peer.get("consumer_revision")
                or not isinstance(receipt.get("receipt_id"), str) or not receipt["receipt_id"]
                or receipt.get("contribution_id") != peer.get("publication_id") or not receipt.get("contribution_id")
                or receipt.get("actor_id") != actor["user_id"] or receipt.get("task_id") != task["task_id"]
                or receipt.get("result_ref") != reference or receipt.get("citation") != citation
                or destination == "engineering-insights" and receipt.get("retrieval_receipt_id") != usage["retrieval_receipt"]["receipt_id"]):
            raise ValueError("later-task receipts do not bind the current user, task and both publications")
    if (usage["retrieval_receipt"]["receipt_id"] == usage["usage_receipt"]["receipt_id"]
            or len({row["publication_id"] for row in consumers}) != 2):
        raise ValueError("later-task consumer receipts must be independent")
    return {key: usage[key] for key in ("citation", "retrieval_receipt", "usage_receipt")}


def collect_usage(client, product, path, selection, result, publication, *, read_only):
    selected = selection["qualification_clients"]
    task_id = selected["design_task_id"]
    actor = human(client)
    if actor["org_id"] != selection["scope"]["tenant_id"]:
        raise ValueError("later-task reader belongs to another tenant")
    run = product.get("runs/" + quote(selection["run_id"], safe=""))
    if run.get("run_id") != selection["run_id"]:
        raise ValueError("later-task origin run changed")
    task = task_identity(client, selection, task_id, run)
    consent = (retained_authorization(client, product.path + "/" + path, selection, actor, result, publication)
               if selected.get("retained_reuse") is True else None)
    response = (client.get(product.path + "/" + path + "/design-usage/" + quote(task_id, safe="")) if read_only
                else client.post(product.path + "/" + path + "/design-usage", json={"task_id": task_id}))
    response.raise_for_status()
    usage = verify_usage(response.json(), result, actor, task, publication)
    if human(client) != actor or task_identity(client, selection, task_id, run) != task:
        raise ValueError("later-task authority changed during collection")
    if consent is not None and retained_authorization(client, product.path + "/" + path, selection, actor, result, publication) != consent:
        raise ValueError("retained-result consent changed during collection")
    origin = {key: run.get(key) for key in ("run_id", "trigger_id", "admission", "input")}
    return {**usage, "task_id": task_id, "actor_id": actor["user_id"], "actor": actor,
            "task": task, "origin": origin, "retained_authorization": consent,
            "source": "authenticated-product-api", "provider_effect_count": None}
