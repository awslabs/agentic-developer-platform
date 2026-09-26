"""E42: one explicitly enrolled human coding Task through the served CLI."""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import common
from capability_contrast import _write_session
from coding_plan import recovery_plan


def fixture_valid(fixture):
    return (
        isinstance(fixture, dict)
        and fixture.get("enrollment_verified") is True
        and fixture.get("shared_budget_authorized") is True
        and fixture.get("max_dispatches") == 1
        and type(fixture.get("max_task_usd")) in (int, float)
        and 0 < fixture["max_task_usd"] <= 1
        and fixture.get("scenario") in {"complete", "cancel"}
        and fixture.get("control_when", "observed") in {"observed", "running"}
        and type(fixture.get("running_wait_seconds", 30)) is int
        and 1 <= fixture.get("running_wait_seconds", 30) <= 60
        and (
            fixture.get("control_when") != "running"
            or fixture.get("scenario") == "cancel"
        )
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


def observe_before_control(cli, task_id, fixture, evidence):
    deadline = time.monotonic() + fixture.get("running_wait_seconds", 30)
    evidence["control_when"] = fixture.get("control_when", "observed")
    evidence["observed_states"] = []
    while True:
        detail = cli.json(["agent", "status", "--run", task_id]).get("detail") or {}
        common.require(detail.get("task_id") == task_id, "Status changed task identity")
        state = detail.get("status")
        common.require(
            state
            in {
                "accepted",
                "queued",
                "running",
                "waiting_for_input",
                "cancel_requested",
                "completed",
                "failed",
                "cancelled",
            },
            "Unknown Task state",
        )
        evidence["observed_states"].append(state)
        evidence["pre_control_status"] = state
        if (
            evidence["control_when"] != "running"
            or state in {"running", "completed", "failed", "cancelled"}
            or time.monotonic() >= deadline
        ):
            return state
        time.sleep(1)


def require_control_receipt(control, task_id, command_id):
    detail = control.get("detail") or {}
    common.require(
        control.get("status") == "pending"
        and detail.get("task_id") == task_id
        and detail.get("command_id") == command_id,
        "Control acceptance unconfirmed; reconcile the original command identity",
    )


def local_receipt(home):
    state = home / ".adp/state"
    paths = list((state / "hosted-tasks").glob("*.json"))
    # The served shell pins ADP_TENANT_ID/SUB before launching its helper.
    # That moves legacy state into a subject/tenant hash namespace.
    paths.extend(state.glob("tenants/*/hosted-tasks/*.json"))
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


def activity_readback(cli, task_detail):
    """Require the returned invocation identity to resolve the same canonical Task."""
    invocation_id = task_detail.get("invocation_id")
    common.require(bool(invocation_id), "Task readback omitted invocation identity")
    response = cli.json(["agent", "status", "--run", invocation_id])
    detail = (response or {}).get("detail") or {}
    native = detail.get("task_snapshot") or {}
    common.require(
        detail.get("source_type") == "task"
        and detail.get("invocation_id") == invocation_id
        and detail.get("task_id") == task_detail.get("task_id")
        and native.get("task_id") == task_detail.get("task_id")
        and native.get("invocation_id") == invocation_id
        and native.get("status") == task_detail.get("status"),
        "Activity detail did not resolve the exact canonical Task",
    )
    return {
        "invocation_id": invocation_id,
        "task_id": detail["task_id"],
        "task_status": native["status"],
        "source_type": detail["source_type"],
        "transcript_status": detail.get("transcript_status"),
    }


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
    common.require(config.get("evaluation_id"), "Stable evaluation identity required")
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Caller must retain exact coding recovery plan before dispatch",
    )
    evidence.update(
        {key: plan[key] for key in ("request_id", "command_id", "cleanup_command_id")}
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
        identity_keys = {"login_user_id", "canonical_user_id", "tenant_id"}
        selected_identity = identity_keys.intersection(fixture)
        common.require(
            not selected_identity or selected_identity == identity_keys,
            "Coding fixture identity must include login, canonical user and tenant",
        )
        if selected_identity:
            common.require(
                config.get("test_user_id") == fixture["login_user_id"],
                "Coding must use the installed fixture login",
            )
        env = common.clean_env(
            config,
            HOME=home,
            **({"ADP_TENANT": fixture["tenant_id"]} if selected_identity else {}),
        )
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
        env["ADP_HOME"] = str(home / ".adp")
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        snapshot = home / "snapshot.json"
        snapshot.write_text(json.dumps(fixture["snapshot"]))
        instructions = home / "instructions.txt"
        instructions.write_text(fixture["instructions"])
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=150)
        if selected_identity:
            principal = cli.json(["models", "mappings", "list"]).get("detail") or {}
            common.require(
                principal.get("tenant_id") == fixture["tenant_id"]
                and principal.get("principal_id") == fixture["canonical_user_id"],
                "Coding fixture resolved a different owner or tenant",
            )
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
            accepted = cli.json([*trigger, "--yes"], expected=None)
            # Preserve only bounded machine fields, never API prose or stderr.
            error = accepted.get("error") or {}
            evidence["trigger_outcome"] = {
                key: value
                for key, value in {
                    "status": accepted.get("status"),
                    "code": error.get("code"),
                    "http_status": error.get("http_status", error.get("status_code")),
                }.items()
                if type(value) is int
                or isinstance(value, str)
                and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value)
            }
            common.require(
                accepted.get("status") == "pending",
                "Task trigger was not accepted; inspect retained machine outcome",
            )
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
            observed = observe_before_control(cli, task_id, fixture, evidence)
            terminal = observed in {"completed", "failed", "cancelled"}
            action = "abort" if fixture["scenario"] == "cancel" else None
            if action:
                common.require(
                    not terminal,
                    "Task became terminal before cancellation; active control unconfirmed",
                )
                common.require(
                    fixture.get("control_when", "observed") != "running"
                    or observed == "running",
                    "Task did not reach running before the bounded control deadline",
                )
                command = [
                    "agent",
                    "abort",
                    "--run",
                    task_id,
                    "--command-id",
                    evidence["command_id"],
                    "--reason",
                    "E42 owned fixture cancellation",
                ]
                cli.json([*command, "--dry-run"])
                control = cli.json([*command, "--yes"], expected=4)
                evidence["control_receipt"] = control.get("detail")
                require_control_receipt(control, task_id, evidence["command_id"])
            else:
                evidence["steering_effect"] = (
                    "unsupported: coding runtime has no input-consumption path"
                )
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
            common.require(
                detail.get("task_id") == task_id,
                "Terminal readback changed task identity",
            )
            evidence["terminal_status"] = detail.get("status")
            terminal = detail.get("status") in {"completed", "failed", "cancelled"}
            common.require(
                detail.get("status")
                == ("cancelled" if action == "abort" else "completed"),
                "Requested terminal outcome was not observed",
            )
            evidence["result"] = detail.get("result")
            evidence["activity_readback"] = activity_readback(cli, detail)
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
                    if recovery["journal"].get("artifact_id") or not recovery["journal"]
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
