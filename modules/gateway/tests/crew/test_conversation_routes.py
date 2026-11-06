"""Owner-only archived conversation reads over the public gateway route."""

import hashlib
import io
import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from src.crew import checkpoint_routes as routes
from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_objects import VersionedCheckpointObjects
from src.crew.conversation_index import read_turns
from src.crew.proxy import current_crew_owner
from src.crew.targets import CrewOwner
from src.shared.models.crew_conversation import CrewConversation
from tests.crew.test_checkpoint_objects import S3Fixture
from tests.crew.test_checkpoint_store import context  # noqa: F401
from tests.crew.test_conversation_index import archive_with_conversations
from tests.crew.test_owner_identity import people  # noqa: F401


async def test_detail_reads_one_archived_file_in_order_with_pagination(context):  # noqa: F811
    store, factory = context
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewConversation.__table__.create)
    owner = CrewOwner("tenant-a", "user-a")
    candidate = await store.admit(owner, "detail-fixture", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = archive_with_conversations()
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grants = await store.grant(candidate.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="detail-version")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "detail-version")] = content
    await store.complete(candidate.id, identity, objects, storage, now=100)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[current_crew_owner] = lambda: owner
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: storage
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        listing = await client.get("/v1/crew/conversations")
        assert listing.status_code == 200
        assert listing.json()["conversations"][0]["source"] == "kiro_crew"
        first = await client.get("/v1/crew/conversations/claude_code/claude-1?limit=1")
        assert first.status_code == 200
        assert first.json() == {
            "turns": [{"role": "user", "time": "2026-01-01T10:00:00+00:00", "text": "PRIVATE BODY", "collapsed": False}],
            "has_more": True,
            "unreadable": False,
            "partial": False,
        }
        second = await client.get("/v1/crew/conversations/claude_code/claude-1?offset=1&limit=1")
        assert second.status_code == 200
        assert second.json()["turns"][0]["role"] == "assistant"
        assert second.json()["turns"][0]["text"] == "SECRET REPLY"
        assert second.json()["has_more"] is False
        kiro = await client.get("/v1/crew/conversations/kiro_crew/dashboard_chat-kiro-1")
        assert kiro.status_code == 200
        assert kiro.json()["turns"][0]["text"] == "PRIVATE BODY"
        assert first.headers["cache-control"] == "no-store"
        reads_before_denial = len(s3.gets)
        for foreign_owner in (CrewOwner("tenant-a", "user-b"), CrewOwner("tenant-b", "user-a")):
            app.dependency_overrides[current_crew_owner] = lambda foreign_owner=foreign_owner: foreign_owner
            foreign_list = await client.get("/v1/crew/conversations")
            foreign_detail = await client.get("/v1/crew/conversations/claude_code/claude-1")
            assert foreign_list.status_code == 200 and foreign_list.json() == {"conversations": []}
            assert foreign_detail.status_code == 404
            assert foreign_detail.headers["cache-control"] == "no-store"
        assert len(s3.gets) == reads_before_denial
        async with factory.begin() as session:
            await session.execute(update(CrewConversation).values(user_id="user-b"))
        app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-b")
        mismatched_list = await client.get("/v1/crew/conversations")
        mismatched_detail = await client.get("/v1/crew/conversations/claude_code/claude-1")
        assert mismatched_list.status_code == 200 and mismatched_list.json() == {"conversations": []}
        assert mismatched_detail.status_code == 404
        assert len(s3.gets) == reads_before_denial


def test_assistant_tool_and_reasoning_blocks_are_collapsed():
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T11:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Here is the answer"},
                    {"type": "thinking", "thinking": "PRIVATE REASONING"},
                    {"type": "tool_use", "input": {"token": "PRIVATE TOOL OUTPUT"}},
                ],
            },
        },
    ]
    turns = read_turns(io.BytesIO(b"".join(json.dumps(record).encode() + b"\n" for record in records)), "claude_code", "session")
    assert turns == [{"role": "assistant", "time": "2026-01-01T11:00:00+00:00", "text": "Here is the answer", "collapsed": True}]
    assert "PRIVATE REASONING" not in repr(turns)
    assert "PRIVATE TOOL OUTPUT" not in repr(turns)


def test_kiro_tool_output_is_collapsed_without_exposing_its_body():
    record = {"role": "tool", "ts": "2026-01-01T11:00:00Z", "text": "PRIVATE TOOL RESPONSE"}
    turns = read_turns(io.BytesIO(json.dumps(record).encode() + b"\n"), "kiro_crew", "session")
    assert turns == [{"role": "tool", "time": "2026-01-01T11:00:00+00:00", "text": "", "collapsed": True}]
    assert "PRIVATE TOOL RESPONSE" not in repr(turns)


async def test_authenticated_member_session_reads_only_its_own_detail(context, people):  # noqa: F811
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db
    from src.shared.models.crew_executor import CrewExecutor
    from tests.crew.test_owner_identity import TENANT
    from tests.crew.test_owner_identity import context as token_context

    store, factory = context
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewConversation.__table__.create)
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).values(tenant_id=TENANT, user_id="github-owner"))
    owner = CrewOwner(TENANT, "github-owner")
    candidate = await store.admit(owner, "member-session", expected_predecessor_id=None, now=100)
    identity = CheckpointHelperIdentity("pod", "a" * 52, hashlib.sha256(candidate.id.encode()).hexdigest(), "sha256:" + "f" * 64, candidate.id)
    s3 = S3Fixture()
    storage = VersionedCheckpointObjects(s3, "fixture-bucket")
    archive, manifest = archive_with_conversations()
    objects = {
        label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for label, content in (("archive", archive), ("manifest", manifest))
    }
    grants = await store.grant(candidate.id, identity, objects, storage, now=100)
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label].update(bucket=grants[label]["bucket"], key=grants[label]["key"], version_id="member-version")
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "member-version")] = content
    await store.complete(candidate.id, identity, objects, storage, now=100)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: storage

    async def people_session():
        yield people

    app.dependency_overrides[get_db] = people_session
    app.dependency_overrides[get_current_user] = lambda: token_context("broker-sub", username="GitHub_123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        owner_detail = await client.get("/v1/crew/conversations/claude_code/claude-1")
        assert owner_detail.status_code == 200
        assert owner_detail.json()["turns"][0]["text"] == "PRIVATE BODY"
        reads_before_denial = len(s3.gets)
        app.dependency_overrides[get_current_user] = lambda: token_context("native-sub")
        foreign_detail = await client.get("/v1/crew/conversations/claude_code/claude-1")
        foreign_list = await client.get("/v1/crew/conversations")
        assert foreign_detail.status_code == 404
        assert foreign_list.status_code == 200 and foreign_list.json() == {"conversations": []}
        assert len(s3.gets) == reads_before_denial
