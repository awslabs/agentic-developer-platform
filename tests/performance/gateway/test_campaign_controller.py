import json
import tempfile
import threading
import unittest
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from campaign_controller import ConfigError, checkpoint, load_config, main, model_ramp, report_campaign
from campaign_guard import CampaignGuard, WorkerClient, serve_permits
from campaign_report import qualify_cell, summarize


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.token = self.root / "token"
        self.token.write_text("local-test-token")
        self.token.chmod(0o600)
        self.config_file = self.root / "config.json"
        self.config = {
            "schema_version": 1, "scenario": "smallest-checkpoint", "seed": 1,
            "revision": "test-revision", "approved_account": "fictional-account",
            "approved_role": "fictional-role", "recovery_owner": "fictional-recovery-owner",
            "target": "http://127.0.0.1:12345/api", "local_test": True,
            "evidence_dir": str(self.root),
            "identities": [{"id": "test-developer", "kind": "developer",
                            "tenant": "test-tenant", "root_human": "test-owner",
                            "ownership_receipt": "protected-reference", "token_file": str(self.token)}],
            "models": [{"api": api, "model": "test-model", "sdk_version": "test-sdk",
                        "max_input_tokens": 100, "max_output_tokens": 10,
                        "price_per_input_token": 0.001, "price_per_output_token": 0.002}
                       for api in ("chat", "responses")],
            "limits": {"attempts": 2, "active_seconds": 10, "drain_seconds": 5,
                       "input_tokens": 200, "output_tokens": 20, "max_spend": 1,
                       "setup_seconds": 10, "cleanup_seconds": 10},
            "slos": {"ttft_ms": 1000, "http_ms": 5000, "terminal_ms": 1000},
            "stages": [{"name": api, "required": True, "desired_sessions": 1,
                        "min_dwell_seconds": 0, "spawn_rate": 1} for api in ("chat", "responses")],
        }

    def save(self):
        self.config_file.write_text(json.dumps(self.config))
        self.config_file.chmod(0o600)

    def test_requires_private_explicit_bindings_and_numeric_limits(self):
        for field in ("revision", "approved_account", "approved_role", "recovery_owner"):
            with self.subTest(field=field):
                value = self.config.pop(field)
                self.save()
                with self.assertRaisesRegex(ConfigError, field):
                    load_config(self.config_file)
                self.config[field] = value
        for field, invalid in (("attempts", 41), ("active_seconds", 181),
                               ("drain_seconds", 121), ("max_spend", "unlimited"),
                               ("input_tokens", 0)):
            with self.subTest(field=field):
                original = self.config["limits"][field]
                self.config["limits"][field] = invalid
                self.save()
                with self.assertRaises(ConfigError):
                    load_config(self.config_file)
                self.config["limits"][field] = original
        stages = self.config.pop("stages")
        self.save()
        with self.assertRaisesRegex(ConfigError, "stages"):
            load_config(self.config_file)
        self.config["stages"] = stages
        self.config["models"][0]["price_per_input_token"] = 0
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.config_file)
        self.config["models"][0]["price_per_input_token"] = 0.001
        self.config["target"] = "http://gateway.example.test/api"
        self.save()
        with self.assertRaisesRegex(ConfigError, "local_test"):
            load_config(self.config_file)
        self.config["target"] = "https://gateway.example.test/api"
        self.config["local_test"] = False
        self.save()
        with self.assertRaisesRegex(ConfigError, "campaign"):
            load_config(self.config_file)
        self.config["target"] = "http://127.0.0.1:12345/api"
        self.config["local_test"] = True
        self.save()
        self.assertEqual(load_config(self.config_file)["target"], self.config["target"])
        self.config_file.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "private permissions"):
            load_config(self.config_file)

    def test_validate_only_never_contacts_target(self):
        self.save()
        self.assertEqual(main(["--config", str(self.config_file), "--validate-only"]), 0)
        self.assertFalse((self.root / "checkpoint-results.json").exists())

    def test_both_gateway_apis_require_content_and_terminal(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if self.path.endswith("/responses"):
                    events = [{"type": "response.output_text.delta", "delta": "1 2 3"},
                              {"type": "response.completed", "response": {"status": "completed"}}]
                else:
                    events = [{"choices": [{"delta": {"content": "1 2 3"}}]}, "[DONE]"]
                for event in events:
                    data = json.dumps(event) if isinstance(event, dict) else event
                    self.wfile.write(f"data: {data}\n\n".encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            self.save()
            self.assertEqual(main(["--config", str(self.config_file), "--run"]), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual([path for path, _, _ in received],
                         ["/api/v1/chat/completions", "/api/openai/v1/responses"])
        self.assertTrue(all(auth == "Bearer local-test-token" for _, auth, _ in received))
        result = json.loads((self.root / "checkpoint-results.json").read_text())
        self.assertEqual(result["revision"], "test-revision")
        self.assertEqual(result["stage_reached"], 2)
        self.assertTrue(result["success"])
        self.assertEqual((self.root / "checkpoint-results.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("local-test-token", json.dumps(result))

    def test_malformed_http_200_stream_fails_and_records_incomplete_stage(self):
        events = [None]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(events[0])}\n\n".encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            self.save()
            for event in (None, {"choices": [None]}, {"response": None}):
                with self.subTest(event=event):
                    events[0] = event
                    self.assertNotEqual(main(["--config", str(self.config_file), "--run"]), 0)
                    result = json.loads((self.root / "checkpoint-results.json").read_text())
                    self.assertFalse(result["success"])
                    self.assertEqual(result["campaign"]["stages"][0]["status"], "fail")
                    self.assertEqual(result["campaign"]["stages"][1]["status"], "not-run")
                    (self.root / "checkpoint-results.json").unlink()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def ramp_config(self):
        config = self.config.copy()
        config["plan"] = "model_ramp"
        config["matrix_cell"] = "opus_ramp"
        config["ramp_api"] = "chat"
        config["think_time_seconds"] = 0.5
        config["limits"] = {**self.config["limits"], "attempts": 30, "input_tokens": 3000,
                            "output_tokens": 300, "max_spend": 4}
        config["stages"] = [
            {"name": f"ramp-{sessions}", "desired_sessions": sessions, "min_dwell_seconds": 0.08,
             "spawn_rate": 1000, "required": True, "ramp": True}
            for sessions in range(1, 6)
        ]
        return config

    def test_ramp_requires_exact_plateaus_finite_global_caps_and_transition(self):
        self.config["plan"] = "model_ramp"
        self.config["matrix_cell"] = "opus_ramp"
        self.config["ramp_api"] = "chat"
        self.config["think_time_seconds"] = 0.5
        self.config["stages"] = [
            {"name": f"ramp-{sessions}", "desired_sessions": sessions,
             "min_dwell_seconds": dwell, "spawn_rate": 50, "required": True, "ramp": True}
            for sessions, dwell in [(10, 60), (60, 120), (100, 120), (150, 120), (200, 300)]
        ]
        self.config["limits"] = {**self.config["limits"], "attempts": 12000,
                                  "active_seconds": 840, "drain_seconds": 240}
        self.save()
        self.assertEqual(main(["--config", str(self.config_file), "--validate-only"]), 0)
        for field, value in [("attempts", 12001), ("active_seconds", 1441),
                             ("drain_seconds", 241)]:
            with self.subTest(field=field):
                self.config["limits"][field] = value
                self.save()
                with self.assertRaises(ConfigError):
                    load_config(self.config_file)
                self.config["limits"][field] = {"attempts": 12000, "active_seconds": 840,
                                                  "drain_seconds": 240}[field]
        self.config["stages"][4]["min_dwell_seconds"] = 0
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.config_file)
        self.config["stages"][4]["min_dwell_seconds"] = 300
        self.config["stages"][0]["spawn_rate"] = 0.01
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.config_file)

    def test_ramp_loopback_full_dwell_and_shared_attempt_bound(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for value in ({"choices": [{"delta": {"content": "1 2 3"}}]}, "[DONE]"):
                    data = json.dumps(value) if isinstance(value, dict) else value
                    self.wfile.write(f"data: {data}\n\n".encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = self.ramp_config()
            config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            code = model_ramp(config)
            self.assertEqual(code, 0, json.loads((self.root / "checkpoint-results.json").read_text())["campaign"])
            report = json.loads((self.root / "checkpoint-results.json").read_text())
            self.assertEqual(report["stage_reached"], 5)
            self.assertTrue(report["success"])
            self.assertEqual([stage["achieved_sessions"] for stage in report["campaign"]["stages"]],
                             [1, 2, 3, 4, 5])
            self.assertTrue(all(stage["actual_dwell_seconds"] >= stage["min_dwell_seconds"]
                                for stage in report["campaign"]["stages"]))
            self.assertEqual(len(received), report["campaign"]["physical_attempts"])
            self.assertEqual(report_campaign(config), 4)
            matrix = json.loads((self.root / "campaign-report.json").read_text())
            self.assertEqual(matrix["matrix"]["opus_ramp"], "blocked")
            self.assertEqual([stage["actual_sessions"] for stage in matrix["measured_cell"]["plateaus"]],
                             [1, 2, 3, 4, 5])
            self.assertNotIn("200 sessions achieved", (self.root / "sanitized-report.md").read_text())
            (self.root / "checkpoint-results.json").unlink()
            config["limits"]["attempts"] = 1
            self.assertNotEqual(model_ramp(config), 0)
            report = json.loads((self.root / "checkpoint-results.json").read_text())
            self.assertEqual(report["campaign"]["physical_attempts"], 1)
            self.assertEqual(report["campaign"]["stop_reason"], "attempt_budget")
            self.assertEqual([stage["status"] for stage in report["campaign"]["stages"]],
                             ["pass", "fail", "not-run", "not-run", "not-run"])
            self.assertFalse(report["success"])
            self.assertEqual(report_campaign(config), 3)
            self.assertEqual(json.loads((self.root / "campaign-report.json").read_text())
                             ["matrix"]["opus_ramp"], "fail")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_ramp_unreached_stage_cannot_pass_from_time_alone(self):
        clock = [0]
        config = self.ramp_config()
        guard = CampaignGuard(config["limits"], config["stages"], clock=lambda: clock[0])
        self.assertTrue(guard.start_stage("ramp-1", 0))
        clock[0] = 10
        self.assertFalse(guard.end_stage())
        self.assertEqual(guard.report()["stages"][0]["actual_dwell_seconds"], 0)
        self.assertEqual(guard.report()["stages"][1]["status"], "not-run")

    def test_global_permits_and_mandatory_dwell_are_honest(self):
        clock = [0]
        stages = [{**stage, "min_dwell_seconds": 10} for stage in self.config["stages"]]
        guard = CampaignGuard({**self.config["limits"], "active_seconds": 30}, stages, clock=lambda: clock[0])
        self.assertTrue(guard.start_stage("chat", 1))
        self.assertTrue(guard.admit(100, 10, 0.12))
        guard.record({"success": True, "complete": True, "ttft_ms": 5})
        clock[0] = 11
        self.assertTrue(guard.end_stage())
        self.assertTrue(guard.start_stage("responses", 1))
        self.assertTrue(guard.admit(100, 10, 0.12))
        guard.record({"success": True, "complete": True, "ttft_ms": 5})
        clock[0] = 15
        self.assertFalse(guard.end_stage())
        result = guard.report()
        self.assertEqual(result["physical_attempts"], 2)
        self.assertEqual(result["reserved_input_tokens"], 200)
        self.assertEqual([stage["status"] for stage in result["stages"]], ["pass", "fail"])
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["stop_reason"], "incomplete_stage")

    def test_shared_permits_count_concurrent_physical_attempts(self):
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        admitted = []
        threads = [threading.Thread(target=lambda: admitted.append(guard.admit(10, 1, 0.01)))
                   for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(admitted), 2)
        self.assertEqual(guard.report()["physical_attempts"], 2)
        self.assertEqual(guard.report()["stop_reason"], "attempt_budget")
        self.assertEqual(guard.report()["stages"][1]["status"], "not-run")

    def test_error_burst_oom_and_restart_signals_abort(self):
        limits = {**self.config["limits"], "attempts": 40, "input_tokens": 40,
                  "output_tokens": 40, "max_spend": 40}
        guard = CampaignGuard(limits, self.config["stages"])
        guard.start_stage("chat", 1)
        for number in range(20):
            self.assertTrue(guard.admit(1, 1, 1))
            guard.record({"success": number >= 5, "complete": number >= 5})
        self.assertEqual(guard.report()["stop_reason"], "error_burst")
        self.assertFalse(guard.admit(1, 1, 1))
        for signal in ("oom", "restart", "telemetry_missing"):
            with self.subTest(signal=signal):
                guard = CampaignGuard(limits, self.config["stages"])
                guard.start_stage("chat", 1)
                guard.signal(signal)
                self.assertFalse(guard.admit(1, 1, 1))
                self.assertEqual(guard.report()["stop_reason"], signal)

    def test_controller_endpoint_authorizes_shared_worker_permits(self):
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        server = serve_permits(guard, "test-secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(route, payload, secret="test-secret"):
            if route == "/permit":
                payload = {**payload, "attempt_id": uuid.uuid4().hex}
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}{route}",
                json.dumps(payload).encode(),
                {"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(request, timeout=2).read())

        try:
            with self.assertRaises(urllib.error.HTTPError):
                post("/permit", {"input_tokens": 1, "output_tokens": 1, "price": 0.01}, "wrong")
            outcomes = []
            workers = [threading.Thread(target=lambda: outcomes.append(post(
                "/permit", {"input_tokens": 1, "output_tokens": 1, "price": 0.01})))
                       for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(sum(result["admitted"] for result in outcomes), 2)
            self.assertEqual(guard.report()["physical_attempts"], 2)
            self.assertEqual(post("/signal", {"kind": "restart"}), {"stopped": True})
            self.assertFalse(post("/permit", {"input_tokens": 1, "output_tokens": 1, "price": 0.01})["admitted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_200_without_terminal_cannot_reach_other_api(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            self.save()
            self.assertEqual(main(["--config", str(self.config_file), "--run"]), 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        result = json.loads((self.root / "checkpoint-results.json").read_text())
        self.assertEqual(requests, ["/api/v1/chat/completions"])
        self.assertFalse(result["success"])
        self.assertEqual(result["stage_reached"], 0)
        self.assertEqual([stage["status"] for stage in result["campaign"]["stages"]],
                         ["fail", "not-run"])
        self.assertEqual(result["campaign"]["physical_attempts"], 1)


    def test_http_200_incomplete_responses_stops_after_chat(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if self.path.endswith("/responses"):
                    events = [{"type": "response.output_text.delta", "delta": "partial"},
                              {"type": "response.incomplete", "response": {"status": "incomplete"}}]
                else:
                    events = [{"choices": [{"delta": {"content": "complete"}}]}, "[DONE]"]
                for event in events:
                    item = json.dumps(event) if isinstance(event, dict) else event
                    self.wfile.write(f"data: {item}\n\n".encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            self.save()
            self.assertNotEqual(main(["--config", str(self.config_file), "--run"]), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        result = json.loads((self.root / "checkpoint-results.json").read_text())
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["stage_reached"], 1)
        self.assertEqual([row["success"] for row in result["rows"]], [True, False])
        self.assertEqual([stage["status"] for stage in result["campaign"]["stages"]],
                         ["pass", "fail"])

    def test_restart_or_oom_guard_denies_admission_before_dispatch(self):
        for signal in ("oom", "restart", "telemetry_missing"):
            with self.subTest(signal=signal):
                guard = CampaignGuard(self.config["limits"], self.config["stages"])
                guard.start_stage("chat", 1)
                server = serve_permits(guard, "test-secret")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    endpoint = f"http://127.0.0.1:{server.server_port}"
                    def post(path, payload):
                        if path == "/permit":
                            payload = {**payload, "attempt_id": uuid.uuid4().hex}
                        request = urllib.request.Request(
                            endpoint + path, json.dumps(payload).encode(),
                            {"Authorization": "Bearer test-secret", "Content-Type": "application/json"})
                        return json.loads(urllib.request.urlopen(request, timeout=2).read())
                    post("/signal", {"kind": signal})
                    decision = post("/permit", {"input_tokens": 1, "output_tokens": 1,
                                                "price": 0.01})
                    self.assertFalse(decision["admitted"])
                    self.assertEqual(guard.report()["physical_attempts"], 0)
                    self.assertEqual(guard.report()["stop_reason"], signal)
                    self.assertEqual(guard.report()["stages"][1]["status"], "not-run")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


    def test_worker_permits_reconcile_duplicate_outcomes_and_fail_closed(self):
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        server = serve_permits(guard, "test-secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = WorkerClient(f"http://127.0.0.1:{server.server_port}", "test-secret")
        try:
            attempt_id = worker.permit(100, 10, 0.12)
            self.assertIsNotNone(attempt_id)
            self.assertTrue(worker.dispatch(attempt_id))
            self.assertFalse(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": True, "complete": True}))
            repeated = worker._send("/outcome", {"attempt_id": attempt_id,
                                                   "success": True, "complete": True})
            self.assertTrue(repeated["recorded"])
            self.assertEqual(guard.report()["physical_attempts"], 1)
            self.assertEqual(guard.report()["completed"], 1)
            self.assertTrue(guard.end_stage())
            self.assertEqual(guard.report()["status"], "fail")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIsNone(worker.permit(1, 1, 0.01))
        self.assertTrue(worker.lost)

    def test_active_stream_abort_and_drain_are_bounded(self):
        import time
        self.config["limits"]["drain_seconds"] = 0.1
        self.config["limits"]["active_seconds"] = 5

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                time.sleep(0.3)
                try:
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n')
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["target"] = f"http://127.0.0.1:{server.server_port}/api"
            guard = CampaignGuard(self.config["limits"], self.config["stages"])
            timer = threading.Timer(0.02, lambda: guard.signal("restart"))
            timer.start()
            self.assertNotEqual(checkpoint(self.config, guard), 0)
            timer.join(timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        result = json.loads((self.root / "checkpoint-results.json").read_text())
        self.assertEqual(result["campaign"]["signals"], ["restart"])
        self.assertEqual(result["campaign"]["physical_attempts"], 1)
        self.assertEqual(result["campaign"]["outstanding"], 0)
        self.assertEqual(result["campaign"]["stages"][1]["status"], "not-run")
        self.assertLess(result["rows"][0]["duration_ms"], 280)


    def test_rolling_error_window_does_not_drop_cumulative_attempts(self):
        limits = {**self.config["limits"], "attempts": 60, "input_tokens": 60,
                  "output_tokens": 60, "max_spend": 60}
        guard = CampaignGuard(limits, self.config["stages"])
        guard.start_stage("chat", 1)
        for _ in range(55):
            self.assertTrue(guard.admit(1, 1, 1))
            guard.record({"success": True, "complete": True})
        self.assertEqual(guard.report()["physical_attempts"], 55)
        self.assertEqual(guard.report()["completed"], 55)
        self.assertEqual(guard.report()["outstanding"], 0)
        self.assertTrue(guard.end_stage())
        self.assertEqual(guard.report()["stages"][1]["status"], "not-run")


    def test_missing_report_evidence_is_blocked_and_private(self):
        self.save()
        self.assertEqual(main(["--config", str(self.config_file), "--report"]), 4)
        report = json.loads((self.root / "campaign-report.json").read_text())
        markdown = (self.root / "sanitized-report.md").read_text()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["acceptance"]["AC-04"], "blocked")
        self.assertEqual(report["matrix"]["opus_ramp"], "blocked")
        self.assertEqual(report["matrix"]["mixed_models"], "not-run")
        self.assertEqual([stage["status"] for stage in report["stages"]],
                         ["not-run", "not-run"])
        self.assertNotIn(self.config["target"], markdown)
        self.assertNotIn(self.token.read_text(), markdown)
        self.assertNotIn(self.config["identities"][0]["id"], markdown)
        self.assertNotIn("200 sessions achieved", markdown)
        for name in ("campaign-report.json", "sanitized-report.md"):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(main(["--config", str(self.config_file), "--report"]), 4)

    def test_incomplete_stage_and_observed_agent_are_not_capacity_proof(self):
        self.config["identities"].append({"id": "test-agent", "kind": "agent", "tenant": "test-tenant",
                                          "root_human": "test-owner", "ownership_receipt": "protected-agent",
                                          "token_file": str(self.token)})
        result = {
            "revision": self.config["revision"],
            "rows": [{"api": "chat", "complete": True, "success": True,
                      "identity_id": "test-developer", "identity_kind": "developer",
                      "duration_ms": 3, "ttft_ms": 1, "terminal_to_eof_ms": 1},
                     {"api": "responses", "complete": False, "success": False,
                      "identity_id": "test-agent", "identity_kind": "agent"}],
            "campaign": {"physical_attempts": 2, "outstanding": 0, "peak_inflight": 1,
                         "time_weighted_inflight": 0.3,
                         "stages": [{"name": "chat", "status": "pass", "achieved_sessions": 1,
                                     "actual_dwell_seconds": 0},
                                    {"name": "responses", "status": "not-run"}]},
        }
        self.save()
        (self.root / "checkpoint-results.json").write_text(json.dumps(result))
        (self.root / "checkpoint-results.json").chmod(0o600)
        self.assertEqual(main(["--config", str(self.config_file), "--report"]), 3)
        report = json.loads((self.root / "campaign-report.json").read_text())
        markdown = (self.root / "sanitized-report.md").read_text()
        self.assertEqual(report["observed_developers"], 1)
        self.assertEqual(report["observed_agents"], 1)
        self.assertEqual(report["configured_developers"], 1)
        self.assertEqual(report["configured_agents"], 1)
        self.assertEqual(report["peak_inflight"], 1)
        self.assertEqual(report["complete_rate"], 0.5)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["stages"][1]["status"], "not-run")
        self.assertNotIn("test-agent", markdown)
        self.assertNotIn("test-tenant", markdown)
        self.assertNotIn(self.config["revision"], markdown)

    def test_full_ramp_requires_every_plateau_and_declared_limits(self):
        ramp = {
            "protected_receipt": "private-ramp-receipt",
            "revision": self.config["revision"],
            "declared_limits": {"attempts": 12000, "active_seconds": 1440,
                                "drain_seconds": 240, "input_tokens": 100000,
                                "output_tokens": 100000, "max_spend": 100},
            "attempts": 100, "complete_streams": 99, "active_seconds": 900,
            "drain_seconds": 10, "input_tokens": 1000, "output_tokens": 1000,
            "spend": 10, "p95": {"ttft_ms": 10, "http_ms": 100,
                                 "terminal_ms": 10},
            "health": {"oom": False, "restarts": 0, "dependencies_ok": True},
            "plateaus": [{"configured_sessions": sessions, "actual_sessions": sessions,
                          "actual_dwell_seconds": dwell, "attempts": 100,
                          "complete_streams": 99}
                         for sessions, dwell in ((10, 60), (60, 120), (100, 120),
                                                 (150, 120), (200, 300))],
        }
        self.assertEqual(qualify_cell("opus_ramp", ramp, self.config), "pass")
        ramp["plateaus"][-1]["actual_dwell_seconds"] = 299
        self.assertEqual(qualify_cell("opus_ramp", ramp, self.config), "fail")
        ramp["plateaus"][-1]["actual_dwell_seconds"] = 300
        ramp["health"]["oom"] = True
        self.assertEqual(qualify_cell("opus_ramp", ramp, self.config), "fail")
        ramp["health"]["oom"] = False
        ramp["declared_limits"]["attempts"] = 12001
        self.assertEqual(qualify_cell("opus_ramp", ramp, self.config), "fail")
        ramp["declared_limits"]["attempts"] = 12000
        ramp["p95"]["ttft_ms"] = 1001
        self.assertEqual(qualify_cell("opus_ramp", ramp, self.config), "fail")
        ramp["p95"]["ttft_ms"] = 10
        self.assertEqual(summarize(self.config, {"rows": [], "matrix_results": {"opus_ramp": ramp}})
                         ["matrix"]["opus_ramp"], "pass")
        self.assertEqual(summarize(self.config, {"rows": [], "matrix_results": {"opus_ramp": ramp}})
                         ["acceptance"]["AC-04"], "blocked")


    def test_expired_permit_never_dispatches_or_replays_inference(self):
        wall_clock = [100]
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        server = serve_permits(guard, "test-secret", permit_ttl=1,
                               wall_clock=lambda: wall_clock[0])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = WorkerClient(f"http://127.0.0.1:{server.server_port}", "test-secret",
                              wall_clock=lambda: wall_clock[0])
        try:
            attempt_id = worker.permit(100, 10, 0.12)
            self.assertIsNotNone(attempt_id)
            wall_clock[0] = 102
            self.assertFalse(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": False, "complete": False}))
            denied = worker._send("/permit", {"attempt_id": attempt_id,
                                               "input_tokens": 100, "output_tokens": 10,
                                               "price": 0.12})
            self.assertFalse(denied["admitted"])
            self.assertFalse(worker._send("/dispatch", {"attempt_id": attempt_id})["authorized"])
            self.assertEqual(guard.report()["physical_attempts"], 1)
            self.assertEqual(guard.report()["completed"], 1)
            self.assertEqual(guard.report()["failures"], 1)
            self.assertFalse(guard.end_stage())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_health_abort_refuses_pending_dispatch(self):
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        server = serve_permits(guard, "test-secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = WorkerClient(f"http://127.0.0.1:{server.server_port}", "test-secret")
        try:
            attempt_id = worker.permit(100, 10, 0.12)
            guard.signal("oom")
            self.assertFalse(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": False, "complete": False}))
            self.assertEqual(guard.report()["stop_reason"], "oom")
            self.assertFalse(guard.end_stage())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_remote_workers_require_tls_and_verified_ca(self):
        import shutil
        import ssl
        import subprocess
        if not shutil.which("openssl"):
            self.skipTest("openssl not installed")
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        with self.assertRaisesRegex(ValueError, "TLS"):
            serve_permits(guard, "test-secret", host="127.0.0.2")
        certificate = self.root / "local-cert.pem"
        private_key = self.root / "local-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
                        "-subj", "/CN=loopback.example.test", "-addext",
                        "subjectAltName=IP:127.0.0.2"], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, private_key)
        server = serve_permits(guard, "test-secret", host="127.0.0.2", tls_context=context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = WorkerClient(f"https://127.0.0.2:{server.server_port}", "test-secret",
                              ca_file=str(certificate))
        try:
            untrusted = WorkerClient(f"https://127.0.0.2:{server.server_port}", "test-secret")
            self.assertIsNone(untrusted.permit(100, 10, 0.12))
            self.assertTrue(untrusted.lost)
            self.assertEqual(guard.report()["physical_attempts"], 0)
            attempt_id = worker.permit(100, 10, 0.12)
            self.assertIsNotNone(attempt_id)
            self.assertTrue(worker.dispatch(attempt_id))
            self.assertFalse(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": True, "complete": True}))
            self.assertEqual(guard.report()["physical_attempts"], 1)
            self.assertEqual(guard.report()["completed"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_server_rejects_expired_permit_despite_stale_worker_clock(self):
        server_clock = [100]
        worker_clock = [100]
        guard = CampaignGuard(self.config["limits"], self.config["stages"])
        guard.start_stage("chat", 1)
        server = serve_permits(guard, "test-secret", permit_ttl=1,
                               wall_clock=lambda: server_clock[0])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = WorkerClient(f"http://127.0.0.1:{server.server_port}", "test-secret",
                              wall_clock=lambda: worker_clock[0])
        try:
            attempt_id = worker.permit(100, 10, 0.12)
            self.assertIsNotNone(attempt_id)
            server_clock[0] = 102
            self.assertFalse(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": False, "complete": False}))
            self.assertEqual(guard.report()["physical_attempts"], 1)
            self.assertEqual(guard.report()["failures"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)



if __name__ == "__main__":
    unittest.main()
