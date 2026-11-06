"""Consistent, allowlisted Crew 0.11.2 home archives for an external checkpoint helper."""

from contextlib import closing, nullcontext
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile


CREW_VERSION = "0.11.2"
# Owner-continuity inventory of a Kiro Crew 0.11.2 home (#7202, memory-backup-contract-6930.md).
#
# PARITY INVARIANT: the gateway verifies every archive it is asked to record against its own
# inventory rules (modules/gateway/src/crew/checkpoint_objects.py ``valid_path``: ALLOWED_FILES,
# ALLOWED_TREES, EXCLUDED_NAMES, EXCLUDED_SUFFIXES, ``.kiro`` limited to ``.kiro/sessions/cli``). A path
# this helper keeps that the gateway refuses makes the whole checkpoint "unsafe archive inventory",
# so FILES/TREES here are exactly the gateway's sets and the exclusions are a superset of its.
# Widening what the owner keeps (connections/, decisions/, consent files, .kiro/settings/ ...) is a
# gateway change first, then a matching change here — tests/test_crew_checkpoint.py checks parity.
#
# Decision table for the entries a real home carries (dev 2026-10-21, gen 25 listing). PRESERVE =
# owner data that follows the owner to a new box. EXCLUDE = machine-bound, derived, secret, or
# re-rendered by the box at configure time. UNKNOWN = anything else: skipped and reported in the
# manifest under ``excluded_reasons``, never fatal (#8223 cause 37).
#
#   memory.db, memory_index.db, lessons.jsonl, notifications.jsonl, telemetry_salt   PRESERVE (FILES)
#   workspace/ memory_stores/ sessions/ trust/ members/ artifacts/ uploads/           PRESERVE (TREES)
#   crew-teams/ plan_memory/ skills/ crew-log/ adp-crew-session-map/ .kiro/sessions/cli/ PRESERVE (TREES)
#   .kiro/agents/ (rendered by crew-kirocrew.ts each generation) .kiro/settings/      EXCLUDE (managed)
#   config.json crons.json snapshots/ backup/ backups/                                 EXCLUDE (managed / derived)
#   .adp-host/ .adp/ .kiro_cli_setup_complete .migrations/ .settings_seeds.lock       EXCLUDE (box-bound markers)
#   .claude/ .codex/ (Claude Code / Codex state except rollout payloads)             PRESERVE (TREES; #8558)
#   .claude.json .claude/.credentials.json .codex/auth.json .local_secret token_signing.key EXCLUDE (re-rendered / secret)
#   admission_policy.json agent_model_state.json cloud.json cloud_launch_state.json    EXCLUDE (host / machine state)
#   live_target.json *-staging/ auth-store-staging/ aws-control-staging/               EXCLUDE (machine / staging)
#   logs/ gateway.log* diag/ cache/ .cache/ inbound-spool/                             EXCLUDE (logs, caches, spool)
#   kiro_pids* kiro_session_pids* dashboard-*.sock *.lock *.pid *.bak *.prev *.tmp     EXCLUDE (runtime)
#   crewmate_prune_v3_migrated.json file-delivery-consent-pending/                     EXCLUDE (migration marker, transient)
#   connections/ decisions/ cron-history/ crew-panels/ mcp/ appearance-library/ apps/  UNKNOWN (reported; gateway change first)
#   decisions_consent.json file_delivery_consent.json aws_service_consent.json         UNKNOWN (reported; gateway change first)
#   connections_ui_migrated.json autonudge.json computer_use.json                      UNKNOWN (reported; gateway change first)
#   credential_redaction.json config.local.json                                       UNKNOWN (reported; gateway change first)
FILES = frozenset({"memory.db", "memory_index.db", "lessons.jsonl", "notifications.jsonl", "telemetry_salt"})
TREES = frozenset({".kiro", "workspace", "memory_stores", "sessions", "trust", "members", "artifacts", "uploads",
                   "crew-teams", "plan_memory", "skills", "crew-log", "adp-crew-session-map", ".claude", ".codex"})
# The only .kiro sub-tree the gateway accepts; .kiro/agents is box-rendered, everything else is skipped.
KIRO_TREES = frozenset({("sessions", "cli")})
KIRO_MANAGED = frozenset({"agents"})
# .adp-host is the Dev Box host's root-only provisioning marker (restore_crew_host.py): box-bound, never portable.
EXCLUDED_ROOTS = frozenset({"config.json", "crons.json", "snapshots", "backup", "backups", ".adp-host", ".adp",  # .adp: adp mcp ledger (#8380)
                            ".kiro_cli_setup_complete", ".migrations", ".settings_seeds.lock", ".claude.json",
                            ".local_secret", "token_signing.key", "admission_policy.json", "agent_model_state.json",
                            "cloud.json", "cloud_launch_state.json", "live_target.json", "logs", "diag", "cache",
                            ".cache", "inbound-spool", "file-delivery-consent-pending", "crewmate_prune_v3_migrated.json"})
HOST_STATE_DIRECTORY = ".adp-host"  # restore_crew_host.MARKER_DIRECTORY: the host's root-only state, never Crew content
# Claude Code and Codex state is owner continuity and is archived (#8558); only their credential files are not:
# .claude/.credentials.json (Claude Code OAuth), .codex/auth.json (Codex), plus shell credential stores.
EXCLUDED_NAMES = frozenset({"sel_hmac.key", ".env", ".aws", ".ssh", "credentials", "credentials.json", ".credentials.json",
                            "tokens", "agents", ".adp", ".git-credentials", "hosts.yml", "auth.json", "model.env", "adp-dev-box-shell"})
EXCLUDED_SUFFIXES = ("-wal", "-shm", ".pid", ".lock", ".sock", ".tmp", ".bak", ".prev", "-staging")
EXCLUDED_PREFIXES = ("gateway.log", "kiro_pids", "kiro_session_pids", "dashboard-")
# Reasons recorded in the manifest's ``excluded_reasons`` so an operator can read why a path is absent.
REASON_MANAGED, REASON_SECRET, REASON_RUNTIME, REASON_UNCLASSIFIED = "managed", "secret", "runtime", "unclassified"


OWNER_CODEX_PREFIX = ".codex/owner-shell"
# Must equal the image's /home/ec2-user/.codex/config.toml (prepare_shell.sh); test_crew_checkpoint pins it.
OWNER_CODEX_CONFIG = b'''model = "adp-unavailable"
model_provider = "adp-shell"
# Bedrock serves no hosted web-search tool for GPT models; Codex sends it by default and the whole turn is
# rejected ("web search is not supported for this request"). ADP's websearch lives in adp-tools (#8859).
web_search = "disabled"
[model_providers.adp-shell]
name = "ADP Dev Box"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
requires_openai_auth = false
[mcp_servers.adp-tools]
command = "/opt/adp/crew/current/venv/bin/python3"
args = ["/opt/adp/crew/current/host/adp/adp-mcp.py"]
# The owner's home is theirs: skip Codex's first-run "trust this folder?" setup screen there (#8719).
[projects."/home/ec2-user"]
trust_level = "trusted"
'''
OWNER_CODEX_METADATA_LIMIT = 1024 * 1024
# Codex writes its full base instructions into the session_meta header: ~22 KB on dev (2026-10-27), which the former
# 16 KiB bound skipped silently, so no Codex session was indexed. Only the small identity fields are kept from it.
OWNER_CODEX_SESSION_META_LIMIT = 1024 * 1024

# The owner's own Claude Code sessions (tmux + `claude` over SSH, #9069) live in /home/ec2-user/.claude/projects.
# Raw transcripts run to hundreds of MiB (tool results, attachments, file snapshots), so the checkpoint stores a
# compact extract per session instead: user and assistant text only, in Claude Code's own record shape so the
# gateway's conversation index and turn reader apply unchanged.
OWNER_CLAUDE_PREFIX = ".claude/owner-shell"
OWNER_CLAUDE_READ_LIMIT = 256 * 1024 * 1024  # bytes read per transcript before the extract is marked truncated
OWNER_CLAUDE_LINE_LIMIT = 4 * 1024 * 1024  # a longer record (a pasted file, an image) is skipped unread
OWNER_CLAUDE_TURN_CHARS = 4000
OWNER_CLAUDE_TITLE_CHARS = 120
OWNER_CLAUDE_SESSION_LIMIT = 1024 * 1024  # extract bytes per session
OWNER_CLAUDE_TOTAL_LIMIT = 64 * 1024 * 1024  # extract bytes per checkpoint, newest sessions first
OWNER_CLAUDE_EXTRACT_MARKER = "adp-owner-shell"
#: Codex rollouts get the same treatment (#9073): a `turns-…jsonl` extract of user and assistant text next to the
#: session metadata, so the owner reads Codex's replies too; the rollout itself (tool calls, shell output) never ships.
OWNER_CODEX_TURNS_PREFIX = "turns-"
# Harness-injected blocks inside a user prompt (never typed by the owner) and credential shapes.
_CLAUDE_HIDDEN_BLOCKS = re.compile(
    r"<(system-reminder|environment_context|ide_[a-z_]+|local-command-stdout|local-command-stderr|command-name|command-message|command-args)>"
    r".*?</\1>",
    re.S,
)
_CLAUDE_SECRETS = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|(?i:\bbearer\s+[A-Za-z0-9._-]{16,})"
)


class InvalidCheckpoint(ValueError):
    pass


def digest(content):
    return hashlib.sha256(content).hexdigest()


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not parts or any(part in (".", "..") for part in parts) or name.startswith("/") or "\\" in name:
        raise InvalidCheckpoint("unsafe archive path")
    if parts[0] not in FILES | TREES or parts[0] in FILES and len(parts) != 1:
        raise InvalidCheckpoint("unclassified archive path")
    if parts[0] == ".kiro" and not _kiro_owned(parts):
        raise InvalidCheckpoint("unclassified Kiro CLI path")
    if _codex_rollout(parts):
        raise InvalidCheckpoint("excluded Codex rollout")
    if any(part in EXCLUDED_NAMES or part.endswith(EXCLUDED_SUFFIXES) or part.startswith(EXCLUDED_PREFIXES) for part in parts):
        raise InvalidCheckpoint("excluded archive path")
    return parts


def _kiro_owned(parts):
    """True for ``.kiro`` itself, the owner-owned ``.kiro`` sub-trees and their ancestors."""
    tail = tuple(parts[1:])
    return not tail or any(tail[: len(tree)] == tree[: len(tail)] for tree in KIRO_TREES)


def _codex_rollout(parts):
    return parts[-1].startswith("rollout-") and parts[-1].endswith(".jsonl")


def classify(relative):
    """``(keep, reason)`` for one relative path of the home; ``keep`` means it enters the archive."""
    parts = PurePosixPath(relative).parts
    name = parts[-1]
    root = parts[0]
    if root == ".kiro" and len(parts) > 1 and parts[1] in KIRO_MANAGED:
        return False, REASON_MANAGED
    if root == ".codex" and len(parts) > 1 and parts[1] == "tmp":
        # Codex's scratch (arg0 shims are symlinks into the bundle); never state worth keeping.
        return False, REASON_RUNTIME
    if _codex_rollout(parts):
        return False, REASON_RUNTIME
    if relative == "token_signing.key" or name in EXCLUDED_NAMES:
        return False, REASON_SECRET
    if root in EXCLUDED_ROOTS:
        return False, REASON_MANAGED
    if name.endswith(EXCLUDED_SUFFIXES) or name.startswith(EXCLUDED_PREFIXES):
        return False, REASON_RUNTIME
    if root not in FILES | TREES or (root in FILES and len(parts) != 1):
        return False, REASON_UNCLASSIFIED
    if root == ".kiro" and not _kiro_owned(parts):
        return False, REASON_UNCLASSIFIED
    return True, None


def read_file(path):
    if path.suffix != ".db":
        return path.read_bytes()
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "backup.db"
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as source:
            with closing(sqlite3.connect(destination)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise InvalidCheckpoint("SQLite backup failed integrity check")
        return destination.read_bytes()


def _owner_file(path, uid, limit, *, first_line=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
            raise InvalidCheckpoint("unsafe owner Codex file")
        data = source.readline(limit + 1) if first_line else source.read(limit + 1)
        return data if len(data) <= limit else None


def _owner_codex_snapshot(owner_home, contents, excluded, reasons):
    owner_home = Path(owner_home)
    if not owner_home.exists() and not owner_home.is_symlink():
        return
    home_info = owner_home.lstat()
    codex = owner_home / ".codex"
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_mode & 0o022:
        raise InvalidCheckpoint("unsafe owner home")
    if not codex.exists() and not codex.is_symlink():
        return
    codex_info = codex.lstat()
    if not stat.S_ISDIR(codex_info.st_mode) or codex_info.st_uid != home_info.st_uid or codex_info.st_mode & 0o022:
        raise InvalidCheckpoint("unsafe owner Codex directory")

    for name in ("history.jsonl", "session_index.jsonl", "config.toml"):
        source = codex / name
        if not source.exists() and not source.is_symlink():
            continue
        target = f"{OWNER_CODEX_PREFIX}/{name}"
        if source.is_symlink():
            excluded.append(target)
            reasons[target] = REASON_SECRET
            continue
        data = _owner_file(source, home_info.st_uid, OWNER_CODEX_METADATA_LIMIT)
        if data is None or name == "config.toml" and data != OWNER_CODEX_CONFIG:
            excluded.append(target)
            reasons[target] = REASON_SECRET if name == "config.toml" else REASON_RUNTIME
            continue
        safe_name(target)
        contents[target] = data

    sessions = codex / "sessions"
    if not sessions.exists() and not sessions.is_symlink():
        return
    session_info = sessions.lstat()
    if not stat.S_ISDIR(session_info.st_mode) or session_info.st_uid != home_info.st_uid:
        raise InvalidCheckpoint("unsafe owner Codex sessions")
    codex_budget = OWNER_CLAUDE_TOTAL_LIMIT
    for root, directories, names in os.walk(sessions, followlinks=False):
        for name in list(directories):
            child = Path(root) / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != home_info.st_uid:
                directories.remove(name)
        for name in sorted(names):
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            source = Path(root) / name
            relative = source.relative_to(sessions).as_posix()
            rollout = f"{OWNER_CODEX_PREFIX}/sessions/{relative}"
            excluded.append(rollout)
            reasons[rollout] = REASON_RUNTIME
            if source.is_symlink():
                continue
            header = _owner_file(source, home_info.st_uid, OWNER_CODEX_SESSION_META_LIMIT, first_line=True)
            if header is None:
                continue
            try:
                event = json.loads(header)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "session_meta" or not isinstance(event.get("payload"), dict):
                continue
            payload = event["payload"]
            fields = {key: payload[key] for key in ("id", "timestamp", "cwd", "originator", "cli_version")
                      if isinstance(payload.get(key), str) and len(payload[key]) <= 1024}
            if "id" not in fields:
                continue
            data = json.dumps(fields, sort_keys=True).encode()
            if len(data) > 4096:
                continue
            target = f"{OWNER_CODEX_PREFIX}/sessions/{relative[:-len('.jsonl')].replace('rollout-', 'metadata-', 1)}.json"
            safe_name(target)
            contents[target] = data
            turns_target = f"{OWNER_CODEX_PREFIX}/sessions/{relative.replace('rollout-', OWNER_CODEX_TURNS_PREFIX, 1)}"
            extract = _codex_extract(source, home_info.st_uid, fields["id"]) if codex_budget > 0 else None
            if extract is None or len(extract) > codex_budget:
                excluded.append(turns_target)
                reasons[turns_target] = REASON_RUNTIME
                continue
            safe_name(turns_target)
            codex_budget -= len(extract)
            contents[turns_target] = extract


def _claude_text(content):
    """(visible text, tools_or_reasoning_present) for one Claude Code message content."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    texts, hidden = [], False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        else:
            hidden = True  # tool_use, tool_result, thinking, image, document: never archived
    return "\n".join(texts), hidden


def _owner_records(path, uid):
    """Yield the parsed JSON records of an owner-owned transcript, bounded; the final yield is the truncation flag."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
            return
        remaining = OWNER_CLAUDE_READ_LIMIT
        while remaining >= 0:
            raw = source.readline(OWNER_CLAUDE_LINE_LIMIT + 1)
            if not raw:
                break
            remaining -= len(raw)
            if len(raw) > OWNER_CLAUDE_LINE_LIMIT:
                while raw and not raw.endswith(b"\n") and remaining >= 0:  # drain the over-long record, bounded
                    raw = source.readline(OWNER_CLAUDE_LINE_LIMIT + 1)
                    remaining -= len(raw)
                continue
            try:
                entry = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                yield entry
        yield remaining < 0


def _clean_text(text):
    text = _CLAUDE_SECRETS.sub("[redacted]", _CLAUDE_HIDDEN_BLOCKS.sub("", text)).strip()
    return text[:OWNER_CLAUDE_TURN_CHARS] + "…" if len(text) > OWNER_CLAUDE_TURN_CHARS else text


def _codex_extract(path, uid, session_id):
    """Bytes of the user/assistant text extract of one Codex rollout, or None when empty or unreadable."""
    records, size, truncated = [], 0, False
    for entry in _owner_records(path, uid):
        if isinstance(entry, bool):
            truncated = entry
            break
        payload = entry.get("payload")
        when = entry.get("timestamp")
        if entry.get("type") != "response_item" or not isinstance(payload, dict) or not isinstance(when, str) or not when:
            continue
        role = payload.get("role")
        if payload.get("type") != "message" or role not in ("user", "assistant") or not isinstance(payload.get("content"), list):
            continue
        text = _clean_text("\n".join(
            part["text"] for part in payload["content"]
            if isinstance(part, dict) and part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str)))
        if not text:
            continue
        line = json.dumps({"role": role, "ts": when[:64], "text": text}, ensure_ascii=False, sort_keys=True).encode() + b"\n"
        if size + len(line) > OWNER_CLAUDE_SESSION_LIMIT:
            truncated = True
            break
        records.append(line)
        size += len(line)
    if not records:
        return None
    head = {"session_id": session_id, "extract": OWNER_CLAUDE_EXTRACT_MARKER, "truncated": truncated}
    return json.dumps(head, ensure_ascii=False, sort_keys=True).encode() + b"\n" + b"".join(records)


def _claude_extract(path, uid, session_id):
    """Bytes of the compact extract for one transcript, or None when the file is not an owner-readable session."""
    records, size, title, truncated, tools_since_text = [], 0, None, False, False
    for entry in _owner_records(path, uid):
        if isinstance(entry, bool):
            truncated = entry
            break
        if entry.get("type") not in ("user", "assistant"):
            continue
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        message, when = entry.get("message"), entry.get("timestamp")
        if not isinstance(message, dict) or not isinstance(when, str) or not when:
            continue
        text, hidden = _claude_text(message.get("content"))
        text = _clean_text(text)
        if not text:
            tools_since_text = tools_since_text or hidden
            continue
        role = entry["type"]
        content = [{"type": "text", "text": text}]
        if hidden or tools_since_text:
            # One stub marks that tool, image or reasoning blocks ran here; their payloads are never archived.
            content.append({"type": "tool_result" if role == "user" else "tool_use"})
        tools_since_text = False
        if title is None and role == "user":
            title = text[:OWNER_CLAUDE_TITLE_CHARS]
        line = json.dumps({"type": role, "timestamp": when[:64], "message": {"role": role, "content": content}},
                          ensure_ascii=False, sort_keys=True).encode() + b"\n"
        if size + len(line) > OWNER_CLAUDE_SESSION_LIMIT:
            truncated = True
            break
        records.append(line)
        size += len(line)
    if not records:
        return None
    head = {"type": "ai-title", "aiTitle": title or "Untitled conversation", "sessionId": session_id,
            "extract": OWNER_CLAUDE_EXTRACT_MARKER, "truncated": truncated}
    return json.dumps(head, ensure_ascii=False, sort_keys=True).encode() + b"\n" + b"".join(records)


def _owner_claude_snapshot(owner_home, contents, excluded, reasons):
    owner_home = Path(owner_home)
    if not owner_home.exists() and not owner_home.is_symlink():
        return
    home_info = owner_home.lstat()
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_mode & 0o022:
        raise InvalidCheckpoint("unsafe owner home")
    projects = owner_home / ".claude" / "projects"
    if not projects.exists() and not projects.is_symlink():
        return
    for directory in (owner_home / ".claude", projects):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != home_info.st_uid:
            raise InvalidCheckpoint("unsafe owner Claude Code directory")
    candidates = []
    with os.scandir(projects) as project_entries:
        for project in project_entries:
            if project.is_symlink() or not project.is_dir(follow_symlinks=False) or project.stat(follow_symlinks=False).st_uid != home_info.st_uid:
                continue
            with os.scandir(project.path) as session_entries:
                for session in session_entries:
                    if not session.name.endswith(".jsonl") or session.is_symlink() or not session.is_file(follow_symlinks=False):
                        continue
                    candidates.append((session.stat(follow_symlinks=False).st_mtime, project.name, session.name, session.path))
    budget = OWNER_CLAUDE_TOTAL_LIMIT
    for _mtime, project, name, path in sorted(candidates, reverse=True):
        target = f"{OWNER_CLAUDE_PREFIX}/projects/{project}/{name}"
        try:
            safe_name(target)
        except InvalidCheckpoint:
            excluded.append(target)
            reasons[target] = REASON_UNCLASSIFIED
            continue
        data = _claude_extract(path, home_info.st_uid, name[: -len(".jsonl")]) if budget > 0 else None
        if data is None or len(data) > budget:
            excluded.append(target)
            reasons[target] = REASON_RUNTIME
            continue
        budget -= len(data)
        contents[target] = data


def capture(home, archive_path, manifest_path, *, quiesced=False, quiescence=None, owner_home=None):
    """Use a caller-held writer pause for handoff; background files are only individually consistent."""
    if quiesced and quiescence is None:
        raise InvalidCheckpoint("final capture requires writer quiescence")
    with quiescence if quiesced else nullcontext():
        return _capture(home, archive_path, manifest_path, quiesced=quiesced, owner_home=owner_home)


def _capture(home, archive_path, manifest_path, *, quiesced, owner_home):
    home = Path(home)
    if home.is_symlink() or not home.is_dir():
        raise InvalidCheckpoint("Crew home unavailable")
    contents, excluded, reasons = {}, [], {}
    for root, directories, names in os.walk(home, followlinks=False):
        for name in sorted(directories + names):
            path = Path(root) / name
            relative = path.relative_to(home).as_posix()
            if owner_home is not None and relative in (OWNER_CODEX_PREFIX, OWNER_CLAUDE_PREFIX):
                excluded.append(relative)
                reasons[relative] = REASON_MANAGED
                if name in directories:
                    directories.remove(name)
                continue
            keep, reason = classify(relative)
            if relative == "token_signing.key":
                # Never lstat-follow or read the dashboard signing key link; skip it unseen.
                excluded.append(relative)
                reasons[relative] = reason
                if name in directories:
                    directories.remove(name)
                continue
            info = path.lstat()
            if not keep:
                # Sockets, fifos and symlinks inside an excluded or unknown entry are skipped with it:
                # a runtime socket (dashboard-5476.sock) must not fail the owner's backup (#8223 cause 37).
                excluded.append(relative)
                reasons[relative] = reason
                if name in directories:
                    directories.remove(name)
                continue
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                # A symlink, socket or fifo inside an OWNED tree is skipped and named — never followed,
                # never archived. Refusing the whole capture instead cost the owner a day of backups
                # (dev 2026-10-25 19:36 → 2026-10-26: Codex left apply_patch symlinks under .codex/tmp
                # and every periodic checkpoint since failed "unsafe Crew home entry").
                excluded.append(relative)
                reasons[relative] = REASON_RUNTIME
                if name in directories:
                    directories.remove(name)
                continue
            safe_name(relative)
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise InvalidCheckpoint("hard link in Crew home")
                contents[relative] = read_file(path)
    if owner_home is not None:
        _owner_codex_snapshot(owner_home, contents, excluded, reasons)
        _owner_claude_snapshot(owner_home, contents, excluded, reasons)
    if not contents:
        raise InvalidCheckpoint("no approved Crew state")
    manifest = {"format": 1, "crew_version": CREW_VERSION, "quiesced": quiesced,
                "files": {name: {"sha256": digest(data), "size": len(data)} for name, data in sorted(contents.items())},
                "excluded": sorted(excluded),
                "excluded_reasons": {name: reasons[name] for name in sorted(reasons)}}
    archive_path, manifest_path = Path(archive_path), Path(manifest_path)
    with tarfile.open(archive_path, "w") as archive:
        for name, data in sorted(contents.items()):
            member = tarfile.TarInfo(name)
            member.mode = 0o600
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    manifest["archive_sha256"] = digest(archive_path.read_bytes())
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def restore(home, archive, manifest_bytes, *, archive_sha256, manifest_sha256, crew_version=CREW_VERSION):
    """Validate fetched exact-version contents before atomically exposing a new home."""
    home = Path(home)
    if home.exists() or home.is_symlink():
        raise InvalidCheckpoint("destination already exists")
    if digest(archive) != archive_sha256 or digest(manifest_bytes) != manifest_sha256:
        raise InvalidCheckpoint("object checksum mismatch")
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != 1 or manifest.get("crew_version") != crew_version:
        raise InvalidCheckpoint("incompatible Crew checkpoint")
    if manifest.get("archive_sha256") != archive_sha256:
        raise InvalidCheckpoint("archive manifest mismatch")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise InvalidCheckpoint("empty checkpoint inventory")
    staged = Path(tempfile.mkdtemp(prefix=".crew-restore-", dir=home.parent))
    try:
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            for member in source:
                parts = safe_name(member.name)
                if not member.isfile() or member.name in seen or member.name not in expected:
                    raise InvalidCheckpoint("invalid archive member")
                seen.add(member.name)
                data = source.extractfile(member).read()
                if {"sha256": digest(data), "size": len(data)} != expected[member.name]:
                    raise InvalidCheckpoint("file checksum mismatch")
                target = staged.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o600)
        if seen != set(expected):
            raise InvalidCheckpoint("missing archive member")
        os.rename(staged, home)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def restore_exact(home, receipt, fetch, *, crew_version=CREW_VERSION):
    """Fetch recorded S3 versions, never the mutable current object or its ETag."""
    objects = []
    for label in ("archive", "manifest"):
        obj = receipt[label]
        if not all(isinstance(obj.get(field), str) and obj[field] for field in ("bucket", "key", "version_id", "sha256")):
            raise InvalidCheckpoint("incomplete versioned object receipt")
        objects.append(fetch(obj["bucket"], obj["key"], obj["version_id"]))
    restore(home, objects[0], objects[1], archive_sha256=receipt["archive"]["sha256"],
            manifest_sha256=receipt["manifest"]["sha256"], crew_version=crew_version)


def restore_empty_mount(mount, receipt, fetch, *, crew_version=CREW_VERSION):
    """Verify a checkpoint on an empty mounted volume before exposing its files."""
    mount = Path(mount)
    # The host's root-only `.adp-host` state (never archived) may already sit on a fresh disk.
    if mount.is_symlink() or not mount.is_dir() or any(entry.name != HOST_STATE_DIRECTORY for entry in mount.iterdir()):
        raise InvalidCheckpoint("restore requires an empty mounted home")
    staged_home = mount / ".crew-verified-restore"
    try:
        restore_exact(staged_home, receipt, fetch, crew_version=crew_version)
        for entry in staged_home.iterdir():
            os.rename(entry, mount / entry.name)
        staged_home.rmdir()
        directory_fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if staged_home.is_dir() and not any(entry.name not in (staged_home.name, HOST_STATE_DIRECTORY) for entry in mount.iterdir()):
            shutil.rmtree(staged_home)
        raise
