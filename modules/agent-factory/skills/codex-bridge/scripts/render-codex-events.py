#!/usr/bin/env python3
"""render-codex-events.py — turn Codex `exec --json` JSONL into a compact,
human-readable step summary on stdout (codex-bridge skill, issue #2753).

Reads Codex's structured event stream on stdin (one JSON object per line, as
emitted by `codex exec --json` in the pinned CLI 0.142.5) and writes:

  * one compact line per meaningful step, in order:
        [codex reasoning] <summary>
        [codex exec] $ <command> (exit 0)
        [codex edit] add <path>
        [codex message] <intermediate narration, truncated>
  * Codex's FINAL agent message, verbatim and untruncated, last;
  * a trailer with the session id, token usage, and (if given) the raw JSONL
    log path.

Design contract (see issue #2753 impact table):
  * stdlib only (Python is already in the worker image);
  * TOLERANT — an unknown event type degrades to one generic line, and a
    malformed (non-JSON) line is passed through raw. The renderer must never
    crash the run; the wrapper takes Codex's own exit code from PIPESTATUS, so
    this script's job is display only.

Observed 0.142.5 schema (envelope + item payload):
  {"type":"thread.started","thread_id":"019f..."}
  {"type":"turn.started"}
  {"type":"item.completed","item":{"type":"file_change","changes":[{"path":"...","kind":"add"}]}}
  {"type":"item.completed","item":{"type":"command_execution","command":"...","exit_code":0}}
  {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
  {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,"output_tokens":..,"reasoning_output_tokens":..}}
"""

from __future__ import annotations

import argparse
import json
import sys

# Keep compact step lines to a single, readable line.
_MAX_LINE = 120


def _first_line(text: str, limit: int = _MAX_LINE) -> str:
    """Collapse a possibly multi-line string to a single truncated line."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


def _render_item(item: dict, out) -> None:
    """Print one compact line for a completed Codex item (never raises)."""
    itype = item.get("type", "unknown")

    if itype == "reasoning":
        # Reasoning payloads have varied across versions: prefer `text`, then a
        # `summary` list of strings.
        text = item.get("text")
        if not text and isinstance(item.get("summary"), list):
            text = " ".join(str(s) for s in item["summary"])
        out.write(f"[codex reasoning] {_first_line(text or '')}\n")

    elif itype == "command_execution":
        cmd = _first_line(item.get("command", ""))
        exit_code = item.get("exit_code")
        suffix = "" if exit_code is None else f" (exit {exit_code})"
        out.write(f"[codex exec] $ {cmd}{suffix}\n")

    elif itype == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list) and changes:
            for ch in changes:
                if isinstance(ch, dict):
                    kind = ch.get("kind", "edit")
                    path = ch.get("path", "?")
                    out.write(f"[codex edit] {kind} {path}\n")
                else:
                    out.write(f"[codex edit] {_first_line(ch)}\n")
        else:
            out.write("[codex edit] (file change)\n")

    # agent_message is handled by render() (the final one is printed verbatim).
    else:
        # Unknown item type → one generic, non-fatal line.
        out.write(f"[codex {itype}] {_first_line(json.dumps(item))}\n")


def _write_trailer(out, session_id, usage, jsonl_path) -> None:
    # Only emit a trailer when there is something to report. This keeps output
    # clean when the stream carried no Codex envelope at all.
    if session_id is None and usage is None and jsonl_path is None:
        return
    out.write("\n── codex run summary ──\n")
    out.write(f"session: {session_id if session_id else 'unknown'}\n")
    if isinstance(usage, dict):
        out.write(
            "tokens:  input={} cached={} output={} reasoning={}\n".format(
                usage.get("input_tokens", 0),
                usage.get("cached_input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("reasoning_output_tokens", 0),
            )
        )
    if jsonl_path:
        out.write(f"log:     {jsonl_path}\n")


def render(stream, out, jsonl_path=None) -> None:
    """Render a Codex JSONL event stream to `out`. Never raises on bad input."""
    session_id: str | None = None
    usage: dict | None = None
    last_agent_message: str | None = None

    for raw in stream:
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            # Not JSON — surface it verbatim so nothing is silently dropped.
            out.write(line + "\n")
            continue

        if not isinstance(obj, dict):
            out.write(line + "\n")
            continue

        etype = obj.get("type")

        if etype == "thread.started":
            session_id = obj.get("thread_id") or session_id
        elif etype == "turn.completed":
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
        elif etype == "item.completed":
            item = obj.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                # Flush any prior message as a compact note; hold the newest as
                # the candidate final deliverable.
                if last_agent_message is not None:
                    out.write(f"[codex message] {_first_line(last_agent_message)}\n")
                last_agent_message = item.get("text", "")
            else:
                _render_item(item, out)
        # thread.started/turn.started/item.started envelopes carry no extra
        # step info we render (item.started duplicates item.completed).

    # Codex's final message: verbatim, untruncated, last.
    if last_agent_message is not None:
        out.write("\n" + last_agent_message.rstrip("\n") + "\n")

    _write_trailer(out, session_id, usage, jsonl_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jsonl-path",
        default=None,
        help="Path to the raw JSONL log to name in the trailer.",
    )
    args = parser.parse_args(argv)
    render(sys.stdin, sys.stdout, jsonl_path=args.jsonl_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
