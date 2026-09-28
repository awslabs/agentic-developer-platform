#!/usr/bin/env python3
"""One bounded no-GitHub-tenant task proving actual clarification and artifact retrieval."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import time
from pathlib import Path
import uuid


def event_type(event):
    """Public SSE uses event:event; the persisted kind is data.type."""
    payload = event.get("data")
    return (
        payload.get("type", event.get("event"))
        if isinstance(payload, dict)
        else event.get("event")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-ready", action="store_true")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--input-release-file", type=Path)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/home/ubuntu/task-delivery-tmp/isolation/isolated-token.json"),
    )
    parser.add_argument(
        "--source", type=Path, default=Path("/home/ubuntu/task-delivery/release")
    )
    args = parser.parse_args()
    if not args.activation_ready:
        parser.error("Wait for root activation and new result-artifact worker image")
    spec = importlib.util.spec_from_file_location(
        "task_client", args.source / "examples/task-api/client.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.Client(
        args.url, json.loads(args.token_file.read_text())["access_token"]
    )
    args.output.mkdir(exist_ok=True, parents=True)
    path = args.output / "state.json"
    state = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "schema_version": "1.0",
            "key": args.key,
            "max_new_tasks": 1,
            "max_usd_per_task": 1,
            "command_id": str(uuid.uuid4()),
            "checks": [],
            "criterion_outcome": "NOT RUN",
            "request": {
                "schema_version": "1.0",
                "persona": "agent-task-investigator",
                "instructions": "Investigate an incident, but no incident facts, logs or metrics have been supplied yet. On the initial turn return findings=[] and explain the missing evidence in uncertainties; do not turn the absence of evidence into a causal finding. Ask the caller for logs before concluding. Once logs arrive as follow-up input, identify the supported mechanism, cite follow_up_input, distinguish observation from hypothesis, and state missing upstream root cause evidence. Use only supplied information; no network, shell, GitHub or other external tools.",
                "inputs": {},
                "external_reference": args.key,
                "acceptance_criteria": [
                    "Request incident evidence before causal findings.",
                    "Use the caller response and cite follow_up_input.",
                    "Return a structured grounded report with explicit uncertainty.",
                ],
            },
        }
    )
    assert state["key"] == args.key

    def save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(state, indent=2) + "\n")

    def check(name, outcome, **data):
        state["checks"].append(
            {
                "check": name,
                "outcome": outcome,
                "at": datetime.now(timezone.utc).isoformat(),
                **data,
            }
        )
        save()

    def wait_for_input_release():
        if args.input_release_file is None or args.input_release_file.exists():
            return
        print(json.dumps({"awaiting_input_release": str(args.input_release_file), "task_id": task_id}), flush=True)
        deadline = time.monotonic() + 600
        while not args.input_release_file.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Input held for ten minutes; task and command are preserved")
            time.sleep(1)

    save()
    if "accepted" not in state:
        state["accepted"] = client.submit(state["request"], state["key"])
        save()
    task_id = state["accepted"]["task_id"]
    print(json.dumps({"task_id": task_id}), flush=True)
    # Resume an interrupted clarification using the exact persisted command.
    # No new task or new command identity is created by a diagnostic restart.
    if state.get("message") and "command_receipt" not in state:
        wait_for_input_release()
        state["command_receipt"] = client.json(
            "POST", f"/v1/tasks/{task_id}/messages", state["message"], retry=True
        )
        save()
        check("clarification-response-accepted", "PASS", command_id=state["command_id"])
    with (args.output / "events.ndjson").open("a") as events:
        for event in client.events(task_id, state.get("cursor"), seconds=600):
            state["cursor"] = event.get("id", state.get("cursor"))
            save()
            events.write(
                json.dumps(
                    {
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "event": event,
                    }
                )
                + "\n"
            )
            events.flush()
            if event_type(event) == "input.required" and "command_receipt" not in state:
                request_id = event["data"]["data"]["input_request_id"]
                state["message"] = {
                    "schema_version": "1.0",
                    "command_id": state["command_id"],
                    "reply_to": request_id,
                    "text": "Incident evidence: at14:08 inventory latency increased from180ms to6200ms; at14:10 checkout logged pool exhausted with all20 connections occupied, then503 acquisition timeout. Configuration pool.max=20 and upstream.timeout_seconds=30. No inventory-service logs or later metrics were supplied. Prioritize pool exhaustion as the checkout failure mechanism; do not invent inventory root cause.",
                }
                save()
                check("actual-input-required", "PASS", input_request_id=request_id)
                wait_for_input_release()
                state["command_receipt"] = client.json(
                    "POST",
                    f"/v1/tasks/{task_id}/messages",
                    state["message"],
                    retry=True,
                )
                save()
                check(
                    "clarification-response-accepted",
                    "PASS",
                    command_id=state["command_id"],
                )
            if event_type(event) in (
                "task.completed",
                "task.failed",
                "task.cancelled",
            ):
                break
    snapshot = client.snapshot(task_id)
    state["snapshot"] = snapshot
    save()
    check(
        "completed",
        "PASS" if snapshot.get("status") == "completed" else "FAIL",
        status=snapshot.get("status"),
    )
    receipt = next(
        (
            item
            for item in snapshot.get("command_receipts", [])
            if item.get("command_id") == state["command_id"]
        ),
        {},
    )
    check(
        "clarification-consumed",
        "PASS"
        if receipt.get("status") == "consumed" and receipt.get("handoff") == "confirmed"
        else "FAIL",
        receipt=receipt,
    )
    result = snapshot.get("result") or {}
    artifacts = result.get("artifact_ids", [])
    check(
        "result-artifact-present",
        "PASS" if artifacts else "FAIL",
        artifact_ids=artifacts,
    )
    for artifact_id in artifacts:
        content = client.artifact(task_id, artifact_id)
        (args.output / (artifact_id + ".json")).write_bytes(content)
        report = json.loads(content)
        check(
            "result-artifact-integrity",
            "PASS" if report == result.get("report") else "FAIL",
            artifact_id=artifact_id,
            sha256=hashlib.sha256(content).hexdigest(),
            bytes=len(content),
        )
    check(
        "cost-attribution",
        "PASS"
        if result.get("total_usd") is not None and 0 <= float(result["total_usd"]) <= 1
        else "NOT RUN",
        total_usd=result.get("total_usd"),
    )
    save()
    return 1 if any(item["outcome"] == "FAIL" for item in state["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
