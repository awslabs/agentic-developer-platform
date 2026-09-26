"""Opt-in two-turn hosted chat diagnostic; E40 remains read-only."""

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

import common
from capability_contrast import _write_session

PERSONA = "agent-task-investigator"
TASK_ID = re.compile(
    r"tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def valid_fixture(value):
    return (
        isinstance(value, dict)
        and value.get("enrollment_verified") is True
        and value.get("shared_budget_authorized") is True
        and value.get("max_tasks") == 2
        and type(value.get("max_task_usd")) in (int, float)
        and 0 < value["max_task_usd"] <= 1
        and isinstance(value.get("canonical_user_id"), str)
        and bool(value["canonical_user_id"])
        and isinstance(value.get("tenant_id"), str)
        and bool(value["tenant_id"])
    )


def detail(value):
    return value.get("detail") or {} if isinstance(value, dict) else {}


def persist(path, evidence):
    with path.open("w") as stream:
        json.dump(evidence["detail"], stream)
        stream.flush()
        os.fsync(stream.fileno())


def reconcile(cli, command, receipt):
    """At most one replay of the same chat request; never replace its identity."""
    for _ in range(2):
        try:
            _, result = cli.run([*command, "--yes"], expected=None)
        except Exception:
            result = None
        value = detail(result)
        task_id = value.get("task_id")
        if (
            value.get("request_id") == receipt["request_id"]
            and value.get("session_id") == receipt["session_id"]
            and isinstance(task_id, str)
            and TASK_ID.fullmatch(task_id)
        ):
            receipt.update(task_id=task_id, phase="accepted")
            return task_id
    return None


def observe_task(cli, receipt):
    """A session read can recover a committed Task after both replies were lost."""
    try:
        _, result = cli.run(
            ["chat", "show", "--session", receipt["session_id"]], expected=None
        )
        value = detail(result)
        task_id = value.get("task_id")
        if (
            value.get("session_id") == receipt["session_id"]
            and value.get("active_request_id") == receipt["request_id"]
            and isinstance(task_id, str)
            and TASK_ID.fullmatch(task_id)
        ):
            receipt.update(task_id=task_id, phase="accepted")
            return task_id
    except Exception:
        pass
    return None


def execute(config, evidence):
    fixture = config.get("human_task_chat")
    common.require(
        valid_fixture(fixture),
        "Explicit fixture-human enrollment and two-task shared-budget bound required",
    )
    common.require(
        config.get("work_dir") and config.get("cli_path"),
        "Installed EC2 fixture and durable run directory required",
    )
    common.require(
        config.get("test_user_id") == fixture.get("login_user_id", fixture["canonical_user_id"]),
        "Chat must use the installed fixture's canonical human",
    )
    marker = "memory-" + uuid.uuid4().hex[:12]
    messages = [
        f"Remember this label for our next turn: {marker}. Reply only NOTED.",
        "What label did I ask you to remember in our previous turn? Reply only with that label.",
    ]
    records = []
    evidence["detail"] = {
        "qualification": "Two hosted chat turns and owned cleanup; E40 read-only coverage is unchanged; spend reconciliation is separate",
        "gateway": config["gateway_url"],
        "tenant_id": fixture["tenant_id"],
        "canonical_user_id": fixture["canonical_user_id"],
        "turns": records,
    }
    recovery_dir = Path(config["work_dir"]) / ("chat-" + uuid.uuid4().hex)
    recovery_dir.mkdir(mode=0o700)
    recovery_path = recovery_dir / "recovery.json"
    recovery_path.touch(mode=0o600, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="adp-hosted-chat-") as directory:
        home = Path(directory)
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
        env["BG_CONFIG_DIR"] = str(home / ".bedrock-gateway")
        env["ADP_TENANT"] = fixture["tenant_id"]
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=150)
        capabilities = detail(cli.json(["chat", "status"]))
        common.require(
            capabilities.get("user_id") == fixture["canonical_user_id"]
            and capabilities.get("tenant_id") == fixture["tenant_id"],
            "Installed fixture session resolved a different human or tenant",
        )
        common.require(
            capabilities.get("general_turns_supported") is True
            and PERSONA in capabilities.get("authorized_personas", []),
            "Fixture human is not admitted for general hosted chat",
        )
        session_id = None
        try:
            for index, message in enumerate(messages):
                request_id = "chatdiag-" + uuid.uuid4().hex
                message_file = home / f"message-{index}.txt"
                message_file.write_text(message)
                command = (
                    [
                        "chat",
                        "start",
                        "--persona",
                        PERSONA,
                        "--message-file",
                        message_file,
                    ]
                    if index == 0
                    else ["chat", "resume", session_id, "--answer-file", message_file]
                ) + ["--request-id", request_id]
                preview = cli.json([*command, "--dry-run"])
                preview_detail = detail(preview)
                common.require(
                    preview.get("status") == "dry_run"
                    and preview_detail.get("dispatched") is False,
                    "Chat preview did not prove no dispatch",
                )
                proposed_session = preview_detail.get("session_id")
                common.require(
                    isinstance(proposed_session, str)
                    and proposed_session.startswith("chat-")
                    and (session_id is None or session_id == proposed_session),
                    "Chat preview changed session identity",
                )
                session_id = proposed_session
                receipt = {
                    "request_id": request_id,
                    "session_id": session_id,
                    "task_id": None,
                    "phase": "submit_attempted",
                    "endpoint": "/chat/sessions"
                    if index == 0
                    else f"/chat/sessions/{session_id}/turns",
                    "body": {
                        "message": message,
                        "request_id": request_id,
                        "persona": PERSONA,
                        "dry_run": False,
                    },
                }
                records.append(receipt)
                persist(recovery_path, evidence)
                task_id = reconcile(cli, command, receipt) or observe_task(cli, receipt)
                persist(recovery_path, evidence)
                common.require(
                    task_id,
                    "Chat acceptance unknown; exact request retained in durable diagnostic detail",
                )
                # Explicit replay proves no second task is created for this turn.
                repeated = reconcile(cli, command, {**receipt})
                common.require(
                    repeated == task_id,
                    "Repeated chat request changed or lost Task identity",
                )
                watched = detail(
                    cli.json(
                        [
                            "chat",
                            "watch",
                            "--session",
                            session_id,
                            "--task-id",
                            task_id,
                            "--timeout",
                            "120",
                        ],
                        timeout=130,
                    )
                )
                common.require(
                    watched.get("session_id") == session_id
                    and watched.get("answer_completion_verified") is True
                    and watched.get("matched_task_id") == task_id,
                    "Exact chat task did not complete with full history",
                )
                answers = [
                    row.get("content", "")
                    for row in watched.get("messages", [])
                    if row.get("role") == "assistant" and row.get("task_id") == task_id
                ]
                common.require(
                    len(answers) == 1,
                    "Chat task must have exactly one correlated final answer",
                )
                if index == 1:
                    common.require(
                        marker in answers[0],
                        "Second turn did not recall first-turn context",
                    )
                receipt.update(phase="completed", answer_verified=True)
                persist(recovery_path, evidence)
            common.require(
                len({record["task_id"] for record in records}) == 2,
                "Two completed turns did not have distinct Task identities",
            )
            shown = detail(cli.json(["chat", "show", "--session", session_id]))
            for receipt in records:
                rows = [
                    row
                    for row in shown.get("messages", [])
                    if row.get("task_id") == receipt["task_id"]
                ]
                common.require(
                    sum(row.get("role") == "user" for row in rows) == 1
                    and sum(row.get("role") == "assistant" for row in rows) == 1,
                    "Durable chat history duplicated or lost a turn",
                )
            evidence["detail"].update(
                session_id=session_id, context_recalled=True, task_count=2
            )
            evidence["success"] = True
        finally:
            for receipt in records:
                if receipt["phase"] == "completed":
                    continue
                task_id = receipt.get("task_id") or observe_task(cli, receipt)
                if not task_id:
                    receipt["phase"] = "acceptance_unknown"
                    continue
                command_id = str(uuid.uuid4())
                receipt["cleanup_command_id"] = command_id
                persist(recovery_path, evidence)
                try:
                    cli.run(
                        [
                            "agent",
                            "abort",
                            "--run",
                            task_id,
                            "--command-id",
                            command_id,
                            "--reason",
                            "Owned chat diagnostic cleanup",
                            "--yes",
                        ],
                        expected=None,
                    )
                    _, readback = cli.run(
                        ["agent", "wait", "--run", task_id, "--timeout", "60"],
                        expected=None,
                        timeout=70,
                    )
                    status = detail(readback).get("status")
                    receipt["phase"] = (
                        status
                        if status in {"completed", "failed", "cancelled"}
                        else "cleanup_pending"
                    )
                except Exception:
                    receipt["phase"] = "cleanup_pending"
            persist(recovery_path, evidence)
            if any(
                row["phase"] in {"acceptance_unknown", "cleanup_pending"}
                for row in records
            ):
                evidence["success"] = False
                common.require(
                    False,
                    "Owned chat recovery remains pending; exact original requests retained in diagnostic detail",
                )
