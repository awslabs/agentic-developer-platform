"""Exercise the real Locust client against a local SSE server; no cloud access."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from campaign_guard import CampaignGuard, serve_permits


class RawSSE(str):
    pass


class StreamingValidationTest(unittest.TestCase):
    def run_streams(
        self, api, events, stages="12:1,13:0", delay=0, expected_count=None,
        extra_env=None
    ):
        index = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal index
                self.rfile.read(int(self.headers["Content-Length"]))
                body = "".join(
                    ("" if isinstance(item, RawSSE) else "data: ")
                    + (item if isinstance(item, str) else json.dumps(item))
                    + ("" if isinstance(item, RawSSE) else "\n\n")
                    for item in events[index]
                ).encode()
                index += 1
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                token = Path(directory) / "token"
                token.write_text("local-test-token")
                env = os.environ | {
                    "ADP_PERF_TOKEN_FILE": str(token),
                    "ADP_PERF_OUTPUT_DIR": directory,
                    "PERF_LABEL": "validation",
                    "PERF_API": api,
                    "PERF_MAX_REQUESTS": str(len(events)),
                    "PERF_STAGES": stages,
                }
                env.update(extra_env or {})
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "locust",
                        "-f",
                        str(Path(__file__).with_name("locustfile.py")),
                        "--headless",
                        "--host",
                        f"http://127.0.0.1:{server.server_port}",
                        "--only-summary",
                        "--stop-timeout",
                        "5",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertIn(result.returncode, (0, 1), result.stderr)
                self.assertNotIn("Unhandled exception", result.stderr)
                rows = [
                    json.loads(line)
                    for line in (Path(directory) / "validation-requests.jsonl")
                    .read_text()
                    .splitlines()
                ]
                self.assertEqual(
                    len(rows),
                    len(events) if expected_count is None else expected_count,
                    result.stderr,
                )
                return rows
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_locust_controller_mode_reports_real_stream_outcomes(self):
        for api, events in (
            ("chat", [{"choices": [{"delta": {"content": "answer"}}]}, "[DONE]"]),
            ("responses", [{"type": "response.output_text.delta", "delta": "answer"},
                           {"type": "response.incomplete", "response": {"status": "incomplete"}}]),
        ):
            with self.subTest(api=api), tempfile.TemporaryDirectory() as directory:
                controller_token = Path(directory) / "controller-token"
                controller_token.write_text("local-controller-token")
                controller_token.chmod(0o600)
                stage = {"name": api, "desired_sessions": 1, "min_dwell_seconds": 0,
                         "required": True, "spawn_rate": 1}
                guard = CampaignGuard(
                    {"attempts": 1, "active_seconds": 20, "drain_seconds": 5,
                     "input_tokens": 64, "output_tokens": 128, "max_spend": 1}, [stage]
                )
                guard.start_stage(api, 1)
                server = serve_permits(guard, "local-controller-token")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    rows = self.run_streams(
                        api, [events], stages="3:1,4:0", extra_env={
                            "PERF_CONTROLLER_URL": f"http://127.0.0.1:{server.server_port}",
                            "PERF_CONTROLLER_TOKEN_FILE": str(controller_token),
                            "PERF_RESERVE_INPUT_TOKENS": "64",
                            "PERF_DRAIN_SECONDS": "5",
                            "PERF_IDENTITY_ID": "local-developer",
                            "PERF_IDENTITY_KIND": "developer",
                            "PERF_PRICE_PER_INPUT_TOKEN": "0.000001",
                            "PERF_PRICE_PER_OUTPUT_TOKEN": "0.000001",
                            "PERF_LONG_RATIO": "0",
                            "PERF_RESPONSES_MAX_TOKENS": "128",
                        }
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                self.assertEqual(guard.report()["physical_attempts"], 1)
                self.assertEqual(guard.report()["completed"], 1)
                self.assertEqual(rows[0].get("success", False), api == "chat")
                self.assertEqual(guard.end_stage(), api == "chat")

    def test_responses_requires_completed_content_without_errors(self):
        content = {"type": "response.output_text.delta", "delta": "answer"}
        completed = {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"output_tokens": 1}},
        }
        rows = self.run_streams(
            "responses",
            [
                [content, completed],
                [content, "[DONE]"],
                [
                    content,
                    {
                        "type": "response.incomplete",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                        },
                    },
                ],
                [completed],
                [
                    content,
                    {"type": "error", "error": "upstream_failure"},
                    {
                        "type": "response.failed",
                        "response": {
                            "status": "failed",
                            "error": {"code": "server_error"},
                        },
                    },
                    completed,
                ],
            ],
        )
        self.assertEqual(
            [bool(row.get("success")) for row in rows],
            [True, False, False, False, False],
        )
        self.assertEqual(rows[0]["usage"], {"output_tokens": 1})
        self.assertEqual(rows[2]["incomplete_details"]["reason"], "max_output_tokens")
        self.assertEqual(rows[4]["terminal_error"]["code"], "server_error")

    def test_chat_requires_done_content_without_errors(self):
        content = {"choices": [{"delta": {"content": "answer"}}]}
        finish = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        upstream_error = RawSSE(
            'event: error\ndata: {"type":"error","error":{"type":"upstream_stream_error"}}\n\n'
        )
        rows = self.run_streams(
            "chat",
            [
                [content, finish, "[DONE]"],
                [],
                [content],
                [content, finish],
                [upstream_error],
                [content, upstream_error, "[DONE]"],
                [content, RawSSE('data: {"choices":\n\n'), "[DONE]"],
                ["[DONE]"],
            ],
        )
        self.assertEqual(
            [bool(row.get("success")) for row in rows], [True, False, False, False, False, False, False, False]
        )
        self.assertTrue(rows[0]["complete"])
        self.assertFalse(rows[2]["complete"])
        self.assertEqual(rows[3]["finish_reason"], "stop")
        self.assertFalse(rows[3]["complete"])
        self.assertIn("upstream_stream_error", rows[4]["error"])
        self.assertIn("upstream_stream_error", rows[5]["error"])
        self.assertEqual(rows[6]["error"], "JSONDecodeError")

    def test_zero_stage_drains_active_users_together(self):
        content = {"choices": [{"delta": {"content": "answer"}}]}
        rows = self.run_streams(
            "chat",
            [[content, "[DONE]"]] * 15,
            stages="2:5,7:0",
            delay=3,
            expected_count=5,
        )
        self.assertTrue(all(row.get("success") for row in rows))


if __name__ == "__main__":
    unittest.main()
