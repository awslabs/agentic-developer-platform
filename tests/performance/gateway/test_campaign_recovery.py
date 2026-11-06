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

from campaign_recovery import RecoveryError, RecoveryJournal
from campaign_guard import CampaignGuard, WorkerClient, running_permits


class FixtureBackend:
    def __init__(self):
        self.resources = {
            ("quota", "owned:quota"): {"value": 10, "version": 1, "owner": "campaign-owner"},
            ("identity", "owned:user"): {"value": None, "version": 1, "owner": "campaign-owner"},
            ("secret", "owned:cache"): {"value": None, "version": 1, "owner": "campaign-owner"},
            ("quota", "unowned:quota"): {"value": 5, "version": 1, "owner": "other-owner"},
        }
        self.healthy = True
        self.token = "never-journal-this-plaintext-token"
        self.journal = None
        self.lost_reply = False
        self.restores = []
        self.restore_receipts = {}
        self.lost_restore_reply = False

    def read(self, kind, binding):
        return dict(self.resources[kind, binding])

    def owns(self, kind, binding, owner, receipt):
        return receipt == "protected-ownership-reference" and self.read(kind, binding)["owner"] == owner

    def compare_write(self, kind, binding, version, desired, owner, intent_id):
        assert self.journal is not None
        intent = json.loads(self.journal.read_text())["intents"][-1]
        assert intent["state"] == "intent"
        assert (intent["kind"], intent["binding"], intent["before_version"]) == (kind, binding, version)
        current = self.resources[kind, binding]
        if current["version"] != version or current["owner"] != owner:
            raise RecoveryError("fixture CAS conflict")
        current.update(value=desired, version=version + 1)
        if self.lost_reply:
            raise TimeoutError("write completed but reply was lost")

    def compare_restore(self, kind, binding, version, before, owner, intent_id):
        current = self.resources[kind, binding]
        if current["version"] != version or current["owner"] != owner:
            raise RecoveryError("restore CAS conflict")
        current.update(value=before, version=version + 1)
        self.restore_receipts[intent_id] = (kind, binding, version + 1, before, owner)
        self.restores.append((kind, binding))
        if self.lost_restore_reply:
            raise TimeoutError("restore completed but reply was lost")

    def verify_restore_receipt(self, intent_id, kind, binding, version, before, owner):
        return self.restore_receipts.get(intent_id) == (kind, binding, version, before, owner)

    def verify_restored(self, kind, binding, before):
        return self.resources[kind, binding]["value"] == before

    def verify_baseline(self):
        return self.healthy


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.clock = [100]
        self.journal = RecoveryJournal(self.root, "test-campaign", "campaign-owner",
                                       clock=lambda: self.clock[0])
        self.backend = FixtureBackend()
        self.backend.journal = self.journal.path

    def mutate(self, kind, binding, desired):
        return self.journal.mutate(self.backend, kind, binding, desired,
                                   "protected-ownership-reference")

    def test_intent_survives_lost_mutation_reply_and_restores_exact_quota(self):
        self.backend.lost_reply = True
        with self.assertRaises(TimeoutError):
            self.mutate("quota", "owned:quota", 20)
        record = json.loads(self.journal.path.read_text())
        self.assertEqual(record["intents"][0]["state"], "intent")
        self.assertEqual(record["intents"][0]["before"], 10)
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.backend.token, self.journal.path.read_text())
        self.clock[0] = 131
        independent = RecoveryJournal(self.root, "test-campaign", "campaign-owner",
                                       clock=lambda: self.clock[0])
        receipt = independent.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "clean")
        self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 10)
        self.assertEqual(self.backend.read("quota", "unowned:quota")["value"], 5)
        self.assertEqual(independent.recover(self.backend, cleanup_seconds=10), receipt)
        with self.assertRaisesRegex(RecoveryError, "lease"):
            self.journal.heartbeat()

    def test_preserves_operator_change_and_reclaims_other_owned_resources(self):
        self.mutate("quota", "owned:quota", 20)
        self.mutate("identity", "owned:user", {"enabled": True})
        self.mutate("secret", "owned:cache", {"reference": "owned-secret-reference"})
        self.backend.resources["quota", "owned:quota"].update(value=30, version=3)
        self.clock[0] = 131
        receipt = self.journal.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "hold")
        self.assertEqual(receipt["restored"], 2)
        self.assertEqual(receipt["holds"][0]["binding"], "owned:quota")
        self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 30)
        self.assertEqual(self.backend.read("identity", "owned:user")["value"], None)
        self.assertEqual(self.backend.read("secret", "owned:cache")["value"], None)
        self.assertEqual(self.backend.read("quota", "unowned:quota")["value"], 5)
        self.assertEqual(set(self.backend.restores), {("identity", "owned:user"),
                                                       ("secret", "owned:cache")})

    def test_unowned_fixture_refused_before_mutation_and_baseline_checked(self):
        with self.assertRaisesRegex(RecoveryError, "ownership"):
            self.mutate("quota", "unowned:quota", 10)
        self.assertFalse(json.loads(self.journal.path.read_text())["intents"])
        self.backend.healthy = False
        with self.assertRaisesRegex(RecoveryError, "baseline"):
            self.mutate("quota", "owned:quota", 20)
        self.assertFalse(json.loads(self.journal.path.read_text())["intents"])

    def test_secret_references_only_and_failed_health_retains_hold_and_accounting(self):
        with self.assertRaisesRegex(RecoveryError, "references only"):
            self.mutate("secret", "owned:cache", self.backend.token)
        self.assertFalse(json.loads(self.journal.path.read_text())["intents"])
        accounting = self.root / "immutable-accounting.json"
        accounting.write_text('{"unknown_usage": true}')
        self.mutate("secret", "owned:cache", {"reference": "owned-secret-reference"})
        self.backend.healthy = False
        self.clock[0] = 131
        receipt = self.journal.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "hold")
        self.assertEqual(receipt["holds"], [{"reason": "baseline_health_unverified"}])
        self.assertEqual(self.backend.read("secret", "owned:cache")["value"], None)
        self.assertEqual(accounting.read_text(), '{"unknown_usage": true}')
        self.assertTrue(self.journal.path.exists())

    def test_bounded_idempotent_restore_retries(self):
        self.mutate("quota", "owned:quota", 20)
        attempts = [0]
        restore = self.backend.compare_restore

        def transient_restore(*args):
            attempts[0] += 1
            if attempts[0] < 3:
                raise TimeoutError("ambiguous transport before restore")
            return restore(*args)

        self.backend.compare_restore = transient_restore
        self.clock[0] = 131
        receipt = self.journal.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "clean")
        self.assertEqual(attempts[0], 3)
        self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 10)

    def test_lost_restore_reply_reconciles_trusted_receipt_once(self):
        self.mutate("quota", "owned:quota", 20)
        self.backend.lost_restore_reply = True
        self.clock[0] = 131
        receipt = self.journal.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "clean")
        self.assertEqual(self.backend.restores, [("quota", "owned:quota")])
        self.assertEqual(self.backend.read("quota", "owned:quota")["version"], 3)

    def test_same_value_after_unrelated_edit_requires_restore_receipt(self):
        self.mutate("quota", "owned:quota", 20)
        self.backend.resources["quota", "owned:quota"].update(value=10, version=3)
        self.clock[0] = 131
        receipt = self.journal.recover(self.backend, cleanup_seconds=10)
        self.assertEqual(receipt["status"], "hold")
        self.assertEqual(receipt["holds"][0]["reason"], "fixture changed after campaign mutation")
        self.assertEqual(self.backend.restores, [])

    def start_adapter(self):
        backend = self.backend
        bindings = {"target": "synthetic-gateway", "revision": "synthetic-revision",
                    "account": "fictional-account", "role": "fictional-role"}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer local-fixture-token":
                    self.send_error(403)
                    return
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                action = self.path.rsplit("/", 1)[-1]
                result = dict(bindings)
                if action == "read":
                    result["resource"] = {"kind": data["kind"], "binding": data["binding"],
                                          **backend.read(data["kind"], data["binding"])}
                elif action == "owns":
                    result["owned"] = backend.owns(data["kind"], data["binding"],
                                                    data["owner"], data["receipt"])
                elif action == "write":
                    backend.compare_write(data["kind"], data["binding"], data["version"],
                                          data["desired"], data["owner"], data["intent_id"])
                elif action == "restore":
                    backend.compare_restore(data["kind"], data["binding"], data["version"],
                                            data["before"], data["owner"], data["intent_id"])
                elif action == "receipt":
                    result["restored"] = backend.verify_restore_receipt(
                        data["intent_id"], data["kind"], data["binding"],
                        data["version"], data["before"], data["owner"])
                elif action == "verify":
                    result["restored"] = backend.verify_restored(
                        data["kind"], data["binding"], data["before"])
                elif action == "baseline":
                    result.update(healthy=backend.healthy, no_owned_active_clients=True,
                                  normal_user_denials_ok=True)
                else:
                    self.send_error(404)
                    return
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token_file = self.root / "adapter-token"
        token_file.write_text("local-fixture-token")
        token_file.chmod(0o600)
        config = {"schema_version": 1, "campaign": "child-campaign", "owner": "campaign-owner",
                  "journal_dir": str(self.root), "target": bindings["target"],
                  "revision": bindings["revision"], "approved_account": bindings["account"],
                  "approved_role": bindings["role"], "adapter_token_file": str(token_file),
                  "adapter_url": f"http://127.0.0.1:{server.server_port}",
                  "cleanup_seconds": 5, "local_test": True}
        config_file = self.root / "recovery-config.json"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        return server, thread, config_file

    def test_killed_runner_independent_cli_restores_and_keeps_unknown_usage(self):
        server, thread, config_file = self.start_adapter()
        self.backend.journal = self.root / "child-campaign.journal.json"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
        setup = """from campaign_recovery import RecoveryJournal, FixtureHTTPBackend
import json, os, sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
backend = FixtureHTTPBackend(config)
journal = RecoveryJournal(config["journal_dir"], config["campaign"], config["owner"], lease_seconds=1)
journal.mutate(backend, "quota", "owned:quota", 20, "protected-ownership-reference")
journal.mutate(backend, "identity", "owned:user", {"enabled": True}, "protected-ownership-reference")
journal.mutate(backend, "secret", "owned:cache", {"reference": "owned-secret-reference"}, "protected-ownership-reference")
journal.record_attempt("inflight-inference", 64, 128, .01)
os._exit(23)
"""
        command = [sys.executable, str(Path(__file__).with_name("campaign_controller.py")),
                   "--config", str(config_file), "--resume-cleanup"]
        try:
            worker = subprocess.run([sys.executable, "-c", setup, str(config_file)],
                                    env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(worker.returncode, 23, worker.stderr)
            self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 20)
            time.sleep(1.1)
            cleanup = subprocess.run(command, env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(cleanup.returncode, 5, cleanup.stderr)
            self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 10)
            self.assertEqual(self.backend.read("identity", "owned:user")["value"], None)
            self.assertEqual(self.backend.read("secret", "owned:cache")["value"], None)
            self.assertEqual(self.backend.read("quota", "unowned:quota")["value"], 5)
            record = json.loads(self.backend.journal.read_text())
            self.assertEqual(record["receipt"]["status"], "clean")
            self.assertEqual(record["receipt"]["accounting"], "unknown")
            self.assertEqual(record["attempts"][0]["status"], "unknown")
            self.assertNotIn(self.backend.token, self.backend.journal.read_text())
            self.assertEqual(subprocess.run(command, env=env, capture_output=True,
                                            text=True, timeout=8).returncode, 5)
            self.assertEqual(len(self.backend.restores), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_killed_partial_setup_is_cleaned_without_unknown_usage(self):
        server, thread, config_file = self.start_adapter()
        self.backend.journal = self.root / "child-campaign.journal.json"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
        setup = """from campaign_recovery import RecoveryJournal, FixtureHTTPBackend
import json, os, sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
backend = FixtureHTTPBackend(config)
journal = RecoveryJournal(config["journal_dir"], config["campaign"], config["owner"], lease_seconds=1)
journal.mutate(backend, "quota", "owned:quota", 20, "protected-ownership-reference")
os._exit(24)
"""
        command = [sys.executable, str(Path(__file__).with_name("campaign_controller.py")),
                   "--config", str(config_file), "--resume-cleanup"]
        try:
            worker = subprocess.run([sys.executable, "-c", setup, str(config_file)],
                                    env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(worker.returncode, 24, worker.stderr)
            time.sleep(1.1)
            cleaned = subprocess.run(command, env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
            receipt = json.loads(self.backend.journal.read_text())["receipt"]
            self.assertEqual(receipt["accounting"], "observed")
            self.assertEqual(receipt["status"], "clean")
            self.assertEqual(self.backend.read("quota", "owned:quota")["value"], 10)
            self.assertEqual(self.backend.read("quota", "unowned:quota")["value"], 5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_active_campaign_renews_lease_during_idle_dwell(self):
        journal = RecoveryJournal(self.root, "dwell-campaign", "campaign-owner", lease_seconds=0.15)
        stages = [{"name": "chat", "required": True, "desired_sessions": 1,
                   "min_dwell_seconds": 0, "spawn_rate": 1}]
        guard = CampaignGuard({"attempts": 2, "active_seconds": 30,
                              "input_tokens": 20, "output_tokens": 20, "max_spend": 1}, stages)
        guard.start_stage("chat", 1)
        with running_permits(guard, journal) as worker:
            time.sleep(0.4)
            attempt_id = worker.permit(10, 10, 0.1)
            self.assertIsNotNone(attempt_id)
            self.assertTrue(worker.dispatch(attempt_id))
            self.assertTrue(worker.finish(attempt_id, {"success": True, "complete": True}))
        self.assertIsNone(guard.report()["stop_reason"])

    def test_worker_permit_is_durable_before_inference_and_fails_after_takeover(self):
        stages = [{"name": "chat", "required": True, "desired_sessions": 1,
                   "min_dwell_seconds": 0, "spawn_rate": 1}]
        guard = CampaignGuard({"attempts": 2, "active_seconds": 60,
                              "input_tokens": 20, "output_tokens": 20, "max_spend": 1}, stages)
        guard.start_stage("chat", 1)
        with running_permits(guard, self.journal) as worker:
            attempt_id = worker.permit(10, 10, 0.1)
            self.assertIsNotNone(attempt_id)
            record = json.loads(self.journal.path.read_text())
            self.assertEqual(record["attempts"], [{"id": attempt_id, "status": "pending",
                                                   "input_tokens": 10, "output_tokens": 10,
                                                   "reserved_spend": 0.1}])
            self.clock[0] = 131
            independent = RecoveryJournal(self.root, "test-campaign", "campaign-owner",
                                           clock=lambda: self.clock[0])
            receipt = independent.recover(self.backend, cleanup_seconds=10)
            self.assertEqual(receipt["accounting"], "unknown")
            self.assertIsNone(worker.permit(1, 1, 0.01))
            self.assertEqual(guard.report()["stop_reason"], "lease_lost")

    def test_active_lease_blocks_independent_janitor_until_expiry(self):
        independent = RecoveryJournal(self.root, "test-campaign", "campaign-owner",
                                       clock=lambda: self.clock[0])
        with self.assertRaisesRegex(RecoveryError, "not expired"):
            independent.recover(self.backend, cleanup_seconds=10)
        self.clock[0] = 131
        self.assertEqual(independent.recover(self.backend, cleanup_seconds=10)["status"], "clean")
        with self.assertRaisesRegex(RecoveryError, "lease"):
            self.mutate("quota", "owned:quota", 20)


if __name__ == "__main__":
    unittest.main()
