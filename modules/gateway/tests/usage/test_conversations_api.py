"""My conversations API (#9026): one entry per conversation, newest first, owner-scoped."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.auth.dependencies import get_current_user
from src.crew.checkpoint_objects import InvalidCheckpointObjectsError
from src.crew.targets import CrewOwner
from src.shared.database import get_db
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage import conversations_routes as routes

ORG = "org-a"
SUBJECT = "cognito-sub-1"
MEMBER = "member-row-1"
T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


def _row(
    i: int,
    *,
    at: datetime,
    user_id: str = SUBJECT,
    owner_user_id: str | None = None,
    harness: str | None = "claude_code",
    conversation_id: str | None = "sess-1",
    origin: str = "direct",
    title: str | None = None,
    model: str = "us.anthropic.claude-fable-5-1",
    chat_log: str | None = None,
    org_id: str = ORG,
) -> UsageLog:
    return UsageLog(
        id=f"row-{i}",
        timestamp=at,
        org_id=org_id,
        department_id="d",
        team_id="t",
        user_id=user_id,
        account_type="human",
        model=model,
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.01"),
        latency_ms=100,
        status_code=200,
        request_id=f"req-{i}",
        harness=harness,
        conversation_id=conversation_id,
        origin=origin,
        conversation_title=title,
        owner_user_id=owner_user_id,
        chat_log_s3_key=chat_log,
    )


@pytest.fixture
async def seeded(db_session):
    rows = [
        # conversation A: Claude Code from the laptop, 3 turns, title on the first
        _row(1, at=T0, title="fix the login bug"),
        _row(2, at=T0 + timedelta(minutes=1)),
        _row(3, at=T0 + timedelta(minutes=5), chat_log="logs/req-3.json"),
        # conversation B: Codex on the Dev Box, written under the guest identity for the member row
        _row(
            4,
            at=T0 + timedelta(minutes=2),
            user_id="iam-agent:dev-box-guest",
            owner_user_id=MEMBER,
            harness="codex_cli",
            conversation_id="thread-9",
            origin="dev_box",
            title="rank repos by commits",
            model="openai.gpt-6-astra",
            org_id="__platform__",
        ),
        _row(
            5,
            at=T0 + timedelta(minutes=10),
            user_id="iam-agent:dev-box-guest",
            owner_user_id=MEMBER,
            harness="codex_cli",
            conversation_id="thread-9",
            origin="dev_box",
            model="openai.gpt-6-astra",
            org_id="__platform__",
        ),
        # untracked Kiro turns on the box, same day
        _row(
            6,
            at=T0 + timedelta(minutes=3),
            user_id="iam-agent:dev-box-guest",
            owner_user_id=MEMBER,
            harness="kiro_crew",
            conversation_id=None,
            origin="dev_box",
            org_id="__platform__",
        ),
        # a row from before the capture shipped: no harness, no origin — not a conversation
        _row(9, at=T0 + timedelta(minutes=30), harness=None, conversation_id=None, origin=None),
        # someone else's conversation in the same org, and the same subject in another org
        _row(7, at=T0 + timedelta(minutes=20), user_id="other-user", conversation_id="sess-other"),
        _row(8, at=T0 + timedelta(minutes=21), org_id="org-b", conversation_id="sess-elsewhere"),
        # Claude Code over SSH on the box: Bedrock mode carries no session id, so the gateway grouped it under a derived id
        _row(
            10,
            at=T0 + timedelta(minutes=4),
            user_id="iam-agent:dev-box-guest",
            owner_user_id=MEMBER,
            conversation_id="d:abc",
            origin="dev_box",
            org_id="__platform__",
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return db_session


@pytest.fixture
def client(seeded):
    app = FastAPI()
    app.include_router(routes.router)

    async def _db():
        yield seeded

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id=SUBJECT,
        org_id=ORG,
        team_id="t",
        department_id="d",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store = MagicMock()
    store.list_conversations = AsyncMock(return_value=[])
    store.conversation_for_owner = AsyncMock(return_value=None)
    with (
        patch("src.crew.proxy.resolve_crew_owner", new=AsyncMock(return_value=CrewOwner(ORG, MEMBER))),
        patch.object(routes, "backup_store", return_value=store),
    ):
        client = TestClient(app)
        client.backup = store  # type: ignore[attr-defined]
        yield client


def test_lists_the_owners_conversations_newest_first_with_both_identities(client):
    body = client.get("/v1/me/conversations").json()
    titles = [(c["harness"], c["conversation_id"], c["origin"], c["turns"]) for c in body["conversations"]]
    assert titles == [
        ("codex_cli", "thread-9", "dev_box", 2),
        ("claude_code", "sess-1", "direct", 3),
        ("claude_code", "d:abc", "dev_box", 1),
        ("kiro_crew", "untracked:2026-10-26", "dev_box", 1),
    ], "newest first; Dev Box rows (platform tenant) count for the owner; other users/orgs and unlabelled rows absent"
    codex, claude, _derived, kiro = body["conversations"]
    assert all(c["source"] == "gateway" for c in body["conversations"])
    assert codex["title"] == "rank repos by commits" and codex["models"] == ["openai.gpt-6-astra"] and codex["cost_usd"] == pytest.approx(0.02)
    assert (
        claude["title"] == "fix the login bug"
        and claude["started_at"].startswith("2026-10-26T10:00:00")
        and claude["last_activity_at"].startswith("2026-10-26T10:05:00")
    )
    assert kiro["untracked"] is True and kiro["title"] == "Untracked turns"
    assert body["next_before"] is None


def test_filters_and_cursor(client):
    assert [c["conversation_id"] for c in client.get("/v1/me/conversations?harness=claude_code").json()["conversations"]] == ["sess-1", "d:abc"]
    assert [c["conversation_id"] for c in client.get("/v1/me/conversations?origin=dev_box").json()["conversations"]] == [
        "thread-9",
        "d:abc",
        "untracked:2026-10-26",
    ]
    first = client.get("/v1/me/conversations?limit=1").json()
    assert [c["conversation_id"] for c in first["conversations"]] == ["thread-9"] and first["next_before"]
    second = client.get(f"/v1/me/conversations?limit=1&before={quote(first['next_before'])}").json()  # '+' in the offset must be encoded
    assert [c["conversation_id"] for c in second["conversations"]] == ["sess-1"]
    assert client.get("/v1/me/conversations?harness=nope").status_code == 400


def test_turns_come_from_the_latest_chat_log_and_fall_back_to_the_request_list(client):
    log = {
        "timestamp": "2026-10-26T10:05:01Z",
        "request": {
            "messages": [
                {"role": "user", "content": "fix the login bug"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Looking."}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file"}]},
            ]
        },
        "response": {"content": [{"type": "text", "text": "Fixed it."}]},
    }
    with patch.object(routes, "read_chat_log", return_value=log) as reader:
        body = client.get("/v1/me/conversations/claude_code/sess-1/turns").json()
    reader.assert_called_once_with("logs/req-3.json")
    assert (
        body["content_available"] is True and body["content_reason"] == "ok" and body["request_count"] == 3 and body["latest_request_id"] == "req-3"
    )
    assert [(t["role"], t["text"], t["collapsed"]) for t in body["turns"]] == [
        ("user", "fix the login bug", False),
        ("assistant", "Looking.", True),
        ("user", "", True),
        ("assistant", "Fixed it.", False),
    ]
    with patch.object(routes, "read_chat_log", return_value=None):
        fallback = client.get("/v1/me/conversations/codex_cli/thread-9/turns").json()
    assert fallback["content_available"] is False and fallback["request_count"] == 2 and fallback["content_reason"] == "not_logged"
    assert [(t["role"], t["text"], t["model"], t["input_tokens"], t["output_tokens"], t["cost_usd"]) for t in fallback["turns"]] == [
        ("request", "rank repos by commits", "openai.gpt-6-astra", 10, 5, 0.01),
        ("request", "", "openai.gpt-6-astra", 10, 5, 0.01),
    ]
    # the log exists but kept no messages (scrub limit / unmapped passthrough body): say so
    with patch.object(routes, "read_chat_log", return_value={"request": {"messages": []}, "response": {"content": []}}):
        retained = client.get("/v1/me/conversations/claude_code/sess-1/turns").json()
    assert retained["content_available"] is False and retained["content_reason"] == "not_retained" and retained["request_count"] == 3
    with patch.object(routes, "read_chat_log", return_value=None):
        assert client.get("/v1/me/conversations/claude_code/sess-1/turns").json()["content_reason"] == "unreadable"


def test_other_users_conversations_and_unknown_harnesses_are_not_found(client):
    assert client.get("/v1/me/conversations/claude_code/sess-other/turns").status_code == 404
    assert client.get("/v1/me/conversations/claude_code/sess-elsewhere/turns").status_code == 404
    assert client.get("/v1/me/conversations/not-a-harness/sess-1/turns").status_code == 404


def _backup(source, conversation_id, title, started, last, turns, path, unreadable=False):
    return {
        "source": source,
        "conversation_id": conversation_id,
        "title": title,
        "started_at": started.isoformat(),
        "last_activity_at": last.isoformat(),
        "turn_count": turns,
        "checkpoint_id": "ck-1",
        "unreadable": unreadable,
        "archive_path": path,
    }


OWNER_SESSION = "2b4f8a9c-1d2e-4f3a-8b7c-0a1b2c3d4e5f"
BACKUP_CODEX = _backup(
    "codex", "thread-9", "rank repos by commits", T0 + timedelta(minutes=2), T0 + timedelta(minutes=10), 2, ".codex/owner-shell/history.jsonl"
)
BACKUP_CLAUDE = _backup(
    "claude_code",
    OWNER_SESSION,
    "fix the login bug on the box",
    T0 + timedelta(minutes=1),
    T0 + timedelta(minutes=6),
    3,
    f".claude/owner-shell/projects/-home-ec2-user/{OWNER_SESSION}.jsonl",
)
BACKUP_KIRO = _backup(
    "kiro_crew", "dashboard_chat-3-1", "Plan delivery", T0 - timedelta(days=1), T0 + timedelta(minutes=3), 80, "sessions/dashboard_chat-3-1.jsonl"
)


def test_backup_conversations_join_the_list_and_absorb_their_gateway_twins(client):
    """#9069: one entry per conversation; the Dev Box's gateway-derived twins (same id, or same harness overlapping in time) fold in."""
    client.backup.list_conversations.return_value = [BACKUP_CODEX, BACKUP_CLAUDE, BACKUP_KIRO]
    body = client.get("/v1/me/conversations").json()
    assert [(c["harness"], c["conversation_id"], c["source"], c["origin"]) for c in body["conversations"]] == [
        ("codex_cli", "thread-9", "backup", "dev_box"),
        ("claude_code", OWNER_SESSION, "backup", "dev_box"),
        ("claude_code", "sess-1", "gateway", "direct"),
        ("kiro_crew", "dashboard_chat-3-1", "backup", "dev_box"),
    ], "thread-9 by id, d:abc and the untracked Kiro turns by time; the laptop session in the same minutes stays separate"
    codex, claude, laptop, kiro = body["conversations"]
    assert codex["title"] == "rank repos by commits" and codex["turns"] == 2 and codex["requests"] == 2 and codex["partial"] is True, (
        "history-only backup"
    )
    assert codex["cost_usd"] == pytest.approx(0.02) and codex["models"] == ["openai.gpt-6-astra"]
    assert claude["turns"] == 3 and claude["requests"] == 1 and claude["cost_usd"] == pytest.approx(0.01) and claude["partial"] is False
    assert laptop["turns"] == 3 and laptop["cost_usd"] == pytest.approx(0.03)
    assert kiro["turns"] == 80 and kiro["requests"] == 1 and kiro["cost_usd"] == pytest.approx(0.01) and kiro["unreadable"] is False
    assert body["next_before"] is None
    client.backup.list_conversations.assert_awaited_once_with(CrewOwner(ORG, MEMBER), source=None, limit=50, before=None, owner_shell=True)


def test_backup_paging_shares_the_cursor_and_filters_reach_the_store(client):
    client.backup.list_conversations.return_value = [BACKUP_CODEX]
    first = client.get("/v1/me/conversations?limit=1").json()
    assert [(c["conversation_id"], c["source"]) for c in first["conversations"]] == [("thread-9", "backup")]
    assert first["next_before"].startswith("2026-10-26T10:10:00"), "both sources filled their page: the cursor is the older of their last entries"
    client.backup.list_conversations.return_value = []
    second = client.get(f"/v1/me/conversations?limit=1&before={quote(first['next_before'])}").json()
    assert [c["conversation_id"] for c in second["conversations"]] == ["sess-1"]
    before = client.backup.list_conversations.await_args.kwargs["before"]
    assert before is not None and before.isoformat().startswith("2026-10-26T10:10:00")
    client.backup.list_conversations.reset_mock()
    client.get("/v1/me/conversations?harness=codex_cli")
    assert client.backup.list_conversations.await_args.kwargs["source"] == "codex"
    client.backup.list_conversations.reset_mock()
    assert [c["conversation_id"] for c in client.get("/v1/me/conversations?origin=direct").json()["conversations"]] == ["sess-1"]
    client.get("/v1/me/conversations?harness=web_chat")
    client.backup.list_conversations.assert_not_awaited()


def test_backup_store_failure_leaves_the_gateway_list(client):
    client.backup.list_conversations.side_effect = RuntimeError("store down")
    body = client.get("/v1/me/conversations").json()
    assert len(body["conversations"]) == 4 and all(c["source"] == "gateway" for c in body["conversations"])


def test_backup_turns_read_the_archive_and_report_partial_unreadable_or_unavailable(client):
    row = SimpleNamespace(unreadable=False, archive_path=BACKUP_CLAUDE["archive_path"])
    checkpoint = SimpleNamespace(id="ck-1")
    client.backup.conversation_for_owner.return_value = (row, checkpoint)
    objects = MagicMock()
    objects.conversation_turns.return_value = (
        [{"role": "user", "time": "2026-10-26T10:01:00+00:00", "text": "fix the login bug on the box", "collapsed": False}],
        True,
    )
    with patch.object(routes, "backup_objects", return_value=objects):
        body = client.get(f"/v1/me/conversations/claude_code/{OWNER_SESSION}/turns?source=backup&limit=1").json()
        objects.conversation_turns.assert_called_once_with(checkpoint, row.archive_path, "claude_code", OWNER_SESSION, 0, 1)
        assert body["content_available"] is True and body["content_reason"] == "ok" and body["has_more"] is True and body["source"] == "backup"
        assert [t["text"] for t in body["turns"]] == ["fix the login bug on the box"]
        client.backup.conversation_for_owner.assert_awaited_with(CrewOwner(ORG, MEMBER), "claude_code", OWNER_SESSION)
        codex = client.get("/v1/me/conversations/codex_cli/thread-9/turns?source=backup&offset=1").json()
        assert codex["content_reason"] == "partial" and objects.conversation_turns.call_args.args[2:] == ("codex", "thread-9", 1, 200)
        extract = SimpleNamespace(unreadable=False, archive_path=".codex/owner-shell/sessions/2026/10/27/turns-2026-10-27T04-01-22-thread-9.jsonl")
        client.backup.conversation_for_owner.return_value = (extract, checkpoint)
        assert client.get("/v1/me/conversations/codex_cli/thread-9/turns?source=backup").json()["content_reason"] == "ok", "#9073 extract: both sides"
        client.backup.conversation_for_owner.return_value = (SimpleNamespace(unreadable=True, archive_path="x"), checkpoint)
        assert client.get("/v1/me/conversations/kiro_crew/dashboard_chat-3-1/turns?source=backup").json()["content_reason"] == "unreadable"
        client.backup.conversation_for_owner.return_value = None
        assert client.get("/v1/me/conversations/kiro_crew/dashboard_chat-3-1/turns?source=backup").status_code == 404
        assert client.get("/v1/me/conversations/web_chat/chat-1/turns?source=backup").status_code == 404
        client.backup.conversation_for_owner.return_value = (row, checkpoint)
        objects.conversation_turns.side_effect = InvalidCheckpointObjectsError("gone")
        assert client.get(f"/v1/me/conversations/claude_code/{OWNER_SESSION}/turns?source=backup").status_code == 503
    with patch.object(routes, "backup_objects", side_effect=HTTPException(503, detail={"error": "checkpoint_storage_unavailable"})):
        assert client.get(f"/v1/me/conversations/claude_code/{OWNER_SESSION}/turns?source=backup").status_code == 503
    client.backup.conversation_for_owner.side_effect = RuntimeError("db down")
    assert client.get(f"/v1/me/conversations/claude_code/{OWNER_SESSION}/turns?source=backup").status_code == 503
    assert client.get("/v1/me/conversations/claude_code/sess-1/turns?source=nope").status_code == 404
