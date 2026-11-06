import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from campaign_controller import ConfigError, load_config, main, model_ramp, verify_capacity_receipt


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.context = self.root / "context"
        self.context.write_text("cache " * 1100)
        self.context.chmod(0o600)
        self.received = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = payload.get("input") or payload["messages"][0]["content"]
                received.append((self.headers.get("Authorization"), prompt))
                hits = sum(previous_prompt == prompt for _, previous_prompt in received)
                cached_tokens = 4 if hits > 1 and len(prompt) > 1000 else 0
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if self.path.endswith("/responses"):
                    events = ({"type": "response.output_text.delta", "delta": "1 2 3"},
                              {"type": "response.completed", "response": {"status": "completed",
                               "usage": {"input_tokens_details": {"cached_tokens": cached_tokens}}}})
                else:
                    events = ({"choices": [{"delta": {"content": "1 2 3"}}],
                               "usage": {"prompt_tokens_details": {"cached_tokens": cached_tokens}}}, "[DONE]")
                for event in events:
                    data = json.dumps(event) if isinstance(event, dict) else event
                    self.wfile.write(f"data: {data}\n\n".encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def identity(self, name, kind="developer", tenant="tenant-one", root="root-one"):
        token = self.root / f"token-{name}"
        token.write_text(f"test-token-{name}")
        token.chmod(0o600)
        return {"id": name, "kind": kind, "tenant": tenant, "root_human": root,
                "ownership_receipt": "local-fixture", "token_file": str(token)}

    def config(self, cell, identities):
        evidence = self.root / f"evidence-{cell}"
        evidence.mkdir(mode=0o700, exist_ok=True)
        sessions = len(identities)
        return {
            "schema_version": 1, "plan": "comparison", "comparison_cell": cell,
            "scenario": "fictional-comparison", "seed": 7, "revision": "test-revision",
            "approved_account": "fictional-account", "approved_role": "fictional-role",
            "recovery_owner": "fictional-recovery-owner", "local_test": True,
            "qualified_capacity_receipt": "local-test-only", "safe_inflight_sessions": sessions,
            "target": f"http://127.0.0.1:{self.server.server_port}/api",
            "evidence_dir": str(evidence), "identities": identities, "ramp_api": "chat",
            "context_file": str(self.context), "think_time_seconds": 0.025,
            "models": [{"api": api, "model": "test-model", "sdk_version": "test-sdk",
                        "max_input_tokens": 10000, "max_output_tokens": 10,
                        "price_per_input_token": 0.000001, "price_per_output_token": 0.000001}
                       for api in ("chat", "responses")],
            "limits": {"attempts": 60, "active_seconds": 3, "drain_seconds": 1,
                       "input_tokens": 600000, "output_tokens": 600, "max_spend": 1,
                       "setup_seconds": 10, "cleanup_seconds": 10},
            "slos": {"ttft_ms": 1000, "http_ms": 2000, "terminal_ms": 1000},
            "stages": [{"name": cell, "required": True, "ramp": True,
                        "desired_sessions": sessions, "min_dwell_seconds": 0.25, "spawn_rate": 100}],
        }

    def validate(self, config):
        source = self.root / "private-config.json"
        source.write_text(json.dumps(config))
        source.chmod(0o600)
        return load_config(source)

    def result(self, config):
        return json.loads((Path(config["evidence_dir"]) / "checkpoint-results.json").read_text())

    def test_owned_identity_and_agent_fanout_cells_use_real_requests(self):
        developer = self.identity("dev-one")
        next_developer = self.identity("dev-two", root="root-two")
        other_tenant = self.identity("dev-three", tenant="tenant-two", root="root-three")
        agent_one = self.identity("agent-one", "agent", root="shared-root")
        agent_two = self.identity("agent-two", "agent", root="shared-root")
        profiles = {
            "shared_identity": [developer], "developer_only": [developer, next_developer],
            "distinct_developers": [developer, next_developer],
            "distinct_tenants": [developer, other_tenant],
            "distinct_agents": [agent_one, agent_two], "fan_out": [agent_one, agent_two],
        }
        for cell, identities in profiles.items():
            with self.subTest(cell=cell):
                config = self.validate(self.config(cell, identities))
                self.assertEqual(model_ramp(config), 0, self.result(config)["campaign"] if
                                 (Path(config["evidence_dir"]) / "checkpoint-results.json").exists() else "no report")
                result = self.result(config)
                observations = result["comparison"]
                self.assertTrue(result["success"])
                self.assertEqual(observations["observed_identity_ids"], sorted(identity["id"] for identity in identities))
                self.assertEqual(observations["live_sessions"], len(identities))
                self.assertLessEqual(observations["peak_inflight"], config["safe_inflight_sessions"])
                self.assertEqual(observations["physical_attempts"], result["campaign"]["physical_attempts"])
                self.assertLessEqual(observations["physical_attempts"], config["limits"]["attempts"])
                if cell == "distinct_tenants":
                    self.assertEqual(observations["observed_tenants"], 2)
                    self.assertEqual(observations["observed_human_roots"], 2)
                if cell == "distinct_developers":
                    self.assertEqual(observations["observed_developers"], 2)
                    self.assertEqual(observations["observed_tenants"], 1)
                if cell == "fan_out":
                    self.assertEqual(set(observations["fan_out_distribution"]["shared-root"]),
                                     {"agent-one", "agent-two"})
        self.assertTrue(all(auth.startswith("Bearer test-token-") for auth, _ in self.received))

    def test_cached_and_unique_contexts_measure_provider_usage_not_prompt_proxies(self):
        owner = self.identity("cache-owner")
        for cell in ("cached_context", "unique_context"):
            with self.subTest(cell=cell):
                config = self.validate(self.config(cell, [owner]))
                self.assertEqual(model_ramp(config), 0, self.result(config)["campaign"] if
                                 (Path(config["evidence_dir"]) / "checkpoint-results.json").exists() else "no report")
                metrics = self.result(config)["comparison"]
                self.assertGreaterEqual(metrics["provider_cache_samples"], 2)
                self.assertEqual(metrics["prompt_variants"] == 1, cell == "cached_context")
                if cell == "cached_context":
                    self.assertGreater(metrics["provider_cached_input_tokens"], 0)
                else:
                    self.assertGreater(metrics["prompt_variants"], 1)
                self.assertNotIn("cache cache", json.dumps(self.result(config)))

    def test_responses_cache_counters_are_observed_only_when_returned(self):
        config = self.config("cached_context", [self.identity("response-owner")])
        config["ramp_api"] = "responses"
        config = self.validate(config)
        self.assertEqual(model_ramp(config), 0)
        metrics = self.result(config)["comparison"]
        self.assertGreaterEqual(metrics["provider_cache_samples"], 2)
        self.assertGreater(metrics["provider_cached_input_tokens"], 0)
        self.assertTrue(all(auth == "Bearer test-token-response-owner" for auth, _ in self.received))

    def test_missing_provider_counters_and_exhausted_budget_fail_closed(self):
        owner = self.identity("cache-owner")
        config = self.validate(self.config("cached_context", [owner]))
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.received.clear()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices": [{"delta": {"content": "1"}}]}\n\ndata: [DONE]\n\n')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        config["target"] = f"http://127.0.0.1:{self.server.server_port}/api"
        self.assertNotEqual(model_ramp(config), 0)
        self.assertEqual(self.result(config)["campaign"]["stop_reason"], "telemetry_missing")
        self.assertEqual(self.result(config)["campaign"]["stages"][0]["status"], "fail")
        budget = self.validate(self.config("shared_identity", [owner]))
        budget["limits"]["attempts"] = 1
        self.assertNotEqual(model_ramp(budget), 0)
        self.assertEqual(self.result(budget)["campaign"]["stop_reason"], "attempt_budget")
        self.assertFalse(self.result(budget)["success"])

    def test_worker_to_matrix_report_rejects_unverified_and_forged_cells(self):
        config = self.config("distinct_developers", [
            self.identity("first", root="root-first"),
            self.identity("second", root="root-second")])
        source = self.root / "private-config.json"
        source.write_text(json.dumps(config))
        source.chmod(0o600)
        self.assertEqual(main(["--config", str(source), "--run"]), 0)
        result = self.result(config)
        directory = Path(config["evidence_dir"])
        self.assertEqual(main(["--config", str(source), "--report"]), 4)
        report = json.loads((directory / "campaign-report.json").read_text())
        self.assertEqual(report["matrix"]["distinct_developers"], "blocked")
        self.assertEqual(report["measured_cell"]["observed_developers"], 2)
        self.assertEqual(report["measured_cell"]["attempts"], result["campaign"]["physical_attempts"])
        self.assertEqual(report["measured_cell"]["plateaus"][0]["actual_sessions"], 2)
        markdown = (directory / "sanitized-report.md").read_text()
        self.assertIn("successful developer/agent identities 2/0", markdown)
        for private in ("root-first", "root-second", "first", "second", config["target"]):
            self.assertNotIn(private, markdown)
        attestation = {"protected_receipt": "fictional-evidence", "operator_verified": True,
                       "revision": config["revision"], "target": config["target"],
                       "config_hash": result["config_hash"],
                       "health": {"oom": False, "restarts": 0, "dependencies_ok": True}}
        evidence_file = directory / "matrix-evidence.json"

        def save_evidence(entry):
            evidence_file.write_text(json.dumps({"distinct_developers": entry,
                                                 "opus_ramp": {"protected_receipt": "fictional-forgery"}}))
            evidence_file.chmod(0o600)

        save_evidence(attestation)
        self.assertEqual(main(["--config", str(source), "--report"]), 4)
        report = json.loads((directory / "campaign-report.json").read_text())
        self.assertEqual(report["matrix"]["distinct_developers"], "blocked")
        self.assertEqual(report["matrix"]["opus_ramp"], "blocked")
        save_evidence({**attestation, "attempts": 200})
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["distinct_developers"], "fail")
        save_evidence({**attestation, "health": {"oom": True, "restarts": 0,
                                                 "dependencies_ok": True}})
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        save_evidence(attestation)
        original_result = json.loads(json.dumps(result))
        first_index = result["rows"][0]["session_index"]
        result["rows"] = [{**row, "session_index": first_index} for row in result["rows"]]
        (directory / "checkpoint-results.json").write_text(json.dumps(result))
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["distinct_developers"], "fail")
        result = json.loads(json.dumps(original_result))
        result["rows"][0]["duration_ms"] = 100000
        (directory / "checkpoint-results.json").write_text(json.dumps(result))
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["distinct_developers"], "fail")
        result = json.loads(json.dumps(original_result))
        result["rows"].pop()
        (directory / "checkpoint-results.json").write_text(json.dumps(result))
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["distinct_developers"], "fail")
        self.assertEqual((directory / "campaign-report.json").stat().st_mode & 0o777, 0o600)

    def test_worker_report_rejects_changed_config_and_lost_worker_accounting(self):
        config = self.config("fan_out", [self.identity("first-agent", "agent"),
                                          self.identity("second-agent", "agent")])
        source = self.root / "private-config.json"
        source.write_text(json.dumps(config))
        source.chmod(0o600)
        self.assertEqual(main(["--config", str(source), "--run"]), 0)
        directory = Path(config["evidence_dir"])
        config["limits"]["attempts"] -= 1
        source.write_text(json.dumps(config))
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["fan_out"], "fail")
        config["limits"]["attempts"] += 1
        source.write_text(json.dumps(config))
        result = self.result(config)
        result["campaign"]["outstanding"] = 1
        (directory / "checkpoint-results.json").write_text(json.dumps(result))
        self.assertEqual(main(["--config", str(source), "--report"]), 3)
        self.assertEqual(json.loads((directory / "campaign-report.json").read_text())
                         ["matrix"]["fan_out"], "fail")
        self.assertFalse(json.loads((directory / "campaign-report.json").read_text())
                         ["measured_cell_status"] == "pass")

    def test_capacity_receipt_requires_exact_safe_revision_and_health(self):
        config = self.config("shared_identity", [self.identity("developer")])
        config["local_test"] = False
        receipt_file = self.root / "capacity-receipt.json"
        receipt = {"revision": config["revision"], "target": config["target"],
                   "approved_account": config["approved_account"], "approved_role": config["approved_role"],
                   "status": "pass", "protected_receipt": "fictional-protected-reference",
                   "safe_inflight_sessions": 2,
                   "health": {"oom": False, "restarts": 0, "dependencies_ok": True}}

        def save():
            receipt_file.write_text(json.dumps(receipt))
            receipt_file.chmod(0o600)

        save()
        verify_capacity_receipt(config, str(receipt_file), 2)
        for field, bad in (("revision", "stale"), ("target", "https://other.example.com"),
                           ("safe_inflight_sessions", 1), ("status", "not-run"),
                           ("protected_receipt", ""), ("health", {"oom": True})):
            with self.subTest(field=field):
                original = receipt[field]
                receipt[field] = bad
                save()
                with self.assertRaises(ConfigError):
                    verify_capacity_receipt(config, str(receipt_file), 2)
                receipt[field] = original
        receipt_file.chmod(0o644)
        with self.assertRaises(ConfigError):
            verify_capacity_receipt(config, str(receipt_file), 2)
        self.assertFalse(self.received)

    def test_invalid_identity_relations_capacity_and_context_rejected_before_run(self):
        developer = self.identity("dev-one")
        second = self.identity("dev-two", root="root-two")
        third = self.identity("dev-three", tenant="tenant-two", root="root-three")
        agent = self.identity("agent-one", "agent")
        invalid = [
            ("distinct_developers", [developer, third], {}),
            ("distinct_tenants", [developer, second], {}),
            ("fan_out", [developer, agent], {}),
            ("cached_context", [developer], {"context_file": str(self.root / "missing")}),
            ("shared_identity", [developer], {"safe_inflight_sessions": 201}),
            ("shared_identity", [developer], {"comparison_cell": []}),
            ("shared_identity", [developer], {"qualified_capacity_receipt": ""}),
            ("shared_identity", [developer], {"limits": {"attempts": 1201}}),
        ]
        for cell, identities, overrides in invalid:
            with self.subTest(cell=cell, overrides=overrides):
                config = self.config(cell, identities)
                if "limits" in overrides:
                    config["limits"].update(overrides["limits"])
                else:
                    config.update(overrides)
                with self.assertRaises(ConfigError):
                    self.validate(config)
        self.assertFalse(self.received)


if __name__ == "__main__":
    unittest.main()
