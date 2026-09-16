"""adp-trigger CLI entry point.

Usage:
  adp-trigger --persona <persona> --issue <number> [--repo <owner/repo>] [--reason <text>]
  adp-trigger status --run <run-id>
  adp-trigger control --run <run-id> --action <verb> --command-id <id>
                      [--instruction <text>] [--reason <text>]

The first form dispatches another agent persona via POST /agent/trigger. It is
unchanged: same flags, same body, same exit codes, and it stays reachable with no
subcommand word so every existing caller — and every prompt that mentions
``adp-trigger --persona`` — keeps working. That backward compatibility is why
argument parsing is hand-rolled rather than moved to ``argparse`` subparsers,
which would require a subcommand token and reject today's invocations.

The two subcommands are new (#5028). Both authenticate the *individual run*
rather than the shared pod IAM role: they present a run credential, and the
gateway derives the caller's invocation and attempt from inside it. Neither sends
``ADP_MESSAGE_ID`` — a worker can rewrite its own environment, so a body-supplied
identity is a self-assertion.

Must run inside an agent pod. Exits 2 for a usage or environment problem, 1 for a
transport failure or refusal, 3 when the verb is not implemented in this
deployment, 4 when a timed-out control command has an unknown outcome.

Examples:
  adp-trigger --persona reviewer --issue 42
  adp-trigger --persona operations --issue 100 --repo aws-e/adp --reason "deploy needed"
  adp-trigger status --run inv-abc123
  adp-trigger control --run inv-abc123 --action pause --command-id cmd-1 --reason "budget review"
"""

from __future__ import annotations

import json
import os
import sys

from adp_trigger.client import (
    EXIT_UNKNOWN,
    ControlOutcomeUnknown,
    build_body,
    build_control_body,
    send_control,
    send_status,
    send_trigger,
    send_wave_binding,
)

# Verbs the CLI will send. This is a usage check, not an authorization check —
# the gateway decides, and answers 501 for a verb it cannot perform. The list
# exists so a typo ("--action pasue") is a local usage error rather than a signed
# request refused remotely for a reason nobody can act on.
#
# Every verb here returns 501 in this deployment. That is the honest answer and it
# is deliberately reachable: an operator who tries one should learn it is not
# built, rather than watch it silently do nothing.
CONTROL_ACTIONS = ("pause", "resume", "steer", "abort")


def _usage() -> None:
    print(
        "Usage:\n"
        "  adp-trigger --persona PERSONA --issue NUMBER [--repo OWNER/REPO] [--reason TEXT]\n"
        "  adp-trigger status --run RUN_ID\n"
        "  adp-trigger bind-wave --repo OWNER/REPO --epic epic-N --wave wave-N --orchestrator ISSUE --evaluation ISSUE\n"
        "  adp-trigger control --run RUN_ID --action ACTION --command-id ID\n"
        "                      [--instruction TEXT] [--reason TEXT]\n"
        "\n"
        "Dispatch another agent persona, or monitor/control a run you hold\n"
        "delegated authority over. Must run inside an agent pod (requires\n"
        "ADP_CORRELATION_ID, ADP_MESSAGE_ID, ADP_CHAIN_DEPTH, ADP_TRIGGER_ENDPOINT;\n"
        "status and control require ADP_RUN_CREDENTIAL_FILE or ADP_RUN_CREDENTIAL).\n"
        "A configured credential file is reread for each request.\n"
        "\n"
        "Dispatch options:\n"
        "  --persona  Target persona to trigger (e.g. developer, reviewer, operations)\n"
        "  --issue    GitHub issue number to dispatch on\n"
        "  --repo     Target repository (default: current repo from GITHUB_REPOSITORY)\n"
        "  --reason   Optional reason for the trigger (shown in Activity)\n"
        "\n"
        "status options:\n"
        "  --run      Run/invocation ID to report on. Your own run always works;\n"
        "             another run requires delegated authority traceable to a\n"
        "             human authorization.\n"
        "\n"
        "control options:\n"
        f"  --action       One of: {', '.join(CONTROL_ACTIONS)}\n"
        "  --run          Target run/invocation ID\n"
        "  --command-id   Idempotency key. Resubmitting the same ID returns the\n"
        "                 recorded outcome instead of acting twice.\n"
        "  --instruction  Text for --action steer\n"
        "  --reason       Why this command was issued (recorded for audit)\n"
        "\n"
        "Exit codes:\n"
        "  0 success   1 refused/unreachable   2 usage or environment   3 verb not\n"
        "  implemented in this deployment   4 command outcome unknown\n"
        "  A control timeout performs one status read and never retries the command.\n",
        file=sys.stderr,
    )
    sys.exit(2)


def _parse_flags(args: list[str], allowed: dict[str, str]) -> dict[str, str]:
    """Parse ``--flag value`` pairs, rejecting anything unrecognized.

    One parser for all three forms so a flag cannot behave differently depending
    on which subcommand it appears under. ``allowed`` maps flag name to the key it
    lands under, and is also the allowlist: an unknown flag is a usage error
    rather than being ignored, because silently dropping ``--reasson`` would lose
    an audit reason the operator believed they had recorded.
    """
    parsed: dict[str, str] = {}
    i = 0
    while i < len(args):
        flag = args[i]
        if flag not in allowed:
            print(f"error: unknown argument: {flag}", file=sys.stderr)
            _usage()
        if i + 1 >= len(args):
            print(f"error: {flag} requires a value", file=sys.stderr)
            _usage()
        value = args[i + 1]
        # A value that looks like a flag is almost always a missing argument
        # ("--run --action pause"), which would otherwise send a request about a
        # run literally named "--action".
        if value.startswith("--"):
            print(f"error: {flag} requires a value, got: {value}", file=sys.stderr)
            _usage()
        parsed[allowed[flag]] = value
        i += 2
    return parsed


def _dispatch(args: list[str]) -> dict:
    """The original ``--persona`` path. Behaviour unchanged."""
    parsed = _parse_flags(
        args,
        {
            "--persona": "persona",
            "--issue": "issue",
            "--repo": "repo",
            "--reason": "reason",
        },
    )

    persona = parsed.get("persona")
    raw_issue = parsed.get("issue")
    repo = parsed.get("repo")
    reason = parsed.get("reason")

    if not persona:
        print("error: --persona is required", file=sys.stderr)
        _usage()
    if raw_issue is None:
        print("error: --issue is required", file=sys.stderr)
        _usage()
    try:
        issue = int(raw_issue)
    except ValueError:
        print(f"error: --issue must be a number, got: {raw_issue}", file=sys.stderr)
        sys.exit(2)

    if not repo:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not repo:
            print(
                "error: --repo is required (GITHUB_REPOSITORY not set in environment)",
                file=sys.stderr,
            )
            sys.exit(2)

    # Build the request body (validates env vars, exits 2 if missing)
    body = build_body(persona=persona, issue=issue, repo=repo, reason=reason)
    return send_trigger(body)


def _bind_wave(args: list[str]) -> dict:
    parsed = _parse_flags(
        args,
        {
            "--repo": "repo",
            "--epic": "epic_ref",
            "--wave": "wave_ref",
            "--orchestrator": "orchestrator_issue",
            "--evaluation": "evaluation_issue",
        },
    )
    parsed.setdefault("repo", os.environ.get("GITHUB_REPOSITORY", ""))
    if not all(
        parsed.get(key)
        for key in ("repo", "epic_ref", "wave_ref", "orchestrator_issue", "evaluation_issue")
    ):
        print(
            "error: bind-wave requires repo, epic, wave, orchestrator and evaluation",
            file=sys.stderr,
        )
        sys.exit(2)
    body = dict(parsed)
    try:
        for key in ("orchestrator_issue", "evaluation_issue"):
            body[key] = int(parsed[key])
            if body[key] <= 0:
                raise ValueError
        if body["orchestrator_issue"] == body["evaluation_issue"]:
            raise ValueError
    except ValueError:
        print(
            "error: orchestrator and evaluation must be distinct positive issue numbers",
            file=sys.stderr,
        )
        sys.exit(2)
    return send_wave_binding(body)


def _status(args: list[str]) -> dict:
    """Report a run's state under delegated authority."""
    parsed = _parse_flags(args, {"--run": "run"})
    run_id = parsed.get("run")
    if not run_id:
        print("error: status requires --run RUN_ID", file=sys.stderr)
        _usage()
    return send_status(run_id)


def _control(args: list[str]) -> dict:
    """Send a control command for a run under delegated authority."""
    parsed = _parse_flags(
        args,
        {
            "--run": "run",
            "--action": "action",
            "--command-id": "command_id",
            "--instruction": "instruction",
            "--reason": "reason",
        },
    )

    run_id = parsed.get("run")
    action = parsed.get("action")
    command_id = parsed.get("command_id")

    if not run_id:
        print("error: control requires --run RUN_ID", file=sys.stderr)
        _usage()
    if not action:
        print("error: control requires --action ACTION", file=sys.stderr)
        _usage()
    if action not in CONTROL_ACTIONS:
        print(
            f"error: --action must be one of: {', '.join(CONTROL_ACTIONS)} (got: {action})",
            file=sys.stderr,
        )
        sys.exit(2)
    if not command_id:
        # Required rather than generated. A generated ID would make every retry a
        # new command, so a caller retrying after a timeout could act twice — the
        # exact double-application the worker's journal exists to prevent.
        print(
            "error: control requires --command-id ID (the idempotency key; reusing "
            "it returns the recorded outcome rather than acting again)",
            file=sys.stderr,
        )
        sys.exit(2)

    instruction = parsed.get("instruction")
    if action == "steer" and not instruction:
        print("error: --action steer requires --instruction TEXT", file=sys.stderr)
        sys.exit(2)
    if action != "steer" and instruction:
        # Rejected rather than dropped: an instruction the caller believes was
        # sent, on a verb that carries none, is a silent misunderstanding.
        print(
            f"error: --instruction is only valid with --action steer (got: {action})",
            file=sys.stderr,
        )
        sys.exit(2)

    body = build_control_body(
        command_id=command_id,
        instruction=instruction,
        reason=parsed.get("reason"),
    )
    return send_control(run_id, action, body)


def main() -> None:
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        _usage()

    # Subcommand dispatch. A bare first token selects a subcommand; a leading flag
    # means the original dispatch form. Checked in this order so
    # ``--persona status`` is still a dispatch of the persona named "status"
    # rather than being reinterpreted as a subcommand.
    if args[0] == "status":
        result = _status(args[1:])
    elif args[0] == "bind-wave":
        result = _bind_wave(args[1:])
    elif args[0] == "control":
        try:
            result = _control(args[1:])
        except ControlOutcomeUnknown as exc:
            print(json.dumps(exc.result, indent=2))
            sys.exit(EXIT_UNKNOWN)
    elif args[0].startswith("--"):
        result = _dispatch(args)
    else:
        print(f"error: unknown command: {args[0]}", file=sys.stderr)
        _usage()

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
