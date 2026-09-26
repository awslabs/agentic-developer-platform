"""E42: one explicitly enrolled human coding Task through the served CLI."""

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

import common
from capability_contrast import _write_session


def fixture_valid(fixture):
    return (
        isinstance(fixture, dict)
        and fixture.get("enrollment_verified") is True
        and fixture.get("shared_budget_authorized") is True
        and fixture.get("max_dispatches") == 1
        and type(fixture.get("max_task_usd")) in (int, float)
        and 0 < fixture["max_task_usd"] <= 1
        and fixture.get("scenario") in {"complete", "cancel"}
        and fixture.get("persona")
        in {"agent-task-claude-developer", "agent-task-codex-developer"}
        and isinstance(fixture.get("snapshot"), dict)
        and isinstance(fixture.get("instructions"), str)
        and 0 < len(json.dumps(fixture["instructions"]).encode()) <= 4096
        and common.redact(fixture["instructions"]) == fixture["instructions"]
    )


TASK_ID = re.compile(
    r"tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def local_receipt(home):
    paths = list((home / ".adp/state/hosted-tasks").glob("*.json"))
    common.require(len(paths) <= 1, "Unexpected multiple owned Task journals")
    if not paths:
        return {}
    value = json.loads(paths[0].read_text())
    return {
        key: value[key]
        for key in ("fingerprint", "artifact_id", "task_id")
        if key in value
    }


def reconcile_acceptance(cli, trigger, home):
    """Reuse the one admitted request identity; never manufacture a replacement."""
    receipt = local_receipt(home)
    task_id = receipt.get("task_id")
    if isinstance(task_id, str) and TASK_ID.fullmatch(task_id):
        return task_id
    if not receipt.get("artifact_id"):
        return None  # CLI cannot submit until the upload receipt is durable.
    try:
        _, replay = cli.run([*trigger, "--yes"], expected=None, timeout=150)
        task_id = ((replay or {}).get("detail") or {}).get("task_id")
    except Exception:
        task_id = None
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        task_id = local_receipt(home).get("task_id")
    return task_id if isinstance(task_id, str) and TASK_ID.fullmatch(task_id) else None


def persist_recovery(path, value):
    # Run-owned private directory. Keep request data/journal, never session credentials.
    with path.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def execute(config, evidence):
    try:
        _execute(config, evidence)
        evidence["success"] = True
    finally:
        # Only detail crosses journey_stage into durable run state/report.json.
        # Never rely on a path on the disposable worker surviving termination.
        detail = {
            key: value
            for key, value in evidence.items()
            if key not in {"detail", "transcript", "result", "recovery_path"}
        }
        if "result" in evidence:
            raw = json.dumps(evidence.pop("result"), sort_keys=True).encode()
            detail["result_sha256"] = hashlib.sha256(raw).hexdigest()
            detail["result_readback"] = (
                "Use the retained Task ID to fetch the canonical result."
            )
        evidence["detail"] = detail


def _execute(config, evidence):
    fixture = config.get("human_task_coding")
    common.require(
        fixture_valid(fixture),
        "Explicit human repository/model enrollment and bounded shared-budget authorization required",
    )
    common.require(config.get("cli_path"), "Served CLI was not installed")
    evidence.update(
        request_id="e42-" + uuid.uuid4().hex,
        command_id=str(uuid.uuid4()),
        cleanup_command_id=str(uuid.uuid4()),
    )
    evidence["qualification"] = (
        "One hosted coding Task; test execution/publication and shared spend reconciliation require separate evidence"
    )
    common.require(
        config.get("work_dir"), "Durable run directory required for Task reconciliation"
    )
    recovery_dir = Path(config["work_dir"]) / ("coding-" + evidence["request_id"])
    recovery_dir.mkdir(mode=0o700)
    recovery_path = recovery_dir / "recovery.json"
    recovery_path.touch(mode=0o600, exist_ok=False)
    recovery = {
        "request_id": evidence["request_id"],
        "gateway": config["gateway_url"],
        "persona": fixture["persona"],
        "snapshot": fixture["snapshot"],
        "instructions": fixture["instructions"],
        "phase": "prepared",
    }
    persist_recovery(recovery_path, recovery)
    evidence["recovery_path"] = str(recovery_path)
    with tempfile.TemporaryDirectory(prefix="adp-hosted-coding-") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        env = common.clean_env(config, HOME=home)
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "CODEX_HOME",
        ):
            target = home / name
            target.mkdir(mode=0o700)
            env[name] = str(target)
        # The seeded session helper uses ~/.bedrock-gateway; pin inherited BG_CONFIG_DIR too.
        env["BG_CONFIG_DIR"] = str(home / ".bedrock-gateway")
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        snapshot = home / "snapshot.json"
        snapshot.write_text(json.dumps(fixture["snapshot"]))
        instructions = home / "instructions.txt"
        instructions.write_text(fixture["instructions"])
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=150)
        trigger = [
            "agent",
            "trigger",
            "--repo",
            fixture["snapshot"]["repository"],
            "--issue",
            fixture["snapshot"]["issue"],
            "--persona",
            fixture["persona"],
            "--snapshot-file",
            snapshot,
            "--instructions-file",
            instructions,
            "--request-id",
            evidence["request_id"],
        ]
        preview = cli.json([*trigger, "--dry-run"])
        common.require(
            preview.get("status") == "dry_run", "Trigger preview was not read-only"
        )
        task_id = None
        terminal = False
        submitted = False
        try:
            recovery["phase"] = "submit_attempted"
            persist_recovery(recovery_path, recovery)
            submitted = True
            accepted = cli.json([*trigger, "--yes"], expected=4)
            task_id = (accepted.get("detail") or {}).get("task_id")
            common.require(
                isinstance(task_id, str) and task_id.startswith("tsk_"),
                "No canonical Task receipt",
            )
            evidence["task_id"] = task_id
            repeated = cli.json([*trigger, "--yes"], expected=4)
            common.require(
                (repeated.get("detail") or {}).get("task_id") == task_id,
                "Idempotent submit changed task identity",
            )
            observed = cli.json(["agent", "status", "--run", task_id])
            common.require(
                (observed.get("detail") or {}).get("task_id") == task_id,
                "Status changed task identity",
            )
            action = "abort" if fixture["scenario"] == "cancel" else "steer"
            text_flag = "--reason" if action == "abort" else "--instruction"
            command = [
                "agent",
                action,
                "--run",
                task_id,
                "--command-id",
                evidence["command_id"],
                text_flag,
                "E42 owned fixture cancellation"
                if action == "abort"
                else "Keep the change limited to the requested issue and submit the actual patch.",
            ]
            cli.json([*command, "--dry-run"])
            control = cli.json([*command, "--yes"], expected=4)
            evidence["control_receipt"] = control.get("detail")
            # Preserve each actual streamed frame, not just the final CLI envelope.
            code, stdout, _ = common.bounded(
                [
                    str(config["cli_path"]),
                    "agent",
                    "logs",
                    "--run",
                    task_id,
                    "--follow",
                    "--timeout",
                    "60",
                    "--json",
                ],
                env=env,
                timeout=70,
            )
            frames = [
                json.loads(line)
                for line in stdout.splitlines()
                if line.strip().startswith("{")
            ]
            evidence["stream_frame_count"] = len(frames)
            events = [frame for frame in frames if frame.get("type") == "event"]
            evidence["stream_event_count"] = len(events)
            evidence["event_cursors"] = [
                (frame.get("data") or {}).get("id") for frame in events
            ]
            evidence["monitor_exit"] = code
            common.require(
                code in {0, 4, 5, 7} and events, "No Task event stream evidence"
            )
            _, result = cli.run(
                [
                    "agent",
                    "wait",
                    "--run",
                    task_id,
                    "--timeout",
                    "60",
                    "--interval",
                    "1",
                ],
                expected=None,
                timeout=70,
            )
            detail = (result or {}).get("detail") or {}
            evidence["terminal_status"] = detail.get("status")
            terminal = detail.get("status") in {"completed", "failed", "cancelled"}
            common.require(
                detail.get("status")
                == ("cancelled" if action == "abort" else "completed"),
                "Requested terminal outcome was not observed",
            )
            evidence["result"] = detail.get("result")
        finally:
            if submitted and not task_id:
                task_id = reconcile_acceptance(cli, trigger, home)
                if task_id:
                    evidence["task_id"] = task_id
                    evidence["acceptance_reconciled"] = True
            recovery.update(
                journal=local_receipt(home),
                task_id=task_id,
                phase="terminal"
                if terminal
                else "cleanup_required"
                if task_id
                else "acceptance_unknown"
                if submitted
                else "prepared",
            )
            persist_recovery(recovery_path, recovery)
            artifact_id = recovery["journal"].get("artifact_id")
            evidence["recovery"] = {
                "request_id": evidence["request_id"],
                "gateway": config["gateway_url"],
                "task_id": task_id,
                "phase": recovery["phase"],
                "journal": recovery["journal"],
                "submit_body": {
                    "schema_version": "1.0",
                    "persona": fixture["persona"],
                    "instructions": fixture["instructions"],
                    "inputs": {"repository_snapshot_artifact": artifact_id},
                    "artifact_ids": [artifact_id],
                    "external_reference": fixture["snapshot"]["repository"]
                    + "#"
                    + str(fixture["snapshot"]["issue"]),
                }
                if artifact_id
                else None,
                "reconcile": "Replay POST /v1/tasks with this exact body and original Idempotency-Key; never create a replacement request.",
            }
            if submitted and not task_id:
                evidence["cleanup_status"] = (
                    "acceptance_unknown"
                    if recovery["journal"].get("artifact_id")
                    else "no_task_submitted"
                )
                common.require(
                    evidence["cleanup_status"] != "acceptance_unknown",
                    "Owned Task acceptance is unknown; durable same-key recovery retained",
                )
            if task_id and not terminal:
                cli.run(
                    [
                        "agent",
                        "abort",
                        "--run",
                        task_id,
                        "--command-id",
                        evidence["cleanup_command_id"],
                        "--reason",
                        "E42 cleanup",
                        "--yes",
                    ],
                    expected=None,
                )
                _, cleanup = cli.run(
                    [
                        "agent",
                        "wait",
                        "--run",
                        task_id,
                        "--timeout",
                        "60",
                        "--interval",
                        "1",
                    ],
                    expected=None,
                    timeout=70,
                )
                evidence["cleanup_status"] = ((cleanup or {}).get("detail") or {}).get(
                    "status"
                )
                recovery["phase"] = evidence["cleanup_status"]
                evidence["recovery"]["phase"] = evidence["cleanup_status"]
                persist_recovery(recovery_path, recovery)
                common.require(
                    evidence["cleanup_status"] in {"completed", "failed", "cancelled"},
                    "Owned Task cleanup remains pending",
                )
