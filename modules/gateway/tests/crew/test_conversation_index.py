"""Fixture-backed metadata extraction and publication tests."""

import hashlib
import io
import json
import sqlite3
import tarfile

import pytest
from sqlalchemy import select

from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_objects import VersionedCheckpointObjects
from src.crew.conversation_index import extract_conversations
from src.crew.targets import CrewOwner
from src.shared.models.crew_conversation import CrewConversation
from tests.crew.test_checkpoint_objects import S3Fixture
from tests.crew.test_checkpoint_store import context  # noqa: F401


def archive_with_conversations(kiro_records=None, include_codex=False, include_owner_claude=False):
    files = {
        ".claude/projects/-workspace/claude-1.jsonl": [
            {"type": "ai-title", "aiTitle": "Review plan", "sessionId": "claude-1"},
            {"type": "user", "timestamp": "2026-01-01T10:00:00Z", "message": {"role": "user", "content": "PRIVATE BODY"}},
            {"type": "assistant", "timestamp": "2026-01-01T11:00:00Z", "message": {"role": "assistant", "content": "SECRET REPLY"}},
        ],
        "sessions/dashboard_chat-kiro-1.jsonl": kiro_records
        if kiro_records is not None
        else [
            {"role": "user", "ts": "2026-01-02T12:00:00Z", "title": "Plan delivery", "text": "PRIVATE BODY"},
        ],
    }
    if include_codex:
        session_id = "11111111-2222-3333-4444-555555555555"
        files[".codex/owner-shell/history.jsonl"] = [
            {"session_id": session_id, "ts": 1792713600, "text": "Inspect backup"},
            {"session_id": session_id, "ts": 1792717200, "text": "PRIVATE CODEX FOLLOW-UP"},
        ]
        files[f".codex/owner-shell/sessions/2026/10/23/metadata-2026-10-23T00-00-00-{session_id}.json"] = [
            {"id": session_id, "timestamp": "2026-10-23T00:00:00Z", "cwd": "/workspace"},
        ]
    if include_owner_claude:
        owner_session = "2b4f8a9c-1d2e-4f3a-8b7c-0a1b2c3d4e5f"
        files[f".claude/owner-shell/projects/-home-ec2-user/{owner_session}.jsonl"] = [
            {"type": "ai-title", "aiTitle": "fix the login bug", "sessionId": owner_session, "extract": "adp-owner-shell", "truncated": False},
            {
                "type": "user",
                "timestamp": "2026-10-27T08:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "fix the login bug"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-10-27T08:00:06Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "PRIVATE OWNER REPLY"}]},
            },
        ]
    archive = io.BytesIO()
    manifest_files = {}
    with tarfile.open(fileobj=archive, mode="w") as output:
        for path, records in files.items():
            content = b"".join(json.dumps(record).encode() + b"\n" for record in records)
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
            manifest_files[path] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    body = archive.getvalue()
    manifest = json.dumps(
        {"format": 1, "crew_version": "0.11.2", "archive_sha256": hashlib.sha256(body).hexdigest(), "files": manifest_files}
    ).encode()
    return body, manifest


def test_headers_keep_only_titles_times_counts_and_archive_pointer():
    body, _ = archive_with_conversations()
    rows = extract_conversations(io.BytesIO(body))
    assert [(row["source"], row["title"], row["turn_count"]) for row in rows] == [
        ("claude_code", "Review plan", 2),
        ("kiro_crew", "Plan delivery", 1),
    ]
    assert all(not row["unreadable"] for row in rows)
    assert "PRIVATE BODY" not in repr(rows)
    assert "SECRET REPLY" not in repr(rows)


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("extra_body", [False, True])
async def test_verified_archive_publishes_owner_scoped_newest_first_rows(context, malformed, extra_body):  # noqa: F811
    store, factory = context
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewConversation.__table__.create)
    owner = CrewOwner("tenant-a", "user-a")
    candidate = await store.admit(owner, "conversations", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = archive_with_conversations(kiro_records=[{"role": "user", "ts": None}] if malformed else None)
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grants = await store.grant(candidate.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="fixture-version")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "fixture-version")] = content
    if extra_body:
        original_headers = storage.conversation_headers
        storage.conversation_headers = lambda items: [dict(header, body="SECRET_MATERIAL_SHOULD_NOT_PERSIST") for header in original_headers(items)]
    published = await store.complete(candidate.id, identity, objects, storage, now=100)
    assert published.status == "verified"
    result = await store.list_conversations(owner)
    if malformed:
        assert [(row["source"], row["unreadable"]) for row in result] == [("claude_code", False), ("kiro_crew", True)]
        assert result[1]["turn_count"] == 0
    else:
        assert [(row["source"], row["title"], row["turn_count"]) for row in result] == [
            ("kiro_crew", "Plan delivery", 1),
            ("claude_code", "Review plan", 2),
        ]
    assert all(row["checkpoint_id"] == candidate.id for row in result)
    assert len(await store.list_conversations(CrewOwner("tenant-a", "other"))) == 0
    assert len(await store.list_conversations(owner, source="claude_code")) == 1
    await store.complete(candidate.id, identity, objects, storage, now=100)
    async with factory() as session:
        assert len((await session.scalars(select(CrewConversation))).all()) == 2
    with sqlite3.connect(factory.kw["bind"].url.database) as database:
        columns = {column[1] for column in database.execute("PRAGMA table_info(crew_conversations)")}
        stored = repr(database.execute("SELECT * FROM crew_conversations").fetchall())
    assert not columns.intersection({"body", "text", "content", "message"})
    assert "PRIVATE BODY" not in stored
    assert "SECRET REPLY" not in stored
    assert "SECRET_MATERIAL_SHOULD_NOT_PERSIST" not in stored


def test_codex_history_joins_session_metadata_without_rollout_payloads():
    session_id = "11111111-2222-3333-4444-555555555555"
    history = [
        {"session_id": session_id, "ts": 1792713600, "text": "Explain the backup"},
        {"session_id": session_id, "ts": 1792717200, "text": "PRIVATE FOLLOW-UP BODY"},
    ]
    metadata_path = f".codex/owner-shell/sessions/2026/10/23/metadata-2026-10-23T00-00-00-{session_id}.json"
    metadata = {"id": session_id, "timestamp": "2026-10-23T00:00:00Z", "cwd": "/workspace", "cli_version": "1.0"}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for path, content in (
            (".codex/owner-shell/history.jsonl", b"".join(json.dumps(entry).encode() + b"\n" for entry in history)),
            (metadata_path, json.dumps(metadata).encode()),
        ):
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    rows = extract_conversations(io.BytesIO(archive.getvalue()))
    assert len(rows) == 1
    assert rows[0]["source"] == "codex"
    assert rows[0]["conversation_id"] == session_id
    assert rows[0]["archive_path"] == metadata_path
    assert rows[0]["title"] == "Explain the backup"
    assert rows[0]["turn_count"] == 2
    assert rows[0]["started_at"].isoformat() == "2026-10-23T00:00:00+00:00"
    assert rows[0]["last_activity_at"].isoformat() == "2026-10-23T01:00:00+00:00"
    assert rows[0]["unreadable"] is False
    assert "PRIVATE FOLLOW-UP BODY" not in repr(rows)


def test_unknown_kiro_record_is_unreadable_without_hiding_claude():
    body, _ = archive_with_conversations(kiro_records=[{"role": "new-vendor-shape", "other": "not a conversation"}])
    rows = extract_conversations(io.BytesIO(body))
    assert [(row["source"], row["unreadable"]) for row in rows] == [("claude_code", False), ("kiro_crew", True)]
    assert rows[1]["turn_count"] == 0
    assert rows[1]["title"] == "Untitled conversation"


def test_broken_codex_metadata_remains_visible_as_unreadable():
    archive = io.BytesIO()
    path = ".codex/owner-shell/sessions/2026/10/23/metadata-broken.json"
    with tarfile.open(fileobj=archive, mode="w") as output:
        member = tarfile.TarInfo(path)
        member.size = len(b"not-json")
        output.addfile(member, io.BytesIO(b"not-json"))
    rows = extract_conversations(io.BytesIO(archive.getvalue()))
    assert [(row["source"], row["conversation_id"], row["unreadable"]) for row in rows] == [("codex", "metadata-broken", True)]


async def test_three_sources_and_malformed_row_survive_two_verified_checkpoints(context):  # noqa: F811
    store, factory = context
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewConversation.__table__.create)
    owner = CrewOwner("tenant-a", "user-a")
    archive, manifest = archive_with_conversations(kiro_records=[{"role": "unknown-vendor-shape"}], include_codex=True)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    predecessor = None
    for sequence in range(2):
        checkpoint = await store.admit(owner, f"three-source-{sequence}", expected_predecessor_id=predecessor, now=100)
        identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(checkpoint.id.encode()).hexdigest(), "sha256:" + "f" * 64, checkpoint.id)
        objects = {
            label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for label, content in (("archive", archive), ("manifest", manifest))
        }
        grants = await store.grant(checkpoint.id, identity, objects, storage, now=100)
        for label, content in (("archive", archive), ("manifest", manifest)):
            objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="three-source-version")
            s3.objects[(objects[label]["bucket"], objects[label]["key"], "three-source-version")] = content
        published = await store.complete(checkpoint.id, identity, objects, storage, now=100)
        assert published.status == "verified"
        predecessor = checkpoint.id
    result = await store.list_conversations(owner)
    assert [(row["source"], row["unreadable"]) for row in result] == [
        ("codex", False),
        ("claude_code", False),
        ("kiro_crew", True),
    ]
    assert all(row["checkpoint_id"] == predecessor for row in result)
    assert result[0]["title"] == "Inspect backup"
    assert len(await store.list_conversations(owner, source="codex")) == 1
    with sqlite3.connect(factory.kw["bind"].url.database) as database:
        stored = repr(database.execute("SELECT * FROM crew_conversations").fetchall())
        assert database.execute("SELECT count(*) FROM crew_conversations").fetchone()[0] == 6
    assert "PRIVATE BODY" not in stored
    assert "PRIVATE CODEX FOLLOW-UP" not in stored


def test_oversized_source_fields_mark_rows_unreadable_before_database_insert():
    long_session = "s" * 300
    long_slug = "x" * 1020
    files = {
        f"sessions/dashboard_chat-{long_session}.jsonl": b'{"role":"user","ts":"2026-01-01T00:00:00Z","title":"Safe title"}\n',
        f".claude/projects/{long_slug}/session.jsonl": b'{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"body"}}\n',
        ".codex/owner-shell/history.jsonl": (json.dumps({"session_id": long_session, "ts": 1792713600, "text": "Prompt title"}) + "\n").encode(),
        ".codex/owner-shell/sessions/2026/10/23/metadata-session.json": json.dumps(
            {"id": long_session, "timestamp": "2026-10-23T00:00:00Z"}
        ).encode(),
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for path, content in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    rows = extract_conversations(io.BytesIO(archive.getvalue()))
    assert {row["source"] for row in rows} == {"kiro_crew", "claude_code", "codex"}
    assert all(row["unreadable"] for row in rows)
    assert all(len(row["conversation_id"]) == 64 and len(row["archive_path"]) <= 1024 for row in rows)
    assert all("body" not in repr(row) for row in rows)


def test_owner_shell_claude_extract_is_indexed_titled_and_readable():
    """#9069: the owner's own Claude Code session (compact extract) lists as claude_code and its turns read back."""
    from src.crew.conversation_index import owner_shell_path, read_turns

    session = "2b4f8a9c-1d2e-4f3a-8b7c-0a1b2c3d4e5f"
    path = f".claude/owner-shell/projects/-home-ec2-user/{session}.jsonl"
    records = [
        {"type": "ai-title", "aiTitle": "fix the login bug", "sessionId": session, "extract": "adp-owner-shell", "truncated": False},
        {
            "type": "user",
            "timestamp": "2026-10-27T08:00:01.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "fix the login bug"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-27T08:00:03.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Looking."}, {"type": "tool_use"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-27T08:00:06.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it."}]},
        },
    ]
    content = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for name in (path, f".claude/owner-shell/projects/-home-ec2-user/{session}/subagents/agent-1.jsonl"):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    rows = extract_conversations(io.BytesIO(archive.getvalue()))
    assert [(row["source"], row["conversation_id"], row["archive_path"], row["title"], row["turn_count"], row["unreadable"]) for row in rows] == [
        ("claude_code", session, path, "fix the login bug", 3, False)
    ], "the subagent transcript is not a conversation"
    assert rows[0]["started_at"].isoformat() == "2026-10-27T08:00:01+00:00" and rows[0]["last_activity_at"].isoformat() == "2026-10-27T08:00:06+00:00"
    assert owner_shell_path(path) and owner_shell_path(".codex/owner-shell/history.jsonl") and not owner_shell_path(".claude/projects/-x/y.jsonl")
    turns = read_turns(io.BytesIO(content), "claude_code", session)
    assert [(turn["role"], turn["text"], turn["collapsed"]) for turn in turns] == [
        ("user", "fix the login bug", False),
        ("assistant", "Looking.", True),
        ("assistant", "Fixed it.", False),
    ]


async def test_owner_shell_scope_keeps_the_owners_own_sessions_and_pages_by_last_activity(context):  # noqa: F811
    """#9069: My conversations lists Kiro chats plus the owner's Claude Code and Codex sessions, never Kiro's internal runs."""
    from datetime import UTC, datetime

    store, factory = context
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewConversation.__table__.create)
    owner = CrewOwner("tenant-a", "user-a")
    archive, manifest = archive_with_conversations(include_codex=True, include_owner_claude=True)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    checkpoint = await store.admit(owner, "owner-shell-0", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(checkpoint.id.encode()).hexdigest(), "sha256:" + "f" * 64, checkpoint.id)
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grants = await store.grant(checkpoint.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="owner-shell-version")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "owner-shell-version")] = content
    assert (await store.complete(checkpoint.id, identity, objects, storage, now=100)).status == "verified"

    everything = await store.list_conversations(owner)
    assert [(row["source"], row["archive_path"].split("/")[1]) for row in everything] == [
        ("claude_code", "owner-shell"),
        ("codex", "owner-shell"),
        ("kiro_crew", "dashboard_chat-kiro-1.jsonl"),
        ("claude_code", "projects"),
    ]
    scoped = await store.list_conversations(owner, owner_shell=True)
    assert [(row["source"], row["title"]) for row in scoped] == [
        ("claude_code", "fix the login bug"),
        ("codex", "Inspect backup"),
        ("kiro_crew", "Plan delivery"),
    ], "Kiro's own Claude Code run under .claude/projects is not one of the owner's conversations"
    page = await store.list_conversations(owner, owner_shell=True, before=datetime(2026, 10, 27, tzinfo=UTC), limit=1)
    assert [row["source"] for row in page] == ["codex"]
    assert "PRIVATE OWNER REPLY" not in repr(everything)


def test_codex_turns_extract_gives_both_sides_and_wins_over_the_prompts_only_history():
    """#9073: with a turns extract the Codex row points at it, counts both roles and reads replies; history remains the fallback."""
    from src.crew.conversation_index import codex_turns_extract, read_turns

    session = "01a17b85-171a-7e31-8e39-a991b5f0ca9b"
    other = "01a17b32-9ee5-7150-98ee-64cdab9f56e1"
    base = ".codex/owner-shell/sessions/2026/10/27"
    turns_path = f"{base}/turns-2026-10-27T04-01-22-{session}.jsonl"
    extract = [
        {"session_id": session, "extract": "adp-owner-shell", "truncated": False},
        {"role": "user", "ts": "2026-10-27T04:01:24.000Z", "text": "rank repos by commits"},
        {"role": "assistant", "ts": "2026-10-27T04:01:27.000Z", "text": "PRIVATE CODEX REPLY"},
    ]
    files = {
        ".codex/owner-shell/history.jsonl": [
            {"session_id": session, "ts": 1792000000, "text": "rank repos by commits"},
            {"session_id": other, "ts": 1792000100, "text": "older prompt"},
        ],
        f"{base}/metadata-2026-10-27T04-01-22-{session}.json": [{"id": session, "timestamp": "2026-10-27T04:01:22Z"}],
        turns_path: extract,
        f"{base}/metadata-2026-10-27T02-31-17-{other}.json": [{"id": other, "timestamp": "2026-10-27T02:31:17Z"}],
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for path, records in files.items():
            content = b"".join(json.dumps(record).encode() + b"\n" for record in records)
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    rows = {row["conversation_id"]: row for row in extract_conversations(io.BytesIO(archive.getvalue()))}
    assert set(rows) == {session, other}
    assert rows[session]["archive_path"] == turns_path and rows[session]["turn_count"] == 2 and rows[session]["unreadable"] is False
    assert rows[session]["title"] == "rank repos by commits"
    assert rows[session]["last_activity_at"].isoformat() == "2026-10-27T04:01:27+00:00"
    assert rows[other]["archive_path"].endswith(f"metadata-2026-10-27T02-31-17-{other}.json") and rows[other]["turn_count"] == 1
    assert "PRIVATE CODEX REPLY" not in repr(rows)
    assert codex_turns_extract(turns_path) and not codex_turns_extract(rows[other]["archive_path"])
    content = b"".join(json.dumps(record).encode() + b"\n" for record in extract)
    assert [(t["role"], t["text"]) for t in read_turns(io.BytesIO(content), "codex", session)] == [
        ("user", "rank repos by commits"),
        ("assistant", "PRIVATE CODEX REPLY"),
    ]
    history = b"".join(json.dumps(record).encode() + b"\n" for record in files[".codex/owner-shell/history.jsonl"])
    assert [(t["role"], t["text"]) for t in read_turns(io.BytesIO(history), "codex", other)] == [("user", "older prompt")]


def test_archive_reader_serves_the_codex_turns_extract_and_the_history_fallback():
    """The versioned archive reader picks the turns extract when the row points at it, else the prompts-only history (#9073)."""
    from types import SimpleNamespace

    session = "01a17b85-171a-7e31-8e39-a991b5f0ca9b"
    other = "01a17b32-9ee5-7150-98ee-64cdab9f56e1"
    turns_path = f".codex/owner-shell/sessions/2026/10/27/turns-2026-10-27T04-01-22-{session}.jsonl"
    files = {
        ".codex/owner-shell/history.jsonl": [{"session_id": other, "ts": 1792000100, "text": "older prompt"}],
        turns_path: [
            {"session_id": session, "extract": "adp-owner-shell", "truncated": False},
            {"role": "user", "ts": "2026-10-27T04:01:24.000Z", "text": "rank repos by commits"},
            {"role": "assistant", "ts": "2026-10-27T04:01:27.000Z", "text": "Here is the ranking."},
        ],
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        for path, records in files.items():
            content = b"".join(json.dumps(record).encode() + b"\n" for record in records)
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    body = archive.getvalue()
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    checkpoint = SimpleNamespace(
        id="ck-1",
        owner_key="o" * 52,
        archive_bucket="fixture-bucket",
        archive_key=f"crew-checkpoints/{'o' * 52}/ck-1/archive",
        archive_version_id="v1",
        archive_size=len(body),
        archive_sha256=hashlib.sha256(body).hexdigest(),
    )
    s3.objects[("fixture-bucket", checkpoint.archive_key, "v1")] = body
    turns, has_more = storage.conversation_turns(checkpoint, turns_path, "codex", session, 0, 1)
    assert [(t["role"], t["text"]) for t in turns] == [("user", "rank repos by commits")] and has_more is True
    turns, has_more = storage.conversation_turns(checkpoint, turns_path, "codex", session, 1, 10)
    assert [(t["role"], t["text"]) for t in turns] == [("assistant", "Here is the ranking.")] and has_more is False
    metadata_path = f".codex/owner-shell/sessions/2026/10/27/metadata-2026-10-27T02-31-17-{other}.json"
    turns, has_more = storage.conversation_turns(checkpoint, metadata_path, "codex", other, 0, 10)
    assert [(t["role"], t["text"]) for t in turns] == [("user", "older prompt")] and has_more is False
