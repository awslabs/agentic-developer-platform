#!/usr/bin/env python3
"""CLI-16 terminal-control diagnostic; does not qualify active-run controls.

Runs only against an explicitly supplied, owned terminal invocation. No agent is
launched and no budget or deployment setting is changed. The existing dispatcher
and installed CLI are reused; this is deliberately outside the full acceptance
matrix until active pause, steering and tenant fixtures are also qualified.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import uuid
from pathlib import Path

import common
from capability_contrast import _write_session

TERMINAL = frozenset(
    {
        "complete",
        "failed",
        "aborted",
        "rejected",
        "rate_limited",
        "no_op",
        "blocked",
        "skipped",
        "budget_stopped",
    }
)


def _terminal(envelope):
    common.require(envelope.get("status") == "ok", "Could not read the owned run")
    detail = envelope.get("detail") or {}
    common.require(
        detail.get("status") in TERMINAL,
        "Terminal-only scenario refuses an active or unknown run",
    )
    return detail["status"]


def _refused(code, payload):
    common.require(
        isinstance(payload, dict), "Refusal did not return a CLI JSON envelope"
    )
    error = payload.get("error") or {}
    # The CLI may refuse from the authoritative state read before POST. A run
    # that terminates between the read and mutation instead returns HTTP 410.
    common.require(
        (
            code == 4
            and payload.get("status") == "unavailable"
            and (payload.get("detail") or {}).get("state") == "terminal"
        )
        or (code != 0 and error.get("http_status") == 410),
        "A terminal control was not authoritatively refused",
    )


def exercise(cli, run_id, evidence):
    before = _terminal(cli.json(["agent", "status", "--run", run_id]))
    command_id = str(uuid.uuid4())
    reason = "CLI-16 terminal-only regression; no active work may be changed"
    base = ["--run", run_id, "--command-id", command_id, "--reason", reason, "--yes"]
    # Two independent client processes, one stable identity. On a terminal run
    # this proves concurrent refusal, NOT the active journal replay guarantee.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: cli.run(["agent", "pause", *base], expected=None), range(2)
            )
        )
    for code, payload in results:
        _refused(code, payload)
    for action in ("resume", "abort", "steer"):
        args = [
            "agent",
            action,
            "--run",
            run_id,
            "--command-id",
            str(uuid.uuid4()),
            "--yes",
        ]
        args += ["--instruction", reason] if action == "steer" else ["--reason", reason]
        _refused(*cli.run(args, expected=None))
    # A transport error is not enough: demand the documented terminal refusal.
    code, result = cli.run(
        ["agent", "logs", "--run", run_id, "--follow", "--timeout", "10"], expected=None
    )
    common.require(
        code != 0 and isinstance(result, dict), "Terminal stream unexpectedly succeeded"
    )
    common.require(
        (result.get("error") or {}).get("http_status") == 409,
        "Terminal stream did not return HTTP 409",
    )
    after = _terminal(cli.json(["agent", "status", "--run", run_id]))
    common.require(
        after == before, "Terminal run status changed during refused controls"
    )
    evidence.update(
        run_id=run_id,
        before_status=before,
        after_status=after,
        command_id=command_id,
        cases=[
            "concurrent-terminal-pause",
            "terminal-resume",
            "terminal-steer",
            "terminal-abort",
            "terminal-stream",
        ],
        qualification="terminal CLI diagnostic only; active pause/steering/tenant acceptance remains open",
    )


def execute(config, evidence):
    fixture = config.get("agent_terminal_controls") or {}
    run_id = fixture.get("run_id")
    common.require(
        isinstance(run_id, str) and 0 < len(run_id) <= 128,
        "Supply an owned terminal run_id",
    )
    common.require(config.get("cli_path"), "install_auth did not retain the served CLI")
    with tempfile.TemporaryDirectory(prefix="adp-agent-terminal-") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        env = common.clean_env(config, HOME=home)
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            path = home / name
            path.mkdir(mode=0o700)
            env[name] = str(path)
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=30)
        exercise(cli, run_id, evidence)
