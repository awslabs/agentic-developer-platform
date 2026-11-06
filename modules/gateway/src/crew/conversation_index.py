"""Extract small conversation headers from an already verified Crew archive."""

import hashlib
import json
import tarfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath

OWNER_CLAUDE_PROJECTS = (".claude", "owner-shell", "projects")
OWNER_SHELL_PREFIXES = (".claude/owner-shell/", ".codex/owner-shell/")
CODEX_SESSIONS_PREFIX = ".codex/owner-shell/sessions/"
CODEX_TURNS_PREFIX = "turns-"


def codex_turns_extract(path):
    """True for the Codex rollout text extract (#9073) — user and assistant turns, unlike the prompts-only history."""
    return path.startswith(CODEX_SESSIONS_PREFIX) and PurePosixPath(path).name.startswith(CODEX_TURNS_PREFIX) and path.endswith(".jsonl")


def _codex_turns_summary(data):
    """(session_id, title, started, last, count) of one turns extract, or None when it is not one."""
    header = json.loads(data.readline())
    if not isinstance(header, dict) or not isinstance(header.get("session_id"), str) or not header["session_id"]:
        return None
    title = started = last = None
    count = 0
    for line in data:
        entry = json.loads(line)
        if not isinstance(entry, dict) or entry.get("role") not in ("user", "assistant") or not isinstance(entry.get("text"), str):
            raise ValueError("invalid codex turn")
        when = _timestamp(entry.get("ts"))
        if when is None:
            raise ValueError("missing codex turn time")
        if title is None and entry["role"] == "user" and entry["text"].strip():
            title = entry["text"].strip()[:255]
        started = min(started, when) if started else when
        last = max(last, when) if last else when
        count += 1
    return header["session_id"], title, started, last, count


def _timestamp(value):
    try:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OverflowError, ValueError):
        pass
    return None


def _source(path):
    parts = PurePosixPath(path).parts
    if len(parts) == 2 and parts[0] == "sessions" and parts[1].startswith("dashboard_chat-") and path.endswith(".jsonl"):
        return "kiro_crew"
    if len(parts) == 4 and parts[:2] == (".claude", "projects") and path.endswith(".jsonl"):
        return "claude_code"
    if len(parts) == 5 and parts[:3] == OWNER_CLAUDE_PROJECTS and path.endswith(".jsonl"):
        return "claude_code"  # the owner's own session over SSH, as a compact extract (#9069)
    return None


def owner_shell_path(path):
    """True for conversations the owner held themselves on the box (Claude Code or Codex over SSH), not Kiro's runs."""
    return path.startswith(OWNER_SHELL_PREFIXES)


def _codex_rows(history, metadata, unreadable=False):
    sessions = defaultdict(lambda: {"title": None, "started_at": None, "last_activity_at": None, "turn_count": 0})
    for path, payload in metadata:
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            session_id = PurePosixPath(path).stem
            sessions[session_id]["unreadable"] = True
        sessions[session_id]["archive_path"] = path
        sessions[session_id]["started_at"] = _timestamp(payload.get("timestamp"))
    for entry in history:
        session_id = entry.get("session_id")
        when = _timestamp(entry.get("ts"))
        text = entry.get("text")
        if not isinstance(session_id, str) or not session_id or when is None or not isinstance(text, str):
            continue
        session = sessions[session_id]
        if session["title"] is None and text.strip():
            session["title"] = text.strip()[:255]
        session["started_at"] = min(session["started_at"], when) if session["started_at"] else when
        session["last_activity_at"] = max(session["last_activity_at"], when) if session["last_activity_at"] else when
        session["turn_count"] += 1
    return [
        {
            "source": "codex",
            "conversation_id": session_id,
            "archive_path": session.get("archive_path", ".codex/owner-shell/history.jsonl"),
            "title": session["title"] or "Untitled conversation",
            "started_at": session["started_at"],
            "last_activity_at": session["last_activity_at"] or session["started_at"],
            "turn_count": session["turn_count"],
            "unreadable": unreadable or session.get("unreadable", False) or not session.get("archive_path") or not session["turn_count"],
        }
        for session_id, session in sessions.items()
    ]


def extract_conversations(archive):
    """Read supported JSONL members only; no message text escapes into the returned rows."""
    rows = []
    codex_history = []
    codex_metadata = []
    codex_turns = {}
    codex_unreadable = False
    codex_history_seen = False
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:") as source:
        for member in source:
            if member.name == ".codex/owner-shell/history.jsonl":
                codex_history_seen = True
                if not member.isfile() or member.size > 1024 * 1024:
                    codex_unreadable = True
                    continue
                try:
                    with source.extractfile(member) as data:
                        codex_history = [json.loads(line) for line in data]
                    if not all(
                        isinstance(entry, dict)
                        and isinstance(entry.get("session_id"), str)
                        and isinstance(entry.get("text"), str)
                        and _timestamp(entry.get("ts")) is not None
                        for entry in codex_history
                    ):
                        codex_unreadable = True
                        codex_history = []
                except (ValueError, UnicodeError):
                    codex_unreadable = True
                    codex_history = []
                continue
            if codex_turns_extract(member.name):
                if member.isfile() and member.size <= 16 * 1024 * 1024:
                    try:
                        with source.extractfile(member) as data:
                            summary = _codex_turns_summary(data)
                    except (ValueError, UnicodeError, TypeError):
                        summary = None
                    if summary and summary[4]:
                        codex_turns[summary[0]] = (member.name, *summary[1:])
                continue
            if (
                member.name.startswith(".codex/owner-shell/sessions/")
                and PurePosixPath(member.name).name.startswith("metadata-")
                and member.name.endswith(".json")
            ):
                if not member.isfile() or member.size > 4096:
                    codex_metadata.append((member.name, {}))
                    continue
                try:
                    with source.extractfile(member) as data:
                        payload = json.load(data)
                    codex_metadata.append((member.name, payload if isinstance(payload, dict) else {}))
                except (ValueError, UnicodeError):
                    codex_metadata.append((member.name, {}))
                continue
            kind = _source(member.name)
            if not kind or not member.isfile():
                continue
            title = None
            started = last = None
            turns = 0
            unreadable = member.size > 16 * 1024 * 1024
            if not unreadable:
                try:
                    with source.extractfile(member) as data:
                        for line in data:
                            entry = json.loads(line)
                            if not isinstance(entry, dict):
                                raise ValueError("invalid conversation record")
                            if kind == "claude_code":
                                if entry.get("type") == "ai-title" and isinstance(entry.get("aiTitle"), str):
                                    title = entry["aiTitle"]
                                if entry.get("type") not in ("user", "assistant"):
                                    continue
                                when = _timestamp(entry.get("timestamp"))
                            else:
                                if isinstance(entry.get("title"), str) and entry["title"]:
                                    title = entry["title"]
                                if entry.get("role") not in ("user", "assistant"):
                                    continue
                                when = _timestamp(entry.get("ts"))
                            if when is None:
                                raise ValueError("missing conversation time")
                            started = min(started, when) if started else when
                            last = max(last, when) if last else when
                            turns += 1
                    if not turns:
                        unreadable = True
                except (UnicodeError, ValueError, TypeError):
                    unreadable = True
            rows.append(
                {
                    "source": kind,
                    "conversation_id": PurePosixPath(member.name).stem,
                    "archive_path": member.name,
                    "title": (title or "Untitled conversation")[:255],
                    "started_at": started,
                    "last_activity_at": last,
                    "turn_count": turns,
                    "unreadable": unreadable,
                }
            )
    for session_id, (path, title, started, last, count) in codex_turns.items():
        if not any(payload.get("id") == session_id for _path, payload in codex_metadata):
            codex_metadata.append((path, {"id": session_id, "timestamp": started.isoformat()}))
    codex_rows = _codex_rows(codex_history, codex_metadata, codex_unreadable)
    for row in codex_rows:
        extract = codex_turns.get(row["conversation_id"])
        if extract is None:
            continue
        path, title, started, last, count = extract
        # The extract carries both sides of the dialogue; the prompts-only history stays the fallback for older checkpoints.
        row.update(
            archive_path=path,
            turn_count=count,
            unreadable=False,
            title=row["title"] if row["title"] != "Untitled conversation" else title or row["title"],
        )
        row["started_at"] = min(filter(None, (row["started_at"], started)), default=None)
        row["last_activity_at"] = max(filter(None, (row["last_activity_at"], last)), default=None)
    rows.extend(codex_rows)
    if codex_history_seen and codex_unreadable and not codex_metadata:
        rows.append(
            {
                "source": "codex",
                "conversation_id": "history",
                "archive_path": ".codex/owner-shell/history.jsonl",
                "title": "Unreadable conversation",
                "started_at": None,
                "last_activity_at": None,
                "turn_count": 0,
                "unreadable": True,
            }
        )
    for row in rows:
        if len(row["conversation_id"]) > 255 or len(row["archive_path"]) > 1024:
            pointer = f"{row['source']}:{row['conversation_id']}:{row['archive_path']}"
            row["conversation_id"] = hashlib.sha256(pointer.encode()).hexdigest()
            row["archive_path"] = row["archive_path"][:1024]
            row["unreadable"] = True
    return rows


def _content(value):
    if isinstance(value, str):
        return value, False
    if isinstance(value, dict):
        return _content(value.get("content"))
    if isinstance(value, list):
        texts = [part.get("text") for part in value if isinstance(part, dict) and part.get("type") == "text"]
        collapsed = any(isinstance(part, dict) and part.get("type") in ("thinking", "tool_use", "tool_result") for part in value)
        return "\n".join(text for text in texts if isinstance(text, str)), collapsed
    return "", False


def read_turns(data, source, conversation_id):
    """Produce visible dialogue from one member; Codex history has user prompts only."""
    turns = []
    for line in data:
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict):
            continue
        if source == "codex":
            if not isinstance(entry.get("text"), str):
                continue
            if entry.get("role") in ("user", "assistant"):  # turns extract (#9073)
                role = entry["role"]
            elif entry.get("session_id") == conversation_id:  # prompts-only history
                role = "user"
            else:
                continue
            when, text, collapsed = _timestamp(entry.get("ts")), entry["text"], False
        elif source == "claude_code":
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            when = _timestamp(entry.get("timestamp"))
            text, collapsed = _content(entry.get("message"))
        else:
            role = entry.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            when = _timestamp(entry.get("ts"))
            text, collapsed = _content(entry.get("content", entry.get("text")))
            collapsed = collapsed or role == "tool"
            if role == "tool":
                text = ""
        if when and (text or collapsed):
            turns.append({"role": role, "time": when.isoformat(), "text": text, "collapsed": collapsed})
    return turns
