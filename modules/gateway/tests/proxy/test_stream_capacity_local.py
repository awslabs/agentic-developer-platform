"""Exercise optional per-worker inference limits with synthetic ASGI requests."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[2] / "src/proxy/stream_capacity.py"
spec = importlib.util.spec_from_file_location("gateway_stream_capacity", MODULE)
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


async def request(middleware, path="/v1/messages", method="POST"):
    responses = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        responses.append(message)

    await middleware({"type": "http", "method": method, "path": path}, receive, send)
    return responses


class StreamCapacityTests(unittest.IsolatedAsyncioTestCase):
    def test_gateway_registers_admission_before_budget_work(self):
        source = (MODULE.parents[1] / "app.py").read_text()
        self.assertIn("admission_settings(os.environ)", source)
        self.assertLess(source.index("app.add_middleware(BudgetEnforcementMiddleware)"), source.index("app.add_middleware(StreamCapacityMiddleware"))
        self.assertLess(source.index("app.add_middleware(StreamCapacityMiddleware"), source.index("app.add_middleware(AgentModelIdentityMiddleware)"))

    def test_explicit_limits_are_required_and_off_by_default(self):
        self.assertIsNone(capacity.admission_settings({}))
        names = ("BG_MAX_ACTIVE_STREAMS_PER_WORKER", "BG_STREAM_ADMISSION_QUEUE_LIMIT", "BG_STREAM_ADMISSION_WAIT_MS")
        self.assertEqual(capacity.admission_settings(dict(zip(names, ("2", "1", "100")))), (2, 1, 100))
        for values in (("2", "1", None), ("0", "1", "10"), ("2", "-1", "10"), ("2", "1", "0"), ("2", "1", "5001"), ("two", "1", "10")):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    capacity.admission_settings(dict(zip(names, values)))

    async def test_busy_streams_keep_permits_until_terminal_and_bound_waiters(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        called = 0

        async def serve(scope, receive, send):
            nonlocal called
            called += 1
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"done"})

        gateway = SimpleNamespace(state=SimpleNamespace(shutting_down=False))
        middleware = capacity.StreamCapacityMiddleware(serve, gateway, max_active=1, queue_limit=1, wait_ms=1000)
        first = asyncio.create_task(request(middleware))
        await entered.wait()
        second = asyncio.create_task(request(middleware, "/openai/v1/responses"))
        await asyncio.sleep(0)
        self.assertEqual(middleware.waiting, 1)
        rejected = await request(middleware, "/model/example/invoke-with-response-stream")
        self.assertEqual(rejected[0]["status"], 503)
        self.assertIn((b"retry-after", b"1"), rejected[0]["headers"])
        self.assertEqual(called, 1)
        release.set()
        self.assertEqual((await first)[0]["status"], 200)
        self.assertEqual((await second)[0]["status"], 200)
        self.assertEqual(called, 2)
        self.assertEqual(middleware.waiting, 0)
        self.assertFalse(middleware.active.locked())

    async def test_wait_timeout_and_dependency_error_release_permit_without_health_restart(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def serve(scope, receive, send):
            if scope["path"] == "/health":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                return
            entered.set()
            await release.wait()
            raise RuntimeError("synthetic dependency failure")

        gateway = SimpleNamespace(state=SimpleNamespace(shutting_down=False))
        middleware = capacity.StreamCapacityMiddleware(serve, gateway, max_active=1, queue_limit=1, wait_ms=5)
        first = asyncio.create_task(request(middleware, "/bedrock/invoke"))
        await entered.wait()
        timeout = await request(middleware, "/v1/chat/completions")
        self.assertEqual(timeout[0]["status"], 503)
        self.assertEqual((await request(middleware, "/health", "GET"))[0]["status"], 200)
        release.set()
        with self.assertRaisesRegex(RuntimeError, "synthetic dependency"):
            await first
        self.assertFalse(middleware.active.locked())
        self.assertEqual(middleware.waiting, 0)

    async def test_shutdown_rejects_queued_admissions_without_cutting_healthy_stream(self):
        started = asyncio.Event()
        release = asyncio.Event()
        called = 0

        async def serve(scope, receive, send):
            nonlocal called
            called += 1
            started.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})

        gateway = SimpleNamespace(state=SimpleNamespace(shutting_down=False))
        middleware = capacity.StreamCapacityMiddleware(serve, gateway, max_active=1, queue_limit=1, wait_ms=1000)
        first = asyncio.create_task(request(middleware))
        await started.wait()
        queued = asyncio.create_task(request(middleware))
        await asyncio.sleep(0)
        gateway.state.shutting_down = True
        release.set()
        self.assertEqual((await first)[0]["status"], 200)
        rejected = await queued
        self.assertEqual(rejected[0]["status"], 503)
        self.assertIn(b"gateway_shutting_down", rejected[1]["body"])
        self.assertEqual(called, 1)
        self.assertEqual(middleware.waiting, 0)

    async def test_cancelled_waiter_and_partial_stream_release_capacity_for_recovery(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        first_release = asyncio.Event()

        async def serve(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if scope["path"] == "/v1/messages":
                await send({"type": "http.response.body", "body": b"partial", "more_body": True})
                first_started.set()
                await first_release.wait()
            else:
                second_started.set()
                await send({"type": "http.response.body", "body": b"complete"})

        gateway = SimpleNamespace(state=SimpleNamespace(shutting_down=False))
        middleware = capacity.StreamCapacityMiddleware(serve, gateway, max_active=1, queue_limit=1, wait_ms=1000)
        first = asyncio.create_task(request(middleware))
        await first_started.wait()
        cancelled_waiter = asyncio.create_task(request(middleware, "/openai/v1/responses"))
        await asyncio.sleep(0)
        self.assertEqual(middleware.waiting, 1)
        cancelled_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_waiter
        self.assertEqual(middleware.waiting, 0)

        recovered = asyncio.create_task(request(middleware, "/openai/v1/responses"))
        await asyncio.sleep(0)
        self.assertEqual(middleware.waiting, 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second_started.wait()
        self.assertEqual((await recovered)[0]["status"], 200)
        self.assertFalse(middleware.active.locked())
        self.assertEqual(middleware.waiting, 0)
        self.assertEqual((await request(middleware, "/openai/v1/responses"))[0]["status"], 200)
