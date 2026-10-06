"""TypeScript tools over local HTTP, mounted gateway routes, Moto and SQLite."""

import asyncio
import json
import os
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_memory import KIND_TTL
from src.agentauth.external_roots import provision_root
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.agentauth.test_chat_data_routes import admit
from tests.agentauth.test_chat_memory import capability as capability_fixture
from tests.agentauth.test_chat_memory import client as client_fixture
from tests.agentauth.test_chat_memory import create, key
from tests.agentauth.test_chat_memory import memory as memory_fixture
from tests.agentauth.test_chat_memory import runtime as runtime_fixture
from tests.agentauth.test_chat_memory import store as store_fixture
from tests.agentauth.test_chat_memory import sts as sts_fixture
from tests.agentauth.test_work_producer import ROLE

pytestmark = [pytest.mark.integration, pytest.mark.chat_ports]
capability = capability_fixture
client = client_fixture
memory = memory_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
AGENT = Path(__file__).resolve().parents[3] / "agent-factory" / "agent"
DRIVER = Path(__file__).with_name("fixtures") / "chat_memory_port.cjs"


@pytest.fixture
async def gateway_http(client):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client._transport.app, lifespan="off", log_config=None, access_log=False, timeout_keep_alive=1))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("Gateway test server did not start")
                await asyncio.sleep(0.01)
        yield f"http://chat-gateway.test:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()


async def node_tools(url, now, *, driver=DRIVER, **operation):
    node = shutil.which("node")
    register = AGENT / "node_modules" / "ts-node" / "register" / "transpile-only.js"
    assert node and register.is_file(), "Install Node >=22 and run npm ci --include=dev in modules/agent-factory/agent"
    config = {"url": url, "now": now, "agent": str(AGENT), **operation}
    process = await asyncio.create_subprocess_exec(
        node,
        "--require",
        str(register),
        str(driver),
        json.dumps(config),
        cwd=AGENT,
        env={**os.environ, "NODE_ENV": "development", "TS_NODE_PROJECT": str(AGENT / "tsconfig.json")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


async def test_expired_memories_do_not_break_port_retrieval(client, capability, runtime, memory, gateway_http, monkeypatch):
    monkeypatch.setitem(KIND_TTL, "fact", 2)
    preference = await create(client, capability, kind="preference", content="Use concise answers")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 1)
    expired = [await create(client, capability, idempotency_key=f"fact-{index}") for index in range(5)]
    now = runtime[-1] + 3
    monkeypatch.setattr(chat_data_routes, "clock", lambda: now)

    for deleted in (False, True):
        if deleted:
            for record in expired:
                memory.delete_item(Key=key(record["memory_id"]))
        result = await node_tools(gateway_http, now, mode="retrieve", query="concise", kinds=["preference"])
        assert [record["id"] for record in result["records"]] == [preference["memory_id"]]
        assert await node_tools(gateway_http, now, mode="retrieve", query="absent", kinds=["fact"]) == {"records": []}

    pointers = [row for row in memory.scan()["Items"] if row["SK"].startswith("mem#") and row["id"] != preference["memory_id"]]
    assert len(pointers) == 5 and all(row["ttl"] == now for row in pointers)
    for pointer in pointers:
        memory.delete_item(Key={"PK": pointer["PK"], "SK": pointer["SK"]})
    result = await node_tools(gateway_http, now, mode="retrieve", query="concise", kinds=["preference"])
    assert [record["id"] for record in result["records"]] == [preference["memory_id"]]
    memory.delete_item(Key=key(preference["memory_id"]))
    assert await node_tools(gateway_http, now, mode="retrieve", query="concise", kinds=["preference"]) == {"error": "incomplete"}


@pytest.mark.parametrize("other_tenant", ["tenant", "other-tenant"])
async def test_user_wide_preference_tools_preserve_owner_isolation(
    client, runtime, memory, gateway_http, db_session_factory, monkeypatch, other_tenant
):
    response = await admit(client, runtime)
    assert response.status_code == 200, response.text
    preference = "Please use concise answers"
    owner = await node_tools(gateway_http, runtime[-1], mode="owner", preference=preference)
    assert owner["scope"] == {"user": "human", "tenant": "tenant"}
    for persona in ("reviewer", "developer"):
        assert preference in owner[persona]["content"][0]["text"]
    assert [record["id"] for record in owner["component"]] == [owner["id"]]
    saved = memory.get_item(Key={"PK": f"memory#{owner['id']}", "SK": "record"})["Item"]
    assert saved["labels"] == {} and saved["kind"] == "preference"

    team_id = "team" if other_tenant == "tenant" else "other-team"
    async with db_session_factory() as db:
        if other_tenant != "tenant":
            db.add_all(
                [
                    Organization(id=other_tenant, name="Other tenant"),
                    Team(id=team_id, org_id=other_tenant, department_id="other-department", name="Other team"),
                ]
            )
        db.add_all(
            [
                User(id="other-human", org_id=other_tenant, team_id=team_id, email="other@example.test"),
                TeamMembership(id="other-member", org_id=other_tenant, user_id="other-human", team_id=team_id, is_primary=True),
            ]
        )
        await db.commit()
    bindings = [{"source": "chat", "producer_role": ROLE, "tenant_id": tenant, "personas": ["developer"]} for tenant in {"tenant", other_tenant}]
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    envelope = {
        "message_id": "run-b",
        "tenant_id": other_tenant,
        "persona": "developer",
        "source_ref": {"repo": "chat/session-b"},
        "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
    }
    provision_root(runtime[1].store, envelope, source="chat", human_id="other-human", now=datetime.fromtimestamp(runtime[-1], UTC))
    runtime[4]["uid"] = "other-chat-pod"
    response = await admit(client, runtime, run_id="run-b", envelope_digest=envelope_digest(envelope), pod_uid="other-chat-pod")
    assert response.status_code == 200, response.text
    other = await node_tools(gateway_http, runtime[-1], mode="other", preference=preference, ownerId=owner["id"])
    assert other["recall"]["content"][0]["text"] == "No matching memories found."
    assert other["read"] == {"error": "denied", "status": 404}
    assert "Other owner prefers detailed answers" in other["own"]["content"][0]["text"]
