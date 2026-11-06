"""Offline contract oracles, not tests of deployed authorization handlers."""

import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from unittest.mock import Mock


class SecurityContractFixtures(unittest.TestCase):
    def test_replayed_messages_keep_one_paid_attempt(self):
        provider = Mock(return_value="native-job-1")
        stored = {"run_id": "run-a", "request_hash": "pinned-input-hash",
                  "attempt_id": None, "state": "queued", "native_job_id": None}

        def dispatch(work_id, request_hash):
            if work_id != stored["run_id"] or request_hash != stored["request_hash"]:
                return "conflict"
            if stored["attempt_id"] is not None:
                return stored["state"]
            stored["attempt_id"] = "attempt-a"
            stored["state"] = "submitting"
            stored["native_job_id"] = provider(stored["run_id"], stored["request_hash"])
            stored["state"] = "outcome_unknown"
            return stored["state"]

        self.assertEqual(dispatch("run-a", "pinned-input-hash"), "outcome_unknown")
        first_readback = stored.copy()
        self.assertEqual(dispatch("run-a", "pinned-input-hash"), "outcome_unknown")
        self.assertEqual(dispatch("run-a", "changed-input-hash"), "conflict")
        self.assertEqual(stored, first_readback)
        provider.assert_called_once_with("run-a", "pinned-input-hash")
        self.assertEqual(stored["attempt_id"], "attempt-a")
        self.assertEqual(stored["native_job_id"], "native-job-1")

    def test_publication_retry_does_not_repeat_assessment(self):
        scenarios = {entry["id"]: entry for entry in json.loads(
            (Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/scenarios.json").read_text())}
        expected = scenarios["publication-recovery"]
        self.assertEqual(expected["fault"], "consumer_timeout")
        provider = Mock(return_value={"result_id": "result-a", "content_hash": "pinned-result"})
        consumer = Mock(side_effect=[TimeoutError("receipt lost"), {"receipt_id": "receipt-a"}])
        result = provider("run-a", "pinned-input-hash")
        run = {"run_id": "run-a", "state": "succeeded", "result": result.copy()}
        publication = {"run_id": run["run_id"], "state": "pending",
                       "contribution_hash": result["content_hash"], "receipt_id": None}

        def publish():
            publication["state"] = "publishing"
            try:
                receipt = consumer(publication["run_id"], publication["contribution_hash"])
            except TimeoutError:
                publication["state"] = "retry_wait"
            else:
                publication["receipt_id"] = receipt["receipt_id"]
                publication["state"] = "published"
            return publication["state"]

        self.assertEqual(publish(), "retry_wait")
        self.assertEqual(run, {"run_id": "run-a", "state": "succeeded", "result": result})
        self.assertIsNone(publication["receipt_id"])
        self.assertEqual(publish(), "published")
        self.assertEqual(publication["receipt_id"], "receipt-a")
        self.assertEqual(run["result"], result)
        provider.assert_called_once_with("run-a", "pinned-input-hash")
        self.assertEqual(consumer.call_args_list, [
            (("run-a", "pinned-result"),), (("run-a", "pinned-result"),)])
        self.assertEqual(expected["states"], ["pending", "publishing", "retry_wait", "publishing", "published"])

    def test_failed_monitoring_read_cannot_complete_assessment(self):
        schema = json.loads((Path(__file__).resolve().parents[4] /
                             "docs/l5-services/contracts/v1/schemas.json").read_text())
        observation = {"observed_at": "2026-01-01T00:10:00Z", "outcome": "running",
                       "progress_coverage": "unavailable",
                       "native_job_ref": {"tenant_id": "tenant-alpha", "project_id": "project-alpha",
                                          "source_connection_id": "connection-alpha", "record_type": "code_review_job",
                                          "record_id": "job-a", "record_revision": "job-version-1"}}
        run = {"state": "running", "provider_state": "running",
               "monitoring": {"state": "current", "progress_state": "unknown",
                              "poll_attempts": 1, "consecutive_poll_errors": 0,
                              "latest_observation": observation}}
        poll = Mock(side_effect=TimeoutError("status unavailable"))
        try:
            poll("run-a")
        except TimeoutError:
            run["monitoring"].update(state="degraded", poll_attempts=2,
                                     consecutive_poll_errors=1)
        self.assertFalse(list(Draft202012Validator({**schema, "$ref": "#/$defs/RunMonitoring"}).iter_errors(run["monitoring"])))
        self.assertEqual(run["monitoring"]["latest_observation"], observation)
        self.assertEqual((run["state"], run["provider_state"]), ("running", "running"))
        self.assertNotIn("succeeded", (run["monitoring"]["state"], observation["outcome"]))
        poll.assert_called_once_with("run-a")
        native_failure = {**observation, "observed_at": "2026-01-01T00:20:00Z",
                          "outcome": "failed", "native_status": "FAILED"}
        self.assertFalse(list(Draft202012Validator({**schema, "$ref": "#/$defs/ProviderObservation"}).iter_errors(native_failure)))
        self.assertNotEqual(native_failure["outcome"], run["monitoring"]["state"])

    def test_security_contracts(self):
        repository = Path(__file__).resolve().parents[4]
        home = repository / "docs/l5-services/contracts/v1"
        fixture = json.loads((home / "security-fixtures.json").read_text())
        schema = json.loads((home / "schemas.json").read_text())
        validator = Draft202012Validator({**schema, "$ref": "#/$defs/RunInput"})
        products = {item["product_id"]: item for item in fixture["products"]}
        sources = {item["source_ref"]["record_id"]: item for item in fixture["products"]}
        receipts = {}
        attempts = []
        provider = Mock()

        def read_source(case, source):
            if case.get("fault") == "source_read_timeout":
                raise TimeoutError("source unavailable")
            return source["source_ref"].copy()

        transport = Mock(side_effect=read_source)
        try:
            for case in fixture["cases"]:
                with self.subTest(case=case["id"]):
                    attempts.append((case["id"], fixture["fixed_clock"]))
                    product = products[case["product_id"]]
                    source = sources[case["source_id"]]
                    reference = source["source_ref"]
                    run_input = {"configuration_revision": 1, "binding_id": product["binding_id"],
                                 "capability": case["capability"], "sources": [reference]}
                    authorized = (source["product_id"] == product["product_id"]
                                  and (reference["tenant_id"], reference["project_id"]) ==
                                  (product["tenant_id"], product["project_id"])
                                  and source["capability"] == case["capability"])
                    if not authorized or not validator.is_valid(run_input):
                        outcome = "denied"
                    else:
                        try:
                            captured = transport(case, source)
                        except TimeoutError:
                            outcome = "unavailable"
                        else:
                            receipts[case["id"]] = {"product_id": product["product_id"],
                                                    "source_ref": captured, "admitted_at": fixture["fixed_clock"]}
                            outcome = "accepted"
                    self.assertEqual(outcome, case["expected"])
            self.assertEqual(list(receipts), fixture["expected_receipts"])
            for case_id in fixture["expected_receipts"]:
                receipt = receipts[case_id]
                self.assertEqual(receipt["source_ref"], products[receipt["product_id"]]["source_ref"])
            self.assertEqual(len(attempts), len(fixture["cases"]))
            provider.assert_not_called()
        finally:
            receipts.clear()
            attempts.clear()
        self.assertFalse(receipts or attempts)

    def test_lifecycle_boundaries(self):
        fixture = json.loads((Path(__file__).resolve().parents[4] /
                              "docs/l5-services/contracts/v1/lifecycle-fixtures.json").read_text())
        binding = fixture["binding"].copy()
        assessments = {item["run_id"]: item for item in fixture["assessments"]}
        current_source = assessments["run-a"]["source_revision"]
        admitted = {}
        attempts = {}
        publication = {"state": "pending", "receipt_id": None, "attempts": 0}
        self.assertGreaterEqual(fixture["deadline_seconds"], 60)
        self.assertLessEqual(fixture["deadline_seconds"], 86400)
        provider = Mock(side_effect=lambda run_id, attempt_id, source_hash: "result-" + run_id)
        consumer = Mock(side_effect=[TimeoutError("consumer response lost"), "receipt-a"])
        try:
            for event in fixture["events"]:
                action = event["action"]
                if action == "admit":
                    allowed = (binding["state"] == "ready" and
                               event["product_id"] == binding["product_id"] and
                               event["run_id"] in assessments)
                    if allowed:
                        pinned = assessments[event["run_id"]].copy()
                        self.assertEqual(pinned["source_revision"], current_source)
                        pinned["binding_generation"] = binding["generation"]
                        pinned["admitted_at"] = fixture["fixed_clock"]
                        pinned["deadline_seconds"] = fixture["deadline_seconds"]
                        admitted[event["run_id"]] = pinned
                    result = "accepted" if allowed else "denied"
                elif action == "source_update":
                    current_source = event["source_revision"]
                    result = "recorded"
                elif action == "dispatch":
                    run_id = event["run_id"]
                    if run_id in attempts:
                        result = "replayed"
                    else:
                        pinned = admitted[run_id]
                        attempts[run_id] = pinned["attempt_id"]
                        pinned["result_id"] = provider(run_id, pinned["attempt_id"], pinned["snapshot_hash"])
                        result = "submitted"
                elif action == "revoke":
                    binding["generation"] += 1
                    binding["state"] = "suspended"
                    result = "suspended"
                else:
                    self.assertEqual(action, "publish")
                    self.assertIn("result_id", admitted[event["run_id"]])
                    publication["attempts"] += 1
                    try:
                        publication["receipt_id"] = consumer(admitted[event["run_id"]]["result_id"])
                    except TimeoutError:
                        publication["state"] = "retry_wait"
                    else:
                        publication["state"] = "published"
                    result = publication["state"]
                self.assertEqual(result, event["expected"], event)
            readback = fixture["expected_readback"]
            for run_id in ("run-a", "run-b"):
                self.assertEqual(admitted[run_id]["source_revision"], readback[run_id])
                self.assertEqual(admitted[run_id]["binding_generation"], 1)
                self.assertEqual(admitted[run_id]["admitted_at"], fixture["fixed_clock"])
                self.assertEqual(admitted[run_id]["deadline_seconds"], fixture["deadline_seconds"])
            self.assertEqual((binding["state"], binding["generation"]),
                             (readback["binding_state"], readback["binding_generation"]))
            self.assertEqual((provider.call_count, publication["attempts"], publication["state"]),
                             (readback["provider_submissions"], readback["publication_attempts"],
                              readback["publication_state"]))
            self.assertEqual(attempts, {run_id: item["attempt_id"] for run_id, item in assessments.items()})
            self.assertEqual(consumer.call_args_list[0], consumer.call_args_list[1])
            self.assertEqual(set(admitted), set(assessments))
        finally:
            admitted.clear()
            attempts.clear()
        self.assertFalse(admitted or attempts)

    def test_compatibility_fixtures(self):
        repository = Path(__file__).resolve().parents[4]
        home = repository / "docs/l5-services/contracts/v1"
        fixture = json.loads((home / "compatibility-fixtures.json").read_text())
        matrix = {row["id"]: row for row in json.loads((home / "compatibility.json").read_text())}
        self.assertEqual(set(fixture["source_row_ids"]), set(matrix))
        self.assertEqual(len(fixture["products"]), 2)
        self.assertEqual(len({item["tenant_id"] for item in fixture["products"]}), 2)
        attempts = []
        evidence_readback = {}
        try:
            for row_id in fixture["source_row_ids"]:
                row = matrix[row_id]
                source_path = repository / row["source"]
                source_bytes = source_path.read_bytes()
                self.assertIn(row["evidence"].encode(), source_bytes, row_id)
                self.assertNotIn("modules/l5-services/", row["target"])
                digest = hashlib.sha256(source_bytes).hexdigest()
                attempts.append((row_id, fixture["fixed_clock"], digest))
                evidence_readback[row_id] = hashlib.sha256(source_path.read_bytes()).hexdigest()
                self.assertEqual(evidence_readback[row_id], digest)
            self.assertEqual(len(attempts), len(matrix))
            archive = fixture["archive"]
            archive_source = (repository / matrix[archive["source_row"]]["source"]).read_text()
            self.assertIn('ARCHIVE_SUFFIX = "' + archive["candidate"] + '"', archive_source)
            self.assertNotEqual(archive["candidate"], archive["rejected"])
            sdk = fixture["sdk"]
            self.assertIn("boto3==" + sdk["gateway_pin"],
                          (repository / matrix[sdk["source_row"]]["source"]).read_text())
            self.assertIn(sdk["worker_candidate"], matrix[sdk["source_row"]]["checkpoint"])
            self.assertFalse(sdk["worker_model_qualified"])
            for gap in fixture["gaps"]:
                self.assertEqual(gap["owner"], matrix[gap["source_row"]]["owner"])
                self.assertIn(gap["phase"], {"setup_and_run", "capability_enablement", "rollout"})
            self.assertEqual(fixture["registry_mapping"],
                             {"state": "unmapped", "owner": "SA01 / PranavSharma1000"})
            order = fixture["rollout_order"]
            self.assertLess(order.index("contracts"), order.index("host_authorization_flag_off"))
            self.assertLess(order.index("additive_migrations"), order.index("worker_dispatch_disabled"))
            self.assertLess(order.index("manual_infra_apply"), order.index("enable_fixture"))
            self.assertLess(order.index("synthetic_recovery"), order.index("enable_fixture"))
            self.assertEqual(fixture["live_access"], "not_checked")
            self.assertFalse(fixture["enable_capability"])
        finally:
            attempts.clear()
            evidence_readback.clear()
        self.assertFalse(attempts or evidence_readback)

    def test_cross_scope_source_refs(self):
        relationships = {
            "product-alpha": ("tenant-alpha", "project-alpha"),
            "product-beta": ("tenant-beta", "project-beta"),
        }
        source_store = {
            "source-alpha": ("product-alpha", "tenant-alpha", "project-alpha", "revision-1"),
            "source-beta": ("product-beta", "tenant-beta", "project-beta", "revision-1"),
            "source-other-project": ("product-alpha", "tenant-alpha", "project-other", "revision-1"),
        }

        def source_visible(product_id, source_id, source_ref):
            stored_product, stored_tenant, stored_project, stored_revision = source_store[source_id]
            tenant_id, project_id = relationships[product_id]
            return (stored_product == product_id and stored_tenant == tenant_id
                    and stored_project == project_id
                    and (source_ref["tenant_id"], source_ref["project_id"],
                         source_ref["record_revision"]) ==
                    (stored_tenant, stored_project, stored_revision))

        permitted = {"tenant_id": "tenant-alpha", "project_id": "project-alpha",
                     "record_revision": "revision-1"}
        self.assertTrue(source_visible("product-alpha", "source-alpha", permitted))
        for source_id, source_ref in (
            ("source-beta", {**permitted, "tenant_id": "tenant-beta", "project_id": "project-beta"}),
            ("source-other-project", {**permitted, "project_id": "project-other"}),
            ("source-beta", permitted),
            ("source-alpha", {**permitted, "record_revision": "revision-2"}),
        ):
            with self.subTest(source_id=source_id, source_ref=source_ref):
                self.assertFalse(source_visible("product-alpha", source_id, source_ref))
