#!/usr/bin/env python3
"""Real TCP backpressure/relay fixture using installed Task SSE transport.

No AWS, credentials, provider calls, or shared task writes. InMemoryTaskStore is
an explicitly controlled fixture. Run with PYTHONPATH=modules/gateway locally or
PYTHONPATH=/app in the gateway image. The ten-second production bound is unchanged.
"""

import asyncio
import json
import socket
import time
from dataclasses import replace

import httpx
import uvicorn
from starlette.requests import Request

from src.tasks.read_store import InMemoryTaskStore, TaskRecord
from src.tasks.response import TaskStreamingResponse
from src.tasks.streaming import StreamOutcome, StreamRequest, stream_events

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
STAMP = "2026-09-25T00:00:00Z"
COUNT = 2048


async def main():
    store = InMemoryTaskStore()
    store.put_task(
        TaskRecord(
            task_id=TASK,
            invocation_id="5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40",
            tenant_id="fixture",
            owner_principal_id="fixture",
            persona="agent-task-investigator",
            status="running",
            version=1,
            created_at=STAMP,
            updated_at=STAMP,
            deadline_at="2026-09-25T01:00:00Z",
        )
    )
    for _ in range(COUNT):
        store.append_event(
            task_id=TASK,
            report_id=None,
            event_type="progress.updated",
            data={"message": "x" * 1024, "stage": "analysis"},
            producer_timestamp=None,
            timestamp=STAMP,
        )
    outcomes, released = {}, {}
    started = time.monotonic()
    producer_at = None

    async def produce():
        nonlocal producer_at
        await asyncio.sleep(0.1)
        store.append_event(
            task_id=TASK,
            report_id=None,
            event_type="task.completed",
            data={"status": "completed", "version": 2},
            producer_timestamp=None,
            timestamp=STAMP,
        )
        store.put_task(replace(store.load_task(task_id=TASK), status="completed"))
        producer_at = time.monotonic() - started

    async def app(scope, receive, send):
        query = scope.get("query_string", b"").decode()
        mode = query or "slow"
        outcome = StreamOutcome(task_id=TASK)
        outcomes[mode] = outcome
        released[mode] = asyncio.Event()

        async def recheck():
            return store.load_task(task_id=TASK)

        iterator = stream_events(
            StreamRequest(TASK, 0, recheck),
            store,
            outcome,
            is_disconnected=Request(scope, receive).is_disconnected,
        )
        response = TaskStreamingResponse(
            iterator,
            outcome=outcome,
            release=released[mode].set,
            media_type="text/event-stream",
        )
        await response(scope, receive, send)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", log_level="critical", loop="asyncio")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        await asyncio.sleep(0.01)

    async def connect(mode):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
        reader, writer = await asyncio.open_connection(sock=sock, limit=1024)
        writer.write(
            f"GET /?{mode} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in headers
        return reader, writer

    try:
        producer = asyncio.create_task(produce())
        reader, writer = await connect("slow")
        # No body reads: the small TCP windows and StreamReader limit saturate
        # the real transport. No fake clock or patched send function is used.
        await asyncio.wait_for(released["slow"].wait(), 15)
        slow_elapsed = time.monotonic() - started
        assert outcomes["slow"].reason == "slow_consumer"
        assert 10 <= slow_elapsed < 15
        assert outcomes["slow"].last_sequence < COUNT
        await producer
        assert producer_at is not None and producer_at < 1
        writer.close()
        await writer.wait_closed()

        relay_reader, relay_writer = await connect("relay")
        relay_writer.transport.abort()
        await asyncio.wait_for(released["relay"].wait(), 2)
        assert outcomes["relay"].reason == "client_disconnected", {
            "reason": outcomes["relay"].reason,
            "sequence": outcomes["relay"].last_sequence,
        }

        async with httpx.AsyncClient(timeout=20) as client:
            replay = await client.get(f"http://127.0.0.1:{port}/?replay")
        assert replay.status_code == 200
        ids = [line[4:] for line in replay.text.splitlines() if line.startswith("id: ")]
        assert ids == [f"{TASK}:{n}" for n in range(1, COUNT + 2)]
        assert '"type":"task.completed"' in replay.text
        assert outcomes["replay"].reason == "terminal"
        assert all(event.is_set() for event in released.values())
        print(
            json.dumps(
                {
                    "lane": "actual TCP; production response and streaming loop; controlled fixture store",
                    "fixture_event_bound": COUNT + 1,
                    "production_send_timeout_seconds": 10,
                    "slow_close_seconds": slow_elapsed,
                    "slow_reason": outcomes["slow"].reason,
                    "slow_resume_cursor": outcomes["slow"].resume_cursor,
                    "producer_terminal_commit_seconds": producer_at,
                    "relay_reason": outcomes["relay"].reason,
                    "replayed_events": len(ids),
                    "terminal_recovered": True,
                    "all_streams_released": True,
                    "shared_writes": 0,
                    "model_calls": 0,
                }
            )
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)


if __name__ == "__main__":
    asyncio.run(main())
